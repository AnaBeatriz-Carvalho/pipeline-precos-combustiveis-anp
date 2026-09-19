"""Etapa 3: carga da tabela analitica no DuckDB.

A granularidade e semana x UF x produto, com chave primaria
(data_inicio, uf, produto). A carga e um upsert dentro de uma transacao:
rodar duas vezes com o mesmo staging nao duplica nenhuma linha, apenas
reescreve as existentes com a data_ingestao mais recente.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

from anp.config import Config
from anp.log import obter_logger

log = obter_logger("carga")

TABELA = "fato_precos_semanais"

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
)

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


def criar_tabela(conexao: duckdb.DuckDBPyConnection) -> None:
    """Cria a tabela analitica caso ainda nao exista."""
    conexao.execute(_DDL)


def conectar(cfg: Config) -> duckdb.DuckDBPyConnection:
    """Abre o banco do mart, criando o diretorio e a tabela se preciso."""
    caminho = cfg.caminho("mart_db")
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conexao = duckdb.connect(str(caminho))
    criar_tabela(conexao)
    return conexao


def carregar(
    cfg: Config,
    df_staging: pd.DataFrame,
    conexao: duckdb.DuckDBPyConnection | None = None,
) -> int:
    """Faz o upsert do staging no mart e devolve o total de linhas afetadas.

    O ON CONFLICT sobre a chave primaria e o que garante idempotencia: a
    segunda execucao atualiza no lugar em vez de inserir de novo.
    """
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
