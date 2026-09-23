"""App ASGI: Streamable HTTP sem estado em /mcp, como pede MCP_TOOLS.md."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from psycopg_pool import ConnectionPool
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config, db
from .server import build_server


MCP_PATH = "/mcp"


class PostOnly:
    """Em /mcp (e /mcp/), só POST; qualquer outro método recebe 405 `Allow: POST`.

    Sem estado, não há sessão: nada que o servidor possa empurrar por um fluxo
    SSE (GET), nem sessão a encerrar (DELETE). O SDK, porém, aceita o GET e
    abre um stream que nunca fecha — no Cloud Run isso prende a instância,
    ocupada e cobrada, até o timeout da requisição. A especificação do
    Streamable HTTP permite 405 quando o servidor não oferece esse fluxo. Os
    demais métodos não fazem sentido no transporte; respondem igual, com o
    header Allow que o 405 pede.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] == "http" and scope["method"] != "POST"
                and scope["path"].rstrip("/") == MCP_PATH):
            response = JSONResponse(
                {"jsonrpc": "2.0", "id": None,
                 "error": {"code": -32600,
                           "message": "Method Not Allowed: servidor sem estado, só POST."}},
                status_code=405, headers={"Allow": "POST"})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_http_app(server: MCPServer, *, on_cloud_run: bool) -> Starlette:
    """Sem estado e com resposta JSON: cada POST é uma requisição completa.

    Sem estado porque o Cloud Run distribui as requisições entre instâncias; o
    clientInfo do initialize, portanto, não chega às chamadas seguintes — o
    cliente é identificado pelo User-Agent de cada requisição.
    """
    # A proteção contra DNS rebinding recusa (421) Host fora de localhost. No
    # Cloud Run o Host é o domínio run.app e o serviço é fechado por IAM — um
    # navegador sem token não chega aqui —, então ela é desligada só lá.
    security = (TransportSecuritySettings(enable_dns_rebinding_protection=False)
                if on_cloud_run else None)
    app = server.streamable_http_app(stateless_http=True, json_response=True,
                                     transport_security=security)
    app.add_middleware(PostOnly)
    return app


def close_pool_on_shutdown(app: Starlette, pool: ConnectionPool) -> None:
    """Compõe o lifespan do SDK (que sobe o gerenciador de sessões) com o fechamento
    do pool, para as conexões saírem limpas quando o Cloud Run para a instância."""
    sdk_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: Starlette) -> AsyncIterator[Any]:
        try:
            async with sdk_lifespan(a) as state:
                yield state
        finally:
            pool.close()

    app.router.lifespan_context = lifespan


def build_app(settings: config.Settings | None = None) -> Starlette:
    # Uma linha por evento no stdout: o Cloud Logging captura sem configuração.
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    # O SDK registra em INFO o texto dos erros de ferramenta, que pode trazer dado
    # de quem chama; do SDK, só aviso e erro.
    logging.getLogger("mcp").setLevel(logging.WARNING)
    settings = settings or config.load()
    pool = db.make_pool(settings.database_url, settings.database_password)
    server = build_server(pool, settings.expected_audiences)
    app = build_http_app(server, on_cloud_run=settings.on_cloud_run)
    close_pool_on_shutdown(app, pool)
    app.state.pool = pool
    return app
