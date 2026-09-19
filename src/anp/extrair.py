"""Etapa 1: download do arquivo da ANP e gravacao na camada raw.

A ANP nao expoe API REST. Publica uma unica planilha XLSX cumulativa, que
sobrescreve a cada semana, com a serie historica inteira por estado. Portanto
cada coleta e um snapshot completo, nao um incremento.

A camada raw guarda o arquivo exatamente como veio, nomeado pela data de
coleta, mais um manifesto JSON Lines com hash, tamanho e origem de cada
coleta. O servidor da ANP nao envia Last-Modified nem ETag, entao a deteccao
de "nada mudou" e feita pelo SHA-256 do conteudo.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from pathlib import Path

import requests

from anp.config import Config
from anp.erros import ErroDownload
from anp.log import obter_logger

log = obter_logger("extracao")

# Todo XLSX e um container ZIP: comeca com a assinatura "PK\x03\x04". O gov.br
# eventualmente responde HTTP 200 com uma pagina de erro em HTML, e essa
# checagem impede que esse HTML entre na camada raw como se fosse planilha.
_ASSINATURA_ZIP = b"PK\x03\x04"

_STATUS_RETENTAVEIS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ResultadoExtracao:
    """O que a etapa de extracao produziu."""

    caminho: Path
    sha256: str
    bytes_totais: int
    url: str
    coletado_em: str
    reutilizado: bool

    def como_dict(self) -> dict:
        dados = asdict(self)
        dados["caminho"] = str(self.caminho)
        return dados


def _sha256_de(caminho: Path) -> str:
    digest = hashlib.sha256()
    with caminho.open("rb") as arquivo:
        for bloco in iter(lambda: arquivo.read(1024 * 1024), b""):
            digest.update(bloco)
    return digest.hexdigest()


def _baixar_com_retry(cfg: Config, destino: Path) -> int:
    """Baixa a planilha com retry e backoff exponencial.

    Retorna o numero de bytes gravados. Levanta ErroDownload quando as
    tentativas se esgotam, preservando a ultima causa.
    """
    fonte = cfg["fonte"]
    dl = cfg["download"]
    url = fonte["url"]
    max_tentativas = int(dl["max_tentativas"])
    espera = float(dl["backoff_inicial_segundos"])

    ultima_falha: Exception | None = None

    for tentativa in range(1, max_tentativas + 1):
        try:
            log.info(
                "iniciando download",
                extra={"evento": "download_inicio", "tentativa": tentativa, "url": url},
            )
            resposta = requests.get(
                url,
                stream=True,
                headers={"User-Agent": dl["user_agent"]},
                timeout=(
                    float(fonte["timeout_conexao_segundos"]),
                    float(fonte["timeout_leitura_segundos"]),
                ),
            )
            if resposta.status_code in _STATUS_RETENTAVEIS:
                raise requests.HTTPError(
                    f"HTTP {resposta.status_code} retentavel em {url}",
                    response=resposta,
                )
            resposta.raise_for_status()

            escritos = 0
            primeiro_bloco = True
            with destino.open("wb") as saida:
                for bloco in resposta.iter_content(chunk_size=1024 * 256):
                    if not bloco:
                        continue
                    if primeiro_bloco:
                        if not bloco.startswith(_ASSINATURA_ZIP):
                            raise ErroDownload(
                                f"A resposta de {url} nao e um arquivo XLSX. "
                                f"Content-Type recebido: "
                                f"{resposta.headers.get('Content-Type', 'ausente')!r}. "
                                "A ANP provavelmente moveu ou renomeou a planilha; "
                                "verifique a URL em [fonte] no config.toml."
                            )
                        primeiro_bloco = False
                    saida.write(bloco)
                    escritos += len(bloco)

            minimo = int(dl["tamanho_minimo_bytes"])
            if escritos < minimo:
                raise ErroDownload(
                    f"Download de {url} truncado: {escritos} bytes, abaixo do "
                    f"minimo de {minimo} definido em download.tamanho_minimo_bytes."
                )

            log.info(
                "download concluido",
                extra={
                    "evento": "download_ok",
                    "tentativa": tentativa,
                    "bytes": escritos,
                },
            )
            return escritos

        except (requests.RequestException, ErroDownload) as erro:
            ultima_falha = erro
            destino.unlink(missing_ok=True)
            if tentativa == max_tentativas:
                break
            log.warning(
                "download falhou, aguardando para nova tentativa",
                extra={
                    "evento": "download_retry",
                    "tentativa": tentativa,
                    "espera_segundos": round(espera, 2),
                    "causa": str(erro),
                },
            )
            time.sleep(espera)
            espera = min(
                espera * float(dl["backoff_fator"]),
                float(dl["backoff_maximo_segundos"]),
            )

    raise ErroDownload(
        f"Nao foi possivel baixar {url} apos {max_tentativas} tentativas. "
        f"Ultima falha: {ultima_falha}"
    ) from ultima_falha


def _ler_manifesto(caminho: Path) -> list[dict]:
    if not caminho.is_file():
        return []
    registros = []
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        if linha.strip():
            registros.append(json.loads(linha))
    return registros


def _registrar_no_manifesto(caminho: Path, resultado: ResultadoExtracao) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("a", encoding="utf-8") as saida:
        saida.write(json.dumps(resultado.como_dict(), ensure_ascii=False) + "\n")


def extrair(cfg: Config, data_coleta: date | None = None) -> ResultadoExtracao:
    """Baixa a planilha da ANP e grava na camada raw.

    Idempotente de duas formas: rodar duas vezes no mesmo dia sobrescreve o
    mesmo arquivo, e se o conteudo baixado for identico a uma coleta anterior
    (mesmo SHA-256), o arquivo novo e descartado e o antigo reaproveitado.
    """
    hoje = data_coleta or date.today()
    dir_raw = cfg.caminho("raw")
    dir_raw.mkdir(parents=True, exist_ok=True)

    nome = f"{cfg['fonte']['nome_base']}_{hoje:%Y-%m-%d}.xlsx"
    destino = dir_raw / nome
    temporario = dir_raw / f".{nome}.parcial"

    bytes_totais = _baixar_com_retry(cfg, temporario)
    sha = _sha256_de(temporario)

    manifesto = cfg.caminho("manifesto_raw")
    anteriores = _ler_manifesto(manifesto)
    ja_coletado = next(
        (r for r in anteriores if r.get("sha256") == sha and Path(r["caminho"]).is_file()),
        None,
    )

    if ja_coletado and Path(ja_coletado["caminho"]) != destino:
        temporario.unlink(missing_ok=True)
        log.info(
            "conteudo identico a uma coleta anterior, reaproveitando arquivo",
            extra={
                "evento": "raw_reutilizado",
                "sha256": sha,
                "caminho": ja_coletado["caminho"],
            },
        )
        resultado = ResultadoExtracao(
            caminho=Path(ja_coletado["caminho"]),
            sha256=sha,
            bytes_totais=int(ja_coletado["bytes_totais"]),
            url=cfg["fonte"]["url"],
            coletado_em=datetime.now(timezone.utc).isoformat(),
            reutilizado=True,
        )
        _registrar_no_manifesto(manifesto, resultado)
        return resultado

    # Renomeia so depois do download integro: a camada raw nunca contem
    # arquivo pela metade.
    temporario.replace(destino)

    resultado = ResultadoExtracao(
        caminho=destino,
        sha256=sha,
        bytes_totais=bytes_totais,
        url=cfg["fonte"]["url"],
        coletado_em=datetime.now(timezone.utc).isoformat(),
        reutilizado=False,
    )
    _registrar_no_manifesto(manifesto, resultado)
    log.info(
        "arquivo gravado na camada raw",
        extra={
            "evento": "raw_gravado",
            "caminho": str(destino),
            "sha256": sha,
            "bytes": bytes_totais,
        },
    )
    return resultado
