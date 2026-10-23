-- Escopo ampliado: margem, coeficientes de variacao e colunas de distribuicao.
--
-- ALTER TABLE ... ADD COLUMN IF NOT EXISTS, e nunca recriar a tabela: num banco
-- que ja tem dados do esquema antigo, as linhas ficam como estao e as colunas
-- novas nascem nulas, ate a carga seguinte preenche-las. Num banco novo, o 002
-- cria a tabela antiga e este arquivo a completa. Rodar de novo nao faz nada.
--
-- Todas aceitam nulo: as colunas de distribuicao e a margem so existem na fonte
-- ate 16/08/2020. Precos em numeric, como o resto do mart.
ALTER TABLE mart.fato_precos_semanais
    ADD COLUMN IF NOT EXISTS coef_variacao_revenda       numeric,
    ADD COLUMN IF NOT EXISTS margem_media_revenda        numeric,
    ADD COLUMN IF NOT EXISTS preco_medio_distribuicao    numeric,
    ADD COLUMN IF NOT EXISTS preco_min_distribuicao      numeric,
    ADD COLUMN IF NOT EXISTS preco_max_distribuicao      numeric,
    ADD COLUMN IF NOT EXISTS desvio_padrao_distribuicao  numeric,
    ADD COLUMN IF NOT EXISTS coef_variacao_distribuicao  numeric;

ALTER TABLE staging.precos_semanais
    ADD COLUMN IF NOT EXISTS coef_variacao_revenda       numeric,
    ADD COLUMN IF NOT EXISTS margem_media_revenda        numeric,
    ADD COLUMN IF NOT EXISTS preco_medio_distribuicao    numeric,
    ADD COLUMN IF NOT EXISTS preco_min_distribuicao      numeric,
    ADD COLUMN IF NOT EXISTS preco_max_distribuicao      numeric,
    ADD COLUMN IF NOT EXISTS desvio_padrao_distribuicao  numeric,
    ADD COLUMN IF NOT EXISTS coef_variacao_distribuicao  numeric;
