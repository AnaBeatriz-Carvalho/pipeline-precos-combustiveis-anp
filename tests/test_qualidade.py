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
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE fato_precos_semanais (
            data_inicio DATE, data_fim DATE, uf VARCHAR, regiao VARCHAR,
            produto VARCHAR, preco_medio DOUBLE, preco_min DOUBLE,
            preco_max DOUBLE, desvio_padrao DOUBLE, numero_postos INTEGER,
            data_ingestao TIMESTAMP
        )
        """
    )
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


# --- cobertura de UFs, por produto ------------------------------------------

def test_cobertura_completa_passa(cfg, mart):
    semana_no_mart(mart, SEMANA)
    resultado = testar_cobertura_de_ufs(mart, cfg)
    assert resultado.status == PASSOU
    assert resultado.detalhes["por_produto"]["GASOLINA C"]["ufs_presentes"] == 27


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
    assert len(resultados) == 4
    assert all(r.status == PASSOU for r in resultados)


def test_executar_testes_levanta_erro_com_todas_as_falhas(cfg, mart):
    todas = sorted(cfg.ufs.values())
    semana_no_mart(mart, SEMANA, precos={"GASOLINA C": 99.0, "ETANOL HIDRATADO": 4.0},
                   ufs_etanol=[u for u in todas if u != "RR"])
    with pytest.raises(ErroQualidade) as exc:
        executar_testes(mart, cfg)
    mensagem = str(exc.value)
    assert "preco_medio_na_faixa" in mensagem
    assert "cobertura_de_ufs" in mensagem
    assert "2 de 4" in mensagem


def test_alerta_sozinho_nao_reprova_a_carga(cfg, mart):
    semana_no_mart(mart, ANTERIOR, precos={"GASOLINA C": 6.0, "ETANOL HIDRATADO": 4.0})
    semana_no_mart(mart, SEMANA, precos={"GASOLINA C": 7.5, "ETANOL HIDRATADO": 4.0})
    resultados = executar_testes(mart, cfg)
    assert [r.status for r in resultados].count(ALERTA) == 1
