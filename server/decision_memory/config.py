"""Configuração por variáveis de ambiente, todas com prefixo DM_."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Settings:
    database_url: str
    database_password: str | None
    # Públicos aceitos no ID token (a URL do serviço). Vazio desliga a checagem.
    expected_audiences: tuple[str, ...]
    on_cloud_run: bool


def load() -> Settings:
    audiences = os.environ.get("DM_EXPECTED_AUDIENCE", "")
    return Settings(
        database_url=os.environ["DM_DATABASE_URL"],
        database_password=os.environ.get("DM_DATABASE_PASSWORD"),
        expected_audiences=tuple(a.strip() for a in audiences.split(",") if a.strip()),
        on_cloud_run="K_SERVICE" in os.environ,
    )


def today() -> date:
    """Data de referência. DM_TODAY deixa os testes determinísticos."""
    override = os.environ.get("DM_TODAY")
    return date.fromisoformat(override) if override else date.today()
