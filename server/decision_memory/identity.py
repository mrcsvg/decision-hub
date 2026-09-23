"""Quem está chamando.

Com o Cloud Run fechado (--no-allow-unauthenticated), a plataforma valida o ID
token do Google e repassa o header Authorization ao container SEM a assinatura
(https://docs.cloud.google.com/run/docs/troubleshooting). Não há como
revalidar: o servidor confia na validação da plataforma, lê as claims e confere
o público. Isso só é seguro com o serviço fechado — ver README, "Deploy".

A identidade viaja num ContextVar posto pela middleware antes de a ferramenta
rodar. Testes que chamam a ferramenta direto usam `acting_as`.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_email: ContextVar[str | None] = ContextVar("dm_principal_email", default=None)
_client: ContextVar[str | None] = ContextVar("dm_client", default=None)


def _audience_ok(aud: object, audiences: tuple[str, ...]) -> bool:
    """`aud` de um JWT pode ser string ou lista; basta um elemento conferir."""
    if not audiences:
        return True
    if isinstance(aud, str):
        return aud in audiences
    if isinstance(aud, list):
        return any(isinstance(a, str) and a in audiences for a in aud)
    return False


def email_from_authorization(header: str | None, audiences: tuple[str, ...]) -> str | None:
    """E-mail do ID token no header, ou None se não houver identidade aceitável.

    Sem `audiences` configurado, o público não é conferido.
    """
    if not header or not header.lower().startswith("bearer "):
        return None
    parts = header[7:].strip().split(".")
    if len(parts) < 2:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(claims, dict):
        return None
    if not _audience_ok(claims.get("aud"), audiences):
        return None
    if claims.get("email_verified") is False:
        return None
    email = claims.get("email")
    return email.strip().lower() if isinstance(email, str) and email.strip() else None


def current_email() -> str | None:
    return _email.get()


def current_client() -> str | None:
    return _client.get()


@contextmanager
def acting_as(email: str | None, client: str | None = "tests") -> Iterator[None]:
    """Põe a identidade para o bloco e restaura a anterior ao sair."""
    t1, t2 = _email.set(email), _client.set(client)
    try:
        yield
    finally:
        _email.reset(t1)
        _client.reset(t2)


def middleware(audiences: tuple[str, ...]):
    """Middleware do servidor MCP que resolve a identidade de cada requisição."""

    async def resolve_identity(ctx, call_next):
        request = getattr(ctx, "request", None)
        headers = request.headers if request is not None else {}
        email = email_from_authorization(headers.get("authorization"), audiences)
        with acting_as(email, headers.get("user-agent")):
            return await call_next(ctx)

    return resolve_identity
