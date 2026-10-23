-- Mesmo grao, mesma chave e mesma ordem de colunas da tabela do DuckDB
-- (src/anp/carregar.py). Precos em numeric, nao double precision: o valor
-- publicado pela ANP e decimal e nao deve ganhar erro de ponto flutuante.
-- data_ingestao segue sem fuso, em UTC, como no DuckDB.
CREATE TABLE IF NOT EXISTS mart.fato_precos_semanais (
    data_inicio    date       NOT NULL,
    data_fim       date       NOT NULL,
    uf             text       NOT NULL,
    regiao         text       NOT NULL,
    produto        text       NOT NULL,
    preco_medio    numeric    NOT NULL,
    preco_min      numeric,
    preco_max      numeric,
    desvio_padrao  numeric,
    numero_postos  integer,
    data_ingestao  timestamp  NOT NULL,
    PRIMARY KEY (data_inicio, uf, produto)
);
