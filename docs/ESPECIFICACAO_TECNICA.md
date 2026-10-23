# Especificação técnica — Pipeline de preços de combustíveis da ANP

Este documento descreve **como o pipeline funciona por dentro**: componentes,
fluxo de dados, contratos entre etapas, modelo de dados, regras de qualidade,
tratamento de falhas e infraestrutura. O README cobre o *porquê* e o *como
rodar*; aqui o foco é o *como foi construído*.

---

## 1. Visão geral

| Item | Valor |
|---|---|
| Objetivo | Tabela analítica semanal de preços de revenda de **gasolina C** e **etanol hidratado** por UF |
| Pergunta de negócio | Em quais UFs a paridade etanol/gasolina fica abaixo de 70%? |
| Fonte | Planilha `semanal-estados-desde-2013.xlsx` da ANP (sem API) |
| Tipo de carga | **Full refresh** com upsert idempotente |
| Frequência | Semanal, quarta-feira 12:00 UTC (09:00 de Brasília) |
| Grão do mart | semana × UF × produto |
| Linguagem | Python 3.12 |
| Orquestração | Airflow 3.3.2 (LocalExecutor) ou CLI / GitHub Actions |
| Destinos | PostgreSQL 16 (Airflow) ou DuckDB (CLI, padrão) |

### Por que full refresh

A ANP publica **um único arquivo cumulativo** que é sobrescrito toda semana e
traz a série inteira desde 30/12/2012. Não existe arquivo "só da semana nova".
Por isso cada execução baixa tudo, reprocessa tudo e faz upsert no mart. Isso
traz três vantagens:

1. Não há estado a guardar entre execuções (bom para runners efêmeros de CI).
2. Revisões retroativas da ANP entram automaticamente.
3. Rodar de novo nunca duplica nada (idempotência).

---

## 2. Arquitetura

### 2.1 Camadas de dados

```
 ANP (XLSX ~12 MB)
     │  extrair.py
     ▼
 RAW       data/raw/semanal-estados_AAAA-MM-DD.xlsx  + _manifesto.jsonl
     │  transformar.py
     ▼
 STAGING   data/staging/precos_semanais.parquet       (tipado, grão da fonte)
     │  carregar.py / carregar_postgres.py
     ▼
 MART      DuckDB: fato_precos_semanais
           Postgres: staging.precos_semanais → mart.fato_precos_semanais
     │  qualidade.py
     ▼
 CONTROLE  controle.cargas (só Postgres)
```

| Camada | Formato | Conteúdo | Ciclo de vida |
|---|---|---|---|
| Raw | XLSX + JSONL | Arquivo exatamente como veio da ANP | Um arquivo por data de coleta; reaproveitado se o SHA-256 for igual |
| Staging (arquivo) | Parquet (snappy) | Dados limpos e tipados, sem agregação | Reescrito a cada execução |
| Staging (banco) | Tabela Postgres | Mesmo conteúdo, com `id_carga` | `TRUNCATE` + `COPY` a cada carga |
| Mart | Tabela DuckDB ou Postgres | Tabela analítica final | Upsert pela chave primária |
| Controle | Tabela Postgres | Uma linha por execução | Só cresce |

### 2.2 Módulos (`src/anp/`)

| Módulo | Responsabilidade |
|---|---|
| `config.py` | Lê e valida o `config.toml` (seções obrigatórias, 27 UFs, backend válido). Resolve caminhos relativos à raiz do projeto |
| `extrair.py` | Download com retry, validações de integridade, gravação na raw e manifesto |
| `transformar.py` | Validação de esquema, limpeza, tipagem e gravação do Parquet |
| `carregar.py` | Carga no DuckDB e roteamento para o Postgres conforme o backend |
| `carregar_postgres.py` | DDL, conexão e os três passos da carga no Postgres |
| `qualidade.py` | Os quatro testes de qualidade |
| `pipeline.py` | CLI que encadeia tudo (`python -m anp.pipeline`) |
| `log.py` | Logging estruturado em JSON Lines |
| `erros.py` | Hierarquia de exceções |

Outros diretórios:

| Caminho | Papel |
|---|---|
| `dags/anp_precos_semanais.py` | DAG do Airflow; só chama funções de `src/anp` |
| `sql/001..004_*.sql` | DDL do Postgres, numerado e idempotente |
| `scripts/gerar_grafico.py` | Gráfico de 24 meses com Sergipe destacado |
| `scripts/resumo_execucao.py` | Converte o log JSONL em resumo Markdown para o GitHub Actions |
| `docker/postgres/init/01-bancos.sh` | Cria os bancos e usuários na primeira subida do Postgres |
| `config.toml` | Toda constante do pipeline (URL, limiares, mapas, caminhos) |

### 2.3 Princípio de projeto

**A lógica fica em `src/anp`; o orquestrador só chama funções.** O DAG, o CLI
e o GitHub Actions são três formas de disparar o mesmo código. Nenhuma regra de
negócio vive no DAG.

---

## 3. Etapas do pipeline

### 3.1 Extração (`extrair.py`)

**Entrada:** URL em `[fonte] url`.
**Saída:** `ResultadoExtracao(caminho, sha256, bytes_totais, url, coletado_em, reutilizado)`.

Passo a passo:

1. Baixa via `requests` em streaming, para um arquivo temporário
   `.semanal-estados_AAAA-MM-DD.xlsx.parcial`.
2. **Valida a assinatura ZIP** (`PK\x03\x04`) no primeiro bloco. Todo XLSX é um
   ZIP; se vier HTML (o gov.br às vezes devolve página de erro com HTTP 200),
   falha na hora.
3. **Valida o tamanho mínimo** (1 MB, em `tamanho_minimo_bytes`) para pegar
   download truncado.
4. Calcula o SHA-256.
5. Consulta o manifesto `data/raw/_manifesto.jsonl`. Se já existir coleta com
   o mesmo hash, descarta o arquivo novo e reaproveita o antigo
   (`reutilizado=True`).
6. Se for novo, renomeia o temporário para o nome final. **A raw nunca contém
   arquivo pela metade**, porque o rename só acontece após o download completo.
7. Acrescenta uma linha no manifesto.

**Retry com backoff exponencial** (configurável em `[download]`):

| Parâmetro | Valor |
|---|---|
| Tentativas | 5 |
| Espera inicial | 2 s |
| Fator | ×2 |
| Espera máxima | 60 s |
| Status retentáveis | 408, 425, 429, 500, 502, 503, 504 |
| Timeouts | 30 s conexão, 180 s leitura |

Esgotadas as tentativas → `ErroDownload` com a última causa.

### 3.2 Transformação (`transformar.py`)

**Entrada:** caminho do XLSX + SHA-256.
**Saída:** DataFrame tipado e o arquivo `data/staging/precos_semanais.parquet`.

Validações, na ordem em que acontecem. Todas levantam `ErroEsquema` com
mensagem que diz o que corrigir:

| # | Validação | Se falhar |
|---|---|---|
| 1 | Cabeçalho na linha 18 com exatamente as 18 colunas de `colunas_esperadas` | Procura o cabeçalho nas linhas vizinhas. Se achar, diz "ajuste `linha_cabecalho` para N"; se não, lista colunas ausentes e novas |
| 2 | Colunas usadas existem após a leitura do pandas | Sugere conferir `aba_indice` |
| 3 | Os produtos do escopo existem na planilha | Lista os rótulos presentes e pede para atualizar `[escopo.produtos]` |
| 4 | Unidade de medida é `R$/l` | Os limiares de preço dependem disso |
| 5 | Todo nome de estado está mapeado em `[ufs]` | Lista os não mapeados |
| 6 | Datas legíveis | Conta quantas falharam |

Transformações aplicadas:

| De (fonte) | Para (staging) |
|---|---|
| `GASOLINA COMUM` | `GASOLINA C` |
| `ETANOL HIDRATADO` | `ETANOL HIDRATADO` |
| Demais produtos (diesel, GLP, GNV, aditivada) | descartados |
| Nome do estado por extenso (`SAO PAULO`) | sigla (`SP`) |
| `-`, vazio, `n/a` | nulo |
| Vírgula decimal (se aparecer) | ponto |
| Colunas de distribuição e margem | descartadas (fora do escopo) |

Regras adicionais:

- Linhas sem `preco_medio` ou `numero_postos` são **descartadas e registradas
  no log** com contagem, proporção e amostra.
- Semanas com duração diferente de 7 dias geram **aviso no log**, não erro
  (a ANP já publicou semanas encurtadas por feriado).
- Se nada sobrar após os filtros → `ErroEsquema`.
- Cada linha recebe `origem_sha256` (rastreabilidade até o arquivo raw) e
  `data_ingestao` (UTC).

O Parquet é gravado num temporário e renomeado, também de forma atômica.

**Colunas do staging:** `data_inicio`, `data_fim`, `uf`, `estado`, `regiao`,
`produto`, `preco_medio`, `preco_min`, `preco_max`, `desvio_padrao`,
`coef_variacao`, `numero_postos`, `unidade_medida`, `origem_sha256`,
`data_ingestao`. O mart usa um subconjunto delas (seção 4).

### 3.3 Carga e qualidade: os dois backends

O comportamento **difere** entre os backends. Essa é uma pendência conhecida.

#### DuckDB (CLI, backend padrão)

```
staging DataFrame ──upsert──▶ mart ──▶ testes de qualidade
```

1. Abre `data/mart/anp.duckdb` e cria a tabela se não existir.
2. Em transação: `INSERT ... ON CONFLICT (data_inicio, uf, produto) DO UPDATE`.
3. Roda os testes **sobre o mart**. Se reprovar, o job falha, mas **os dados
   já estão no mart**.

#### PostgreSQL (Airflow, ou CLI com `backend = "postgres"`)

```
staging DataFrame ──▶ staging.precos_semanais ──▶ testes ──(se aprovado)──▶ mart
```

Três funções independentes, ligadas apenas pelo `id_carga` (UUID):

**`carregar_staging(cfg, df, conexao) → id_carga`**
1. Gera um `id_carga` novo.
2. Insere em `controle.cargas` com status `iniciada`, o sha256, o arquivo e a
   URL de origem (lidos do manifesto) e `linhas_lidas`.
3. Em transação: `TRUNCATE staging.precos_semanais` + `COPY` das linhas.
   `NaN` e `pd.NA` viram `NULL` (senão o Postgres gravaria `'NaN'` como valor
   num `numeric`).

**`validar(cfg, conexao, id_carga) → resultados`**
1. Confere que a staging ainda pertence a esta carga (contagem de linhas com o
   `id_carga` = `linhas_lidas`). Isso protege contra outra carga ter
   sobrescrito a staging no meio do caminho.
2. Copia a staging para um **DuckDB em memória** e roda os testes de
   `qualidade.py` lá. Os testes estão escritos no dialeto do DuckDB (pendência:
   reescrever em SQL do Postgres).
3. Reprovação → status `falhou` com o motivo em `erro` e `ErroQualidade`. **O
   mart não é tocado** e a staging fica disponível para inspeção.

**`promover(conexao, id_carga, resultados) → linhas`**
1. Recusa a carga se o status não for `iniciada`.
2. Confere de novo que a staging é desta carga.
3. **Numa única transação:** upsert staging → mart e atualização do status para
   `concluida` (ou `concluida_com_avisos`, se houver algum ALERTA). Ou os dois
   são confirmados, ou nenhum.
4. Se der erro: rollback, e o status `falhou` é gravado numa transação separada.

**Detalhe técnico — autocommit.** A conexão é aberta com `autocommit=True`.
Assim cada bloco `conexao.transaction()` é um `BEGIN/COMMIT` real. Sem isso, o
psycopg abriria uma transação implícita e os blocos virariam savepoints que
nunca seriam confirmados. `_exigir_autocommit` garante essa condição.

**DDL.** `inicializar_banco()` executa os arquivos `sql/NNN_*.sql` em ordem
numérica, numa transação. Todos usam `IF NOT EXISTS`, então rodar de novo não
causa efeito. O DDL é um passo explícito: abrir conexão não o aplica.

### 3.4 Testes de qualidade (`qualidade.py`)

Os quatro testes **sempre rodam todos** antes de qualquer exceção, para o
relatório sair completo. Se algum bloqueante falhar, levanta `ErroQualidade`
com o detalhamento de cada falha.

| # | Teste | Regra | Severidade |
|---|---|---|---|
| 1 | `chave_primaria_sem_duplicata` | Nenhuma combinação `(data_inicio, uf, produto)` repetida | Bloqueia |
| 2 | `preco_medio_na_faixa` | `preco_medio` não nulo e entre R$ 1,00 e R$ 15,00 | Bloqueia |
| 3 | `cobertura_de_ufs` | Na semana avaliada, todas as 27 UFs presentes **por produto**, exceto as toleradas | Bloqueia |
| 4 | `variacao_semanal` | Variação do preço médio contra a semana anterior acima de 15% | Alerta |

**Semana avaliada:** a informada em `--semana` ou, por padrão, a mais recente
(`MAX(data_inicio)`). Os testes 1 e 2 olham a tabela inteira; os testes 3 e 4
olham só a semana avaliada.

**Allowlist de cobertura** (`[qualidade.ufs_ausentes_toleradas]`):

| Produto | UFs toleradas |
|---|---|
| GASOLINA C | nenhuma |
| ETANOL HIDRATADO | AP |

A allowlist é por produto: AP faltando na gasolina reprova, mesmo sendo
tolerado no etanol.

**Variação semanal com `LAG`.** A semana anterior é obtida com
`LAG(preco_medio) OVER (PARTITION BY uf, produto ORDER BY data_inicio)`, e não
com `data - 7 dias`. A série tem três buracos (ago/2015, ago–out/2020, set/2022),
e o `LAG` pega a semana anterior que existe. O resultado informa
`dias_desde_semana_anterior` para deixar o buraco visível.

**Erro ao executar um teste** (por exemplo, SQL inválido) não derruba os
demais: aquele teste vira `FALHOU` com a mensagem do erro.

---

## 4. Modelo de dados

### 4.1 `mart.fato_precos_semanais`

Mesmo grão, chave e ordem de colunas no DuckDB e no Postgres.

| Coluna | Postgres | DuckDB | Nulo | Descrição |
|---|---|---|---|---|
| `data_inicio` | date | DATE | não | Início da semana de coleta (PK) |
| `data_fim` | date | DATE | não | Fim da semana |
| `uf` | text | VARCHAR | não | Sigla da UF (PK) |
| `regiao` | text | VARCHAR | não | Região geográfica |
| `produto` | text | VARCHAR | não | `GASOLINA C` ou `ETANOL HIDRATADO` (PK) |
| `preco_medio` | numeric | DOUBLE | não | Preço médio de revenda, R$/l |
| `preco_min` | numeric | DOUBLE | sim | Menor preço encontrado |
| `preco_max` | numeric | DOUBLE | sim | Maior preço encontrado |
| `desvio_padrao` | numeric | DOUBLE | sim | Desvio padrão do preço |
| `numero_postos` | integer | INTEGER | sim | Postos pesquisados |
| `data_ingestao` | timestamp | TIMESTAMP | não | Momento da carga, em UTC |

**Chave primária:** `(data_inicio, uf, produto)`.

No Postgres os preços são `numeric`, e não `double precision`, para não
introduzir erro de ponto flutuante num valor que a ANP publica como decimal.

### 4.2 `staging.precos_semanais` (Postgres)

Mesmas colunas do mart, mais `id_carga uuid NOT NULL`. **De propósito, sem
chave primária e sem `NOT NULL` nas colunas de dados:** duplicata e nulo
precisam chegar aos testes de qualidade e ser reprovados por eles, com mensagem
clara, em vez de estourar como erro genérico no `INSERT`.

### 4.3 `controle.cargas` (Postgres)

| Coluna | Tipo | Descrição |
|---|---|---|
| `id_carga` | uuid (PK) | Identifica a execução e liga os três passos |
| `sha256` | text | Hash do arquivo raw de origem |
| `arquivo` | text | Nome do arquivo raw (nulo se veio de `--usar-raw`) |
| `url` | text | URL de origem |
| `status` | text | `iniciada`, `concluida`, `concluida_com_avisos` ou `falhou` |
| `linhas_lidas` | integer | Linhas gravadas na staging |
| `linhas_carregadas` | integer | Linhas afetadas pelo upsert no mart |
| `inicio` | timestamptz | Início da carga |
| `fim` | timestamptz | Fim da carga |
| `erro` | text | Motivo da falha |

Máquina de estados:

```
                ┌──────────────▶ concluida
                │
 iniciada ──────┼──────────────▶ concluida_com_avisos   (algum teste ALERTA)
                │
                └──────────────▶ falhou                  (erro ou teste bloqueante)
```

### 4.4 Volume atual

| Métrica | Valor (22/10/2026) |
|---|---|
| Linhas no mart | 38.134 |
| Período | 30/12/2012 a 11/10/2026 |
| UFs | 27 |
| Produtos | 2 |

Das combinações possíveis semana × UF × produto, faltam cerca de 205 na fonte:
180 são o etanol no Amapá (estrutural) e o restante são ausências episódicas
documentadas no README.

---

## 5. Orquestração

### 5.1 DAG `anp_precos_semanais` (Airflow)

```
inicializar_banco ─┐
                   ▼
               extrair ──▶ transformar ──▶ carregar_staging ──▶ validar ──▶ promover
```

| Propriedade | Valor |
|---|---|
| Agenda | `0 12 * * 3` (quarta, 12:00 UTC) |
| `start_date` | 2026-01-01 UTC |
| `catchup` | `False` (não roda semanas passadas) |
| `max_active_runs` | 1 (nunca duas execuções simultâneas) |
| Retries padrão | 0 |
| Estado inicial | pausado |
| Backend | sempre PostgreSQL, ignorando o `config.toml` |

**Comunicação entre tasks (XCom).** Só valores pequenos passam pelo XCom:

| Task | Devolve |
|---|---|
| `inicializar_banco` | lista de arquivos SQL aplicados |
| `extrair` | `{caminho_raw, sha256}` |
| `transformar` | `{caminho_staging, sha256}` |
| `carregar_staging` | `id_carga` |
| `validar` | lista de `{nome, status, mensagem}` |
| `promover` | número de linhas carregadas |

O DataFrame **não** passa pelo XCom: ele vai de `transformar` para
`carregar_staging` pelo arquivo Parquet, num volume Docker compartilhado
(`anp-data`).

**Política de retry:**

| Task | Retry | Motivo |
|---|---|---|
| `extrair` | 3×, backoff exponencial de 2 até 30 min | Falha típica é o gov.br fora do ar |
| `validar` | nenhum, e reprovação lança `AirflowFailException` | Reprovação é definitiva; repetir não muda o dado |
| demais | nenhum | Uma falha já grava `falhou`, e `promover` recusa carga que não esteja `iniciada` |

Os imports de `anp.*` ficam **dentro** das tasks: o dag-processor relê o
arquivo do DAG periodicamente, e assim pandas, duckdb e openpyxl só são
carregados na execução.

### 5.2 CLI (`python -m anp.pipeline`)

| Argumento | Efeito |
|---|---|
| `--config` | Outro `config.toml` |
| `--data-coleta AAAA-MM-DD` | Data usada no nome do arquivo raw |
| `--usar-raw CAMINHO` | Usa um XLSX local, sem baixar |
| `--semana AAAA-MM-DD` | Semana avaliada pelos testes 3 e 4 |
| `--log-nivel` | DEBUG, INFO, WARNING ou ERROR |

**Códigos de saída:**

| Código | Significado |
|---|---|
| 0 | Sucesso (alertas não reprovam; são impressos no stderr) |
| 1 | Falha de qualidade (`ErroQualidade`) |
| 2 | Falha operacional (download, esquema, config, banco) |

### 5.3 GitHub Actions

**`pipeline-semanal.yml`** — execução de produção, sem Airflow:
- Gatilhos: cron de quarta 12:00 UTC e disparo manual (com `semana` opcional).
- `concurrency: pipeline-anp`: nunca duas execuções ao mesmo tempo.
- Passos: `pytest` → pipeline (DuckDB) → gráfico → resumo no
  `GITHUB_STEP_SUMMARY` → artefatos (log, `.duckdb`, Parquet, PNG; 30 dias).

**`ci.yml`** — integração contínua, em push e PR para `main`:

| Job | O que verifica |
|---|---|
| `testes` | `pytest` sem Postgres e sem Airflow (esses testes são pulados) |
| `integracao-postgres` | Sobe um Postgres 16 como serviço, roda `pytest -m postgres` e **falha se algum teste for pulado** |
| `imagem-airflow` | Build da imagem, `pip check`, `tests/test_dag.py` dentro da imagem, sobe o Compose inteiro com segredos descartáveis e confere que o DAG carrega sem erro de import. O DAG não é disparado |

---

## 6. Infraestrutura (Docker Compose)

### 6.1 Serviços

| Serviço | Imagem | Papel | Memória |
|---|---|---|---|
| `postgres` | `postgres:16` | Banco `airflow` (metadados) e banco `anp` (dados) | 512 MB |
| `airflow-init` | `anp-airflow:3.3.2` | Migra metadados, cria o admin e encerra | 768 MB |
| `airflow-apiserver` | `anp-airflow:3.3.2` | Interface web e API em `localhost:8080` | 1 GB |
| `airflow-scheduler` | `anp-airflow:3.3.2` | Agenda e executa as tasks (LocalExecutor) | 2 GB |
| `airflow-dag-processor` | `anp-airflow:3.3.2` | Lê e serializa os DAGs | 768 MB |

Total: cerca de 5 GB, dimensionado para um servidor de 8 GB.

Ordem de subida: `postgres` (healthy) → `airflow-init` (concluído com sucesso)
→ os outros três.

Removidos do compose oficial por não serem necessários com LocalExecutor:
Celery, Redis, worker, Flower, triggerer e airflow-cli.

### 6.2 Imagem (`Dockerfile`)

Estende `apache/airflow:3.3.2-python3.12`, instala o `requirements.txt`
fixando o Airflow na mesma versão da imagem, e copia `config.toml`, `sql/`,
`src/` e `dags/` para `/opt/anp`, espelhando o layout do repositório. Por isso
`RAIZ_PROJETO` resolve os mesmos caminhos dentro e fora do container.

### 6.3 Volumes

| Volume | Montado em | Conteúdo |
|---|---|---|
| `postgres-db-volume` | `/var/lib/postgresql/data` | Dados do Postgres |
| `airflow-logs` | `/opt/airflow/logs` | Logs das tasks |
| `anp-data` | `/opt/anp/data` (só no scheduler) | Raw e staging |
| `./dags` (bind, somente leitura) | `/opt/anp/dags` | DAG editável sem rebuild |

Mudanças em `src/` ou `requirements.txt` exigem `docker compose build`.

### 6.4 Bancos e usuários

Criados por `docker/postgres/init/01-bancos.sh` **apenas na primeira subida**
(volume vazio):

| Banco | Dono | Observação |
|---|---|---|
| `airflow` | `airflow` | Metadados do Airflow |
| `anp` | `anp` | Dados do pipeline. O usuário tem `CREATEDB` porque os testes criam bancos temporários |

Ambos têm `REVOKE ALL ... FROM PUBLIC`.

### 6.5 Segredos e rede

- Todos os segredos vêm do `.env` (não versionado; modelo em `.env.example`).
  O Compose recusa subir se faltar algum (`${VAR:?}`).
- Só o scheduler recebe `DATABASE_URL` do banco `anp`, porque só ele executa
  tasks.
- O Postgres **não publica porta** no host. Para acessá-lo de fora, copie
  `compose.override.example.yaml`, que publica em `127.0.0.1:15432`.

---

## 7. Configuração (`config.toml`)

Nenhuma constante do pipeline fica no código.

| Seção | Conteúdo |
|---|---|
| `[fonte]` | URL, nome base do arquivo, timeouts |
| `[download]` | Tentativas, backoff, user-agent, tamanho mínimo |
| `[destino]` | `backend = "duckdb"` ou `"postgres"` |
| `[caminhos]` | raw, staging, mart, manifesto, gráficos |
| `[extracao]` | Aba, linha do cabeçalho (18), colunas esperadas, marcadores de nulo |
| `[escopo]` | Unidade esperada e mapa de produtos |
| `[qualidade]` | Faixa de preço, nº de UFs, limiar de variação, allowlist |
| `[ufs]` | Nome do estado → sigla (as 27 são obrigatórias) |
| `[grafico]` | UF em destaque, produto, janela em meses |

Credenciais nunca vão no `config.toml`: `DATABASE_URL` vem do ambiente ou do
`.env`.

---

## 8. Tratamento de erros

### 8.1 Hierarquia

```
ErroPipeline                  config inválido, DATABASE_URL ausente, staging inconsistente
├── ErroDownload              download falhou após todos os retries, HTML no lugar do XLSX, arquivo truncado
├── ErroEsquema               a planilha da ANP mudou de formato
└── ErroQualidade             teste bloqueante reprovou
```

Regra do projeto: **nenhum `except` silencioso**. Toda mensagem diz o que era
esperado, o que veio e o que ajustar.

### 8.2 Cenários de falha

| Cenário | Onde detecta | Resultado |
|---|---|---|
| gov.br fora do ar | `extrair` | Retry com backoff; depois `ErroDownload` |
| gov.br devolve HTML com HTTP 200 | `extrair` (assinatura ZIP) | `ErroDownload` apontando a URL do config |
| ANP adiciona linha de nota no topo | `validar_cabecalho` | `ErroEsquema`: "ajuste `linha_cabecalho` para 19" |
| ANP renomeia uma coluna | `validar_cabecalho` | `ErroEsquema` listando a coluna sumida e a nova |
| ANP renomeia um produto | `transformar` | `ErroEsquema` listando os rótulos presentes |
| Preço lido errado (ex.: 589 em vez de 5,89) | teste 2 | Reprovação; mart intacto (Postgres) |
| UF faltando na semana | teste 3 | Reprovação, a menos que esteja na allowlist |
| Alta de 20% no preço | teste 4 | Alerta; carga `concluida_com_avisos` |
| Duas cargas sobrescrevendo a staging | `_exigir_staging_da_carga` | `ErroPipeline`; a carga não é promovida |
| Erro no meio do upsert | `promover` | Rollback completo; status `falhou` |

---

## 9. Idempotência

Garantida em três níveis:

1. **Raw:** coleta com SHA-256 igual reaproveita o arquivo anterior, sem
   acumular cópias de 12 MB.
2. **Staging:** Parquet reescrito a cada execução; tabela de staging com
   `TRUNCATE`.
3. **Mart:** `INSERT ... ON CONFLICT (data_inicio, uf, produto) DO UPDATE`
   dentro de transação. Rodar N vezes seguidas produz 0 linhas novas e 0
   duplicatas; só `data_ingestao` e eventuais revisões da ANP mudam.

Evidência: as quatro cargas de 22/10/2026 em `controle.cargas` têm todas
38.134 linhas, e o mart continua com 38.134 linhas.

---

## 10. Observabilidade

**Logs em JSON Lines** (`log.py`): um evento por linha, com campos fixos `ts`,
`nivel`, `logger`, `mensagem` e `etapa`, mais o contexto de cada evento.

Principais eventos:

| Etapa | Eventos |
|---|---|
| extracao | `download_inicio`, `download_retry`, `download_ok`, `raw_gravado`, `raw_reutilizado` |
| transformacao | `esquema_ok`, `leitura_ok`, `escopo_filtrado`, `duracao_atipica`, `linhas_descartadas`, `transformacao_ok`, `staging_ok` |
| carga | `ddl_ok`, `staging_pg_ok`, `carga_ok`, `carga_falhou` |
| qualidade | `teste_qualidade`, `cobertura_tolerada`, `qualidade_resumo` |
| pipeline | `pipeline_inicio`, `pipeline_fim`, `pipeline_reprovado`, `pipeline_erro` |

O evento `carga_ok` informa linhas antes, depois, novas e atualizadas.

`scripts/resumo_execucao.py` lê esses eventos e monta a tabela de resultados
que aparece no resumo do GitHub Actions.

No Postgres, `controle.cargas` é o histórico consultável de todas as
execuções.

---

## 11. Testes automatizados

78 testes em `tests/`:

| Arquivo | Qtd. | Cobre |
|---|---|---|
| `test_transformar.py` | 20 | Validação de cabeçalho, mapeamentos, nulos, descartes |
| `test_qualidade.py` | 23 | Os quatro testes, allowlist, `LAG` com buracos na série |
| `test_carregar_postgres.py` | 15 | DDL idempotente, carga dupla, mart intacto em reprovação, rollback, staging sobrescrita, `numeric` exato, códigos de saída |
| `test_scripts.py` | 9 | Gráfico e resumo da execução |
| `test_carregar.py` | 6 | Upsert no DuckDB |
| `test_dag.py` | 5 | DagBag sem erro, ordem das tasks, agenda, retries, reprovação sem retry |

Marcadores e pulos:
- `@pytest.mark.postgres`: pulado sem `DATABASE_URL`. Quando roda, cria um
  **banco temporário** no mesmo servidor e o remove no final, sem tocar no
  banco configurado.
- `test_dag.py`: pulado sem Airflow instalado; roda dentro da imagem.

---

## 12. Dependências

| Pacote | Versão | Uso |
|---|---|---|
| pandas | 2.2.2 | Transformação |
| openpyxl | 3.1.5 | Leitura do XLSX e validação do cabeçalho |
| requests | 2.34.2 | Download |
| duckdb | 1.1.3 | Mart local e execução dos testes de qualidade |
| pyarrow | 18.1.0 | Parquet |
| psycopg[binary] | 3.3.5 | PostgreSQL |
| python-dotenv | 1.0.1 | Leitura do `.env` |
| matplotlib | 3.9.2 | Gráfico |
| pytest | 8.3.3 | Testes |
| apache-airflow | 3.3.2 | Orquestração (só na imagem) |

---

## 13. Limitações e pendências

| Tema | Situação |
|---|---|
| Backends diferentes | DuckDB valida depois de gravar no mart; Postgres valida antes |
| Testes em dialeto DuckDB | No Postgres, a staging é copiada para um DuckDB em memória para rodar os testes |
| pandas | `requirements.txt` fixa 2.2.2; a imagem do Airflow traz 3.0.5, e o build sobrescreve |
| Usuário com `CREATEDB` | Necessário para os testes; em produção, separar usuário de testes e de runtime |
| Valores nominais | Preços não são deflacionados; comparações longas incluem inflação |
| Qualidade só na semana avaliada | Uma anomalia antiga trazida por revisão retroativa da ANP não é detectada pelos testes 3 e 4 |
| Sem `ETag`/`Last-Modified` | É preciso baixar os 12 MB para saber se a ANP republicou |
| Escopo | Só gasolina C e etanol hidratado, só preço de revenda, granularidade estadual |
