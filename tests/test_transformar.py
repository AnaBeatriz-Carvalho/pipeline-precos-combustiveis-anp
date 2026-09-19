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


def test_gasolina_comum_vira_gasolina_c(cfg, planilha_valida):
    df = transformar(cfg, planilha_valida, SHA)
    assert set(df["produto"].unique()) == {"GASOLINA C", "ETANOL HIDRATADO"}


def test_estado_por_extenso_vira_sigla(cfg, planilha_valida):
    df = transformar(cfg, planilha_valida, SHA)
    assert df.loc[df["estado"] == "SERGIPE", "uf"].eq("SE").all()
    assert df.loc[df["estado"] == "SAO PAULO", "uf"].eq("SP").all()
    assert df["uf"].nunique() == 27


def test_produtos_fora_do_escopo_sao_descartados(cfg, tmp_path):
    linhas = semana_completa(date(2026, 9, 13))
    linhas.append(linha_anp(date(2026, 9, 13), "SERGIPE", "NORDESTE", "GLP", 100.0))
    linhas.append(
        linha_anp(date(2026, 9, 13), "SERGIPE", "NORDESTE", "GASOLINA ADITIVADA", 7.5)
    )
    arq = escrever_planilha(tmp_path / "extras.xlsx", linhas)
    df = transformar(cfg, arq, SHA)
    assert set(df["produto"].unique()) == {"GASOLINA C", "ETANOL HIDRATADO"}
    assert len(df) == 54  # 27 UFs x 2 produtos


def test_marcador_de_ausencia_vira_nulo_sem_quebrar(cfg, tmp_path):
    """O "-" da ANP em coluna opcional nao derruba a linha."""
    linhas = semana_completa(date(2026, 9, 13))
    linhas[0][9] = "-"   # PRECO MINIMO REVENDA
    arq = escrever_planilha(tmp_path / "traco.xlsx", linhas)
    df = transformar(cfg, arq, SHA)
    assert len(df) == 54
    assert df["preco_min"].isna().sum() == 1


def test_linha_sem_preco_medio_e_descartada(cfg, tmp_path, caplog):
    linhas = semana_completa(date(2026, 9, 13))
    linhas[0][7] = "-"  # PRECO MEDIO REVENDA, coluna critica
    arq = escrever_planilha(tmp_path / "sem_preco.xlsx", linhas)
    with caplog.at_level("WARNING"):
        df = transformar(cfg, arq, SHA)
    assert len(df) == 53
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
    assert len(df) == 108  # 2 semanas x 27 UFs x 2 produtos
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
    assert len(df) == 54
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
