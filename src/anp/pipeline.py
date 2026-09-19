"""Orquestracao do pipeline: extrair -> transformar -> carregar -> validar.

Sem framework de orquestracao. A fonte da ANP e cumulativa (cada coleta traz a
serie historica inteira), entao o pipeline e full refresh: nao existe estado a
preservar entre execucoes, o que o torna idempotente por construcao e adequado
a um runner efemero de CI.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from anp.carregar import carregar, conectar
from anp.config import Config, carregar_config
from anp.erros import ErroPipeline, ErroQualidade
from anp.extrair import extrair
from anp.log import configurar_logging, obter_logger
from anp.qualidade import ALERTA, FALHOU, executar_testes
from anp.transformar import gravar_staging, transformar

log = obter_logger("pipeline")


def executar(
    cfg: Config,
    data_coleta: date | None = None,
    usar_raw_existente: Path | None = None,
    semana_alvo: date | None = None,
) -> list:
    """Roda o pipeline de ponta a ponta e devolve os resultados dos testes."""
    inicio = datetime.now()
    log.info("pipeline iniciado", extra={"evento": "pipeline_inicio"})

    if usar_raw_existente is not None:
        if not usar_raw_existente.is_file():
            raise ErroPipeline(
                f"Arquivo indicado em --usar-raw nao existe: {usar_raw_existente}"
            )
        caminho_raw = usar_raw_existente
        sha = "raw-local-reaproveitado"
        log.info(
            "download ignorado, usando arquivo local",
            extra={"evento": "download_pulado", "caminho": str(caminho_raw)},
        )
    else:
        resultado = extrair(cfg, data_coleta=data_coleta)
        caminho_raw = resultado.caminho
        sha = resultado.sha256

    df = transformar(cfg, caminho_raw, sha)
    gravar_staging(cfg, df)

    conexao = conectar(cfg)
    try:
        carregar(cfg, df, conexao=conexao)
        resultados = executar_testes(conexao, cfg, semana=semana_alvo)
    finally:
        conexao.close()

    log.info(
        "pipeline concluido",
        extra={
            "evento": "pipeline_fim",
            "duracao_segundos": round((datetime.now() - inicio).total_seconds(), 1),
            "linhas": len(df),
        },
    )
    return resultados


def _analisar_argumentos(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="anp-pipeline",
        description="Pipeline ETL semanal dos precos de combustiveis da ANP.",
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Caminho do config.toml (padrao: config.toml na raiz do projeto).",
    )
    parser.add_argument(
        "--data-coleta", type=date.fromisoformat, default=None,
        help="Data de coleta no nome do arquivo raw, em AAAA-MM-DD (padrao: hoje).",
    )
    parser.add_argument(
        "--usar-raw", type=Path, default=None,
        help="Reaproveita um XLSX ja baixado em vez de acessar a ANP.",
    )
    parser.add_argument(
        "--semana", type=date.fromisoformat, default=None,
        help="Semana a validar, em AAAA-MM-DD (padrao: a mais recente do mart).",
    )
    parser.add_argument(
        "--log-nivel", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Ponto de entrada. Codigo 1 em erro de qualidade, 2 nos demais."""
    args = _analisar_argumentos(argv)
    configurar_logging(args.log_nivel)

    try:
        cfg = carregar_config(args.config)
        resultados = executar(
            cfg,
            data_coleta=args.data_coleta,
            usar_raw_existente=args.usar_raw,
            semana_alvo=args.semana,
        )
    except ErroQualidade as erro:
        # Distinguir qualidade de falha operacional torna o log do Actions
        # imediatamente legivel.
        log.error(
            "pipeline reprovado nos testes de qualidade",
            extra={"evento": "pipeline_reprovado", "detalhe": str(erro)},
        )
        print(f"\nFALHA DE QUALIDADE\n{erro}\n", file=sys.stderr)
        return 1
    except ErroPipeline as erro:
        log.error(
            "pipeline interrompido",
            extra={"evento": "pipeline_erro", "detalhe": str(erro)},
        )
        print(f"\nFALHA NO PIPELINE\n{erro}\n", file=sys.stderr)
        return 2

    alertas = [r for r in resultados if r.status == ALERTA]
    if alertas:
        print(f"\nPipeline concluido com {len(alertas)} alerta(s):", file=sys.stderr)
        for alerta in alertas:
            print(f"  - {alerta.mensagem}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
