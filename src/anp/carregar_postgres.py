"""Destino PostgreSQL: conexao e DDL.

O DDL vive em sql/, em arquivos numerados e idempotentes, aplicados em ordem a
cada conexao. A conexao trabalha em autocommit: cada bloco
conexao.transaction() e entao uma transacao real (BEGIN/COMMIT), e nao um
savepoint dentro de uma transacao implicita que ninguem confirmaria.
"""

from __future__ import annotations

import os

import psycopg
from dotenv import load_dotenv

from anp.config import RAIZ_PROJETO, Config
from anp.erros import ErroPipeline

DIR_SQL = RAIZ_PROJETO / "sql"
ARQUIVO_ENV = RAIZ_PROJETO / ".env"


def url_conexao() -> str:
    """DATABASE_URL do ambiente; o .env da raiz so preenche o que faltar."""
    load_dotenv(ARQUIVO_ENV, override=False)
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise ErroPipeline(
            'destino.backend = "postgres" no config.toml, mas DATABASE_URL nao '
            "esta definida. Copie .env.example para .env e preencha a URL de "
            "conexao, ou exporte a variavel no ambiente."
        )
    return url


def aplicar_ddl(conexao: psycopg.Connection) -> None:
    """Executa, em ordem numerica, os arquivos de sql/. Todos sao idempotentes."""
    arquivos = sorted(DIR_SQL.glob("[0-9][0-9][0-9]_*.sql"))
    if not arquivos:
        raise ErroPipeline(f"Nenhum arquivo de DDL encontrado em {DIR_SQL}.")
    with conexao.transaction():
        for arquivo in arquivos:
            conexao.execute(arquivo.read_text(encoding="utf-8"))


def conectar(cfg: Config) -> psycopg.Connection:
    """Abre a conexao com o PostgreSQL e garante schemas e tabelas."""
    conexao = psycopg.connect(url_conexao(), autocommit=True)
    try:
        aplicar_ddl(conexao)
    except Exception:
        conexao.close()
        raise
    return conexao
