"""Entrada: `python -m decision_memory`. Escuta em $PORT (Cloud Run) ou 8080."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import uvicorn

from .app import build_app


def uvicorn_options(environ: Mapping[str, str]) -> dict[str, Any]:
    options: dict[str, Any] = {"host": "0.0.0.0", "port": int(environ.get("PORT", "8080"))}
    if "K_SERVICE" in environ:
        # No Cloud Run só o front end do Google chega ao container, e é ele que
        # põe X-Forwarded-Proto/For: confiar neles mantém https nos redirects
        # (/mcp/ → /mcp) em vez de apontar para http.
        options["forwarded_allow_ips"] = "*"
    return options


def main() -> None:
    uvicorn.run(build_app(), **uvicorn_options(os.environ))


if __name__ == "__main__":
    main()
