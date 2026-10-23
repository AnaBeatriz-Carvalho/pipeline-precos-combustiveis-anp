"""Pipeline semanal da ANP no Airflow, com destino PostgreSQL.

    inicializar_banco -> extrair -> transformar -> carregar_staging -> validar -> promover

Cada task so liga uma funcao de anp.* ao Airflow: carrega o config.toml, abre
a conexao quando precisa e chama a funcao. Regras de extracao, transformacao,
qualidade e carga ficam em src/anp.

Entre tasks so passam valores pequenos por XCom: caminhos de arquivo, sha256,
id_carga e o resumo dos testes de qualidade. O DataFrame vai da transformacao
para a carga pelo Parquet do staging, no volume de dados do scheduler.

O DAG usa sempre o backend PostgreSQL, qualquer que seja [destino] backend no
config.toml: as tasks chamam anp.carregar_postgres diretamente. O CLI
(python -m anp.pipeline) continua respeitando o config.toml.

Retries: so extrair tem retry, com backoff, porque a falha tipica ali e o
gov.br fora do ar. Nos passos da carga, uma falha ja grava "falhou" em
controle.cargas, e promover recusa carga que nao esteja "iniciada"; repetir
validar ou promover nao teria efeito. Uma reprovacao na qualidade e definitiva
para a execucao (AirflowFailException), mesmo que alguem ligue retries depois.
"""

from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.sdk.exceptions import AirflowFailException
from airflow.sdk import dag, task

# Os imports de anp.* ficam dentro das tasks: o dag-processor rele este arquivo
# periodicamente, e pandas, duckdb e openpyxl so precisam carregar na execucao.


@dag(
    dag_id="anp_precos_semanais",
    description="Precos semanais de combustiveis da ANP -> PostgreSQL (staging, qualidade, mart)",
    # Toda quarta-feira as 12:00 UTC, como o workflow do GitHub Actions.
    schedule="0 12 * * 3",
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 0},
    tags=["anp", "postgres"],
)
def anp_precos_semanais():
    @task
    def inicializar_banco() -> list[str]:
        """Aplica o DDL de sql/ (idempotente). Devolve os arquivos aplicados."""
        from anp import carregar_postgres

        return carregar_postgres.inicializar_banco()

    @task(
        retries=3,
        retry_delay=timedelta(minutes=2),
        retry_exponential_backoff=True,
        max_retry_delay=timedelta(minutes=30),
    )
    def extrair() -> dict[str, str]:
        from anp.config import carregar_config
        from anp.extrair import extrair as extrair_anp

        resultado = extrair_anp(carregar_config())
        return {"caminho_raw": str(resultado.caminho), "sha256": resultado.sha256}

    @task
    def transformar(extracao: dict[str, str]) -> dict[str, str]:
        from pathlib import Path

        from anp.config import carregar_config
        from anp.transformar import gravar_staging, transformar as transformar_anp

        cfg = carregar_config()
        df = transformar_anp(cfg, Path(extracao["caminho_raw"]), extracao["sha256"])
        caminho = gravar_staging(cfg, df)
        return {"caminho_staging": str(caminho), "sha256": extracao["sha256"]}

    @task
    def carregar_staging(staging: dict[str, str]) -> str:
        """Devolve o id_carga, que liga os tres passos da carga."""
        import pandas as pd

        from anp import carregar_postgres
        from anp.config import carregar_config

        cfg = carregar_config()
        df = pd.read_parquet(staging["caminho_staging"])
        with carregar_postgres.conectar(cfg) as conexao:
            return carregar_postgres.carregar_staging(cfg, df, conexao)

    @task
    def validar(id_carga: str) -> list[dict[str, str]]:
        """Resumo dos testes (nome, status, mensagem) para promover."""
        from anp import carregar_postgres
        from anp.config import carregar_config
        from anp.erros import ErroQualidade

        cfg = carregar_config()
        with carregar_postgres.conectar(cfg) as conexao:
            try:
                resultados = carregar_postgres.validar(cfg, conexao, id_carga)
            except ErroQualidade as erro:
                # validar ja gravou "falhou" em controle.cargas; aqui so se
                # garante que o Airflow nao tente de novo.
                raise AirflowFailException(str(erro)) from erro
        return [
            {"nome": r.nome, "status": r.status, "mensagem": r.mensagem}
            for r in resultados
        ]

    @task
    def promover(id_carga: str, resultados: list[dict[str, str]]) -> int:
        from anp import carregar_postgres
        from anp.config import carregar_config
        from anp.qualidade import Resultado

        cfg = carregar_config()
        with carregar_postgres.conectar(cfg) as conexao:
            return carregar_postgres.promover(
                conexao, id_carga, [Resultado(**r) for r in resultados]
            )

    extracao = extrair()
    inicializar_banco() >> extracao
    id_carga = carregar_staging(transformar(extracao))
    promover(id_carga, validar(id_carga))


anp_precos_semanais()
