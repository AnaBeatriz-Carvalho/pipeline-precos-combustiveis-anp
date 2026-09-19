"""Logging estruturado em JSON Lines.

Cada etapa emite eventos com campos fixos (etapa, evento) mais o contexto
relevante. Saida em uma linha JSON por evento, legivel tanto no terminal
quanto no log do GitHub Actions.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_CAMPOS_PADRAO = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)


class FormatadorJson(logging.Formatter):
    """Serializa o LogRecord como JSON, incluindo campos extras."""

    def format(self, record: logging.LogRecord) -> str:
        evento: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "nivel": record.levelname,
            "logger": record.name,
            "mensagem": record.getMessage(),
        }
        for chave, valor in record.__dict__.items():
            if chave not in _CAMPOS_PADRAO and chave != "taskName":
                evento[chave] = valor
        if record.exc_info:
            evento["excecao"] = self.formatException(record.exc_info)
        return json.dumps(evento, ensure_ascii=False, default=str)


def configurar_logging(nivel: str = "INFO") -> None:
    """Instala o formatador JSON no logger raiz. Idempotente."""
    raiz = logging.getLogger()
    raiz.setLevel(nivel)
    for handler in list(raiz.handlers):
        raiz.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(FormatadorJson())
    raiz.addHandler(handler)


class _AdaptadorEtapa(logging.LoggerAdapter):
    """Mescla o contexto da etapa com o extra de cada chamada.

    O LoggerAdapter padrao substitui o extra da chamada pelo do adaptador; aqui
    os dois convivem, com a chamada tendo precedencia.
    """

    def process(self, msg, kwargs):
        extra = dict(self.extra or {})
        extra.update(kwargs.get("extra") or {})
        kwargs["extra"] = extra
        return msg, kwargs


def obter_logger(etapa: str) -> logging.LoggerAdapter:
    """Logger que carimba a etapa do pipeline em todo evento."""
    return _AdaptadorEtapa(logging.getLogger(f"anp.{etapa}"), {"etapa": etapa})
