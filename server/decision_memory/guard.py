"""Recusa confiança ou expectativa vinda de agente, sob qualquer nome.

Nenhuma ferramenta tem parâmetro assim; se chegar um, a resposta é recusa
explícita, como resultado de ferramenta com is_error — não erro de protocolo —
para o agente ler a mensagem e seguir sem o campo.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from typing import Any

import mcp.types as t

FORBIDDEN_ARGUMENT_TERMS = (
    "confidence",
    "confianca",
    "expectation",
    "expectativa",
    "expected_metric",
    "expected_magnitude",
    "resultado_esperado",
    "magnitude_esperada",
    "certeza",
)

EXPECTATION_REFUSAL = (
    "Expectativa é registrada pela pessoa na atestação, não pelo agente. Siga sem ela."
)


def fold(text: str) -> str:
    """Minúsculas e sem acento, para `confiança` casar com `confianca`."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def forbidden_keys(arguments: Any) -> list[str]:
    """Chaves de `arguments` que carregam confiança ou expectativa, ordenadas."""
    if not isinstance(arguments, Mapping):
        return []
    return sorted(
        str(key)
        for key in arguments
        if any(term in fold(str(key)) for term in FORBIDDEN_ARGUMENT_TERMS)
    )


async def refuse_expectation(ctx, call_next):
    """Middleware: recusa `tools/call` com argumento proibido antes da ferramenta."""
    if ctx.method == "tools/call" and isinstance(ctx.params, Mapping):
        offending = forbidden_keys(ctx.params.get("arguments") or {})
        if offending:
            text = " ".join(
                (
                    EXPECTATION_REFUSAL,
                    f"Campos recusados: {', '.join(offending)}.",
                    "Chame de novo sem eles.",
                )
            )
            return t.CallToolResult(
                is_error=True, content=[t.TextContent(type="text", text=text)]
            )
    return await call_next(ctx)
