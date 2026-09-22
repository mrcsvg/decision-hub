"""Ambiente determinístico e harness de sessão MCP real.

Data fixa (as revisões vencidas do corpus dependem dela) e log de escrita
redirecionado para fora do repositório.

`client_session` sobe servidor e cliente sobre streams em memória e fala o
protocolo de verdade. É o único caminho que passa pela middleware — chamar
`MCPServer.call_tool()` direto a contorna —, então a recusa de expectativa só
pode ser testada por aqui.
"""

from __future__ import annotations

import functools
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["DECISION_MEMORY_STUB_TODAY"] = "2026-09-22"
os.environ.setdefault(
    "DECISION_MEMORY_STUB_LOG",
    str(Path(os.environ.get("TMPDIR", "/tmp")) / "decision-memory-stub-tests.jsonl"),
)


@asynccontextmanager
async def client_session(server):
    import anyio
    from mcp.client.session import ClientSession
    from mcp.shared.memory import create_client_server_memory_streams

    lowlevel = server._lowlevel_server
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as group:
            group.start_soon(
                functools.partial(
                    lowlevel.run,
                    server_streams[0],
                    server_streams[1],
                    lowlevel.create_initialization_options(),
                    raise_exceptions=False,
                )
            )
            async with ClientSession(*client_streams) as session:
                await session.initialize()
                yield session
            group.cancel_scope.cancel()
