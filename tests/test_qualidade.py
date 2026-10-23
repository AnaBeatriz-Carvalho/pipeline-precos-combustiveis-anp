"""Testes das regras de qualidade que rodam ao fim da carga."""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
import pytest

from anp.erros import ErroQualidade
from anp.qualidade import (
    ALERTA,
    FALHOU,
    PASSOU,
    executar_testes,
    testar_chave_primaria,
    testar_colunas_opcionais,
    testar_cobertura_de_ufs,
    testar_faixa_de_preco,
    testar_variacao_semanal,
)
from conftest import inserir, semana_no_mart

SEMANA = date(2026, 9, 13)
ANTERIOR = date(2026, 9, 6)


# --- chave primaria ---------------------------------------------------------

def test_chave_primaria_sem_duplicata_passa(cfg, mart):
    semana_no_mart(mart, SEMANA)
    assert testar_chave_primaria(mart, cfg).status == PASSOU


def test_constraint_do_banco_rejeita_chave_repetida(cfg, mart):
    """A PK e a primeira linha de defesa: a segunda insercao nem entra."""
    linha = {"data_inicio": SEMANA, "uf": "SE", "produto": "GASOLINA C",
             "preco_medio": 7.0}
    inserir(mart, [linha])
    with pytest.raises(duckdb.ConstraintException):
        inserir(mart, [linha])


def test_duplicata_e_detectada_quando_a_constraint_nao_existe(cfg):
    """Se a tabela for recriada sem PK, o teste ainda pega a duplicata."""
    from anp.carregar import criar_tabela

    con = duckdb.connect(":memory:")
    criar_tabela(con)
    # CREATE TABLE AS copia as colunas, mas nao a chave primaria.
    con.execute("CREATE TABLE sem_pk AS SELECT * FROM fato_precos_semanais LIMIT 0")
    con.execute("DROP TABLE fato_precos_semanais")
    con.execute("ALTER TABLE sem_pk RENAME TO fato_precos_semanais")
    linha = {"data_inicio": SEMANA, "uf": "SE", "produto": "GASOLINA C",
             "preco_medio": 7.0}
    inserir(con, [linha])
    inserir(con, [linha])

    resultado = testar_chave_primaria(con, cfg)
    assert resultado.status == FALHOU
    assert "SE" in resultado.mensagem
    assert resultado.detalhes["duplicatas"][0]["ocorrencias"] == 2
    con.close()


# --- faixa de preco ---------------------------------------------------------

def test_preco_dentro_da_faixa_passa(cfg, mart):
    semana_no_mart(mart, SEMANA)
    assert testar_faixa_de_preco(mart, cfg).status == PASSOU


@pytest.mark.parametrize("preco", [0.99, 0.0, 15.01, 150.0])
def test_preco_fora_da_faixa_falha(cfg, mart, preco):
    inserir(mart, [{"data_inicio": SEMANA, "uf": "SE", "produto": "GASOLINA C",
                    "preco_medio": preco}])
    resultado = testar_faixa_de_preco(mart, cfg)
    assert resultado.status == FALHOU
    assert resultado.detalhes["linhas_fora"] == 1


@pytest.mark.parametrize("preco", [1.0, 15.0, 7.5])
def test_limites_da_faixa_sao_inclusivos(cfg, mart, preco):
    inserir(mart, [{"data_inicio": SEMANA, "uf": "SE", "produto": "GASOLINA C",
                    "preco_medio": preco}])
    assert testar_faixa_de_preco(mart, cfg).status == PASSOU


def test_glp_em_reais_por_13kg_passa_na_faixa_dele(cfg, mart):
    """R$ 110 o botijao e normal para GLP; seria absurdo para gasolina."""
    inserir(mart, [{"data_inicio": SEMANA, "uf": "SE", "produto": "GLP",
                    "preco_medio": 110.0}])
    assert testar_faixa_de_preco(mart, cfg).status == PASSOU
    assert testar_colunas_opcionais(mart, cfg).status == PASSOU


@pytest.mark.parametrize("produto, preco", [
    ("GLP", 19.99),          # abaixo de R$ 20: preco por kg, nao por botijao
    ("GLP", 250.01),
    ("GASOLINA C", 110.0),   # o preco do GLP na gasolina
    ("GNV", 0.47),           # erro de escala /10
])
def test_faixa_de_preco_e_por_produto(cfg, mart, produto, preco):
    inserir(mart, [{"data_inicio": SEMANA, "uf": "SE", "produto": produto,
                    "preco_medio": preco}])
    resultado = testar_faixa_de_preco(mart, cfg)
    assert resultado.status == FALHOU
    assert resultado.detalhes["por_produto"] == {produto: 1}


def test_produto_sem_bloco_no_config_reprova(cfg, mart):
    inserir(mart, [{"data_inicio": SEMANA, "uf": "SE", "produto": "QUEROSENE",
                    "preco_medio": 5.0}])
    resultado = testar_faixa_de_preco(mart, cfg)
    assert resultado.status == FALHOU
    assert "QUEROSENE" in resultado.mensagem
    assert "[[escopo.produtos]]" in resultado.mensagem


# --- colunas opcionais: margem, coeficientes e distribuicao -----------------

def test_distribuicao_nula_passa(cfg, mart):
    """Depois de agosto de 2020 a ANP manda "-": nulo e o normal."""
    semana_no_mart(mart, SEMANA)
    nulos = mart.execute(
        "SELECT COUNT(*) FROM fato_precos_semanais WHERE preco_medio_distribuicao IS NULL"
    ).fetchone()[0]
    assert nulos > 0
    assert testar_colunas_opcionais(mart, cfg).status == PASSOU


def test_distribuicao_preenchida_dentro_da_faixa_passa(cfg, mart):
    inserir(mart, [
        {"data_inicio": SEMANA, "uf": "SE", "produto": "GASOLINA C", "preco_medio": 4.5,
         "preco_medio_distribuicao": 3.6, "preco_min_distribuicao": 3.4,
         "preco_max_distribuicao": 3.8, "desvio_padrao_distribuicao": 0.07,
         "coef_variacao_distribuicao": 0.02, "margem_media_revenda": 0.9,
         "coef_variacao_revenda": 0.03},
        {"data_inicio": SEMANA, "uf": "SE", "produto": "GLP", "preco_medio": 70.0,
         "preco_medio_distribuicao": 44.0, "preco_min_distribuicao": 14.0,
         "margem_media_revenda": 26.0, "desvio_padrao": 13.0},
    ])
    assert testar_colunas_opcionais(mart, cfg).status == PASSOU


@pytest.mark.parametrize("coluna, valor", [
    ("preco_medio_distribuicao", 20.0),
    ("preco_min_distribuicao", 0.3),
    ("preco_max_distribuicao", 15.5),
    ("desvio_padrao_distribuicao", 6.0),
    ("coef_variacao_distribuicao", 1.2),
    ("margem_media_revenda", -0.1),
    ("coef_variacao_revenda", -0.01),
    ("desvio_padrao", 5.5),
    ("preco_max", 16.0),
])
def test_valor_presente_fora_da_faixa_reprova(cfg, mart, coluna, valor):
    inserir(mart, [{"data_inicio": SEMANA, "uf": "SE", "produto": "GASOLINA C",
                    "preco_medio": 6.0, coluna: valor}])
    resultado = testar_colunas_opcionais(mart, cfg)
    assert resultado.status == FALHOU
    assert resultado.detalhes["por_coluna"] == {coluna: 1}
    assert resultado.detalhes["amostra"][0]["valor"] == pytest.approx(valor)


def test_faixa_de_distribuicao_tambem_e_por_produto(cfg, mart):
    """R$ 44 de distribuicao passa no GLP e reprova na gasolina."""
    inserir(mart, [
        {"data_inicio": SEMANA, "uf": "SE", "produto": "GLP", "preco_medio": 70.0,
         "preco_medio_distribuicao": 44.0},
        {"data_inicio": SEMANA, "uf": "SE", "produto": "GASOLINA C", "preco_medio": 6.0,
         "preco_medio_distribuicao": 44.0},
    ])
    resultado = testar_colunas_opcionais(mart, cfg)
    assert resultado.status == FALHOU
    assert [a["produto"] for a in resultado.detalhes["amostra"]] == ["GASOLINA C"]


# --- cobertura de UFs, por produto ------------------------------------------

def test_cobertura_completa_passa(cfg, mart):
    semana_no_mart(mart, SEMANA)
    resultado = testar_cobertura_de_ufs(mart, cfg)
    assert resultado.status == PASSOU
    assert resultado.detalhes["por_produto"]["GASOLINA C"]["ufs_presentes"] == 27
    assert set(resultado.detalhes["por_produto"]) == set(cfg.produtos)


GNV_TOLERADAS = ["AC", "AP", "DF", "GO", "MA", "MS", "PA", "PI", "RO", "RR", "TO"]


def test_gnv_sem_as_ufs_sem_pesquisa_passa(cfg, mart):
    """A ANP nao pesquisa GNV em dez UFs, e MS falta com frequencia."""
    todas = sorted(cfg.ufs.values())
    semana_no_mart(mart, SEMANA, ufs={"GNV": [u for u in todas if u not in GNV_TOLERADAS]})
    resultado = testar_cobertura_de_ufs(mart, cfg)
    assert resultado.status == PASSOU
    assert resultado.detalhes["por_produto"]["GNV"]["ufs_exigidas"] == 16
    assert resultado.detalhes["por_produto"]["GNV"]["ausentes_toleradas"] == GNV_TOLERADAS


@pytest.mark.parametrize("produto, uf", [
    ("GNV", "SP"),
    ("GNV", "AM"),
    ("GLP", "AP"),
    ("OLEO DIESEL S10", "AP"),
    ("OLEO DIESEL", "SE"),
])
def test_uf_ausente_nao_tolerada_reprova(cfg, mart, produto, uf):
    todas = sorted(cfg.ufs.values())
    semana_no_mart(mart, SEMANA, ufs={produto: [u for u in todas if u != uf]})
    resultado = testar_cobertura_de_ufs(mart, cfg)
    assert resultado.status == FALHOU
    assert resultado.detalhes["por_produto"][produto]["ausentes_nao_toleradas"] == [uf]
    assert produto in resultado.mensagem


@pytest.mark.parametrize("produto", ["GASOLINA ADITIVADA", "OLEO DIESEL"])
def test_amapa_tolerado_nos_produtos_com_ausencia_frequente(cfg, mart, produto):
    todas = sorted(cfg.ufs.values())
    semana_no_mart(mart, SEMANA, ufs={produto: [u for u in todas if u != "AP"]})
    assert testar_cobertura_de_ufs(mart, cfg).status == PASSOU


def test_produto_sem_serie_ate_a_semana_nao_e_cobrado(cfg, mart):
    """A GASOLINA ADITIVADA so comeca em outubro de 2020."""
    outros = [p for p in cfg.produtos if p != "GASOLINA ADITIVADA"]
    semana_no_mart(mart, date(2019, 1, 6), produtos=outros)
    semana_no_mart(mart, SEMANA)
    resultado = testar_cobertura_de_ufs(mart, cfg, semana=date(2019, 1, 6))
    assert resultado.status == PASSOU
    assert resultado.detalhes["sem_serie"] == ["GASOLINA ADITIVADA"]


def test_produto_com_historico_e_sem_a_semana_reprova(cfg, mart):
    """Se a semana chegou sem um produto inteiro, a extracao perdeu linhas."""
    semana_no_mart(mart, ANTERIOR)
    semana_no_mart(mart, SEMANA, produtos=[p for p in cfg.produtos if p != "GLP"])
    resultado = testar_cobertura_de_ufs(mart, cfg)
    assert resultado.status == FALHOU
    assert resultado.detalhes["por_produto"]["GLP"]["ufs_presentes"] == 0


def test_etanol_sem_amapa_passa_por_estar_na_allowlist(cfg, mart):
    """A ANP nao pesquisa etanol no AP em boa parte das semanas."""
    todas = sorted(cfg.ufs.values())
    semana_no_mart(mart, SEMANA, ufs_etanol=[u for u in todas if u != "AP"])
    resultado = testar_cobertura_de_ufs(mart, cfg)
    assert resultado.status == PASSOU
    assert resultado.detalhes["por_produto"]["ETANOL HIDRATADO"]["ausentes_toleradas"] == ["AP"]


def test_etanol_sem_uf_fora_da_allowlist_falha(cfg, mart):
    todas = sorted(cfg.ufs.values())
    semana_no_mart(mart, SEMANA, ufs_etanol=[u for u in todas if u != "RR"])
    resultado = testar_cobertura_de_ufs(mart, cfg)
    assert resultado.status == FALHOU
    assert "RR" in resultado.mensagem
    assert resultado.detalhes["por_produto"]["ETANOL HIDRATADO"]["ausentes_nao_toleradas"] == ["RR"]


def test_gasolina_sem_amapa_falha_apesar_da_allowlist_do_etanol(cfg, mart):
    """A allowlist e por produto: AP so e tolerado no etanol."""
    todas = sorted(cfg.ufs.values())
    semana_no_mart(mart, SEMANA, ufs_gasolina=[u for u in todas if u != "AP"])
    resultado = testar_cobertura_de_ufs(mart, cfg)
    assert resultado.status == FALHOU
    assert "GASOLINA C" in resultado.mensagem
    assert "AP" in resultado.mensagem


def test_cobertura_avalia_a_semana_mais_recente(cfg, mart):
    """Semana antiga incompleta nao contamina o teste da semana carregada."""
    todas = sorted(cfg.ufs.values())
    semana_no_mart(mart, ANTERIOR, ufs_gasolina=["SE", "BA"], ufs_etanol=["SE"])
    semana_no_mart(mart, SEMANA)
    assert testar_cobertura_de_ufs(mart, cfg).status == PASSOU
    assert testar_cobertura_de_ufs(mart, cfg, semana=ANTERIOR).status == FALHOU


def test_mart_vazio_falha(cfg, mart):
    assert testar_cobertura_de_ufs(mart, cfg).status == FALHOU


# --- variacao semanal -------------------------------------------------------

def test_variacao_pequena_passa(cfg, mart):
    semana_no_mart(mart, ANTERIOR, precos={"GASOLINA C": 6.0, "ETANOL HIDRATADO": 4.0})
    semana_no_mart(mart, SEMANA, precos={"GASOLINA C": 6.3, "ETANOL HIDRATADO": 4.1})
    assert testar_variacao_semanal(mart, cfg).status == PASSOU


def test_variacao_acima_do_limiar_gera_alerta_e_nao_erro(cfg, mart):
    semana_no_mart(mart, ANTERIOR, precos={"GASOLINA C": 6.0, "ETANOL HIDRATADO": 4.0})
    semana_no_mart(mart, SEMANA, precos={"GASOLINA C": 7.2, "ETANOL HIDRATADO": 4.0})
    resultado = testar_variacao_semanal(mart, cfg)
    assert resultado.status == ALERTA
    assert not resultado.bloqueante
    assert len(resultado.detalhes["ocorrencias"]) == 27
    assert resultado.detalhes["ocorrencias"][0]["variacao_pct"] == pytest.approx(20.0)


def test_variacao_logo_abaixo_do_limiar_nao_alerta(cfg, mart):
    """O alerta e para variacao acima de 15%.

    Nao se testa o limiar exato: 0,15 nao tem representacao binaria exata, e
    uma razao "de 15%" cai de um lado ou do outro conforme os operandos.
    """
    semana_no_mart(mart, ANTERIOR, precos={"GASOLINA C": 6.0, "ETANOL HIDRATADO": 4.0})
    semana_no_mart(mart, SEMANA, precos={"GASOLINA C": 6.84, "ETANOL HIDRATADO": 4.0})
    assert testar_variacao_semanal(mart, cfg).status == PASSOU


def test_variacao_logo_acima_do_limiar_alerta(cfg, mart):
    semana_no_mart(mart, ANTERIOR, precos={"GASOLINA C": 6.0, "ETANOL HIDRATADO": 4.0})
    semana_no_mart(mart, SEMANA, precos={"GASOLINA C": 6.96, "ETANOL HIDRATADO": 4.0})
    assert testar_variacao_semanal(mart, cfg).status == ALERTA


def test_queda_forte_tambem_alerta(cfg, mart):
    semana_no_mart(mart, ANTERIOR, precos={"GASOLINA C": 6.0, "ETANOL HIDRATADO": 4.0})
    semana_no_mart(mart, SEMANA, precos={"GASOLINA C": 4.8, "ETANOL HIDRATADO": 4.0})
    resultado = testar_variacao_semanal(mart, cfg)
    assert resultado.status == ALERTA
    assert resultado.detalhes["ocorrencias"][0]["variacao_pct"] == pytest.approx(-20.0)


def test_lag_usa_a_semana_anterior_existente_e_nao_menos_sete_dias(cfg, mart):
    """A serie da ANP tem buracos; comparar com data-7 nao acharia nada.

    Reproduz a suspensao de 2020: 16/08 e depois so 18/10, 63 dias adiante.
    """
    semana_no_mart(mart, date(2020, 8, 16), precos={"GASOLINA C": 4.0,
                                                    "ETANOL HIDRATADO": 3.0})
    semana_no_mart(mart, date(2020, 10, 18), precos={"GASOLINA C": 5.0,
                                                     "ETANOL HIDRATADO": 3.0})
    resultado = testar_variacao_semanal(mart, cfg, semana=date(2020, 10, 18))
    assert resultado.status == ALERTA
    ocorrencia = resultado.detalhes["ocorrencias"][0]
    assert ocorrencia["semana_anterior"] == "2020-08-16"
    assert ocorrencia["dias_desde_semana_anterior"] == 63
    assert ocorrencia["variacao_pct"] == pytest.approx(25.0)


def test_primeira_semana_da_serie_nao_gera_alerta(cfg, mart):
    """Sem semana anterior nao ha variacao a calcular."""
    semana_no_mart(mart, SEMANA)
    assert testar_variacao_semanal(mart, cfg).status == PASSOU


def test_limiar_de_variacao_e_por_produto(cfg, mart):
    """+20% alerta na gasolina (limiar 15%) e nao no GNV (limiar 25%)."""
    semana_no_mart(mart, ANTERIOR)
    semana_no_mart(mart, SEMANA, precos={"GASOLINA C": 7.2, "GNV": 4.7 * 1.2})
    resultado = testar_variacao_semanal(mart, cfg)
    assert resultado.status == ALERTA
    assert {o["produto"] for o in resultado.detalhes["ocorrencias"]} == {"GASOLINA C"}
    assert resultado.detalhes["ocorrencias"][0]["limiar_pct"] == pytest.approx(15.0)
    assert resultado.detalhes["limiares"]["GNV"] == pytest.approx(0.25)


def test_gnv_acima_do_limiar_dele_alerta(cfg, mart):
    semana_no_mart(mart, ANTERIOR)
    semana_no_mart(mart, SEMANA, precos={"GNV": 4.7 * 1.3})
    resultado = testar_variacao_semanal(mart, cfg)
    assert resultado.status == ALERTA
    assert {o["produto"] for o in resultado.detalhes["ocorrencias"]} == {"GNV"}


def test_variacao_isola_cada_uf_e_produto(cfg, mart):
    """A janela e por (uf, produto): o salto de uma UF nao respinga nas outras."""
    inserir(mart, [
        {"data_inicio": ANTERIOR, "uf": "SE", "produto": "GASOLINA C", "preco_medio": 6.0},
        {"data_inicio": ANTERIOR, "uf": "BA", "produto": "GASOLINA C", "preco_medio": 6.0},
        {"data_inicio": SEMANA, "uf": "SE", "produto": "GASOLINA C", "preco_medio": 8.0},
        {"data_inicio": SEMANA, "uf": "BA", "produto": "GASOLINA C", "preco_medio": 6.1},
    ])
    resultado = testar_variacao_semanal(mart, cfg)
    assert [o["uf"] for o in resultado.detalhes["ocorrencias"]] == ["SE"]


# --- orquestracao dos testes ------------------------------------------------

def test_executar_testes_passa_em_dados_saudaveis(cfg, mart):
    semana_no_mart(mart, ANTERIOR)
    semana_no_mart(mart, SEMANA)
    resultados = executar_testes(mart, cfg)
    assert len(resultados) == 5
    assert all(r.status == PASSOU for r in resultados)


def test_executar_testes_levanta_erro_com_todas_as_falhas(cfg, mart):
    todas = sorted(cfg.ufs.values())
    semana_no_mart(mart, SEMANA, precos={"GASOLINA C": 99.0, "ETANOL HIDRATADO": 4.0},
                   ufs_etanol=[u for u in todas if u != "RR"])
    with pytest.raises(ErroQualidade) as exc:
        executar_testes(mart, cfg)
    mensagem = str(exc.value)
    assert "preco_medio_na_faixa" in mensagem
    assert "colunas_opcionais_na_faixa" in mensagem  # minimo e maximo acompanham
    assert "cobertura_de_ufs" in mensagem
    assert "3 de 5" in mensagem


def test_alerta_sozinho_nao_reprova_a_carga(cfg, mart):
    semana_no_mart(mart, ANTERIOR, precos={"GASOLINA C": 6.0, "ETANOL HIDRATADO": 4.0})
    semana_no_mart(mart, SEMANA, precos={"GASOLINA C": 7.5, "ETANOL HIDRATADO": 4.0})
    resultados = executar_testes(mart, cfg)
    assert [r.status for r in resultados].count(ALERTA) == 1
