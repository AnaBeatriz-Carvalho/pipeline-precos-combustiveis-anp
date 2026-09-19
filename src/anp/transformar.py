"""Etapa 2: transformacao da planilha bruta em Parquet tipado (staging).

O staging preserva o grao da fonte -- uma linha por semana x estado x produto,
sem nenhuma agregacao. O que muda em relacao ao raw e a forma: colunas
renomeadas, estado resolvido para sigla, produto normalizado, "-" tratado como
nulo e tipos explicitos.

Qualquer divergencia estrutural em relacao ao que o config.toml declara
levanta ErroEsquema com mensagem acionavel. Nada e engolido em silencio.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import pandas as pd

from anp.config import Config
from anp.erros import ErroEsquema
from anp.log import obter_logger

log = obter_logger("transformacao")

# Colunas da fonte que o staging aproveita -> nome normalizado.
# Margem e as cinco colunas de DISTRIBUICAO ficam de fora: estao fora do
# escopo da v1 e sao justamente as que a ANP preenche com "-".
_RENOMEACAO = {
    "DATA INICIAL": "data_inicio",
    "DATA FINAL": "data_fim",
    "REGIÃO": "regiao",
    "ESTADO": "estado",
    "PRODUTO": "produto",
    "NÚMERO DE POSTOS PESQUISADOS": "numero_postos",
    "UNIDADE DE MEDIDA": "unidade_medida",
    "PREÇO MÉDIO REVENDA": "preco_medio",
    "DESVIO PADRÃO REVENDA": "desvio_padrao",
    "PREÇO MÍNIMO REVENDA": "preco_min",
    "PREÇO MÁXIMO REVENDA": "preco_max",
    "COEF DE VARIAÇÃO REVENDA": "coef_variacao",
}

_COLUNAS_NUMERICAS = (
    "preco_medio",
    "preco_min",
    "preco_max",
    "desvio_padrao",
    "coef_variacao",
)

# Sem estes valores a linha nao serve para o mart.
_COLUNAS_CRITICAS = ("preco_medio", "numero_postos")

COLUNAS_STAGING = (
    "data_inicio",
    "data_fim",
    "uf",
    "estado",
    "regiao",
    "produto",
    "preco_medio",
    "preco_min",
    "preco_max",
    "desvio_padrao",
    "coef_variacao",
    "numero_postos",
    "unidade_medida",
    "origem_sha256",
    "data_ingestao",
)


def validar_cabecalho(caminho: Path, cfg: Config) -> list[str]:
    """Confere que a linha declarada no config contem o cabecalho esperado.

    A posicao vem da configuracao, mas nunca e assumida: le a linha, compara
    com o conjunto declarado e levanta ErroEsquema se divergir. Tambem procura
    o cabecalho nas linhas vizinhas para dizer, na mensagem de erro, se ele
    apenas mudou de lugar.
    """
    linha_cfg = int(cfg["extracao"]["linha_cabecalho"])
    esperadas = [str(c) for c in cfg["extracao"]["colunas_esperadas"]]

    workbook = openpyxl.load_workbook(caminho, read_only=True, data_only=True)
    try:
        aba = workbook.worksheets[int(cfg["extracao"]["aba_indice"])]
        # Uma janela generosa em volta da linha declarada, para diagnostico.
        limite = linha_cfg + 15
        linhas: dict[int, list[str]] = {}
        for numero, valores in enumerate(
            aba.iter_rows(min_row=1, max_row=limite, values_only=True), start=1
        ):
            linhas[numero] = [
                str(v).strip() if v is not None else "" for v in valores
            ]
    finally:
        workbook.close()

    if linha_cfg not in linhas:
        raise ErroEsquema(
            f"A planilha {caminho.name} tem menos de {linha_cfg} linhas, mas "
            f"extracao.linha_cabecalho aponta para a linha {linha_cfg}. "
            "O arquivo baixado provavelmente nao e a serie semanal por estado."
        )

    encontradas = [c for c in linhas[linha_cfg] if c]
    if set(encontradas) == set(esperadas):
        log.info(
            "cabecalho validado",
            extra={
                "evento": "esquema_ok",
                "linha": linha_cfg,
                "colunas": len(encontradas),
            },
        )
        return encontradas

    # O cabecalho esta em outro lugar? Isso muda o conserto necessario.
    realocado = next(
        (n for n, vals in linhas.items() if set(v for v in vals if v) == set(esperadas)),
        None,
    )
    if realocado is not None:
        raise ErroEsquema(
            f"O cabecalho de {caminho.name} mudou de posicao: esperado na linha "
            f"{linha_cfg}, encontrado na linha {realocado}. A ANP alterou o "
            f"numero de linhas de nota de rodape. Ajuste "
            f"extracao.linha_cabecalho para {realocado} no config.toml."
        )

    faltando = sorted(set(esperadas) - set(encontradas))
    sobrando = sorted(set(encontradas) - set(esperadas))
    raise ErroEsquema(
        f"O esquema de {caminho.name} mudou. Na linha {linha_cfg} esperava "
        f"{len(esperadas)} colunas e encontrei {len(encontradas)}.\n"
        f"  Colunas ausentes: {faltando or 'nenhuma'}\n"
        f"  Colunas novas:    {sobrando or 'nenhuma'}\n"
        f"  Linha lida:       {encontradas[:6]}{' ...' if len(encontradas) > 6 else ''}\n"
        "Revise extracao.colunas_esperadas no config.toml e confira se o "
        "mapeamento de colunas do modulo de transformacao ainda vale."
    )


def _para_numero(serie: pd.Series, marcadores: list[str]) -> pd.Series:
    """Converte para float tratando os marcadores de ausencia da ANP."""
    if pd.api.types.is_numeric_dtype(serie):
        return serie.astype("float64")
    texto = serie.astype("string").str.strip()
    texto = texto.replace({m: pd.NA for m in marcadores})
    # A fonte usa ponto decimal, mas virgula ja apareceu em planilhas da ANP.
    texto = texto.str.replace(".", "", regex=False).where(
        texto.str.count(",") == 1, texto
    )
    texto = texto.str.replace(",", ".", regex=False)
    return pd.to_numeric(texto, errors="coerce").astype("float64")


def transformar(cfg: Config, caminho_raw: Path, origem_sha256: str) -> pd.DataFrame:
    """Le a planilha bruta e devolve o DataFrame do staging, tipado."""
    validar_cabecalho(caminho_raw, cfg)

    linha_cfg = int(cfg["extracao"]["linha_cabecalho"])
    bruto = pd.read_excel(
        caminho_raw,
        sheet_name=int(cfg["extracao"]["aba_indice"]),
        header=linha_cfg - 1,
        engine="openpyxl",
    )
    bruto.columns = [str(c).strip() for c in bruto.columns]
    log.info(
        "planilha lida",
        extra={"evento": "leitura_ok", "linhas": len(bruto), "colunas": len(bruto.columns)},
    )

    ausentes = [c for c in _RENOMEACAO if c not in bruto.columns]
    if ausentes:
        raise ErroEsquema(
            f"Colunas necessarias ausentes em {caminho_raw.name} apos a leitura: "
            f"{ausentes}. O cabecalho passou na validacao, entao a leitura do "
            "pandas divergiu: verifique extracao.aba_indice no config.toml."
        )

    df = bruto[list(_RENOMEACAO)].rename(columns=_RENOMEACAO)

    # --- produto: "Gasolina C" nao existe na fonte, o rotulo e GASOLINA COMUM.
    mapa_produtos = cfg.produtos
    df["produto"] = df["produto"].astype("string").str.strip().str.upper()
    disponiveis = set(df["produto"].dropna().unique())
    sumidos = [p for p in mapa_produtos if p not in disponiveis]
    if sumidos:
        raise ErroEsquema(
            f"Produtos do escopo v1 ausentes em {caminho_raw.name}: {sumidos}. "
            f"Rotulos presentes na planilha: {sorted(disponiveis)}. "
            "A ANP renomeou o produto; atualize [escopo.produtos] no config.toml."
        )
    df = df[df["produto"].isin(mapa_produtos)].copy()
    df["produto"] = df["produto"].map(mapa_produtos).astype("string")
    log.info(
        "escopo aplicado",
        extra={
            "evento": "escopo_filtrado",
            "linhas": len(df),
            "produtos": sorted(mapa_produtos.values()),
        },
    )

    # --- unidade de medida: preco por litro. Se mudar, os limiares nao valem.
    unidade_esperada = str(cfg["escopo"]["unidade_esperada"]).strip().lower()
    unidades = (
        df["unidade_medida"].astype("string").str.strip().str.lower().dropna().unique()
    )
    divergentes = [u for u in unidades if u != unidade_esperada]
    if divergentes:
        raise ErroEsquema(
            f"Unidade de medida inesperada em {caminho_raw.name}: {divergentes}. "
            f"A v1 assume {cfg['escopo']['unidade_esperada']!r} para gasolina e "
            "etanol, e os limiares de preco em [qualidade] dependem disso."
        )

    # --- estado -> sigla. A fonte so traz o nome por extenso e sem acento.
    mapa_ufs = cfg.ufs
    df["estado"] = df["estado"].astype("string").str.strip().str.upper()
    desconhecidos = sorted(set(df["estado"].dropna()) - set(mapa_ufs))
    if desconhecidos:
        raise ErroEsquema(
            f"Estados nao mapeados em {caminho_raw.name}: {desconhecidos}. "
            "A secao [ufs] do config.toml precisa cobrir todo nome de estado "
            "que a fonte usa."
        )
    df["uf"] = df["estado"].map(mapa_ufs).astype("string")
    df["regiao"] = df["regiao"].astype("string").str.strip().str.upper()

    # --- datas
    for coluna in ("data_inicio", "data_fim"):
        df[coluna] = pd.to_datetime(df[coluna], errors="coerce")
    datas_invalidas = int(df["data_inicio"].isna().sum() + df["data_fim"].isna().sum())
    if datas_invalidas:
        raise ErroEsquema(
            f"{datas_invalidas} datas ilegiveis em {caminho_raw.name}. "
            "As colunas DATA INICIAL e DATA FINAL deixaram de ser datas."
        )

    duracao = (df["data_fim"] - df["data_inicio"]).dt.days
    fora_do_padrao = int((duracao != 6).sum())
    if fora_do_padrao:
        # Nao e erro de esquema: a ANP ja publicou semanas encurtadas por
        # feriado. Fica registrado para nao passar despercebido.
        log.warning(
            "semanas com duracao diferente de 7 dias",
            extra={
                "evento": "duracao_atipica",
                "linhas": fora_do_padrao,
                "duracoes": sorted(duracao[duracao != 6].unique().tolist()),
            },
        )

    # --- numericos, com "-" tratado como ausencia
    marcadores = [str(m) for m in cfg["extracao"]["marcadores_nulos"]]
    for coluna in _COLUNAS_NUMERICAS:
        df[coluna] = _para_numero(df[coluna], marcadores)
    df["numero_postos"] = (
        _para_numero(df["numero_postos"], marcadores).round().astype("Int64")
    )

    # --- linhas sem o essencial saem, mas contadas e registradas
    sem_essencial = df[list(_COLUNAS_CRITICAS)].isna().any(axis=1)
    descartadas = int(sem_essencial.sum())
    if descartadas:
        amostra = (
            df.loc[sem_essencial, ["data_inicio", "uf", "produto"]]
            .head(5)
            .astype(str)
            .to_dict("records")
        )
        log.warning(
            "linhas descartadas por ausencia de preco medio ou numero de postos",
            extra={
                "evento": "linhas_descartadas",
                "linhas": descartadas,
                "proporcao": round(descartadas / max(len(df), 1), 4),
                "amostra": amostra,
            },
        )
        df = df[~sem_essencial].copy()

    if df.empty:
        raise ErroEsquema(
            f"Nenhuma linha utilizavel sobrou de {caminho_raw.name} apos filtrar "
            "escopo e valores ausentes. A planilha mudou de conteudo."
        )

    df["unidade_medida"] = df["unidade_medida"].astype("string").str.strip()
    df["origem_sha256"] = pd.Series(origem_sha256, index=df.index, dtype="string")
    df["data_ingestao"] = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)

    df = df[list(COLUNAS_STAGING)].sort_values(
        ["data_inicio", "uf", "produto"], ignore_index=True
    )
    log.info(
        "transformacao concluida",
        extra={
            "evento": "transformacao_ok",
            "linhas": len(df),
            "semanas": int(df["data_inicio"].nunique()),
            "ufs": int(df["uf"].nunique()),
            "semana_mais_recente": str(df["data_inicio"].max().date()),
        },
    )
    return df


def gravar_staging(cfg: Config, df: pd.DataFrame) -> Path:
    """Grava o Parquet do staging.

    A fonte e cumulativa, entao cada execucao reescreve o arquivo inteiro:
    full refresh, idempotente por construcao.
    """
    destino = cfg.caminho("staging") / "precos_semanais.parquet"
    destino.parent.mkdir(parents=True, exist_ok=True)
    temporario = destino.with_suffix(".parquet.parcial")
    df.to_parquet(temporario, index=False, engine="pyarrow", compression="snappy")
    temporario.replace(destino)
    log.info(
        "staging gravado",
        extra={
            "evento": "staging_ok",
            "caminho": str(destino),
            "linhas": len(df),
            "bytes": destino.stat().st_size,
        },
    )
    return destino
