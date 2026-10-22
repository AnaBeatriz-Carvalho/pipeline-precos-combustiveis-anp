-- Uma linha por execucao da carga no PostgreSQL.
-- A linha nasce "iniciada" e termina em "concluida", "concluida_com_avisos"
-- (testes de qualidade com ALERTA) ou "falhou" (erro na carga ou teste de
-- qualidade bloqueante, com a mensagem em erro).
CREATE TABLE IF NOT EXISTS controle.cargas (
    id_carga           uuid         PRIMARY KEY,
    sha256             text         NOT NULL,
    arquivo            text,
    url                text,
    status             text         NOT NULL
        CHECK (status IN ('iniciada', 'concluida', 'concluida_com_avisos', 'falhou')),
    linhas_lidas       integer,
    linhas_carregadas  integer,
    inicio             timestamptz  NOT NULL DEFAULT now(),
    fim                timestamptz,
    erro               text
);
