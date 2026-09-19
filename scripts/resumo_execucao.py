#!/usr/bin/env python3
"""Converte o log JSON Lines do pipeline num resumo Markdown.

Usado pelo GitHub Actions para escrever no GITHUB_STEP_SUMMARY, de modo que o
motivo de uma reprovacao apareca na propria pagina da execucao, sem obrigar
ninguem a abrir o log cru.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MARCA = {"PASSOU": "aprovado", "ALERTA": "ALERTA", "FALHOU": "FALHOU"}


def ler_eventos(caminho: Path) -> list[dict]:
    """Le o log ignorando linhas que nao sejam JSON (ruido de dependencias)."""
    if not caminho.is_file():
        return []
    eventos = []
    for linha in caminho.read_text(encoding="utf-8", errors="replace").splitlines():
        linha = linha.strip()
        if not linha.startswith("{"):
            continue
        try:
            eventos.append(json.loads(linha))
        except json.JSONDecodeError:
            continue
    return eventos


def montar_resumo(eventos: list[dict], sucesso: bool) -> str:
    testes = [e for e in eventos if e.get("evento") == "teste_qualidade"]
    partes: list[str] = ["# Pipeline semanal ANP", ""]

    if sucesso:
        partes += ["## Status: APROVADO", ""]
    else:
        partes += ["## Status: REPROVADO", ""]

    carga = next((e for e in eventos if e.get("evento") == "carga_ok"), None)
    if carga:
        partes += [
            f"Linhas no mart: **{carga.get('linhas_depois')}** "
            f"(novas: {carga.get('linhas_novas')}, "
            f"atualizadas: {carga.get('linhas_atualizadas')})",
            "",
        ]

    if testes:
        partes += ["### Testes de qualidade", "", "| Teste | Status |", "| --- | --- |"]
        for teste in testes:
            partes.append(
                f"| {teste.get('teste')} | {MARCA.get(teste.get('status'), '?')} |"
            )
        partes.append("")
    else:
        partes += [
            "Nenhum teste de qualidade chegou a rodar: o pipeline parou antes.",
            "",
        ]

    for rotulo, status in (("Falhas", "FALHOU"), ("Alertas (nao bloqueiam)", "ALERTA")):
        pertinentes = [t for t in testes if t.get("status") == status]
        if not pertinentes:
            continue
        partes += [f"### {rotulo}", ""]
        for teste in pertinentes:
            partes.append(f"- **{teste.get('teste')}**: {teste.get('mensagem')}")
        partes.append("")

    erros = [
        e for e in eventos
        if e.get("evento") in ("pipeline_erro", "pipeline_reprovado")
    ]
    if erros:
        partes += ["### Detalhe do erro", "", "```"]
        for erro in erros:
            partes.append(str(erro.get("detalhe", erro.get("mensagem", ""))))
        partes += ["```", ""]

    return "\n".join(partes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resumo Markdown da execucao.")
    parser.add_argument("--log", type=Path, default=Path("execucao.log"))
    parser.add_argument(
        "--resultado", default="success",
        help="Desfecho do passo do pipeline, como o Actions reporta.",
    )
    parser.add_argument("--saida", type=Path, default=None)
    args = parser.parse_args(argv)

    resumo = montar_resumo(ler_eventos(args.log), args.resultado == "success")
    if args.saida:
        with args.saida.open("a", encoding="utf-8") as arquivo:
            arquivo.write(resumo + "\n")
    else:
        sys.stdout.write(resumo + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
