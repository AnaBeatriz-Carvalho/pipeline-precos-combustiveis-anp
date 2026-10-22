"""Testes do destino PostgreSQL que nao precisam de um servidor."""

from __future__ import annotations

import pandas as pd
import pytest

from anp import carregar_postgres
from anp.carregar import carregar, conectar
from anp.config import carregar_config
from anp.erros import ErroPipeline


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


def test_carga_no_postgres_ainda_nao_disponivel(cfg, monkeypatch):
    monkeypatch.setitem(cfg.dados["destino"], "backend", "postgres")
    with pytest.raises(ErroPipeline, match="ainda nao esta disponivel"):
        carregar(cfg, pd.DataFrame(), conexao=object())
