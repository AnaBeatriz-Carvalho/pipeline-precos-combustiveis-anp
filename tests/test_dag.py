"""DAG anp_precos_semanais: importa sem erro, ordem das tasks e retries.

Pulado quando o Airflow nao esta instalado, como no CI. Para rodar, use a
imagem do Compose (ver README).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

# skipif em vez de importorskip: o importorskip pula o modulo ja na coleta, e
# esse pulo escapa do filtro -m (o job integracao-postgres o contaria).
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("airflow") is None, reason="Airflow nao instalado"
)

DIR_DAGS = Path(__file__).resolve().parents[1] / "dags"
DAG_ID = "anp_precos_semanais"
ORDEM = [
    "inicializar_banco",
    "extrair",
    "transformar",
    "carregar_staging",
    "validar",
    "promover",
]


@pytest.fixture(scope="module")
def dagbag():
    try:
        from airflow.dag_processing.dagbag import DagBag
    except ImportError:  # Airflow anterior a 3.2
        from airflow.models.dagbag import DagBag

    # No Airflow 3.3 o DagBag so le a pasta indicada; nao ha DAGs de exemplo.
    return DagBag(dag_folder=str(DIR_DAGS))


@pytest.fixture(scope="module")
def dag(dagbag):
    return dagbag.dags[DAG_ID]


def test_dagbag_sem_erro_de_import(dagbag):
    assert dagbag.import_errors == {}
    assert DAG_ID in dagbag.dags


def test_tasks_em_sequencia(dag):
    assert {t.task_id for t in dag.tasks} == set(ORDEM)
    for anterior, seguinte in zip(ORDEM, ORDEM[1:]):
        assert seguinte in dag.get_task(anterior).downstream_task_ids, (
            f"{anterior} deveria preceder {seguinte}"
        )
    # promover tambem recebe o id_carga direto de carregar_staging.
    assert dag.get_task("promover").upstream_task_ids == {"carregar_staging", "validar"}
    assert dag.get_task("inicializar_banco").upstream_task_ids == set()


def test_agendamento(dag):
    # Cron interpretado no fuso do start_date: quarta-feira, 12:00 UTC.
    assert dag.schedule == "0 12 * * 3"
    assert str(dag.start_date.tzinfo) == "UTC"
    assert dag.catchup is False
    assert dag.max_active_runs == 1


def test_retries(dag):
    extrair = dag.get_task("extrair")
    assert extrair.retries == 3
    assert extrair.retry_exponential_backoff
    assert dag.get_task("validar").retries == 0
    assert dag.get_task("promover").retries == 0


def test_reprovacao_na_qualidade_nao_tem_retry(dag, monkeypatch):
    """ErroQualidade vira AirflowFailException: falha sem nova tentativa."""
    from contextlib import nullcontext

    from airflow.sdk.exceptions import AirflowFailException

    from anp import carregar_postgres
    from anp.erros import ErroQualidade

    def reprovar(*_args, **_kwargs):
        raise ErroQualidade("cobertura_de_ufs: faltou SE")

    monkeypatch.setattr(carregar_postgres, "conectar", lambda _cfg: nullcontext())
    monkeypatch.setattr(carregar_postgres, "validar", reprovar)

    with pytest.raises(AirflowFailException, match="cobertura_de_ufs"):
        dag.get_task("validar").python_callable("id-qualquer")
