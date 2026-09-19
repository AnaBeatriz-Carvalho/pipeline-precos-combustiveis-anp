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
    def produtos(self) -> dict[str, str]:
        """Mapa rotulo-na-ANP -> rotulo normalizado do mart."""
        return dict(self.dados["escopo"]["produtos"])

    @property
    def ufs(self) -> dict[str, str]:
        """Mapa nome-do-estado-por-extenso -> sigla."""
        return dict(self.dados["ufs"])


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

    return Config(dados=dados, raiz=RAIZ_PROJETO)
