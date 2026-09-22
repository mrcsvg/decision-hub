"""Entrada do stub: `python -m decision_memory_stub`.

Transporte stdio. MCP_TOOLS.md especifica Streamable HTTP com OAuth 2.1 para o
servidor de verdade; o stub usa stdio de propósito, para tirar instalação e
autenticação do caminho do experimento. Ver README, "Divergências".
"""

from __future__ import annotations

import asyncio

from .server import build_server


def main() -> None:
    asyncio.run(build_server().run_stdio_async())


if __name__ == "__main__":
    main()
