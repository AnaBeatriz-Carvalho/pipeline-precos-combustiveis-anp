"""Fixtures compartilhadas: planilhas sinteticas no formato da ANP."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import openpyxl
import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anp.config import RAIZ_PROJETO, carregar_config  # noqa: E402


def pytest_collection_modifyitems(config, items):
    """Pula os testes @pytest.mark.postgres quando nao ha DATABASE_URL."""
    import os

    from dotenv import load_dotenv

    load_dotenv(RAIZ_PROJETO / ".env", override=False)
    if os.environ.get("DATABASE_URL", "").strip():
        return
    pular = pytest.mark.skip(reason="DATABASE_URL nao definida")
    for item in items:
        if "postgres" in item.keywords:
            item.add_marker(pular)

COLUNAS_ANP = [
    "DATA INICIAL",
    "DATA FINAL",
    "REGIÃO",
    "ESTADO",
    "PRODUTO",
    "NÚMERO DE POSTOS PESQUISADOS",
    "UNIDADE DE MEDIDA",
    "PREÇO MÉDIO REVENDA",
    "DESVIO PADRÃO REVENDA",
    "PREÇO MÍNIMO REVENDA",
    "PREÇO MÁXIMO REVENDA",
    "MARGEM MÉDIA REVENDA",
    "COEF DE VARIAÇÃO REVENDA",
    "PREÇO MÉDIO DISTRIBUIÇÃO",
    "DESVIO PADRÃO DISTRIBUIÇÃO",
    "PREÇO MÍNIMO DISTRIBUIÇÃO",
    "PREÇO MÁXIMO DISTRIBUIÇÃO",
    "COEF DE VARIAÇÃO DISTRIBUIÇÃO",
]

# Os 27 nomes de estado exatamente como a ANP escreve: maiusculas, sem acento.
ESTADOS = [
    ("ACRE", "NORTE"), ("ALAGOAS", "NORDESTE"), ("AMAPA", "NORTE"),
    ("AMAZONAS", "NORTE"), ("BAHIA", "NORDESTE"), ("CEARA", "NORDESTE"),
    ("DISTRITO FEDERAL", "CENTRO OESTE"), ("ESPIRITO SANTO", "SUDESTE"),
    ("GOIAS", "CENTRO OESTE"), ("MARANHAO", "NORDESTE"),
    ("MATO GROSSO", "CENTRO OESTE"), ("MATO GROSSO DO SUL", "CENTRO OESTE"),
    ("MINAS GERAIS", "SUDESTE"), ("PARA", "NORTE"), ("PARAIBA", "NORDESTE"),
    ("PARANA", "SUL"), ("PERNAMBUCO", "NORDESTE"), ("PIAUI", "NORDESTE"),
    ("RIO DE JANEIRO", "SUDESTE"), ("RIO GRANDE DO NORTE", "NORDESTE"),
    ("RIO GRANDE DO SUL", "SUL"), ("RONDONIA", "NORTE"), ("RORAIMA", "NORTE"),
    ("SANTA CATARINA", "SUL"), ("SAO PAULO", "SUDESTE"),
    ("SERGIPE", "NORDESTE"), ("TOCANTINS", "NORTE"),
]


# Produtos do escopo como a ANP os escreve: rotulo, unidade e um preco tipico.
# As unidades usam a grafia atual da planilha (R$/13kg, R$/m³).
PRODUTOS_ANP = (
    ("GASOLINA COMUM", "R$/l", 6.0),
    ("GASOLINA ADITIVADA", "R$/l", 6.5),
    ("ETANOL HIDRATADO", "R$/l", 4.0),
    ("OLEO DIESEL", "R$/l", 6.2),
    ("OLEO DIESEL S10", "R$/l", 6.3),
    ("GNV", "R$/m³", 4.7),
    ("GLP", "R$/13kg", 110.0),
)
UNIDADE_ANP = {rotulo: unidade for rotulo, unidade, _ in PRODUTOS_ANP}

# Mesmo preco tipico, pelo rotulo do mart.
PRECOS_MART = {
    "GASOLINA C": 6.0, "GASOLINA ADITIVADA": 6.5, "ETANOL HIDRATADO": 4.0,
    "OLEO DIESEL": 6.2, "OLEO DIESEL S10": 6.3, "GNV": 4.7, "GLP": 110.0,
}
N_PRODUTOS = len(PRODUTOS_ANP)
LINHAS_POR_SEMANA = 27 * N_PRODUTOS


def linha_anp(inicio: date, estado: str, regiao: str, produto: str,
              preco: float, postos: int = 30, unidade: str | None = None,
              distribuicao: float | None = None) -> list:
    """Uma linha no layout da planilha.

    Sem distribuicao, as colunas de distribuicao e a margem vem com "-", como
    na planilha depois de agosto de 2020. Com distribuicao, vem preenchidas e
    a margem e revenda - distribuicao, como a ANP calcula.
    """
    unidade = unidade or UNIDADE_ANP.get(produto, "R$/l")
    if distribuicao is None:
        margem, dist = "-", ["-", "-", "-", "-", "-"]
    else:
        margem = round(preco - distribuicao, 4)
        dist = [distribuicao, 0.05, round(distribuicao - 0.1, 4),
                round(distribuicao + 0.1, 4), 0.02]
    return [
        inicio, inicio + timedelta(days=6), regiao, estado, produto, postos,
        unidade, preco, 0.05, round(preco - 0.2, 3), round(preco + 0.2, 3),
        margem, 0.01, *dist,
    ]


def escrever_planilha(destino: Path, linhas: list[list],
                      cabecalho: list[str] | None = None,
                      linhas_preambulo: int = 17) -> Path:
    """Monta um XLSX com o preambulo institucional da ANP antes do cabecalho."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ESTADOS - DESDE 30.12.2012"
    for i in range(1, linhas_preambulo + 1):
        ws.cell(row=i, column=1, value=f"NOTA INSTITUCIONAL {i}")
    ws.append(cabecalho if cabecalho is not None else COLUNAS_ANP)
    for linha in linhas:
        ws.append(linha)
    destino.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destino)
    return destino


def semana_completa(inicio: date, preco_gasolina: float = 6.0,
                    preco_etanol: float = 4.0,
                    distribuicao: bool = False) -> list[list]:
    """Uma semana com os 27 estados e os sete produtos do escopo.

    Com distribuicao=True, as colunas de distribuicao vem preenchidas com 80%
    do preco de revenda, como na planilha ate agosto de 2020.
    """
    precos = {rotulo: preco for rotulo, _, preco in PRODUTOS_ANP}
    precos["GASOLINA COMUM"] = preco_gasolina
    precos["ETANOL HIDRATADO"] = preco_etanol
    linhas = []
    for estado, regiao in ESTADOS:
        for rotulo, _, _ in PRODUTOS_ANP:
            preco = precos[rotulo]
            linhas.append(linha_anp(
                inicio, estado, regiao, rotulo, preco,
                distribuicao=round(preco * 0.8, 4) if distribuicao else None,
            ))
    return linhas


@pytest.fixture
def cfg():
    return carregar_config()


@pytest.fixture
def planilha_valida(tmp_path) -> Path:
    """Duas semanas consecutivas, completas e dentro do escopo."""
    linhas = semana_completa(date(2026, 9, 6)) + semana_completa(date(2026, 9, 13))
    return escrever_planilha(tmp_path / "anp_valida.xlsx", linhas)


@pytest.fixture
def mart():
    """Mart em memoria com a tabela analitica ja criada."""
    import duckdb

    from anp.carregar import criar_tabela

    conexao = duckdb.connect(":memory:")
    criar_tabela(conexao)
    yield conexao
    conexao.close()


def inserir(conexao, linhas: list[dict]) -> None:
    """Insere linhas no mart a partir de dicionarios parciais."""
    import pandas as pd

    from anp.carregar import COLUNAS_MART

    from anp.carregar import COLUNAS_OPCIONAIS

    padrao = {
        "data_fim": None, "regiao": "NORDESTE", "desvio_padrao": 0.1,
        "numero_postos": 30, "data_ingestao": pd.Timestamp("2026-09-19 12:00:00"),
        **{coluna: None for coluna in COLUNAS_OPCIONAIS},
    }
    completas = []
    for linha in linhas:
        registro = {**padrao, **linha}
        if registro["data_fim"] is None:
            registro["data_fim"] = registro["data_inicio"] + timedelta(days=6)
        # Minimo e maximo de revenda acompanham o medio, salvo se vierem dados.
        medio = registro["preco_medio"]
        registro.setdefault("preco_min", None if medio is None else round(medio * 0.97, 4))
        registro.setdefault("preco_max", None if medio is None else round(medio * 1.03, 4))
        completas.append(registro)
    df = pd.DataFrame(completas)[list(COLUNAS_MART)]
    conexao.register("entrada", df)
    conexao.execute(
        f"INSERT INTO fato_precos_semanais SELECT {', '.join(COLUNAS_MART)} FROM entrada"
    )
    conexao.unregister("entrada")


def semana_no_mart(
    conexao, inicio: date, precos: dict[str, float] | None = None,
    ufs_gasolina: list[str] | None = None, ufs_etanol: list[str] | None = None,
    ufs: dict[str, list[str]] | None = None,
    produtos: list[str] | None = None,
) -> None:
    """Carrega uma semana inteira no mart, com controle de quais UFs entram.

    Por padrao entram os sete produtos em todas as 27 UFs, com o preco tipico
    de PRECOS_MART. precos sobrescreve o preco de alguns produtos; ufs (ou os
    atalhos ufs_gasolina e ufs_etanol) restringe as UFs de alguns produtos; e
    produtos limita quais produtos entram.
    """
    from anp.config import carregar_config

    todas = sorted(carregar_config().ufs.values())
    precos = {**PRECOS_MART, **(precos or {})}
    ufs = dict(ufs or {})
    if ufs_gasolina is not None:
        ufs["GASOLINA C"] = ufs_gasolina
    if ufs_etanol is not None:
        ufs["ETANOL HIDRATADO"] = ufs_etanol
    linhas = []
    for produto in (produtos if produtos is not None else list(PRECOS_MART)):
        for uf in ufs.get(produto, todas):
            linhas.append({"data_inicio": inicio, "uf": uf, "produto": produto,
                           "preco_medio": precos[produto]})
    inserir(conexao, linhas)
