"""Etapa 4: testes de qualidade, executados contra o mart apos a carga.

Quatro testes sao bloqueantes e levantam ErroQualidade: chave primaria sem
duplicata, preco medio dentro da faixa do produto, colunas opcionais (margem,
coeficientes e distribuicao) dentro da faixa quando preenchidas, e cobertura de
UFs na semana carregada. O quinto, variacao semanal acima do limiar do produto,
e informativo e sai como ALERTA -- oscilacao forte de preco e fato do mercado,
nao defeito de dado.

Faixas, UFs toleradas e limiar de variacao vem de [[escopo.produtos]] no
config.toml, um conjunto por produto.
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


def _limites(cfg: Config) -> tuple[str, list[Any]]:
    """CTE com os limites de cada produto do config, e os parametros dela.

    Junta as regras ao dado por SQL, num JOIN por produto, em vez de uma
    consulta por produto.
    """
    produtos = list(cfg.produtos.values())
    linha = (
        "(CAST(? AS VARCHAR), CAST(? AS DOUBLE), CAST(? AS DOUBLE), CAST(? AS DOUBLE), "
        "CAST(? AS DOUBLE), CAST(? AS DOUBLE), CAST(? AS DOUBLE), CAST(? AS DOUBLE))"
    )
    cte = (
        "limites (produto, revenda_min, revenda_max, distribuicao_min, distribuicao_max, "
        "desvio_max, margem_max, limiar_variacao) AS (VALUES "
        + ", ".join(linha for _ in produtos)
        + ")"
    )
    parametros: list[Any] = []
    for p in produtos:
        parametros += [
            p.produto, *p.faixa_preco_revenda, *p.faixa_preco_distribuicao,
            p.desvio_padrao_maximo, p.margem_maxima, p.variacao_semanal_alerta,
        ]
    return cte, parametros


def testar_faixa_de_preco(con: duckdb.DuckDBPyConnection, cfg: Config) -> Resultado:
    """preco_medio obrigatorio e dentro da faixa de revenda do seu produto.

    A faixa e por produto: R$ 20 e absurdo para gasolina e normal para GLP.
    Produto no mart sem bloco em [[escopo.produtos]] tambem reprova, porque nao
    ha faixa contra a qual avalia-lo.
    """
    cte, parametros = _limites(cfg)
    fora = con.execute(
        f"""
        WITH {cte}
        SELECT f.data_inicio, f.uf, f.produto, f.preco_medio,
               l.revenda_min, l.revenda_max
        FROM {TABELA} f
        LEFT JOIN limites l ON l.produto = f.produto
        WHERE l.produto IS NULL
           OR f.preco_medio IS NULL
           OR f.preco_medio < l.revenda_min
           OR f.preco_medio > l.revenda_max
        ORDER BY f.produto, f.preco_medio
        """,
        parametros,
    ).fetchall()

    if fora:
        por_produto: dict[str, int] = {}
        for _, _, produto, _, _, _ in fora:
            por_produto[produto] = por_produto.get(produto, 0) + 1
        amostra = [
            {"data_inicio": str(d), "uf": u, "produto": p, "preco_medio": v,
             "faixa": None if mn is None else [mn, mx]}
            for d, u, p, v, mn, mx in fora[:10]
        ]
        sem_config = sorted({p for _, _, p, _, mn, _ in fora if mn is None})
        return Resultado(
            nome="preco_medio_na_faixa",
            status=FALHOU,
            mensagem=(
                f"{len(fora)} linhas com preco_medio nulo ou fora da faixa de revenda "
                f"do produto ({por_produto}). Isso indica erro de parsing ou mudanca "
                f"de unidade na fonte."
                + (f" Produtos sem bloco em [[escopo.produtos]]: {sem_config}." if sem_config else "")
                + f" Amostra: {amostra}"
            ),
            detalhes={"linhas_fora": len(fora), "por_produto": por_produto, "amostra": amostra},
        )
    faixas = {p.produto: list(p.faixa_preco_revenda) for p in cfg.produtos.values()}
    return Resultado(
        nome="preco_medio_na_faixa",
        status=PASSOU,
        mensagem="Todos os precos medios dentro da faixa de revenda do produto.",
        detalhes={"faixas": faixas},
    )


# (coluna, minimo, maximo): os limites sao expressoes sobre a CTE limites.
_REGRAS_OPCIONAIS = (
    ("preco_min", "l.revenda_min", "l.revenda_max"),
    ("preco_max", "l.revenda_min", "l.revenda_max"),
    ("desvio_padrao", "0", "l.desvio_max"),
    ("coef_variacao_revenda", "0", "?"),
    ("margem_media_revenda", "0", "l.margem_max"),
    ("preco_medio_distribuicao", "l.distribuicao_min", "l.distribuicao_max"),
    ("preco_min_distribuicao", "l.distribuicao_min", "l.distribuicao_max"),
    ("preco_max_distribuicao", "l.distribuicao_min", "l.distribuicao_max"),
    ("desvio_padrao_distribuicao", "0", "l.desvio_max"),
    ("coef_variacao_distribuicao", "0", "?"),
)


def testar_colunas_opcionais(con: duckdb.DuckDBPyConnection, cfg: Config) -> Resultado:
    """Colunas que aceitam nulo, mas, quando preenchidas, ficam na faixa do produto.

    A distribuicao e a margem so existem na fonte ate 16/08/2020, entao nulo
    passa. Valor presente fora da faixa reprova. Nao se exige min <= medio <= max
    na distribuicao: a propria ANP publica centenas de linhas que violam isso.
    """
    cte, parametros = _limites(cfg)
    coef_max = float(cfg["qualidade"]["coef_variacao_maximo"])
    consultas = []
    parametros_regras: list[Any] = []
    for coluna, minimo, maximo in _REGRAS_OPCIONAIS:
        consultas.append(
            f"""
            SELECT '{coluna}' AS coluna, f.data_inicio, f.uf, f.produto,
                   CAST(f.{coluna} AS DOUBLE) AS valor
            FROM {TABELA} f
            JOIN limites l ON l.produto = f.produto
            WHERE f.{coluna} IS NOT NULL
              AND (f.{coluna} < {minimo} OR f.{coluna} > {maximo})
            """
        )
        parametros_regras += [coef_max] * maximo.count("?")
    fora = con.execute(
        f"WITH {cte} SELECT * FROM ({' UNION ALL '.join(consultas)}) "
        "ORDER BY coluna, produto, data_inicio, uf",
        parametros + parametros_regras,
    ).fetchall()

    if fora:
        por_coluna: dict[str, int] = {}
        for coluna, *_ in fora:
            por_coluna[coluna] = por_coluna.get(coluna, 0) + 1
        amostra = [
            {"coluna": c, "data_inicio": str(d), "uf": u, "produto": p, "valor": v}
            for c, d, u, p, v in fora[:10]
        ]
        return Resultado(
            nome="colunas_opcionais_na_faixa",
            status=FALHOU,
            mensagem=(
                f"{len(fora)} valores preenchidos fora da faixa plausivel do produto "
                f"({por_coluna}). Nulo e aceito nessas colunas; valor fora da faixa "
                f"indica erro de parsing ou de unidade. Amostra: {amostra}"
            ),
            detalhes={"valores_fora": len(fora), "por_coluna": por_coluna, "amostra": amostra},
        )
    return Resultado(
        nome="colunas_opcionais_na_faixa",
        status=PASSOU,
        mensagem="Colunas opcionais nulas ou dentro da faixa do produto.",
    )


def testar_cobertura_de_ufs(
    con: duckdb.DuckDBPyConnection, cfg: Config, semana: date | None = None
) -> Resultado:
    """Cobertura de UFs na semana carregada, avaliada por produto.

    A regra nao pode ser global: a ANP nao pesquisa todo produto em toda UF em
    toda semana (o GNV, por exemplo, nao existe em dez UFs). Cada produto tem a
    sua lista de ausencias legitimas em ufs_ausentes_toleradas. Falta fora da
    lista e erro.

    Produto que ainda nao tem nenhuma linha ate a semana avaliada nao e cobrado:
    a GASOLINA ADITIVADA, por exemplo, so comeca em 18/10/2020. Produto com
    historico e sem nenhuma UF na semana reprova, como qualquer falta.
    """
    alvo = _semana_alvo(con, semana)
    if alvo is None:
        return Resultado(
            nome="cobertura_de_ufs",
            status=FALHOU,
            mensagem="O mart esta vazio: nao ha semana para avaliar.",
        )

    esperadas = set(cfg.ufs.values())

    problemas: list[str] = []
    detalhes: dict[str, Any] = {"semana": str(alvo), "por_produto": {}, "sem_serie": []}

    for produto in cfg.produtos.values():
        nome = produto.produto
        tem_historico = con.execute(
            f"SELECT COUNT(*) > 0 FROM {TABELA} WHERE produto = ? AND data_inicio <= ?",
            [nome, alvo],
        ).fetchone()[0]
        if not tem_historico:
            detalhes["sem_serie"].append(nome)
            continue

        presentes = {
            linha[0]
            for linha in con.execute(
                f"SELECT DISTINCT uf FROM {TABELA} WHERE data_inicio = ? AND produto = ?",
                [alvo, nome],
            ).fetchall()
        }
        toleradas = set(produto.ufs_ausentes_toleradas)
        ausentes = esperadas - presentes
        nao_toleradas = sorted(ausentes - toleradas)
        toleradas_ausentes = sorted(ausentes & toleradas)

        detalhes["por_produto"][nome] = {
            "ufs_presentes": len(presentes),
            "ufs_exigidas": len(esperadas - toleradas),
            "ausentes_toleradas": toleradas_ausentes,
            "ausentes_nao_toleradas": nao_toleradas,
        }

        if nao_toleradas:
            problemas.append(
                f"{nome}: faltam {nao_toleradas} na semana de {alvo} "
                f"({len(presentes)} de {len(esperadas)} UFs presentes)"
            )
        elif toleradas_ausentes:
            log.info(
                "UF ausente dentro da allowlist",
                extra={
                    "evento": "cobertura_tolerada",
                    "produto": nome,
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
                "ufs_ausentes_toleradas do produto em [[escopo.produtos]] no "
                "config.toml; caso contrario, a extracao perdeu linhas."
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
    """Variacao de preco acima do limiar do produto entre semanas consecutivas.

    A comparacao usa LAG sobre as semanas ordenadas de cada serie, e nao
    data_inicio - 7 dias: a serie da ANP tem tres descontinuidades (agosto de
    2015, a suspensao de agosto a outubro de 2020 e setembro de 2022), e
    subtrair sete dias compararia contra semana inexistente nesses pontos.

    O limiar e por produto (o GNV oscila mais). O desfecho e ALERTA, nunca
    erro: preco de combustivel oscila de verdade.
    """
    alvo = _semana_alvo(con, semana)
    if alvo is None:
        return Resultado(
            nome="variacao_semanal",
            status=FALHOU,
            mensagem="O mart esta vazio: nao ha semana para avaliar.",
        )

    limiares = {p.produto: p.variacao_semanal_alerta for p in cfg.produtos.values()}
    cte, parametros = _limites(cfg)
    linhas = con.execute(
        f"""
        WITH {cte},
        serie AS (
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
            s.uf,
            s.produto,
            s.semana_anterior,
            s.preco_anterior,
            s.preco_medio,
            (s.preco_medio - s.preco_anterior) / s.preco_anterior AS variacao,
            date_diff('day', s.semana_anterior, s.data_inicio) AS dias_desde_anterior
        FROM serie s
        JOIN limites l ON l.produto = s.produto
        WHERE s.data_inicio = ?
          AND s.preco_anterior IS NOT NULL
          AND s.preco_anterior > 0
          AND abs((s.preco_medio - s.preco_anterior) / s.preco_anterior) > l.limiar_variacao
        ORDER BY abs(variacao) DESC
        """,
        parametros + [alvo],
    ).fetchall()

    if not linhas:
        return Resultado(
            nome="variacao_semanal",
            status=PASSOU,
            mensagem=(
                f"Nenhuma variacao acima do limiar do produto na semana de {alvo}."
            ),
            detalhes={"semana": str(alvo), "limiares": limiares},
        )

    ocorrencias = [
        {
            "uf": uf,
            "produto": produto,
            "semana_anterior": str(anterior),
            "preco_anterior": round(pa, 3),
            "preco_atual": round(pm, 3),
            "variacao_pct": round(var * 100, 2),
            "limiar_pct": round(limiares[produto] * 100, 2),
            "dias_desde_semana_anterior": dias,
        }
        for uf, produto, anterior, pa, pm, var, dias in linhas
    ]
    return Resultado(
        nome="variacao_semanal",
        status=ALERTA,
        mensagem=(
            f"{len(ocorrencias)} series com variacao acima do limiar do produto na "
            f"semana de {alvo}. Nao bloqueia a carga."
        ),
        detalhes={"semana": str(alvo), "limiares": limiares, "ocorrencias": ocorrencias},
    )


TESTES = (
    testar_chave_primaria,
    testar_faixa_de_preco,
    testar_colunas_opcionais,
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
