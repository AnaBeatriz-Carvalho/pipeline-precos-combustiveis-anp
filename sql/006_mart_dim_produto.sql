-- Dimensao de produto, uma linha por produto do escopo.
--
-- O conteudo vem de [[escopo.produtos]] no config.toml e e sincronizado por
-- upsert na promocao de cada carga, na mesma transacao do mart: produto que
-- entra no fato sempre tem linha aqui.
CREATE TABLE IF NOT EXISTS mart.dim_produto (
    produto     text  PRIMARY KEY,
    rotulo_anp  text  NOT NULL UNIQUE,
    unidade     text  NOT NULL,
    categoria   text  NOT NULL
        CHECK (categoria IN ('combustivel_veicular', 'glp'))
);
