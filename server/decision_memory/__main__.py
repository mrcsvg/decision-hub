"""Entrada: `python -m decision_memory`. Escuta em $PORT (Cloud Run) ou 8080."""

from __future__ import annotations

import os

import uvicorn

from .app import build_app


def main() -> None:
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
