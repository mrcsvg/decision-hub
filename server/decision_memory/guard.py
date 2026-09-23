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
    """Caminhos das chaves que carregam confiança ou expectativa, ordenados.

    Desce por dicionários e listas aninhados; olha só chaves, não valores. Chave
    no topo sai pelo nome (`confidence`); aninhada, pelo caminho
    (`meta.confidence`, `alternatives[0].expectativa`).
    """
    found: set[str] = set()

    def walk(node: Any, prefix: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                if any(term in fold(str(key)) for term in FORBIDDEN_ARGUMENT_TERMS):
                    found.add(path)
                walk(value, path)
        elif isinstance(node, list | tuple):
            for index, item in enumerate(node):
                walk(item, f"{prefix}[{index}]")

    if isinstance(arguments, Mapping):
        walk(arguments, "")
    return sorted(found)


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
