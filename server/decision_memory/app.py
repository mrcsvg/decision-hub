"""App ASGI: Streamable HTTP sem estado em /mcp, como pede MCP_TOOLS.md."""

from __future__ import annotations

import logging

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config, db
from .server import build_server


MCP_PATH = "/mcp"


class PostOnly:
    """Responde 405 (`Allow: POST`) a GET e DELETE em /mcp; o resto segue intacto.

    Sem estado, não há sessão: nada que o servidor possa empurrar por um fluxo
    SSE, nem sessão a encerrar. O SDK, porém, aceita o GET e abre um stream que
    nunca fecha — no Cloud Run isso prende a instância, ocupada e cobrada, até
    o timeout da requisição. A especificação do Streamable HTTP permite 405
    quando o servidor não oferece esse fluxo. O DELETE o SDK já recusava com
    405, mas sem o header Allow; passa por aqui para responder igual.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] == "http" and scope["method"] in ("GET", "DELETE")
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


def build_app(settings: config.Settings | None = None) -> Starlette:
    # Uma linha por evento no stdout: o Cloud Logging captura sem configuração.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = settings or config.load()
    pool = db.make_pool(settings.database_url, settings.database_password)
    server = build_server(pool, settings.expected_audiences)
    return build_http_app(server, on_cloud_run=settings.on_cloud_run)
