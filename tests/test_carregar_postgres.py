"""Testes da carga no PostgreSQL: DDL, staging, qualidade, promocao e rollback.

Os testes marcados com postgres nao tocam no banco de DATABASE_URL: criam um
banco temporario no mesmo servidor, uma vez por sessao, e o removem ao final.
"""

from __future__ import annotations

import json
import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from anp import carregar_postgres
from anp.carregar import COLUNAS_OPCIONAIS, carregar, conectar
from anp.config import carregar_config
from anp.erros import ErroPipeline, ErroQualidade
from anp.pipeline import executar
from anp.qualidade import ALERTA
from anp.transformar import transformar
from conftest import LINHAS_POR_SEMANA, escrever_planilha, semana_completa

SHA = "c" * 64
URL_FONTE = "https://exemplo.invalido/semanal-estados.xlsx"


# --- sem PostgreSQL ---------------------------------------------------------

def test_backend_padrao_e_duckdb(cfg):
    assert cfg.backend == "duckdb"


def test_backend_desconhecido_e_rejeitado(tmp_path):
    from anp.config import CAMINHO_PADRAO

    texto = CAMINHO_PADRAO.read_text(encoding="utf-8").replace(
        'backend = "duckdb"', 'backend = "sqlite"'
    )
    alvo = tmp_path / "config.toml"
    alvo.write_text(texto, encoding="utf-8")
    with pytest.raises(ErroPipeline, match="sqlite"):
        carregar_config(alvo)


def test_postgres_sem_database_url_falha_com_mensagem_acionavel(cfg, tmp_path, monkeypatch):
    monkeypatch.setitem(cfg.dados["destino"], "backend", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(carregar_postgres, "ARQUIVO_ENV", tmp_path / "inexistente.env")
    with pytest.raises(ErroPipeline, match="DATABASE_URL"):
        conectar(cfg)


def test_conectar_nao_aplica_ddl(cfg, monkeypatch):
    """O DDL e um passo explicito (inicializar_banco), nao efeito de conectar."""
    monkeypatch.setitem(cfg.dados["destino"], "backend", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:s@host.invalido/anp")
    aberturas = []
    monkeypatch.setattr(
        carregar_postgres.psycopg, "connect",
        lambda url, **kw: aberturas.append((url, kw)) or object(),
    )

    def proibido(_conexao):
        raise AssertionError("conectar nao deveria aplicar DDL")

    monkeypatch.setattr(carregar_postgres, "aplicar_ddl", proibido)

    conectar(cfg)
    assert aberturas == [("postgresql://u:s@host.invalido/anp", {"autocommit": True})]


# --- com PostgreSQL ---------------------------------------------------------

@pytest.fixture(scope="session")
def url_banco_teste():
    """Banco descartavel no servidor de DATABASE_URL, criado e removido aqui."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    base = os.environ["DATABASE_URL"]
    nome = f"anp_pytest_{uuid4().hex[:8]}"
    try:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(nome)))
    except psycopg.Error as erro:
        pytest.fail(
            f"Nao foi possivel criar o banco de teste {nome} no servidor de "
            f"DATABASE_URL (o usuario precisa de CREATEDB): {erro}"
        )
    url = make_conninfo(base, dbname=nome)
    # Como no DAG, o DDL e aplicado uma vez, antes de qualquer carga.
    with psycopg.connect(url, autocommit=True) as con:
        carregar_postgres.aplicar_ddl(con)
    yield url
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(nome)))


@pytest.fixture
def cfg_pg(cfg, url_banco_teste, tmp_path, monkeypatch):
    """Config com backend postgres apontando para o banco de teste, ja vazio."""
    monkeypatch.setenv("DATABASE_URL", url_banco_teste)
    monkeypatch.setitem(cfg.dados["destino"], "backend", "postgres")
    monkeypatch.setitem(cfg.dados["caminhos"], "staging", str(tmp_path / "staging"))
    monkeypatch.setitem(cfg.dados["caminhos"], "mart_db", str(tmp_path / "mart.duckdb"))
    manifesto = tmp_path / "raw" / "_manifesto.jsonl"
    monkeypatch.setitem(cfg.dados["caminhos"], "manifesto_raw", str(manifesto))
    manifesto.parent.mkdir(parents=True)
    manifesto.write_text(
        json.dumps({
            "caminho": str(tmp_path / "raw" / "semanal-estados_2026-09-20.xlsx"),
            "sha256": SHA, "bytes_totais": 1, "url": URL_FONTE,
            "coletado_em": "2026-09-20T12:00:00+00:00", "reutilizado": False,
        }) + "\n",
        encoding="utf-8",
    )

    con = conectar(cfg)
    con.execute(
        "TRUNCATE mart.fato_precos_semanais, staging.precos_semanais, controle.cargas"
    )
    con.close()
    return cfg


@pytest.fixture
def con_pg(cfg_pg):
    con = conectar(cfg_pg)
    yield con
    con.close()


@pytest.fixture
def df_valido(cfg_pg, planilha_valida):
    return transformar(cfg_pg, planilha_valida, SHA)


def _contar(con, tabela: str = "mart.fato_precos_semanais") -> int:
    return con.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]


def _precos_do_mart(con) -> list[tuple]:
    return con.execute(
        "SELECT data_inicio, uf, produto, preco_medio FROM mart.fato_precos_semanais "
        "ORDER BY 1, 2, 3"
    ).fetchall()


def _cargas(con) -> list[dict]:
    cursor = con.execute(
        "SELECT id_carga, sha256, arquivo, url, status, linhas_lidas, "
        "linhas_carregadas, inicio, fim, erro FROM controle.cargas ORDER BY inicio"
    )
    nomes = [c.name for c in cursor.description]
    return [dict(zip(nomes, linha)) for linha in cursor.fetchall()]


@pytest.mark.postgres
def test_ddl_e_idempotente_e_mart_espelha_o_duckdb(con_pg):
    aplicados = carregar_postgres.inicializar_banco()
    assert carregar_postgres.inicializar_banco() == aplicados
    assert aplicados == [
        "001_schemas.sql", "002_mart_fato_precos_semanais.sql",
        "003_controle_cargas.sql", "004_staging_precos_semanais.sql",
        "005_colunas_escopo_produtos.sql", "006_mart_dim_produto.sql",
    ]

    colunas = con_pg.execute(
        """
        SELECT column_name, data_type FROM information_schema.columns
        WHERE table_schema = 'mart' AND table_name = 'fato_precos_semanais'
        ORDER BY ordinal_position
        """
    ).fetchall()
    assert [c for c, _ in colunas] == [
        "data_inicio", "data_fim", "uf", "regiao", "produto", "preco_medio",
        "preco_min", "preco_max", "desvio_padrao", "numero_postos", "data_ingestao",
        "coef_variacao_revenda", "margem_media_revenda", "preco_medio_distribuicao",
        "preco_min_distribuicao", "preco_max_distribuicao",
        "desvio_padrao_distribuicao", "coef_variacao_distribuicao",
    ]
    tipos = dict(colunas)
    assert tipos["preco_medio"] == "numeric"
    assert tipos["data_inicio"] == "date"
    assert tipos["numero_postos"] == "integer"
    for coluna in COLUNAS_OPCIONAIS:
        assert tipos[coluna] == "numeric", coluna

    chave = {
        linha[0] for linha in con_pg.execute(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = 'mart.fato_precos_semanais'::regclass AND i.indisprimary
            """
        ).fetchall()
    }
    assert chave == {"data_inicio", "uf", "produto"}


@pytest.mark.postgres
def test_carga_dupla_nao_muda_contagem(cfg_pg, con_pg, df_valido):
    carregar(cfg_pg, df_valido, conexao=con_pg)
    apos_primeira = _contar(con_pg)
    carregar(cfg_pg, df_valido, conexao=con_pg)
    carregar(cfg_pg, df_valido, conexao=con_pg)

    assert apos_primeira == len(df_valido) == 2 * LINHAS_POR_SEMANA
    assert _contar(con_pg) == apos_primeira
    assert [c["status"] for c in _cargas(con_pg)] == ["concluida"] * 3


@pytest.mark.postgres
def test_carga_aprovada_registrada_como_concluida(cfg_pg, con_pg, df_valido):
    carregar(cfg_pg, df_valido, conexao=con_pg)

    [carga] = _cargas(con_pg)
    assert carga["status"] == "concluida"
    assert carga["sha256"] == SHA
    assert carga["arquivo"] == "semanal-estados_2026-09-20.xlsx"
    assert carga["url"] == URL_FONTE
    assert carga["linhas_lidas"] == carga["linhas_carregadas"] == 2 * LINHAS_POR_SEMANA
    assert carga["inicio"] <= carga["fim"]
    assert carga["erro"] is None


@pytest.mark.postgres
def test_staging_nao_toca_no_mart_e_e_reescrita_a_cada_carga(cfg_pg, con_pg, df_valido):
    primeira = carregar_postgres.carregar_staging(cfg_pg, df_valido, con_pg)
    segunda = carregar_postgres.carregar_staging(cfg_pg, df_valido.head(10), con_pg)

    assert _contar(con_pg) == 0
    assert con_pg.execute(
        "SELECT id_carga::text, COUNT(*) FROM staging.precos_semanais GROUP BY 1"
    ).fetchall() == [(segunda, 10)]
    status = {str(c["id_carga"]): c["status"] for c in _cargas(con_pg)}
    assert status == {primeira: "iniciada", segunda: "iniciada"}


@pytest.mark.postgres
def test_precos_persistidos_como_numeric_exato(cfg_pg, con_pg, df_valido):
    revisado = df_valido.copy()
    revisado.loc[(revisado["uf"] == "SE") & (revisado["produto"] == "GASOLINA C"),
                 "preco_medio"] = 6.66
    carregar(cfg_pg, revisado, conexao=con_pg)

    precos = con_pg.execute(
        "SELECT DISTINCT preco_medio FROM mart.fato_precos_semanais "
        "WHERE uf = 'SE' AND produto = 'GASOLINA C'"
    ).fetchall()
    assert precos == [(Decimal("6.66"),)]


@pytest.mark.postgres
def test_carga_reprovada_nao_altera_o_mart(cfg_pg, con_pg, df_valido):
    """O requisito central: falha bloqueante deixa o mart exatamente como estava."""
    carregar(cfg_pg, df_valido, conexao=con_pg)
    mart_antes = _precos_do_mart(con_pg)

    # Uma revisao que mudaria todas as linhas, com um preco fora da faixa.
    reprovado = df_valido.copy()
    reprovado["preco_medio"] = (reprovado["preco_medio"] * 1.05).round(3)
    reprovado.loc[reprovado.index[0], "preco_medio"] = 99.0

    id_carga = carregar_postgres.carregar_staging(cfg_pg, reprovado, con_pg)
    with pytest.raises(ErroQualidade, match="preco_medio"):
        carregar_postgres.validar(cfg_pg, con_pg, id_carga)

    assert _precos_do_mart(con_pg) == mart_antes

    ultima = _cargas(con_pg)[-1]
    assert str(ultima["id_carga"]) == id_carga
    assert ultima["status"] == "falhou"
    assert ultima["erro"].startswith("Reprovada nos testes de qualidade")
    assert "preco_medio" in ultima["erro"]
    assert ultima["linhas_carregadas"] is None

    # A staging preserva o que foi reprovado, para inspecao.
    assert con_pg.execute(
        "SELECT COUNT(*) FROM staging.precos_semanais "
        "WHERE id_carga = %s AND preco_medio = 99.0",
        [id_carga],
    ).fetchone()[0] == 1
    assert _contar(con_pg, "staging.precos_semanais") == len(reprovado)

    # E uma carga reprovada nao pode ser promovida depois.
    with pytest.raises(ErroPipeline, match="falhou"):
        carregar_postgres.promover(cfg_pg, con_pg, id_carga, [])
    assert _precos_do_mart(con_pg) == mart_antes


@pytest.mark.postgres
def test_nulo_e_duplicata_sao_reprovados_pela_qualidade(cfg_pg, con_pg, df_valido):
    """A staging e permissiva de proposito: quem barra o dado ruim e a qualidade."""
    import pandas as pd

    ruim = pd.concat([df_valido, df_valido.head(1)], ignore_index=True)  # chave repetida
    ruim.loc[ruim.index[5], "preco_medio"] = None                        # coluna NOT NULL no mart

    with pytest.raises(ErroQualidade) as excinfo:
        carregar(cfg_pg, ruim, conexao=con_pg)

    mensagem = str(excinfo.value)
    assert "chave_primaria_sem_duplicata" in mensagem
    assert "preco_medio_na_faixa" in mensagem
    assert _contar(con_pg) == 0
    assert _cargas(con_pg)[-1]["status"] == "falhou"


@pytest.mark.postgres
def test_falha_na_promocao_desfaz_o_upsert(cfg_pg, con_pg, df_valido, monkeypatch):
    """Upsert e status final sao uma transacao so: um nao confirma sem o outro."""
    carregar(cfg_pg, df_valido, conexao=con_pg)
    mart_antes = _precos_do_mart(con_pg)

    revisado = df_valido.copy()
    revisado["preco_medio"] = (revisado["preco_medio"] * 1.05).round(3)

    def explodir(*_):
        raise RuntimeError("falha simulada ao atualizar controle.cargas")

    monkeypatch.setattr(carregar_postgres, "_marcar_concluida", explodir)
    with pytest.raises(RuntimeError, match="falha simulada"):
        carregar(cfg_pg, revisado, conexao=con_pg)

    assert _precos_do_mart(con_pg) == mart_antes
    ultima = _cargas(con_pg)[-1]
    assert ultima["status"] == "falhou"
    assert "falha simulada" in ultima["erro"]


@pytest.mark.postgres
def test_staging_sobrescrita_por_outra_carga_nao_e_promovida(cfg_pg, con_pg, df_valido):
    """Entre tasks, outra carga pode reescrever a staging: promover percebe."""
    antiga = carregar_postgres.carregar_staging(cfg_pg, df_valido, con_pg)
    resultados = carregar_postgres.validar(cfg_pg, con_pg, antiga)
    carregar_postgres.carregar_staging(cfg_pg, df_valido.head(10), con_pg)

    with pytest.raises(ErroPipeline, match="reescreveu a staging"):
        carregar_postgres.promover(cfg_pg, con_pg, antiga, resultados)
    assert _contar(con_pg) == 0
    assert {str(c["id_carga"]): c["status"] for c in _cargas(con_pg)}[antiga] == "falhou"


@pytest.mark.postgres
def test_pipeline_com_alerta_promove_como_concluida_com_avisos(cfg_pg, con_pg, tmp_path):
    linhas = (
        semana_completa(date(2026, 9, 6), preco_gasolina=6.0)
        + semana_completa(date(2026, 9, 13), preco_gasolina=7.2)  # +20%: ALERTA
    )
    planilha = escrever_planilha(tmp_path / "anp_alerta.xlsx", linhas)

    resultados = executar(cfg_pg, usar_raw_existente=planilha)

    assert {r.nome: r.status for r in resultados}["variacao_semanal"] == ALERTA
    assert _contar(con_pg) == 2 * LINHAS_POR_SEMANA
    [carga] = _cargas(con_pg)
    assert carga["status"] == "concluida_com_avisos"
    assert carga["linhas_carregadas"] == 2 * LINHAS_POR_SEMANA
    assert carga["sha256"] == "raw-local-reaproveitado"
    assert carga["arquivo"] is None  # --usar-raw nao passa pelo manifesto


@pytest.mark.postgres
def test_pipeline_reprovado_sai_com_codigo_1_e_mart_intacto(cfg_pg, con_pg, tmp_path,
                                                            monkeypatch):
    import anp.pipeline as modulo

    valida = escrever_planilha(
        tmp_path / "ok.xlsx",
        semana_completa(date(2026, 9, 6)) + semana_completa(date(2026, 9, 13)),
    )
    executar(cfg_pg, usar_raw_existente=valida)
    mart_antes = _precos_do_mart(con_pg)

    sem_sergipe = [
        linha for linha in semana_completa(date(2026, 9, 6), preco_gasolina=6.5)
        + semana_completa(date(2026, 9, 13), preco_gasolina=6.5)
        if not (linha[3] == "SERGIPE" and linha[0] == date(2026, 9, 13))
    ]
    reprovada = escrever_planilha(tmp_path / "sem_se.xlsx", sem_sergipe)

    monkeypatch.setattr(modulo, "carregar_config", lambda _caminho=None: cfg_pg)
    codigo = modulo.main(["--usar-raw", str(reprovada)])

    assert codigo == 1
    assert _precos_do_mart(con_pg) == mart_antes
    assert [c["status"] for c in _cargas(con_pg)] == ["concluida", "falhou"]
    assert "cobertura_de_ufs" in _cargas(con_pg)[-1]["erro"]


# --- escopo ampliado: migracao do esquema e dim_produto ---------------------

@pytest.fixture
def url_banco_esquema_antigo():
    """Banco descartavel so com o DDL de antes do escopo ampliado (001 a 004)."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    base = os.environ["DATABASE_URL"]
    nome = f"anp_pytest_antigo_{uuid4().hex[:8]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(nome)))
    url = make_conninfo(base, dbname=nome)
    with psycopg.connect(url, autocommit=True) as con:
        for arquivo in sorted(carregar_postgres.DIR_SQL.glob("00[1-4]_*.sql")):
            con.execute(arquivo.read_text(encoding="utf-8"))
    yield url
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(nome)))


@pytest.mark.postgres
def test_ddl_novo_sobre_banco_antigo_preserva_linhas_e_carga_preenche(
    cfg, url_banco_esquema_antigo, tmp_path, monkeypatch,
):
    """O 005 e o 006 rodam sobre um banco com dados: nada e recriado nem perdido."""
    import psycopg

    monkeypatch.setenv("DATABASE_URL", url_banco_esquema_antigo)
    monkeypatch.setitem(cfg.dados["destino"], "backend", "postgres")
    monkeypatch.setitem(cfg.dados["caminhos"], "staging", str(tmp_path / "staging"))
    monkeypatch.setitem(cfg.dados["caminhos"], "manifesto_raw",
                        str(tmp_path / "raw" / "_manifesto.jsonl"))

    with psycopg.connect(url_banco_esquema_antigo, autocommit=True) as con:
        con.execute(
            "INSERT INTO mart.fato_precos_semanais VALUES ('2020-08-16', '2020-08-22', "
            "'SE', 'NORDESTE', 'GASOLINA C', 4.5, 4.3, 4.7, 0.1, 30, "
            "'2026-09-19 12:00:00')"
        )
        colunas_antes = con.execute(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = 'mart' AND table_name = 'fato_precos_semanais'"
        ).fetchone()[0]
    assert colunas_antes == 11

    carregar_postgres.inicializar_banco()
    carregar_postgres.inicializar_banco()  # idempotente sobre o banco migrado

    with psycopg.connect(url_banco_esquema_antigo, autocommit=True) as con:
        linhas = con.execute(
            "SELECT uf, produto, preco_medio, preco_medio_distribuicao, "
            "margem_media_revenda FROM mart.fato_precos_semanais"
        ).fetchall()
        assert linhas == [("SE", "GASOLINA C", Decimal("4.5"), None, None)]
        assert con.execute("SELECT COUNT(*) FROM mart.dim_produto").fetchone()[0] == 0

    planilha = escrever_planilha(
        tmp_path / "com_distribuicao.xlsx",
        semana_completa(date(2020, 8, 16), preco_gasolina=4.5, distribuicao=True),
    )
    con = conectar(cfg)
    try:
        carregar(cfg, transformar(cfg, planilha, SHA), conexao=con)
        linha = con.execute(
            "SELECT preco_medio, preco_medio_distribuicao, margem_media_revenda, "
            "coef_variacao_revenda FROM mart.fato_precos_semanais "
            "WHERE uf = 'SE' AND produto = 'GASOLINA C'"
        ).fetchone()
        assert linha == (Decimal("4.5"), Decimal("3.6"), Decimal("0.9"), Decimal("0.01"))
        assert _contar(con) == LINHAS_POR_SEMANA
        assert con.execute("SELECT COUNT(*) FROM mart.dim_produto").fetchone()[0] == 7
        assert [c["status"] for c in _cargas(con)] == ["concluida"]
    finally:
        con.close()


@pytest.mark.postgres
def test_dim_produto_sincronizada_com_o_config(cfg_pg, con_pg, df_valido):
    con_pg.execute("TRUNCATE mart.dim_produto")
    carregar(cfg_pg, df_valido, conexao=con_pg)
    carregar(cfg_pg, df_valido, conexao=con_pg)

    dim = con_pg.execute(
        "SELECT produto, rotulo_anp, unidade, categoria FROM mart.dim_produto ORDER BY 1"
    ).fetchall()
    assert dim == sorted(
        (p.produto, p.rotulo_anp, p.unidade, p.categoria)
        for p in cfg_pg.produtos.values()
    )
    orfaos = con_pg.execute(
        "SELECT COUNT(*) FROM mart.fato_precos_semanais f "
        "LEFT JOIN mart.dim_produto d USING (produto) WHERE d.produto IS NULL"
    ).fetchone()[0]
    assert orfaos == 0
    assert con_pg.execute(
        "SELECT produto, COUNT(*) FROM mart.fato_precos_semanais GROUP BY 1 ORDER BY 1"
    ).fetchall() == [(p, 2 * 27) for p in sorted(cfg_pg.produtos)]


@pytest.mark.postgres
def test_carga_reprovada_nao_sincroniza_dim_produto(cfg_pg, con_pg, df_valido):
    """dim_produto so muda em promover, junto com o mart."""
    con_pg.execute("TRUNCATE mart.dim_produto")
    reprovado = df_valido.copy()
    reprovado.loc[reprovado.index[0], "preco_medio"] = 99.0
    with pytest.raises(ErroQualidade):
        carregar(cfg_pg, reprovado, conexao=con_pg)
    assert con_pg.execute("SELECT COUNT(*) FROM mart.dim_produto").fetchone()[0] == 0
