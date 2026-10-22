"""Carga no PostgreSQL em tres passos: staging, validacao e promocao ao mart.

    carregar_staging -> validar -> promover

Cada passo e uma funcao independente, que recebe a conexao e o id_carga e nao
depende de estado em memoria do passo anterior: cada uma deve virar uma task
do Airflow. O que passa de um passo para o outro e o id_carga, mais a lista de
resultados da qualidade entre validar e promover.

- carregar_staging: registra a carga como "iniciada" e reescreve
  staging.precos_semanais (TRUNCATE + INSERT) com o DataFrame do staging.
- validar: roda os testes de qualidade sobre a staging, sem tocar no mart.
  Falha bloqueante marca a carga como "falhou", com o motivo em erro, e
  preserva a staging para inspecao.
- promover: numa unica transacao, faz o upsert staging -> mart e marca a carga
  como "concluida" ou "concluida_com_avisos". Em erro, rollback e "falhou"
  gravado numa transacao separada.

O mart so muda em promover. O DDL vive em sql/, em arquivos numerados e
idempotentes, aplicados em ordem a cada conexao.

A conexao trabalha em autocommit: cada bloco conexao.transaction() e entao uma
transacao real (BEGIN/COMMIT), e nao um savepoint dentro de uma transacao
implicita que ninguem confirmaria.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any
from uuid import uuid4

import duckdb
import pandas as pd
import psycopg
from dotenv import load_dotenv

from anp.carregar import COLUNAS_MART, TABELA
from anp.config import RAIZ_PROJETO, Config
from anp.erros import ErroPipeline, ErroQualidade
from anp.extrair import _ler_manifesto
from anp.log import obter_logger
from anp.qualidade import ALERTA, Resultado, executar_testes

log = obter_logger("carga")

TABELA_MART = f"mart.{TABELA}"
TABELA_STAGING = "staging.precos_semanais"
TABELA_CARGAS = "controle.cargas"
DIR_SQL = RAIZ_PROJETO / "sql"
ARQUIVO_ENV = RAIZ_PROJETO / ".env"

INICIADA = "iniciada"
CONCLUIDA = "concluida"
CONCLUIDA_COM_AVISOS = "concluida_com_avisos"
FALHOU = "falhou"

_COLUNAS_NUMERIC = ("preco_medio", "preco_min", "preco_max", "desvio_padrao")

# Tipos da copia da staging no DuckDB, os mesmos do mart DuckDB, mas sem
# restricoes: duplicata e nulo tem que chegar aos testes de qualidade.
_TIPOS_DUCKDB = {
    "data_inicio": "DATE",
    "data_fim": "DATE",
    "uf": "VARCHAR",
    "regiao": "VARCHAR",
    "produto": "VARCHAR",
    "preco_medio": "DOUBLE",
    "preco_min": "DOUBLE",
    "preco_max": "DOUBLE",
    "desvio_padrao": "DOUBLE",
    "numero_postos": "INTEGER",
    "data_ingestao": "TIMESTAMP",
}


# --- conexao e DDL ------------------------------------------------------------

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


def _exigir_autocommit(conexao: psycopg.Connection) -> None:
    if not conexao.autocommit:
        raise ErroPipeline(
            "A conexao com o PostgreSQL precisa estar em autocommit para que "
            "cada transacao da carga seja de fato confirmada. Use "
            "anp.carregar.conectar(cfg) para abri-la."
        )


# --- controle.cargas ------------------------------------------------------------

def _registrar_inicio(
    conexao: psycopg.Connection, id_carga: str, sha256: str,
    arquivo: str | None, url: str | None, linhas_lidas: int,
) -> None:
    with conexao.transaction():
        conexao.execute(
            f"""
            INSERT INTO {TABELA_CARGAS}
                (id_carga, sha256, arquivo, url, status, linhas_lidas, inicio)
            VALUES (%s, %s, %s, %s, %s, %s, now())
            """,
            [id_carga, sha256, arquivo, url, INICIADA, linhas_lidas],
        )


def _marcar_concluida(
    conexao: psycopg.Connection, id_carga: str, status: str, linhas: int
) -> None:
    """Chamada dentro da transacao do upsert: os dois confirmam ou nenhum."""
    conexao.execute(
        f"""
        UPDATE {TABELA_CARGAS}
        SET status = %s, linhas_carregadas = %s, fim = clock_timestamp()
        WHERE id_carga = %s
        """,
        [status, linhas, id_carga],
    )


def _marcar_falha(conexao: psycopg.Connection, id_carga: str, mensagem: str) -> None:
    """Grava "falhou" numa transacao propria.

    Se nem isso for possivel (conexao perdida, por exemplo), registra no log e
    deixa a excecao original seguir: ela e mais util que a do registro.
    """
    try:
        with conexao.transaction():
            conexao.execute(
                f"""
                UPDATE {TABELA_CARGAS}
                SET status = %s, erro = %s, fim = clock_timestamp()
                WHERE id_carga = %s
                """,
                [FALHOU, mensagem, id_carga],
            )
    except psycopg.Error as erro_registro:
        log.error(
            "nao foi possivel registrar a falha em controle.cargas",
            extra={
                "evento": "registro_falha_erro",
                "id_carga": id_carga,
                "causa": str(erro_registro),
            },
        )


def _falhar(conexao: psycopg.Connection, id_carga: str, passo: str, erro: Exception,
            mensagem: str | None = None) -> None:
    mensagem = mensagem or f"{type(erro).__name__}: {erro}"
    _marcar_falha(conexao, id_carga, mensagem)
    log.error(
        f"carga falhou em {passo}",
        extra={
            "evento": "carga_falhou",
            "backend": "postgres",
            "passo": passo,
            "id_carga": id_carga,
            "causa": mensagem,
        },
    )


def _carga(conexao: psycopg.Connection, id_carga: str) -> tuple[str, int]:
    """Status e linhas_lidas da carga. Levanta ErroPipeline se ela nao existir."""
    linha = conexao.execute(
        f"SELECT status, linhas_lidas FROM {TABELA_CARGAS} WHERE id_carga = %s",
        [id_carga],
    ).fetchone()
    if linha is None:
        raise ErroPipeline(f"Carga {id_carga} nao existe em {TABELA_CARGAS}.")
    return linha[0], linha[1]


def _exigir_staging_da_carga(conexao: psycopg.Connection, id_carga: str) -> None:
    """A staging e unica e reescrita a cada carga: confere que ainda e desta."""
    _, lidas = _carga(conexao, id_carga)
    presentes = conexao.execute(
        f"SELECT COUNT(*) FROM {TABELA_STAGING} WHERE id_carga = %s", [id_carga]
    ).fetchone()[0]
    if presentes != lidas:
        raise ErroPipeline(
            f"A staging tem {presentes} linhas da carga {id_carga}, mas a carga "
            f"leu {lidas}. Outra carga provavelmente reescreveu a staging; "
            "rode a carga de novo a partir de carregar_staging."
        )


# --- passo 1: staging -------------------------------------------------------------

def _origem(cfg: Config, sha256: str) -> tuple[str | None, str | None]:
    """Arquivo e URL da coleta com este sha256, segundo o manifesto da camada raw.

    Um raw reaproveitado via --usar-raw nao passa pela extracao e, portanto, nao
    tem entrada no manifesto: arquivo e url ficam nulos.
    """
    registros = [
        r for r in _ler_manifesto(cfg.caminho("manifesto_raw")) if r.get("sha256") == sha256
    ]
    if not registros:
        return None, None
    ultimo = registros[-1]
    return Path(ultimo["caminho"]).name, ultimo.get("url")


def _sha256_do_staging(df_staging: pd.DataFrame) -> str:
    if "origem_sha256" not in df_staging.columns or df_staging.empty:
        return "desconhecido"
    return str(df_staging["origem_sha256"].iloc[0])


def _registros(df_staging: pd.DataFrame, id_carga: str) -> list[tuple[Any, ...]]:
    """Linhas (id_carga, *COLUNAS_MART) com tipos nativos do Python.

    NaN e pd.NA viram None: um NaN float chegaria ao numeric como 'NaN', que o
    PostgreSQL aceita como valor, em vez de nulo.
    """
    dados = df_staging[list(COLUNAS_MART)].copy()
    dados["data_inicio"] = pd.to_datetime(dados["data_inicio"]).dt.date
    dados["data_fim"] = pd.to_datetime(dados["data_fim"]).dt.date
    dados["data_ingestao"] = pd.to_datetime(dados["data_ingestao"])
    dados["numero_postos"] = dados["numero_postos"].astype("Int64")

    def nativo(valor: Any) -> Any:
        if valor is None or pd.isna(valor):
            return None
        if isinstance(valor, pd.Timestamp):
            return valor.to_pydatetime()
        return valor.item() if hasattr(valor, "item") else valor

    return [
        (id_carga, *(nativo(v) for v in linha))
        for linha in dados.astype(object).itertuples(index=False, name=None)
    ]


def carregar_staging(
    cfg: Config, df_staging: pd.DataFrame, conexao: psycopg.Connection
) -> str:
    """Registra a carga e reescreve staging.precos_semanais. Devolve o id_carga."""
    _exigir_autocommit(conexao)
    id_carga = str(uuid4())
    registros = _registros(df_staging, id_carga)
    sha256 = _sha256_do_staging(df_staging)
    arquivo, url = _origem(cfg, sha256)
    _registrar_inicio(conexao, id_carga, sha256, arquivo, url, len(registros))

    colunas = ", ".join(("id_carga", *COLUNAS_MART))
    try:
        with conexao.transaction():
            conexao.execute(f"TRUNCATE {TABELA_STAGING}")
            with conexao.cursor() as cursor:
                with cursor.copy(f"COPY {TABELA_STAGING} ({colunas}) FROM STDIN") as copia:
                    for registro in registros:
                        copia.write_row(registro)
    except Exception as erro:
        _falhar(conexao, id_carga, "carregar_staging", erro)
        raise

    log.info(
        "staging carregada",
        extra={
            "evento": "staging_pg_ok",
            "id_carga": id_carga,
            "sha256": sha256,
            "linhas": len(registros),
        },
    )
    return id_carga


# --- passo 2: validacao -------------------------------------------------------------

@contextmanager
def _staging_em_duckdb(
    conexao: psycopg.Connection, id_carga: str
) -> Iterator[duckdb.DuckDBPyConnection]:
    """Copia a staging desta carga para um DuckDB em memoria.

    Os testes de anp.qualidade sao escritos no dialeto do DuckDB e consultam a
    tabela fato_precos_semanais. Em vez de reescreve-los, rodam sobre uma copia
    da staging com esse nome. A fonte e cumulativa, entao a staging traz a serie
    inteira e os testes que olham o historico (variacao semanal) continuam validos.
    """
    colunas = ", ".join(COLUNAS_MART)
    linhas = conexao.execute(
        f"SELECT {colunas} FROM {TABELA_STAGING} WHERE id_carga = %s", [id_carga]
    ).fetchall()
    df = pd.DataFrame(linhas, columns=list(COLUNAS_MART))
    for coluna in _COLUNAS_NUMERIC:
        df[coluna] = pd.to_numeric(df[coluna]).astype("float64")
    df["numero_postos"] = df["numero_postos"].astype("Int64")

    copia = duckdb.connect(":memory:")
    try:
        copia.register("staging_postgres", df)
        selecao = ", ".join(f"CAST({c} AS {t}) AS {c}" for c, t in _TIPOS_DUCKDB.items())
        copia.execute(f"CREATE TABLE {TABELA} AS SELECT {selecao} FROM staging_postgres")
        copia.unregister("staging_postgres")
        yield copia
    finally:
        copia.close()


def validar(
    cfg: Config,
    conexao: psycopg.Connection,
    id_carga: str,
    semana: date | None = None,
) -> list[Resultado]:
    """Roda os testes de qualidade sobre a staging, sem tocar no mart.

    Falha bloqueante marca a carga como "falhou", com o motivo em erro, e
    levanta ErroQualidade. A staging fica como esta, para inspecao.
    """
    try:
        _exigir_staging_da_carga(conexao, id_carga)
        with _staging_em_duckdb(conexao, id_carga) as copia:
            resultados = executar_testes(copia, cfg, semana=semana)
    except ErroQualidade as erro:
        _falhar(conexao, id_carga, "validar", erro,
                mensagem=f"Reprovada nos testes de qualidade: {erro}")
        raise
    except Exception as erro:
        _falhar(conexao, id_carga, "validar", erro)
        raise
    return resultados


# --- passo 3: promocao ao mart ---------------------------------------------------------

def promover(
    conexao: psycopg.Connection,
    id_carga: str,
    resultados: list[Resultado],
) -> int:
    """Upsert staging -> mart e status final da carga, na mesma transacao.

    So promove carga ainda "iniciada": uma carga reprovada em validar ja esta
    "falhou" e nunca chega ao mart. Devolve o total de linhas carregadas.
    """
    _exigir_autocommit(conexao)
    status_atual, _ = _carga(conexao, id_carga)
    if status_atual != INICIADA:
        raise ErroPipeline(
            f"A carga {id_carga} esta com status {status_atual!r} e nao pode ser "
            f"promovida ao mart: so cargas {INICIADA!r} e aprovadas em validar."
        )

    status_final = (
        CONCLUIDA_COM_AVISOS if any(r.status == ALERTA for r in resultados) else CONCLUIDA
    )
    colunas = ", ".join(COLUNAS_MART)
    atualizaveis = [c for c in COLUNAS_MART if c not in ("data_inicio", "uf", "produto")]
    set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in atualizaveis)

    try:
        _exigir_staging_da_carga(conexao, id_carga)
        with conexao.transaction():
            antes = conexao.execute(f"SELECT COUNT(*) FROM {TABELA_MART}").fetchone()[0]
            cursor = conexao.execute(
                f"""
                INSERT INTO {TABELA_MART} ({colunas})
                SELECT {colunas} FROM {TABELA_STAGING} WHERE id_carga = %s
                ON CONFLICT (data_inicio, uf, produto) DO UPDATE SET {set_clause}
                """,
                [id_carga],
            )
            carregadas = cursor.rowcount
            _marcar_concluida(conexao, id_carga, status_final, carregadas)
            depois = conexao.execute(f"SELECT COUNT(*) FROM {TABELA_MART}").fetchone()[0]
    except Exception as erro:
        _falhar(conexao, id_carga, "promover", erro)
        raise

    log.info(
        "carga concluida",
        extra={
            "evento": "carga_ok",
            "backend": "postgres",
            "id_carga": id_carga,
            "status": status_final,
            "linhas_processadas": carregadas,
            "linhas_antes": antes,
            "linhas_depois": depois,
            "linhas_novas": depois - antes,
            "linhas_atualizadas": carregadas - (depois - antes),
        },
    )
    return carregadas


# --- os tres passos em sequencia ------------------------------------------------------

def carregar(
    cfg: Config,
    df_staging: pd.DataFrame,
    conexao: psycopg.Connection | None = None,
    semana: date | None = None,
) -> int:
    """carregar_staging -> validar -> promover, para quem chama anp.carregar.carregar.

    Mesmo contrato da carga no DuckDB: devolve o total de linhas carregadas e
    deixa a excecao seguir em caso de erro. Aqui, uma reprovacao na qualidade
    tambem e erro (ErroQualidade), e o mart nao e alterado.
    """
    propria = conexao is None
    con = conexao or conectar(cfg)
    try:
        id_carga = carregar_staging(cfg, df_staging, con)
        resultados = validar(cfg, con, id_carga, semana=semana)
        return promover(con, id_carga, resultados)
    finally:
        if propria:
            con.close()
