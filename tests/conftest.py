"""Fixtures compartilhadas: planilhas sinteticas no formato da ANP."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import openpyxl
import pytest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anp.config import carregar_config  # noqa: E402

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


def linha_anp(inicio: date, estado: str, regiao: str, produto: str,
              preco: float, postos: int = 30, unidade: str = "R$/l") -> list:
    """Uma linha no layout da planilha, com "-" nas colunas de distribuicao."""
    return [
        inicio, inicio + timedelta(days=6), regiao, estado, produto, postos,
        unidade, preco, 0.05, round(preco - 0.2, 3), round(preco + 0.2, 3),
        "-", 0.01, "-", "-", "-", "-", "-",
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
                    preco_etanol: float = 4.0) -> list[list]:
    """Uma semana com os 27 estados e os dois produtos do escopo."""
    linhas = []
    for estado, regiao in ESTADOS:
        linhas.append(linha_anp(inicio, estado, regiao, "GASOLINA COMUM", preco_gasolina))
        linhas.append(linha_anp(inicio, estado, regiao, "ETANOL HIDRATADO", preco_etanol))
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

    padrao = {
        "data_fim": None, "regiao": "NORDESTE", "preco_min": 1.0,
        "preco_max": 9.0, "desvio_padrao": 0.1, "numero_postos": 30,
        "data_ingestao": pd.Timestamp("2026-09-19 12:00:00"),
    }
    completas = []
    for linha in linhas:
        registro = {**padrao, **linha}
        if registro["data_fim"] is None:
            registro["data_fim"] = registro["data_inicio"] + timedelta(days=6)
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
) -> None:
    """Carrega uma semana inteira no mart, com controle de quais UFs entram."""
    from anp.config import carregar_config

    todas = sorted(carregar_config().ufs.values())
    precos = precos or {"GASOLINA C": 6.0, "ETANOL HIDRATADO": 4.0}
    linhas = []
    for uf in (ufs_gasolina if ufs_gasolina is not None else todas):
        linhas.append({"data_inicio": inicio, "uf": uf, "produto": "GASOLINA C",
                       "preco_medio": precos["GASOLINA C"]})
    for uf in (ufs_etanol if ufs_etanol is not None else todas):
        linhas.append({"data_inicio": inicio, "uf": uf, "produto": "ETANOL HIDRATADO",
                       "preco_medio": precos["ETANOL HIDRATADO"]})
    inserir(conexao, linhas)
