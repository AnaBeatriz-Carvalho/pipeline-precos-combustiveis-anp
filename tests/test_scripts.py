"""Testes dos scripts auxiliares: resumo do Actions e geracao do grafico."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from conftest import semana_no_mart  # noqa: E402
from gerar_grafico import gerar  # noqa: E402
from resumo_execucao import ler_eventos, montar_resumo  # noqa: E402

from anp.carregar import conectar  # noqa: E402
from anp.erros import ErroPipeline  # noqa: E402


# --- resumo da execucao -----------------------------------------------------

def _log(tmp_path: Path, eventos: list[dict]) -> Path:
    caminho = tmp_path / "execucao.log"
    caminho.write_text(
        "\n".join(json.dumps(e) for e in eventos) + "\n", encoding="utf-8"
    )
    return caminho


def test_resumo_de_execucao_aprovada(tmp_path):
    log = _log(tmp_path, [
        {"evento": "carga_ok", "linhas_depois": 100, "linhas_novas": 4,
         "linhas_atualizadas": 96},
        {"evento": "teste_qualidade", "teste": "chave_primaria_sem_duplicata",
         "status": "PASSOU", "mensagem": "ok"},
        {"evento": "teste_qualidade", "teste": "cobertura_de_ufs",
         "status": "PASSOU", "mensagem": "ok"},
    ])
    resumo = montar_resumo(ler_eventos(log), sucesso=True)
    assert "## Status: APROVADO" in resumo
    assert "cobertura_de_ufs | aprovado" in resumo
    assert "Linhas no mart: **100**" in resumo


def test_resumo_destaca_o_teste_que_falhou(tmp_path):
    log = _log(tmp_path, [
        {"evento": "teste_qualidade", "teste": "cobertura_de_ufs",
         "status": "FALHOU", "mensagem": "faltam ['AP'] na semana"},
        {"evento": "pipeline_reprovado", "detalhe": "1 de 4 testes falharam"},
    ])
    resumo = montar_resumo(ler_eventos(log), sucesso=False)
    assert "## Status: REPROVADO" in resumo
    assert "### Falhas" in resumo
    assert "faltam ['AP'] na semana" in resumo
    assert "1 de 4 testes falharam" in resumo


def test_resumo_separa_alerta_de_falha(tmp_path):
    log = _log(tmp_path, [
        {"evento": "teste_qualidade", "teste": "variacao_semanal",
         "status": "ALERTA", "mensagem": "3 series acima de 15%"},
    ])
    resumo = montar_resumo(ler_eventos(log), sucesso=True)
    assert "### Alertas (nao bloqueiam)" in resumo
    assert "### Falhas" not in resumo


def test_resumo_aguenta_log_truncado_ou_sujo(tmp_path):
    """Linha cortada ao meio ou ruido de dependencia nao derruba o resumo."""
    caminho = tmp_path / "execucao.log"
    caminho.write_text(
        'nao e json\n'
        '{"evento": "teste_qualidade", "teste": "t", "status": "PASSOU", "mensagem": "ok"}\n'
        '{"evento": "carga_ok", "linhas_dep\n',
        encoding="utf-8",
    )
    resumo = montar_resumo(ler_eventos(caminho), sucesso=True)
    assert "| t | aprovado |" in resumo


def test_resumo_quando_o_pipeline_parou_antes_dos_testes(tmp_path):
    log = _log(tmp_path, [
        {"evento": "pipeline_erro", "detalhe": "Nao foi possivel baixar"},
    ])
    resumo = montar_resumo(ler_eventos(log), sucesso=False)
    assert "Nenhum teste de qualidade chegou a rodar" in resumo
    assert "Nao foi possivel baixar" in resumo


def test_log_inexistente_nao_quebra(tmp_path):
    resumo = montar_resumo(ler_eventos(tmp_path / "nao_existe.log"), sucesso=False)
    assert "## Status: REPROVADO" in resumo


# --- grafico ----------------------------------------------------------------

@pytest.fixture
def cfg_com_mart(cfg, tmp_path, monkeypatch):
    monkeypatch.setitem(cfg.dados["caminhos"], "mart_db", str(tmp_path / "mart.duckdb"))
    monkeypatch.setitem(cfg.dados["caminhos"], "graficos", str(tmp_path / "graficos"))
    return cfg


def test_grafico_e_gerado_a_partir_do_mart(cfg_com_mart):
    con = conectar(cfg_com_mart)
    for dia in (6, 13, 20):
        semana_no_mart(con, date(2026, 9, dia))
    con.close()

    caminho = gerar(cfg_com_mart)
    assert caminho.is_file()
    assert caminho.stat().st_size > 10_000
    assert caminho.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_grafico_falha_com_mensagem_util_se_o_mart_estiver_vazio(cfg_com_mart):
    conectar(cfg_com_mart).close()
    with pytest.raises(ErroPipeline, match="mart esta vazio"):
        gerar(cfg_com_mart)


def test_grafico_falha_se_a_uf_de_destaque_nao_tiver_dados(cfg_com_mart, monkeypatch):
    con = conectar(cfg_com_mart)
    semana_no_mart(con, date(2026, 9, 13))
    con.close()
    monkeypatch.setitem(cfg_com_mart.dados["grafico"], "uf_destaque", "XX")
    with pytest.raises(ErroPipeline, match="uf_destaque"):
        gerar(cfg_com_mart)
