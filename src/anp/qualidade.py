"""Etapa 4: testes de qualidade, executados contra o mart apos a carga.

Tres testes sao bloqueantes e levantam ErroQualidade: chave primaria sem
duplicata, preco medio dentro da faixa plausivel e cobertura de UFs na semana
carregada. O quarto, variacao semanal acima do limiar, e informativo e sai
como ALERTA -- oscilacao forte de preco e fato do mercado, nao defeito de dado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import duckdb

from anp.carregar import TABELA
from anp.config import Config
from anp.erros import ErroQualidade
from anp.log import obter_logger

log = obter_logger("qualidade")

PASSOU = "PASSOU"
ALERTA = "ALERTA"
FALHOU = "FALHOU"


@dataclass
class Resultado:
    """Desfecho de um teste de qualidade."""

    nome: str
    status: str
    mensagem: str
    detalhes: dict[str, Any] = field(default_factory=dict)

    @property
    def bloqueante(self) -> bool:
        return self.status == FALHOU


def _semana_alvo(con: duckdb.DuckDBPyConnection, semana: date | None) -> date | None:
    """A semana sob teste: a informada, ou a mais recente do mart."""
    if semana is not None:
        return semana
    linha = con.execute(f"SELECT MAX(data_inicio) FROM {TABELA}").fetchone()
    return linha[0] if linha else None


def testar_chave_primaria(con: duckdb.DuckDBPyConnection, cfg: Config) -> Resultado:
    """A chave (data_inicio, uf, produto) nao pode repetir."""
    duplicatas = con.execute(
        f"""
        SELECT data_inicio, uf, produto, COUNT(*) AS ocorrencias
        FROM {TABELA}
        GROUP BY 1, 2, 3
        HAVING COUNT(*) > 1
        ORDER BY ocorrencias DESC
        LIMIT 10
        """
    ).fetchall()

    if duplicatas:
        amostra = [
            {"data_inicio": str(d), "uf": u, "produto": p, "ocorrencias": n}
            for d, u, p, n in duplicatas
        ]
        return Resultado(
            nome="chave_primaria_sem_duplicata",
            status=FALHOU,
            mensagem=(
                f"{len(duplicatas)} combinacoes de (data_inicio, uf, produto) "
                f"aparecem mais de uma vez no mart. A carga deixou de ser "
                f"idempotente. Amostra: {amostra}"
            ),
            detalhes={"duplicatas": amostra},
        )
    return Resultado(
        nome="chave_primaria_sem_duplicata",
        status=PASSOU,
        mensagem="Nenhuma duplicata na chave primaria.",
    )


def testar_faixa_de_preco(con: duckdb.DuckDBPyConnection, cfg: Config) -> Resultado:
    """preco_medio precisa ficar na faixa plausivel definida no config."""
    minimo = float(cfg["qualidade"]["preco_medio_minimo"])
    maximo = float(cfg["qualidade"]["preco_medio_maximo"])

    fora = con.execute(
        f"""
        SELECT data_inicio, uf, produto, preco_medio
        FROM {TABELA}
        WHERE preco_medio IS NULL OR preco_medio < ? OR preco_medio > ?
        ORDER BY preco_medio
        LIMIT 10
        """,
        [minimo, maximo],
    ).fetchall()
    total = con.execute(
        f"""
        SELECT COUNT(*) FROM {TABELA}
        WHERE preco_medio IS NULL OR preco_medio < ? OR preco_medio > ?
        """,
        [minimo, maximo],
    ).fetchone()[0]

    if total:
        amostra = [
            {"data_inicio": str(d), "uf": u, "produto": p, "preco_medio": v}
            for d, u, p, v in fora
        ]
        return Resultado(
            nome="preco_medio_na_faixa",
            status=FALHOU,
            mensagem=(
                f"{total} linhas com preco_medio fora da faixa de R$ {minimo:.2f} "
                f"a R$ {maximo:.2f}. Isso indica erro de parsing ou mudanca de "
                f"unidade na fonte. Amostra: {amostra}"
            ),
            detalhes={"linhas_fora": total, "amostra": amostra},
        )
    return Resultado(
        nome="preco_medio_na_faixa",
        status=PASSOU,
        mensagem=f"Todos os precos medios entre R$ {minimo:.2f} e R$ {maximo:.2f}.",
    )


def testar_cobertura_de_ufs(
    con: duckdb.DuckDBPyConnection, cfg: Config, semana: date | None = None
) -> Resultado:
    """Cobertura de UFs na semana carregada, avaliada por produto.

    A regra nao pode ser global: a ANP nao pesquisa etanol hidratado em toda
    UF em toda semana. Cada produto tem sua allowlist de ausencias legitimas
    em [qualidade.ufs_ausentes_toleradas]. Falta fora da allowlist e erro.
    """
    alvo = _semana_alvo(con, semana)
    if alvo is None:
        return Resultado(
            nome="cobertura_de_ufs",
            status=FALHOU,
            mensagem="O mart esta vazio: nao ha semana para avaliar.",
        )

    esperadas = set(cfg.ufs.values())
    toleradas_cfg = cfg["qualidade"]["ufs_ausentes_toleradas"]

    problemas: list[str] = []
    detalhes: dict[str, Any] = {"semana": str(alvo), "por_produto": {}}

    for produto in sorted(cfg.produtos.values()):
        presentes = {
            linha[0]
            for linha in con.execute(
                f"SELECT DISTINCT uf FROM {TABELA} WHERE data_inicio = ? AND produto = ?",
                [alvo, produto],
            ).fetchall()
        }
        toleradas = set(toleradas_cfg.get(produto, []))
        ausentes = esperadas - presentes
        nao_toleradas = sorted(ausentes - toleradas)
        toleradas_ausentes = sorted(ausentes & toleradas)

        detalhes["por_produto"][produto] = {
            "ufs_presentes": len(presentes),
            "ufs_exigidas": len(esperadas - toleradas),
            "ausentes_toleradas": toleradas_ausentes,
            "ausentes_nao_toleradas": nao_toleradas,
        }

        if nao_toleradas:
            problemas.append(
                f"{produto}: faltam {nao_toleradas} na semana de {alvo} "
                f"({len(presentes)} de {len(esperadas)} UFs presentes)"
            )
        elif toleradas_ausentes:
            log.info(
                "UF ausente dentro da allowlist",
                extra={
                    "evento": "cobertura_tolerada",
                    "produto": produto,
                    "ufs": toleradas_ausentes,
                    "semana": str(alvo),
                },
            )

    if problemas:
        return Resultado(
            nome="cobertura_de_ufs",
            status=FALHOU,
            mensagem=(
                "Cobertura de UFs incompleta na semana carregada. "
                + "; ".join(problemas)
                + ". Se a ausencia for legitima, inclua a UF em "
                "[qualidade.ufs_ausentes_toleradas] no config.toml; caso "
                "contrario, a extracao perdeu linhas."
            ),
            detalhes=detalhes,
        )

    resumo = ", ".join(
        f"{p}: {d['ufs_presentes']}/{len(esperadas)}"
        for p, d in detalhes["por_produto"].items()
    )
    return Resultado(
        nome="cobertura_de_ufs",
        status=PASSOU,
        mensagem=f"Cobertura completa na semana de {alvo} ({resumo}).",
        detalhes=detalhes,
    )


def testar_variacao_semanal(
    con: duckdb.DuckDBPyConnection, cfg: Config, semana: date | None = None
) -> Resultado:
    """Variacao de preco acima do limiar entre semanas consecutivas.

    A comparacao usa LAG sobre as semanas ordenadas de cada serie, e nao
    data_inicio - 7 dias: a serie da ANP tem tres descontinuidades (agosto de
    2015, a suspensao de agosto a outubro de 2020 e setembro de 2022), e
    subtrair sete dias compararia contra semana inexistente nesses pontos.

    O desfecho e ALERTA, nunca erro: preco de combustivel oscila de verdade.
    """
    alvo = _semana_alvo(con, semana)
    if alvo is None:
        return Resultado(
            nome="variacao_semanal",
            status=FALHOU,
            mensagem="O mart esta vazio: nao ha semana para avaliar.",
        )

    limiar = float(cfg["qualidade"]["variacao_semanal_alerta"])
    linhas = con.execute(
        f"""
        WITH serie AS (
            SELECT
                data_inicio,
                uf,
                produto,
                preco_medio,
                LAG(preco_medio) OVER (
                    PARTITION BY uf, produto ORDER BY data_inicio
                ) AS preco_anterior,
                LAG(data_inicio) OVER (
                    PARTITION BY uf, produto ORDER BY data_inicio
                ) AS semana_anterior
            FROM {TABELA}
        )
        SELECT
            uf,
            produto,
            semana_anterior,
            preco_anterior,
            preco_medio,
            (preco_medio - preco_anterior) / preco_anterior AS variacao,
            date_diff('day', semana_anterior, data_inicio) AS dias_desde_anterior
        FROM serie
        WHERE data_inicio = ?
          AND preco_anterior IS NOT NULL
          AND preco_anterior > 0
          AND abs((preco_medio - preco_anterior) / preco_anterior) > ?
        ORDER BY abs(variacao) DESC
        """,
        [alvo, limiar],
    ).fetchall()

    if not linhas:
        return Resultado(
            nome="variacao_semanal",
            status=PASSOU,
            mensagem=(
                f"Nenhuma variacao acima de {limiar:.0%} na semana de {alvo}."
            ),
            detalhes={"semana": str(alvo), "limiar": limiar},
        )

    ocorrencias = [
        {
            "uf": uf,
            "produto": produto,
            "semana_anterior": str(anterior),
            "preco_anterior": round(pa, 3),
            "preco_atual": round(pm, 3),
            "variacao_pct": round(var * 100, 2),
            "dias_desde_semana_anterior": dias,
        }
        for uf, produto, anterior, pa, pm, var, dias in linhas
    ]
    return Resultado(
        nome="variacao_semanal",
        status=ALERTA,
        mensagem=(
            f"{len(ocorrencias)} series com variacao acima de {limiar:.0%} na "
            f"semana de {alvo}. Nao bloqueia a carga."
        ),
        detalhes={"semana": str(alvo), "limiar": limiar, "ocorrencias": ocorrencias},
    )


TESTES = (
    testar_chave_primaria,
    testar_faixa_de_preco,
    testar_cobertura_de_ufs,
    testar_variacao_semanal,
)


def executar_testes(
    con: duckdb.DuckDBPyConnection, cfg: Config, semana: date | None = None
) -> list[Resultado]:
    """Roda todos os testes e levanta ErroQualidade se algum for bloqueante.

    Todos rodam antes de qualquer excecao: um relatorio parcial esconderia
    problemas que so apareceriam na execucao seguinte.
    """
    resultados: list[Resultado] = []
    for teste in TESTES:
        try:
            resultado = teste(con, cfg, semana) if teste in (
                testar_cobertura_de_ufs,
                testar_variacao_semanal,
            ) else teste(con, cfg)
        except duckdb.Error as erro:
            resultado = Resultado(
                nome=getattr(teste, "__name__", str(teste)),
                status=FALHOU,
                mensagem=f"O teste nao pode ser executado: {erro}",
            )
        resultados.append(resultado)

        nivel = {PASSOU: log.info, ALERTA: log.warning, FALHOU: log.error}[resultado.status]
        nivel(
            resultado.mensagem,
            extra={
                "evento": "teste_qualidade",
                "teste": resultado.nome,
                "status": resultado.status,
                **resultado.detalhes,
            },
        )

    falhas = [r for r in resultados if r.bloqueante]
    alertas = [r for r in resultados if r.status == ALERTA]
    log.info(
        "testes de qualidade finalizados",
        extra={
            "evento": "qualidade_resumo",
            "total": len(resultados),
            "passou": len(resultados) - len(falhas) - len(alertas),
            "alerta": len(alertas),
            "falhou": len(falhas),
        },
    )

    if falhas:
        detalhamento = "\n".join(f"  - [{r.nome}] {r.mensagem}" for r in falhas)
        raise ErroQualidade(
            f"{len(falhas)} de {len(resultados)} testes de qualidade falharam:\n"
            f"{detalhamento}"
        )
    return resultados
