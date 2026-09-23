"""Quem está chamando.

Com o Cloud Run fechado (--no-allow-unauthenticated), a plataforma valida o ID
token do Google e repassa o header ao container SEM a assinatura
(https://docs.cloud.google.com/run/docs/troubleshooting). Não há como
revalidar: o servidor confia na validação da plataforma, lê as claims e confere
público e emissor. Isso só é seguro com o serviço fechado — ver README, "Deploy".

O público aceito é a URL do serviço (DM_EXPECTED_AUDIENCE) ou o cliente OAuth
do gcloud (GCLOUD_CLIENT_ID): é esse o `aud` do token de conta de usuário que
o `gcloud run services proxy` injeta.

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
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

# O logger do pacote: o "mcp" fica em WARNING (app.build_app), este não.
log = logging.getLogger("decision_memory")

_email: ContextVar[str | None] = ContextVar("dm_principal_email", default=None)
_client: ContextVar[str | None] = ContextVar("dm_client", default=None)

GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# Id público do cliente OAuth do gcloud CLI. O ID token que o gcloud emite para
# conta de usuário — o do `gcloud run services proxy` e o de `gcloud auth
# print-identity-token` — traz esse id como `aud` (e como `azp`), não a URL do
# serviço; sem ele na lista, toda pessoa pelo proxy ficaria sem identidade. O
# Cloud Run já conferiu o público na borda, antes de a requisição chegar aqui:
# esta checagem no servidor é defesa em profundidade, não a barreira.
GCLOUD_CLIENT_ID = "32555940559.apps.googleusercontent.com"


def _audience_ok(aud: object, audiences: tuple[str, ...]) -> bool:
    """`aud` de um JWT pode ser string ou lista; basta um elemento conferir.

    Com `audiences` configurado, vale também o cliente do gcloud.
    """
    if not audiences:
        return True
    accepted = (*audiences, GCLOUD_CLIENT_ID)
    if isinstance(aud, str):
        return aud in accepted
    if isinstance(aud, list):
        return any(isinstance(a, str) and a in accepted for a in aud)
    return False


# Quanto do aud/iss recusado vai ao log: são valores de quem chama.
MAX_LOGGED = 200


def _check(header: str | None, audiences: tuple[str, ...]) -> tuple[str | None, str | None, dict]:
    """(e-mail, motivo da recusa, claims). Sem header, nem e-mail nem motivo."""
    if header is None:
        return None, None, {}
    if not header.lower().startswith("bearer "):
        return None, "no_bearer", {}
    parts = header[7:].strip().split(".")
    if len(parts) < 2:
        return None, "unreadable", {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError):
        return None, "unreadable", {}
    if not isinstance(claims, dict):
        return None, "unreadable", {}
    if claims.get("iss") not in GOOGLE_ISSUERS:
        return None, "bad_iss", claims
    if not _audience_ok(claims.get("aud"), audiences):
        return None, "bad_aud", claims
    # Só False recusa: a claim falta em alguns tokens federados, e o header já
    # foi verificado pela plataforma.
    if claims.get("email_verified") is False:
        return None, "unverified", claims
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return None, "no_email", claims
    return email.strip().lower(), None, claims


def email_from_authorization(header: str | None, audiences: tuple[str, ...]) -> str | None:
    """E-mail do ID token no header, ou None se não houver identidade aceitável.

    Sem `audiences` configurado, o público não é conferido; com ele, vale
    também GCLOUD_CLIENT_ID. O emissor tem de ser o Google sempre.
    """
    return _check(header, audiences)[0]


def _clip(value: object) -> object:
    """aud/iss como vieram, cortados: texto, lista de textos ou só o nome do tipo."""
    if value is None:
        return None
    if isinstance(value, str):
        return value[:MAX_LOGGED]
    if isinstance(value, list):
        return [v[:MAX_LOGGED] if isinstance(v, str) else type(v).__name__ for v in value[:5]]
    return type(value).__name__


def _log_rejection(reason: str, header_name: str, claims: dict) -> None:
    """Uma linha JSON por recusa, para diagnosticar o deploy. Nunca o e-mail nem o
    token: só o motivo, o header e os valores de aud e iss que não serviram."""
    log.warning(json.dumps({"severity": "WARNING", "event": "identity_rejected",
                            "reason": reason, "header": header_name,
                            "iss": _clip(claims.get("iss")), "aud": _clip(claims.get("aud"))}))


def email_from_headers(headers: Mapping[str, str], audiences: tuple[str, ...]) -> str | None:
    """E-mail do header que o Cloud Run verificou.

    Com X-Serverless-Authorization presente, só ele conta; o Authorization é
    ignorado, porque nesse caso a plataforma não o conferiu. Header presente e
    recusado deixa uma linha `identity_rejected` no log; header ausente, não.
    """
    name = ("x-serverless-authorization" if headers.get("x-serverless-authorization") is not None
            else "authorization")
    email, reason, claims = _check(headers.get(name), audiences)
    if reason is not None:
        _log_rejection(reason, name, claims)
    return email


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
