-- Area de pouso da carga no PostgreSQL, reescrita a cada execucao
-- (TRUNCATE + INSERT). Os testes de qualidade rodam sobre ela antes de
-- qualquer escrita no mart; se reprovar, o conteudo fica aqui para inspecao.
--
-- Mesmas colunas do mart, mais id_carga, e de proposito sem chave primaria
-- e sem NOT NULL nos dados: chave duplicada ou preco ausente precisam chegar
-- aos testes de qualidade e ser reprovados por eles, nao estourar no INSERT.
CREATE SCHEMA IF NOT EXISTS staging;

CREATE TABLE IF NOT EXISTS staging.precos_semanais (
    id_carga       uuid       NOT NULL,
    data_inicio    date,
    data_fim       date,
    uf             text,
    regiao         text,
    produto        text,
    preco_medio    numeric,
    preco_min      numeric,
    preco_max      numeric,
    desvio_padrao  numeric,
    numero_postos  integer,
    data_ingestao  timestamp
);
