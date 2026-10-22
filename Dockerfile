# Imagem do Airflow com as dependencias e o codigo do pipeline.
#
# Estende a imagem oficial, como recomenda
# https://airflow.apache.org/docs/docker-stack/build.html, em vez de instalar
# pacotes a cada subida com _PIP_ADDITIONAL_REQUIREMENTS.
#
# Layout dentro da imagem, espelhando o repositorio para que
# anp.config.RAIZ_PROJETO resolva config.toml, sql/ e data/ como no host:
#
#   /opt/anp/config.toml
#   /opt/anp/sql/        DDL aplicado pela task inicializar_banco
#   /opt/anp/src/anp/    codigo do pipeline (PYTHONPATH)
#   /opt/anp/dags/       DAGs (no Compose local, sobreposto por bind mount)
#   /opt/anp/data/       raw e staging, num volume nomeado
FROM apache/airflow:3.3.2-python3.12

ENV ANP_HOME=/opt/anp \
    PYTHONPATH=/opt/anp/src

USER root
RUN mkdir -p /opt/anp/data/raw /opt/anp/data/staging /opt/anp/data/mart \
    && chown -R airflow:root /opt/anp
USER airflow

# Airflow fixado na mesma versao da imagem: o pip resolve requirements.txt
# sem trocar a versao do Airflow por baixo.
COPY --chown=airflow:root requirements.txt /opt/anp/requirements.txt
RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" -r /opt/anp/requirements.txt

COPY --chown=airflow:root config.toml /opt/anp/config.toml
COPY --chown=airflow:root sql /opt/anp/sql
COPY --chown=airflow:root src /opt/anp/src
COPY --chown=airflow:root dags /opt/anp/dags
