"""Leitura e validacao do config.toml.

Toda constante do pipeline vive no arquivo de configuracao. Este modulo so
carrega, valida o minimo e resolve caminhos relativos a raiz do projeto.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anp.erros import ErroPipeline

RAIZ_PROJETO = Path(__file__).resolve().parents[2]
CAMINHO_PADRAO = RAIZ_PROJETO / "config.toml"

_SECOES_OBRIGATORIAS = (
    "fonte",
    "download",
    "caminhos",
    "extracao",
    "escopo",
    "qualidade",
    "ufs",
    "grafico",
)

BACKENDS = ("duckdb", "postgres")

CATEGORIAS = ("combustivel_veicular", "glp")

_CAMPOS_PRODUTO = (
    "produto",
    "rotulo_anp",
    "unidade",
    "categoria",
    "faixa_preco_revenda",
    "faixa_preco_distribuicao",
    "desvio_padrao_maximo",
    "margem_maxima",
    "variacao_semanal_alerta",
    "ufs_ausentes_toleradas",
)


def normalizar_unidade(unidade: str) -> str:
    """Forma comparavel da unidade: sem espacos nas pontas, minuscula, ³ -> 3.

    A ANP muda a grafia no meio da serie (R$/13Kg -> R$/13kg, R$/m3 -> R$/m³)
    sem mudar a unidade.
    """
    return str(unidade).strip().lower().replace("³", "3")


@dataclass(frozen=True)
class Produto:
    """Um produto do escopo, como declarado em [[escopo.produtos]]."""

    produto: str
    rotulo_anp: str
    unidade: str
    categoria: str
    faixa_preco_revenda: tuple[float, float]
    faixa_preco_distribuicao: tuple[float, float]
    desvio_padrao_maximo: float
    margem_maxima: float
    variacao_semanal_alerta: float
    ufs_ausentes_toleradas: tuple[str, ...]


def _faixa(bruto: Any, onde: str) -> tuple[float, float]:
    if not isinstance(bruto, list) or len(bruto) != 2:
        raise ErroPipeline(f"{onde} deve ser uma lista [minimo, maximo], veio {bruto!r}.")
    minimo, maximo = float(bruto[0]), float(bruto[1])
    if not 0 <= minimo < maximo:
        raise ErroPipeline(
            f"{onde} = {bruto!r} e invalida: o minimo precisa ser >= 0 e menor que o maximo."
        )
    return minimo, maximo


def _ler_produtos(dados: dict[str, Any], alvo: Path) -> dict[str, Produto]:
    """Valida [[escopo.produtos]] e devolve os produtos pelo rotulo do mart."""
    blocos = dados["escopo"].get("produtos")
    if not isinstance(blocos, list) or not blocos:
        raise ErroPipeline(
            f"Config {alvo}: [[escopo.produtos]] deve declarar ao menos um produto, "
            "um bloco [[escopo.produtos]] por produto."
        )
    siglas = set(dados["ufs"].values())
    produtos: dict[str, Produto] = {}
    rotulos: set[str] = set()
    for i, bloco in enumerate(blocos, start=1):
        nome = bloco.get("produto", f"#{i}")
        onde = f"Config {alvo}: produto {nome!r} em [[escopo.produtos]]"
        faltando = [c for c in _CAMPOS_PRODUTO if c not in bloco]
        if faltando:
            raise ErroPipeline(f"{onde} sem os campos: {', '.join(faltando)}.")
        if bloco["categoria"] not in CATEGORIAS:
            raise ErroPipeline(
                f"{onde}: categoria {bloco['categoria']!r} invalida. "
                f"Use uma destas: {', '.join(CATEGORIAS)}."
            )
        rotulo = str(bloco["rotulo_anp"]).strip().upper()
        if nome in produtos or rotulo in rotulos:
            raise ErroPipeline(f"{onde}: produto ou rotulo_anp repetido.")
        toleradas = tuple(bloco["ufs_ausentes_toleradas"])
        desconhecidas = sorted(set(toleradas) - siglas)
        if desconhecidas:
            raise ErroPipeline(
                f"{onde}: ufs_ausentes_toleradas tem siglas que nao estao em [ufs]: "
                f"{desconhecidas}."
            )
        for campo in ("desvio_padrao_maximo", "margem_maxima", "variacao_semanal_alerta"):
            if float(bloco[campo]) <= 0:
                raise ErroPipeline(f"{onde}: {campo} precisa ser maior que zero.")
        produtos[nome] = Produto(
            produto=str(nome),
            rotulo_anp=rotulo,
            unidade=str(bloco["unidade"]),
            categoria=str(bloco["categoria"]),
            faixa_preco_revenda=_faixa(bloco["faixa_preco_revenda"],
                                       f"{onde}: faixa_preco_revenda"),
            faixa_preco_distribuicao=_faixa(bloco["faixa_preco_distribuicao"],
                                            f"{onde}: faixa_preco_distribuicao"),
            desvio_padrao_maximo=float(bloco["desvio_padrao_maximo"]),
            margem_maxima=float(bloco["margem_maxima"]),
            variacao_semanal_alerta=float(bloco["variacao_semanal_alerta"]),
            ufs_ausentes_toleradas=toleradas,
        )
        rotulos.add(rotulo)
    return produtos


@dataclass(frozen=True)
class Config:
    """Configuracao do pipeline, com caminhos ja resolvidos."""

    dados: dict[str, Any]
    raiz: Path

    def __getitem__(self, chave: str) -> Any:
        return self.dados[chave]

    def caminho(self, chave: str) -> Path:
        """Resolve um caminho da secao [caminhos] contra a raiz do projeto."""
        bruto = self.dados["caminhos"][chave]
        return (self.raiz / bruto).resolve()

    @property
    def produtos(self) -> dict[str, Produto]:
        """Produtos do escopo pelo rotulo do mart, na ordem do config."""
        return _ler_produtos(self.dados, self.raiz / "config.toml")

    @property
    def rotulos_anp(self) -> dict[str, str]:
        """Mapa rotulo-na-ANP -> rotulo normalizado do mart."""
        return {p.rotulo_anp: p.produto for p in self.produtos.values()}

    @property
    def ufs(self) -> dict[str, str]:
        """Mapa nome-do-estado-por-extenso -> sigla."""
        return dict(self.dados["ufs"])

    @property
    def backend(self) -> str:
        """Destino da carga. Sem a secao [destino], o mart continua no DuckDB."""
        return str(self.dados.get("destino", {}).get("backend", "duckdb"))


def carregar_config(caminho: Path | str | None = None) -> Config:
    """Carrega o config.toml e valida que as secoes obrigatorias existem."""
    alvo = Path(caminho) if caminho else CAMINHO_PADRAO
    if not alvo.is_file():
        raise ErroPipeline(
            f"Arquivo de configuracao nao encontrado em {alvo}. "
            "Copie o config.toml da raiz do projeto ou passe --config."
        )

    with alvo.open("rb") as arquivo:
        dados = tomllib.load(arquivo)

    faltando = [s for s in _SECOES_OBRIGATORIAS if s not in dados]
    if faltando:
        raise ErroPipeline(
            f"Config {alvo} esta incompleto. Secoes obrigatorias ausentes: "
            f"{', '.join(faltando)}."
        )

    if len(dados["ufs"]) != 27:
        raise ErroPipeline(
            f"Config {alvo}: a secao [ufs] deve mapear as 27 unidades da "
            f"federacao, mas tem {len(dados['ufs'])}."
        )

    backend = dados.get("destino", {}).get("backend", "duckdb")
    if backend not in BACKENDS:
        raise ErroPipeline(
            f"Config {alvo}: destino.backend = {backend!r} nao e suportado. "
            f"Use um destes: {', '.join(BACKENDS)}."
        )

    # Valida ja na carga: um erro no bloco de produtos aparece antes do download.
    _ler_produtos(dados, alvo)

    return Config(dados=dados, raiz=RAIZ_PROJETO)
