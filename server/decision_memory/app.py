"""App ASGI: Streamable HTTP sem estado em /mcp, como pede MCP_TOOLS.md."""

from __future__ import annotations

import logging

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from . import config, db
from .server import build_server


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
    return server.streamable_http_app(stateless_http=True, json_response=True,
                                      transport_security=security)


def build_app(settings: config.Settings | None = None) -> Starlette:
    # Uma linha por evento no stdout: o Cloud Logging captura sem configuração.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = settings or config.load()
    pool = db.make_pool(settings.database_url, settings.database_password)
    server = build_server(pool, settings.expected_audiences)
    return build_http_app(server, on_cloud_run=settings.on_cloud_run)
