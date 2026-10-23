"""Testes da leitura de [[escopo.produtos]] no config.toml."""

from __future__ import annotations

import pytest

from anp.config import CAMINHO_PADRAO, carregar_config, normalizar_unidade
from anp.erros import ErroPipeline


def _config_com(tmp_path, antes: str, depois: str):
    texto = CAMINHO_PADRAO.read_text(encoding="utf-8")
    assert antes in texto, antes
    alvo = tmp_path / "config.toml"
    alvo.write_text(texto.replace(antes, depois, 1), encoding="utf-8")
    return alvo


def test_produtos_do_escopo(cfg):
    produtos = cfg.produtos
    assert list(produtos) == [
        "GASOLINA C", "GASOLINA ADITIVADA", "ETANOL HIDRATADO", "OLEO DIESEL",
        "OLEO DIESEL S10", "GNV", "GLP",
    ]
    assert produtos["GASOLINA C"].rotulo_anp == "GASOLINA COMUM"
    assert produtos["GLP"].faixa_preco_revenda == (20.0, 250.0)
    assert produtos["GLP"].categoria == "glp"
    assert produtos["ETANOL HIDRATADO"].ufs_ausentes_toleradas == ("AP",)
    assert cfg.rotulos_anp["GASOLINA COMUM"] == "GASOLINA C"


def test_so_o_glp_tem_categoria_glp(cfg):
    categorias = {p.produto: p.categoria for p in cfg.produtos.values()}
    assert {p for p, c in categorias.items() if c == "glp"} == {"GLP"}
    assert set(categorias.values()) == {"combustivel_veicular", "glp"}


@pytest.mark.parametrize("bruta, esperada", [
    ("R$/13Kg", "r$/13kg"),
    (" R$/13kg ", "r$/13kg"),
    ("R$/m³", "r$/m3"),
    ("R$/m3", "r$/m3"),
    ("R$/l", "r$/l"),
])
def test_normalizar_unidade(bruta, esperada):
    assert normalizar_unidade(bruta) == esperada


@pytest.mark.parametrize("antes, depois, trecho", [
    ('categoria = "glp"', 'categoria = "gas"', "categoria"),
    ("faixa_preco_revenda = [20.0, 250.0]", "faixa_preco_revenda = [250.0, 20.0]",
     "faixa_preco_revenda"),
    ("faixa_preco_revenda = [20.0, 250.0]", "faixa_preco_revenda = [20.0]",
     "faixa_preco_revenda"),
    ('ufs_ausentes_toleradas = ["AP"]', 'ufs_ausentes_toleradas = ["XX"]', "XX"),
    ('rotulo_anp = "GLP"', 'rotulo_anp = "GNV"', "repetido"),
    ('unidade = "R$/13kg"\n', "", "unidade"),
    ("margem_maxima = 60.0", "margem_maxima = 0", "margem_maxima"),
])
def test_bloco_de_produto_invalido_e_rejeitado(tmp_path, antes, depois, trecho):
    with pytest.raises(ErroPipeline, match=trecho):
        carregar_config(_config_com(tmp_path, antes, depois))


def test_config_sem_produtos_e_rejeitado(tmp_path):
    texto = CAMINHO_PADRAO.read_text(encoding="utf-8")
    inicio = texto.index("[[escopo.produtos]]")
    fim = texto.index("# Nome por extenso")
    alvo = tmp_path / "config.toml"
    alvo.write_text(texto[:inicio] + "[escopo]\n\n" + texto[fim:], encoding="utf-8")
    with pytest.raises(ErroPipeline, match=r"\[\[escopo.produtos\]\]"):
        carregar_config(alvo)
