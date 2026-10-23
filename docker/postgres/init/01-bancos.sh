#!/bin/bash
# Cria os dois bancos do servico postgres, cada um com o seu usuario:
#
#   airflow  metadados do Airflow     usuario airflow
#   anp      dados do pipeline        usuario anp
#
# Roda uma unica vez, quando o volume do Postgres esta vazio (regra da imagem
# oficial para /docker-entrypoint-initdb.d). Trocar as senhas no .env depois
# disso nao altera os usuarios: veja o README.
#
# O usuario anp tem CREATEDB porque os testes @pytest.mark.postgres criam e
# removem um banco temporario no mesmo servidor.
set -euo pipefail

: "${AIRFLOW_DB_PASSWORD:?AIRFLOW_DB_PASSWORD nao definida no .env}"
: "${ANP_DB_PASSWORD:?ANP_DB_PASSWORD nao definida no .env}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v airflow_senha="$AIRFLOW_DB_PASSWORD" \
    -v anp_senha="$ANP_DB_PASSWORD" <<'SQL'
CREATE ROLE airflow LOGIN PASSWORD :'airflow_senha';
CREATE DATABASE airflow OWNER airflow;
REVOKE ALL ON DATABASE airflow FROM PUBLIC;

CREATE ROLE anp LOGIN CREATEDB PASSWORD :'anp_senha';
CREATE DATABASE anp OWNER anp;
REVOKE ALL ON DATABASE anp FROM PUBLIC;
SQL
