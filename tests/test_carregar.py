"""Testes da carga no DuckDB: esquema, idempotencia e atomicidade."""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd
import pytest

from anp.carregar import TABELA, carregar, conectar
from anp.transformar import transformar
from conftest import LINHAS_POR_SEMANA, escrever_planilha, semana_completa, semana_no_mart

SHA = "b" * 64


@pytest.fixture
def cfg_temporario(cfg, tmp_path, monkeypatch):
    """Config apontando o mart para um banco descartavel."""
    monkeypatch.setitem(cfg.dados["caminhos"], "mart_db", str(tmp_path / "mart.duckdb"))
    monkeypatch.setitem(cfg.dados["caminhos"], "staging", str(tmp_path / "staging"))
    return cfg


def test_esquema_da_tabela_e_a_granularidade_combinada(cfg_temporario):
    con = conectar(cfg_temporario)
    esquema = con.execute(f"DESCRIBE {TABELA}").df()
    assert list(esquema["column_name"]) == [
        "data_inicio", "data_fim", "uf", "regiao", "produto", "preco_medio",
        "preco_min", "preco_max", "desvio_padrao", "numero_postos", "data_ingestao",
        "coef_variacao_revenda", "margem_media_revenda", "preco_medio_distribuicao",
        "preco_min_distribuicao", "preco_max_distribuicao",
        "desvio_padrao_distribuicao", "coef_variacao_distribuicao",
    ]
    chave = set(esquema.loc[esquema["key"] == "PRI", "column_name"])
    assert chave == {"data_inicio", "uf", "produto"}
    con.close()


def test_carga_dupla_nao_duplica_linha(cfg_temporario, planilha_valida):
    """O requisito central: rodar duas vezes nao duplica nada."""
    df = transformar(cfg_temporario, planilha_valida, SHA)
    con = conectar(cfg_temporario)

    carregar(cfg_temporario, df, conexao=con)
    apos_primeira = con.execute(f"SELECT COUNT(*) FROM {TABELA}").fetchone()[0]
    carregar(cfg_temporario, df, conexao=con)
    carregar(cfg_temporario, df, conexao=con)
    apos_terceira = con.execute(f"SELECT COUNT(*) FROM {TABELA}").fetchone()[0]

    assert apos_primeira == len(df) == 2 * LINHAS_POR_SEMANA
    assert apos_terceira == apos_primeira
    duplicatas = con.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM {TABELA} "
        "GROUP BY data_inicio, uf, produto HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    assert duplicatas == 0
    con.close()


def test_recarga_atualiza_a_linha_existente(cfg_temporario, planilha_valida):
    """Preco revisado pela ANP sobrescreve o valor antigo, sem inserir outro."""
    df = transformar(cfg_temporario, planilha_valida, SHA)
    con = conectar(cfg_temporario)
    carregar(cfg_temporario, df, conexao=con)

    revisado = df.copy()
    alvo = (revisado["uf"] == "SE") & (revisado["produto"] == "GASOLINA C")
    revisado.loc[alvo, "preco_medio"] = 6.66
    carregar(cfg_temporario, revisado, conexao=con)

    precos = con.execute(
        f"SELECT DISTINCT preco_medio FROM {TABELA} "
        "WHERE uf = 'SE' AND produto = 'GASOLINA C'"
    ).fetchall()
    assert precos == [(6.66,)]
    assert con.execute(f"SELECT COUNT(*) FROM {TABELA}").fetchone()[0] == len(df)
    con.close()


def test_carga_parcial_nao_e_deixada_para_tras(cfg_temporario, planilha_valida):
    """Uma linha invalida aborta a transacao inteira, sem meia carga."""
    df = transformar(cfg_temporario, planilha_valida, SHA)
    con = conectar(cfg_temporario)
    carregar(cfg_temporario, df, conexao=con)
    antes = con.execute(f"SELECT COUNT(*) FROM {TABELA}").fetchone()[0]

    quebrado = df.copy()
    quebrado.loc[quebrado.index[0], "preco_medio"] = None  # coluna NOT NULL
    with pytest.raises(duckdb.Error):
        carregar(cfg_temporario, quebrado, conexao=con)

    assert con.execute(f"SELECT COUNT(*) FROM {TABELA}").fetchone()[0] == antes
    con.close()


def test_tipos_persistidos_sao_os_esperados(cfg_temporario, planilha_valida):
    df = transformar(cfg_temporario, planilha_valida, SHA)
    con = conectar(cfg_temporario)
    carregar(cfg_temporario, df, conexao=con)

    linha = con.execute(
        f"SELECT data_inicio, data_fim, numero_postos, preco_medio FROM {TABELA} LIMIT 1"
    ).fetchone()
    assert isinstance(linha[0], date)
    assert isinstance(linha[1], date)
    assert isinstance(linha[2], int)
    assert isinstance(linha[3], float)
    con.close()


def test_semana_nova_e_acrescentada_sem_tocar_nas_antigas(cfg_temporario):
    con = conectar(cfg_temporario)
    semana_no_mart(con, date(2026, 9, 6))
    antes = con.execute(f"SELECT COUNT(*) FROM {TABELA}").fetchone()[0]
    semana_no_mart(con, date(2026, 9, 13))
    depois = con.execute(f"SELECT COUNT(*) FROM {TABELA}").fetchone()[0]
    assert depois == antes * 2
    assert con.execute(
        f"SELECT COUNT(DISTINCT data_inicio) FROM {TABELA}"
    ).fetchone()[0] == 2
    con.close()


# --- escopo ampliado: colunas novas e dim_produto ---------------------------

_DDL_ANTIGO = f"""
CREATE TABLE {TABELA} (
    data_inicio DATE NOT NULL, data_fim DATE NOT NULL, uf VARCHAR NOT NULL,
    regiao VARCHAR NOT NULL, produto VARCHAR NOT NULL, preco_medio DOUBLE NOT NULL,
    preco_min DOUBLE, preco_max DOUBLE, desvio_padrao DOUBLE, numero_postos INTEGER,
    data_ingestao TIMESTAMP NOT NULL,
    PRIMARY KEY (data_inicio, uf, produto)
)
"""


def test_mart_antigo_ganha_as_colunas_sem_perder_linhas(cfg_temporario, tmp_path):
    """Um anp.duckdb criado antes do escopo ampliado e migrado no lugar."""
    caminho = cfg_temporario.caminho("mart_db")
    antigo = duckdb.connect(str(caminho))
    antigo.execute(_DDL_ANTIGO)
    antigo.execute(
        f"INSERT INTO {TABELA} VALUES ('2020-08-16', '2020-08-22', 'SE', 'NORDESTE', "
        "'GASOLINA C', 4.5, 4.3, 4.7, 0.1, 30, '2026-09-19 12:00:00')"
    )
    antigo.close()

    con = conectar(cfg_temporario)
    assert con.execute(f"SELECT COUNT(*) FROM {TABELA}").fetchone()[0] == 1
    assert con.execute(
        f"SELECT preco_medio, margem_media_revenda FROM {TABELA}"
    ).fetchone() == (4.5, None)

    # A carga seguinte preenche as colunas novas da mesma linha.
    planilha = escrever_planilha(
        tmp_path / "com_distribuicao.xlsx",
        semana_completa(date(2020, 8, 16), preco_gasolina=4.5, distribuicao=True),
    )
    carregar(cfg_temporario, transformar(cfg_temporario, planilha, SHA), conexao=con)
    linha = con.execute(
        f"SELECT preco_medio_distribuicao, margem_media_revenda FROM {TABELA} "
        "WHERE uf = 'SE' AND produto = 'GASOLINA C'"
    ).fetchone()
    assert linha == pytest.approx((3.6, 0.9))
    assert con.execute(f"SELECT COUNT(*) FROM {TABELA}").fetchone()[0] == LINHAS_POR_SEMANA
    con.close()


def test_dim_produto_reflete_o_config(cfg_temporario, planilha_valida):
    df = transformar(cfg_temporario, planilha_valida, SHA)
    con = conectar(cfg_temporario)
    carregar(cfg_temporario, df, conexao=con)
    carregar(cfg_temporario, df, conexao=con)  # o upsert nao duplica

    dim = con.execute(
        "SELECT produto, rotulo_anp, unidade, categoria FROM dim_produto ORDER BY produto"
    ).fetchall()
    esperado = sorted(
        (p.produto, p.rotulo_anp, p.unidade, p.categoria)
        for p in cfg_temporario.produtos.values()
    )
    assert dim == esperado
    assert ("GLP", "GLP", "R$/13kg", "glp") in dim
    assert ("GASOLINA C", "GASOLINA COMUM", "R$/l", "combustivel_veicular") in dim

    orfaos = con.execute(
        f"SELECT COUNT(*) FROM {TABELA} f LEFT JOIN dim_produto d USING (produto) "
        "WHERE d.produto IS NULL"
    ).fetchone()[0]
    assert orfaos == 0
    con.close()
