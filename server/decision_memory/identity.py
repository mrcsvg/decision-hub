"""Quem está chamando.

Com o Cloud Run fechado (--no-allow-unauthenticated), a plataforma valida o ID
token do Google e repassa o header ao container SEM a assinatura
(https://docs.cloud.google.com/run/docs/troubleshooting). Não há como
revalidar: o servidor confia na validação da plataforma, lê as claims e confere
público e emissor. Isso só é seguro com o serviço fechado — ver README, "Deploy".

Qual header a plataforma validou: se vierem `X-Serverless-Authorization` e
`Authorization`, o Cloud Run confere SÓ o primeiro e repassa o segundo intacto
(https://docs.cloud.google.com/run/docs/authenticating/service-to-service).
Quem tem acesso ao serviço poderia então mandar o próprio token no
X-Serverless e um `Authorization` forjado, sem assinatura, com o e-mail de
outra pessoa. Por isso, havendo X-Serverless-Authorization, a identidade vem
só dele — e, se ele for ilegível, não há identidade; nunca se cai para o
Authorization.

A identidade viaja num ContextVar posto pela middleware antes de a ferramenta
rodar. Testes que chamam a ferramenta direto usam `acting_as`.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

_email: ContextVar[str | None] = ContextVar("dm_principal_email", default=None)
_client: ContextVar[str | None] = ContextVar("dm_client", default=None)

GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


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

    Sem `audiences` configurado, o público não é conferido. O emissor tem de
    ser o Google sempre.
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
    if claims.get("iss") not in GOOGLE_ISSUERS:
        return None
    if not _audience_ok(claims.get("aud"), audiences):
        return None
    # Só False recusa: a claim falta em alguns tokens federados, e o header já
    # foi verificado pela plataforma.
    if claims.get("email_verified") is False:
        return None
    email = claims.get("email")
    return email.strip().lower() if isinstance(email, str) and email.strip() else None


def email_from_headers(headers: Mapping[str, str], audiences: tuple[str, ...]) -> str | None:
    """E-mail do header que o Cloud Run verificou.

    Com X-Serverless-Authorization presente, só ele conta; o Authorization é
    ignorado, porque nesse caso a plataforma não o conferiu.
    """
    serverless = headers.get("x-serverless-authorization")
    if serverless is not None:
        return email_from_authorization(serverless, audiences)
    return email_from_authorization(headers.get("authorization"), audiences)


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
        email = email_from_headers(headers, audiences)
        # O cliente (User-Agent) é controlado por quem chama: serve só para
        # telemetria, nunca para autorização.
        with acting_as(email, headers.get("user-agent")):
            return await call_next(ctx)

    return resolve_identity
