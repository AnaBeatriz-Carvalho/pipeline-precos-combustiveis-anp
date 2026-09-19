#!/usr/bin/env python3
"""Gera o grafico da serie por UF, com Sergipe destacado.

Duas faixas empilhadas, ambas sobre a janela de 24 meses:

  1. preco medio da gasolina C por UF
  2. paridade etanol/gasolina por UF, com a linha de referencia em 70%

As 27 UFs nao recebem 27 cores: isso seria ilegivel e nenhuma paleta
categorica suporta tantas series. As demais UFs formam uma faixa de contexto
em cinza e so Sergipe carrega cor, que e o que a leitura pede.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import duckdb
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from dateutil.relativedelta import relativedelta  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anp.carregar import TABELA  # noqa: E402
from anp.config import carregar_config  # noqa: E402
from anp.erros import ErroPipeline  # noqa: E402
from anp.log import configurar_logging, obter_logger  # noqa: E402

log = obter_logger("grafico")

MESES_PT = ("jan", "fev", "mar", "abr", "mai", "jun",
            "jul", "ago", "set", "out", "nov", "dez")


def _mes_ano(d) -> str:
    """Rotulo de mes em portugues, sem depender do locale do sistema."""
    return f"{MESES_PT[d.month - 1]}/{d.year}"

# Paleta validada com scripts/validate_palette.js do guia de dataviz:
# as duas cores de identidade passam em todos os checks no fundo claro.
DESTAQUE = "#2a78d6"    # Sergipe
REFERENCIA = "#d03b3b"  # linha dos 70%
CONTEXTO = "#898781"    # demais UFs: cinza, sem identidade propria
INK = "#0b0b0b"
INK_SECUNDARIO = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
EIXO = "#c3c2b7"
SUPERFICIE = "#fcfcfb"

plt.rcParams.update({
    "font.family": ["DejaVu Sans"],
    "figure.facecolor": SUPERFICIE,
    "axes.facecolor": SUPERFICIE,
    "savefig.facecolor": SUPERFICIE,
    "axes.edgecolor": EIXO,
    "axes.labelcolor": INK_SECUNDARIO,
    "text.color": INK,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.spines.top": False,
    "axes.spines.right": False,
})


def consultar(con, produto: str, desde: date):
    """Serie semanal de precos de um produto, por UF."""
    return con.execute(
        f"""
        SELECT data_inicio, uf, preco_medio
        FROM {TABELA}
        WHERE produto = ? AND data_inicio >= ?
        ORDER BY uf, data_inicio
        """,
        [produto, desde],
    ).df()


def consultar_paridade(con, desde: date):
    """Razao etanol/gasolina por semana e UF, em pontos percentuais.

    O INNER JOIN e proposital: sem os dois precos na mesma semana nao existe
    paridade, e o AP, que costuma nao ter etanol, simplesmente nao aparece.
    """
    return con.execute(
        f"""
        SELECT
            e.data_inicio,
            e.uf,
            100.0 * e.preco_medio / g.preco_medio AS paridade
        FROM {TABELA} e
        JOIN {TABELA} g
          ON g.data_inicio = e.data_inicio
         AND g.uf = e.uf
         AND g.produto = 'GASOLINA C'
        WHERE e.produto = 'ETANOL HIDRATADO'
          AND e.data_inicio >= ?
        ORDER BY e.uf, e.data_inicio
        """,
        [desde],
    ).df()


def _desenhar_series(ax, df, coluna: str, uf_destaque: str) -> None:
    """Faixa de contexto em cinza, com uma UF por cima em cor."""
    for uf, grupo in df.groupby("uf"):
        if uf == uf_destaque:
            continue
        ax.plot(grupo["data_inicio"], grupo[coluna],
                color=CONTEXTO, linewidth=0.7, alpha=0.35, zorder=1)

    destaque = df[df["uf"] == uf_destaque]
    if destaque.empty:
        raise ErroPipeline(
            f"A UF de destaque {uf_destaque!r} nao tem dados na janela pedida. "
            "Verifique grafico.uf_destaque no config.toml."
        )
    # Anel da cor da superficie por baixo: separa a linha destacada da faixa.
    ax.plot(destaque["data_inicio"], destaque[coluna],
            color=SUPERFICIE, linewidth=4.0, zorder=2)
    ax.plot(destaque["data_inicio"], destaque[coluna],
            color=DESTAQUE, linewidth=2.0, zorder=3)
    return destaque


def gerar(cfg, destino: Path | None = None) -> Path:
    uf_destaque = str(cfg["grafico"]["uf_destaque"])
    meses = int(cfg["grafico"]["janela_meses"])

    con = duckdb.connect(str(cfg.caminho("mart_db")), read_only=True)
    try:
        ultima = con.execute(f"SELECT MAX(data_inicio) FROM {TABELA}").fetchone()[0]
        if ultima is None:
            raise ErroPipeline(
                "O mart esta vazio. Rode o pipeline antes de gerar o grafico."
            )
        desde = ultima - relativedelta(months=meses)

        precos = consultar(con, "GASOLINA C", desde)
        paridade = consultar_paridade(con, desde)
    finally:
        con.close()

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11, 9), sharex=True,
        gridspec_kw={"height_ratios": [1, 1], "hspace": 0.26},
    )

    # --- faixa 1: preco da gasolina C
    serie_uf = _desenhar_series(ax1, precos, "preco_medio", uf_destaque)
    ax1.set_title(
        f"Gasolina C: {uf_destaque} contra as demais UFs",
        loc="left", fontsize=13, fontweight="bold", color=INK, pad=10,
    )
    ax1.set_ylabel("Preço médio (R$/l)", fontsize=10)
    ultimo_preco = serie_uf.iloc[-1]
    ax1.annotate(
        f"{uf_destaque}  R$ {ultimo_preco['preco_medio']:.2f}",
        xy=(ultimo_preco["data_inicio"], ultimo_preco["preco_medio"]),
        xytext=(8, 0), textcoords="offset points",
        color=DESTAQUE, fontsize=10, fontweight="bold", va="center",
    )

    # --- faixa 2: paridade, com o limiar dos 70%
    serie_par = _desenhar_series(ax2, paridade, "paridade", uf_destaque)
    ax2.axhline(70, color=REFERENCIA, linewidth=1.6, linestyle=(0, (5, 3)), zorder=4)
    ax2.annotate(
        "70% — abaixo disto o etanol compensa",
        xy=(0.004, 70), xycoords=("axes fraction", "data"),
        xytext=(0, -14), textcoords="offset points",
        color=REFERENCIA, fontsize=9.5, fontweight="bold",
    )
    ax2.set_title(
        "Paridade etanol / gasolina C",
        loc="left", fontsize=13, fontweight="bold", color=INK, pad=10,
    )
    ax2.set_ylabel("Etanol ÷ gasolina (%)", fontsize=10)
    ultima_par = serie_par.iloc[-1]
    ax2.annotate(
        f"{uf_destaque}  {ultima_par['paridade']:.1f}%",
        xy=(ultima_par["data_inicio"], ultima_par["paridade"]),
        xytext=(8, 0), textcoords="offset points",
        color=DESTAQUE, fontsize=10, fontweight="bold", va="center",
    )

    for eixo in (ax1, ax2):
        eixo.grid(axis="y", color=GRID, linewidth=0.8)
        eixo.set_axisbelow(True)
        eixo.margins(x=0.02)
        eixo.tick_params(labelsize=9.5)

    # Legenda sempre presente: a identidade nunca fica so na cor. Vai no
    # rodape da figura para nao competir com o titulo da segunda faixa.
    fig.legend(
        handles=[
            Line2D([], [], color=DESTAQUE, linewidth=2.0, label=f"{uf_destaque} (destaque)"),
            Line2D([], [], color=CONTEXTO, linewidth=1.0, alpha=0.6,
                   label="Demais 26 UFs"),
            Line2D([], [], color=REFERENCIA, linewidth=1.6, linestyle=(0, (5, 3)),
                   label="Paridade de 70%"),
        ],
        loc="lower left", bbox_to_anchor=(0.065, 0.005), frameon=False,
        fontsize=9.5, ncol=3, columnspacing=2.2,
    )

    fig.suptitle(
        f"Preço de combustíveis por UF — últimos {meses} meses",
        x=0.065, y=0.975, ha="left", fontsize=16, fontweight="bold", color=INK,
    )
    fig.text(
        0.065, 0.932,
        f"Levantamento semanal de preços da ANP · {_mes_ano(desde)} a "
        f"{_mes_ano(ultima)} · "
        "valores nominais, sem correção pela inflação",
        ha="left", fontsize=9.5, color=INK_SECUNDARIO,
    )
    # Posicionamento explicito em vez de tight_layout: o suptitle, o subtitulo
    # e a legenda de rodape sao artefatos da figura, e o tight_layout nao os
    # enxerga (avisa e reposiciona os eixos por cima deles).
    fig.subplots_adjust(left=0.075, right=0.90, top=0.885, bottom=0.085)

    alvo = destino or (cfg.caminho("graficos") / "serie_precos_por_uf.png")
    alvo.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(alvo, dpi=160, bbox_inches="tight")
    plt.close(fig)

    log.info(
        "grafico gerado",
        extra={
            "evento": "grafico_ok",
            "caminho": str(alvo),
            "uf_destaque": uf_destaque,
            "janela_meses": meses,
            "semanas": int(precos["data_inicio"].nunique()),
        },
    )
    return alvo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gera o grafico da serie por UF.")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--saida", type=Path, default=None)
    args = parser.parse_args(argv)

    configurar_logging()
    try:
        caminho = gerar(carregar_config(args.config), args.saida)
    except ErroPipeline as erro:
        print(f"\nFALHA AO GERAR O GRAFICO\n{erro}\n", file=sys.stderr)
        return 2
    print(f"Grafico salvo em {caminho}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
