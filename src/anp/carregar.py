"""Etapa 3: carga da tabela analitica no DuckDB ou no PostgreSQL.

A granularidade e semana x UF x produto, com chave primaria
(data_inicio, uf, produto). A carga e um upsert dentro de uma transacao:
rodar duas vezes com o mesmo staging nao duplica nenhuma linha, apenas
reescreve as existentes com a data_ingestao mais recente.

O destino vem de [destino] backend no config.toml. Este modulo implementa o
DuckDB e repassa o PostgreSQL para anp.carregar_postgres, onde a carga passa
por staging e qualidade antes de chegar ao mart.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import pandas as pd

from anp.config import Config
from anp.log import obter_logger

if TYPE_CHECKING:
    import psycopg

log = obter_logger("carga")

TABELA = "fato_precos_semanais"
TABELA_DIM_PRODUTO = "dim_produto"

# Colunas que vieram com o escopo ampliado. Ficam no fim da tabela, depois de
# data_ingestao, porque e onde o ALTER TABLE ... ADD COLUMN as poe num banco que
# ja existia: assim DuckDB e PostgreSQL tem a mesma ordem, novo ou migrado.
COLUNAS_OPCIONAIS = (
    "coef_variacao_revenda",
    "margem_media_revenda",
    "preco_medio_distribuicao",
    "preco_min_distribuicao",
    "preco_max_distribuicao",
    "desvio_padrao_distribuicao",
    "coef_variacao_distribuicao",
)

# A ordem das colunas aqui e a ordem do INSERT mais adiante.
COLUNAS_MART = (
    "data_inicio",
    "data_fim",
    "uf",
    "regiao",
    "produto",
    "preco_medio",
    "preco_min",
    "preco_max",
    "desvio_padrao",
    "numero_postos",
    "data_ingestao",
    *COLUNAS_OPCIONAIS,
)

COLUNAS_DIM_PRODUTO = ("produto", "rotulo_anp", "unidade", "categoria")

_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABELA} (
    data_inicio    DATE       NOT NULL,
    data_fim       DATE       NOT NULL,
    uf             VARCHAR    NOT NULL,
    regiao         VARCHAR    NOT NULL,
    produto        VARCHAR    NOT NULL,
    preco_medio    DOUBLE     NOT NULL,
    preco_min      DOUBLE,
    preco_max      DOUBLE,
    desvio_padrao  DOUBLE,
    numero_postos  INTEGER,
    data_ingestao  TIMESTAMP  NOT NULL,
    PRIMARY KEY (data_inicio, uf, produto)
);
"""

# Idempotente: num mart criado antes do escopo ampliado, acrescenta as colunas
# sem recriar a tabela; num mart novo, nao faz nada.
_DDL_COLUNAS_OPCIONAIS = "\n".join(
    f"ALTER TABLE {TABELA} ADD COLUMN IF NOT EXISTS {coluna} DOUBLE;"
    for coluna in COLUNAS_OPCIONAIS
)

# Sem UNIQUE em rotulo_anp, ao contrario do PostgreSQL: o DuckDB nao deixa o
# ON CONFLICT DO UPDATE atribuir a coluna com indice. A unicidade e garantida
# antes, na validacao de [[escopo.produtos]] em anp.config.
_DDL_DIM_PRODUTO = f"""
CREATE TABLE IF NOT EXISTS {TABELA_DIM_PRODUTO} (
    produto     VARCHAR  PRIMARY KEY,
    rotulo_anp  VARCHAR  NOT NULL,
    unidade     VARCHAR  NOT NULL,
    categoria   VARCHAR  NOT NULL CHECK (categoria IN ('combustivel_veicular', 'glp'))
);
"""


def criar_tabela(conexao: duckdb.DuckDBPyConnection) -> None:
    """Cria ou atualiza o esquema do mart, sem tocar nas linhas existentes."""
    conexao.execute(_DDL)
    conexao.execute(_DDL_COLUNAS_OPCIONAIS)
    conexao.execute(_DDL_DIM_PRODUTO)


def linhas_dim_produto(cfg: Config) -> list[tuple[str, str, str, str]]:
    """As linhas da dim_produto, tiradas de [[escopo.produtos]]."""
    return [
        (p.produto, p.rotulo_anp, p.unidade, p.categoria) for p in cfg.produtos.values()
    ]


def _postgres(cfg: Config) -> bool:
    return cfg.backend == "postgres"


def conectar(cfg: Config) -> duckdb.DuckDBPyConnection | psycopg.Connection:
    """Abre o banco do mart, criando o diretorio e a tabela se preciso."""
    if _postgres(cfg):
        # Import tardio: carregar_postgres importa este modulo.
        from anp import carregar_postgres

        return carregar_postgres.conectar(cfg)

    caminho = cfg.caminho("mart_db")
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conexao = duckdb.connect(str(caminho))
    criar_tabela(conexao)
    return conexao


def carregar(
    cfg: Config,
    df_staging: pd.DataFrame,
    conexao: duckdb.DuckDBPyConnection | psycopg.Connection | None = None,
) -> int:
    """Faz o upsert do staging no mart e devolve o total de linhas afetadas.

    O ON CONFLICT sobre a chave primaria e o que garante idempotencia: a
    segunda execucao atualiza no lugar em vez de inserir de novo.
    """
    if _postgres(cfg):
        from anp import carregar_postgres

        return carregar_postgres.carregar(cfg, df_staging, conexao)

    propria = conexao is None
    con = conexao or conectar(cfg)
    try:
        dados = df_staging[list(COLUNAS_MART)].copy()
        dados["data_inicio"] = pd.to_datetime(dados["data_inicio"]).dt.date
        dados["data_fim"] = pd.to_datetime(dados["data_fim"]).dt.date
        dados["numero_postos"] = dados["numero_postos"].astype("Int64")

        antes = con.execute(f"SELECT COUNT(*) FROM {TABELA}").fetchone()[0]

        colunas = ", ".join(COLUNAS_MART)
        atualizaveis = [c for c in COLUNAS_MART if c not in ("data_inicio", "uf", "produto")]
        set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in atualizaveis)

        con.execute("BEGIN TRANSACTION")
        try:
            con.executemany(
                f"""
                INSERT INTO {TABELA_DIM_PRODUTO} ({", ".join(COLUNAS_DIM_PRODUTO)})
                VALUES (?, ?, ?, ?)
                ON CONFLICT (produto) DO UPDATE SET
                    rotulo_anp = EXCLUDED.rotulo_anp,
                    unidade = EXCLUDED.unidade,
                    categoria = EXCLUDED.categoria
                """,
                linhas_dim_produto(cfg),
            )
            con.register("staging_df", dados)
            con.execute(
                f"""
                INSERT INTO {TABELA} ({colunas})
                SELECT {colunas} FROM staging_df
                ON CONFLICT (data_inicio, uf, produto) DO UPDATE SET {set_clause}
                """
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.unregister("staging_df")

        depois = con.execute(f"SELECT COUNT(*) FROM {TABELA}").fetchone()[0]
        log.info(
            "carga concluida",
            extra={
                "evento": "carga_ok",
                "linhas_processadas": len(dados),
                "linhas_antes": antes,
                "linhas_depois": depois,
                "linhas_novas": depois - antes,
                "linhas_atualizadas": len(dados) - (depois - antes),
            },
        )
        return len(dados)
    finally:
        if propria:
            con.close()
