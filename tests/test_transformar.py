"""Testes da transformacao: normalizacao e deteccao de mudanca de esquema."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from anp.erros import ErroEsquema
from anp.transformar import (
    COLUNAS_STAGING,
    transformar,
    validar_cabecalho,
    gravar_staging,
)
from conftest import (
    COLUNAS_ANP,
    LINHAS_POR_SEMANA,
    escrever_planilha,
    linha_anp,
    semana_completa,
)

SHA = "a" * 64


def eventos(caplog) -> set[str]:
    """Os campos 'evento' dos logs capturados.

    caplog.text mostra so a mensagem; os campos estruturados ficam no record.
    """
    return {getattr(r, "evento", None) for r in caplog.records}


# --- validacao de cabecalho -------------------------------------------------

def test_cabecalho_valido_e_aceito(cfg, planilha_valida):
    colunas = validar_cabecalho(planilha_valida, cfg)
    assert set(colunas) == set(COLUNAS_ANP)


def test_cabecalho_deslocado_aponta_a_linha_correta(cfg, tmp_path):
    """Se a ANP mudar o numero de notas de rodape, o erro diz a nova linha."""
    arq = escrever_planilha(
        tmp_path / "deslocada.xlsx",
        semana_completa(date(2026, 9, 13)),
        linhas_preambulo=19,  # cabecalho vai para a linha 20, nao 18
    )
    with pytest.raises(ErroEsquema) as exc:
        validar_cabecalho(arq, cfg)
    mensagem = str(exc.value)
    assert "mudou de posicao" in mensagem
    assert "linha 20" in mensagem
    assert "extracao.linha_cabecalho" in mensagem


def test_coluna_renomeada_e_reportada_pelo_nome(cfg, tmp_path):
    cabecalho = list(COLUNAS_ANP)
    cabecalho[7] = "PRECO MEDIO REVENDA (R$)"  # ANP tira os acentos
    arq = escrever_planilha(
        tmp_path / "renomeada.xlsx", semana_completa(date(2026, 9, 13)), cabecalho
    )
    with pytest.raises(ErroEsquema) as exc:
        validar_cabecalho(arq, cfg)
    mensagem = str(exc.value)
    assert "PREÇO MÉDIO REVENDA" in mensagem       # a que sumiu
    assert "PRECO MEDIO REVENDA (R$)" in mensagem  # a que apareceu


def test_coluna_removida_e_detectada(cfg, tmp_path):
    cabecalho = [c for c in COLUNAS_ANP if c != "NÚMERO DE POSTOS PESQUISADOS"]
    linhas = [
        [v for i, v in enumerate(linha) if i != 5]
        for linha in semana_completa(date(2026, 9, 13))
    ]
    arq = escrever_planilha(tmp_path / "removida.xlsx", linhas, cabecalho)
    with pytest.raises(ErroEsquema, match="NÚMERO DE POSTOS PESQUISADOS"):
        validar_cabecalho(arq, cfg)


def test_arquivo_curto_demais_falha_com_mensagem_util(cfg, tmp_path):
    arq = escrever_planilha(tmp_path / "curta.xlsx", [], linhas_preambulo=3)
    with pytest.raises(ErroEsquema) as exc:
        validar_cabecalho(arq, cfg)
    assert "serie semanal por estado" in str(exc.value)


# --- normalizacao -----------------------------------------------------------

def test_colunas_e_tipos_do_staging(cfg, planilha_valida):
    df = transformar(cfg, planilha_valida, SHA)
    assert list(df.columns) == list(COLUNAS_STAGING)
    assert pd.api.types.is_datetime64_any_dtype(df["data_inicio"])
    assert pd.api.types.is_float_dtype(df["preco_medio"])
    assert str(df["numero_postos"].dtype) == "Int64"
    assert str(df["uf"].dtype) == "string"
    for coluna in ("margem_media_revenda", "coef_variacao_revenda",
                   "preco_medio_distribuicao", "coef_variacao_distribuicao"):
        assert pd.api.types.is_float_dtype(df[coluna]), coluna


def test_gasolina_comum_vira_gasolina_c(cfg, planilha_valida):
    df = transformar(cfg, planilha_valida, SHA)
    assert "GASOLINA COMUM" not in set(df["produto"])
    assert set(df["produto"].unique()) == {
        "GASOLINA C", "GASOLINA ADITIVADA", "ETANOL HIDRATADO", "OLEO DIESEL",
        "OLEO DIESEL S10", "GNV", "GLP",
    }


def test_estado_por_extenso_vira_sigla(cfg, planilha_valida):
    df = transformar(cfg, planilha_valida, SHA)
    assert df.loc[df["estado"] == "SERGIPE", "uf"].eq("SE").all()
    assert df.loc[df["estado"] == "SAO PAULO", "uf"].eq("SP").all()
    assert df["uf"].nunique() == 27


def test_produtos_fora_do_escopo_sao_descartados(cfg, tmp_path):
    linhas = semana_completa(date(2026, 9, 13))
    linhas.append(linha_anp(date(2026, 9, 13), "SERGIPE", "NORDESTE",
                            "QUEROSENE DE AVIACAO", 5.0))
    arq = escrever_planilha(tmp_path / "extras.xlsx", linhas)
    df = transformar(cfg, arq, SHA)
    assert "QUEROSENE DE AVIACAO" not in set(df["produto"])
    assert len(df) == LINHAS_POR_SEMANA  # 27 UFs x 7 produtos


def test_marcador_de_ausencia_vira_nulo_sem_quebrar(cfg, tmp_path):
    """O "-" da ANP em coluna opcional nao derruba a linha."""
    linhas = semana_completa(date(2026, 9, 13))
    linhas[0][9] = "-"   # PRECO MINIMO REVENDA
    arq = escrever_planilha(tmp_path / "traco.xlsx", linhas)
    df = transformar(cfg, arq, SHA)
    assert len(df) == LINHAS_POR_SEMANA
    assert df["preco_min"].isna().sum() == 1


def test_linha_sem_preco_medio_e_descartada(cfg, tmp_path, caplog):
    linhas = semana_completa(date(2026, 9, 13))
    linhas[0][7] = "-"  # PRECO MEDIO REVENDA, coluna critica
    arq = escrever_planilha(tmp_path / "sem_preco.xlsx", linhas)
    with caplog.at_level("WARNING"):
        df = transformar(cfg, arq, SHA)
    assert len(df) == LINHAS_POR_SEMANA - 1
    assert "linhas_descartadas" in eventos(caplog)


def test_decimal_com_virgula_e_convertido(cfg, tmp_path):
    linhas = semana_completa(date(2026, 9, 13))
    linhas[0][7] = "6,25"
    arq = escrever_planilha(tmp_path / "virgula.xlsx", linhas)
    df = transformar(cfg, arq, SHA)
    alvo = df[(df["uf"] == "AC") & (df["produto"] == "GASOLINA C")]
    assert alvo["preco_medio"].iloc[0] == pytest.approx(6.25)


def test_sem_agregacao_preserva_o_grao_da_fonte(cfg, planilha_valida):
    df = transformar(cfg, planilha_valida, SHA)
    assert len(df) == 2 * LINHAS_POR_SEMANA  # 2 semanas x 27 UFs x 7 produtos
    assert not df.duplicated(subset=["data_inicio", "uf", "produto"]).any()


def test_saida_ordenada_por_semana_uf_produto(cfg, planilha_valida):
    df = transformar(cfg, planilha_valida, SHA)
    esperado = df.sort_values(["data_inicio", "uf", "produto"], ignore_index=True)
    pd.testing.assert_frame_equal(df, esperado)


def test_linhagem_registrada_em_cada_linha(cfg, planilha_valida):
    df = transformar(cfg, planilha_valida, SHA)
    assert df["origem_sha256"].eq(SHA).all()
    assert df["data_ingestao"].notna().all()


# --- mudancas de conteudo que invalidam o escopo ----------------------------

def test_produto_do_escopo_ausente_falha(cfg, tmp_path):
    linhas = [
        linha_anp(date(2026, 9, 13), e, r, "GASOLINA COMUM", 6.0) for e, r in
        [("SERGIPE", "NORDESTE"), ("BAHIA", "NORDESTE")]
    ]
    arq = escrever_planilha(tmp_path / "sem_etanol.xlsx", linhas)
    with pytest.raises(ErroEsquema) as exc:
        transformar(cfg, arq, SHA)
    assert "ETANOL HIDRATADO" in str(exc.value)
    assert "[escopo.produtos]" in str(exc.value)


def test_unidade_de_medida_diferente_falha(cfg, tmp_path):
    linhas = semana_completa(date(2026, 9, 13))
    linhas[0][6] = "R$/galao"
    arq = escrever_planilha(tmp_path / "galao.xlsx", linhas)
    with pytest.raises(ErroEsquema) as exc:
        transformar(cfg, arq, SHA)
    assert "galao" in str(exc.value).lower()


def test_unidade_e_validada_por_produto(cfg, tmp_path):
    """GLP em R$/l e erro, mesmo sendo a unidade certa da gasolina."""
    linhas = [
        linha if linha[4] != "GLP" or linha[3] != "BAHIA"
        else linha_anp(linha[0], "BAHIA", "NORDESTE", "GLP", 110.0, unidade="R$/l")
        for linha in semana_completa(date(2026, 9, 13))
    ]
    arq = escrever_planilha(tmp_path / "glp_litro.xlsx", linhas)
    with pytest.raises(ErroEsquema) as exc:
        transformar(cfg, arq, SHA)
    mensagem = str(exc.value)
    assert "GLP" in mensagem
    assert "R$/13kg" in mensagem
    assert "GASOLINA C" not in mensagem


def test_grafias_antigas_da_unidade_sao_aceitas(cfg, tmp_path):
    """A ANP escreveu R$/13Kg e R$/m3 ate julho de 2020; a unidade e a mesma."""
    antigas = {"GLP": "R$/13Kg", "GNV": "R$/m3"}
    linhas = [
        linha_anp(linha[0], linha[3], linha[2], linha[4], linha[7],
                  unidade=antigas[linha[4]]) if linha[4] in antigas else linha
        for linha in semana_completa(date(2020, 7, 26))
    ] + semana_completa(date(2026, 9, 13))
    arq = escrever_planilha(tmp_path / "grafias.xlsx", linhas)
    df = transformar(cfg, arq, SHA)
    assert len(df) == 2 * LINHAS_POR_SEMANA
    glp = df[df["produto"] == "GLP"]
    assert set(glp["unidade_medida"]) == {"R$/13Kg", "R$/13kg"}


def test_colunas_de_distribuicao_e_margem_sao_mantidas(cfg, tmp_path):
    """Preenchidas ate 2020, "-" depois: os dois casos convivem no staging."""
    linhas = (semana_completa(date(2020, 8, 16), distribuicao=True)
              + semana_completa(date(2026, 9, 13)))
    arq = escrever_planilha(tmp_path / "distribuicao.xlsx", linhas)
    df = transformar(cfg, arq, SHA)

    antiga = df[df["data_inicio"] == "2020-08-16"]
    recente = df[df["data_inicio"] == "2026-09-13"]
    distribuicao = ["preco_medio_distribuicao", "preco_min_distribuicao",
                    "preco_max_distribuicao", "desvio_padrao_distribuicao",
                    "coef_variacao_distribuicao", "margem_media_revenda"]
    assert antiga[distribuicao].notna().all().all()
    assert recente[distribuicao].isna().all().all()
    assert df["coef_variacao_revenda"].notna().all()

    glp = antiga[(antiga["produto"] == "GLP") & (antiga["uf"] == "SE")].iloc[0]
    assert glp["preco_medio_distribuicao"] == pytest.approx(88.0)
    assert glp["margem_media_revenda"] == pytest.approx(22.0)


def test_estado_desconhecido_falha(cfg, tmp_path):
    linhas = semana_completa(date(2026, 9, 13))
    linhas.append(
        linha_anp(date(2026, 9, 13), "GUANABARA", "SUDESTE", "GASOLINA COMUM", 6.0)
    )
    arq = escrever_planilha(tmp_path / "guanabara.xlsx", linhas)
    with pytest.raises(ErroEsquema) as exc:
        transformar(cfg, arq, SHA)
    assert "GUANABARA" in str(exc.value)
    assert "[ufs]" in str(exc.value)


def test_semana_com_duracao_atipica_gera_warning_nao_erro(cfg, tmp_path, caplog):
    linhas = semana_completa(date(2026, 9, 13))
    linhas[0][1] = date(2026, 9, 17)  # semana de 5 dias
    arq = escrever_planilha(tmp_path / "curta_semana.xlsx", linhas)
    with caplog.at_level("WARNING"):
        df = transformar(cfg, arq, SHA)
    assert len(df) == LINHAS_POR_SEMANA
    assert "duracao_atipica" in eventos(caplog)


# --- staging ----------------------------------------------------------------

def test_gravar_staging_e_idempotente(cfg, planilha_valida, tmp_path, monkeypatch):
    monkeypatch.setitem(cfg.dados["caminhos"], "staging", str(tmp_path / "staging"))
    df = transformar(cfg, planilha_valida, SHA)
    primeiro = gravar_staging(cfg, df)
    tamanho = primeiro.stat().st_size
    segundo = gravar_staging(cfg, df)
    assert primeiro == segundo
    assert segundo.stat().st_size == tamanho
    assert len(list(segundo.parent.glob("*.parquet"))) == 1
    pd.testing.assert_frame_equal(pd.read_parquet(segundo), df)
