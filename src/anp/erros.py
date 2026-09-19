"""Excecoes do pipeline.

Cada erro carrega uma mensagem acionavel: o que se esperava, o que veio e de
onde. Nenhuma etapa do pipeline captura excecao de forma silenciosa.
"""


class ErroPipeline(Exception):
    """Raiz de todos os erros do pipeline."""


class ErroDownload(ErroPipeline):
    """Falha ao baixar o arquivo da ANP depois de esgotadas as tentativas."""


class ErroEsquema(ErroPipeline):
    """O arquivo da ANP nao tem o formato que o pipeline espera.

    Levantado quando o cabecalho muda, uma coluna some, um produto some ou a
    unidade de medida deixa de ser a esperada. E o erro que avisa que a ANP
    mexeu na planilha e o codigo precisa ser revisto.
    """


class ErroQualidade(ErroPipeline):
    """Um teste de qualidade bloqueante falhou apos a carga."""
