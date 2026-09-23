from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import mcp.types as t
from conftest import run

from decision_memory.guard import forbidden_keys, nul_paths, refuse_nul
from decision_memory.identity import (
    GCLOUD_CLIENT_ID,
    acting_as,
    current_client,
    current_email,
    email_from_authorization,
    email_from_headers,
)


def token(claims: dict, iss: str | None = "https://accounts.google.com") -> str:
    """ID token como o Cloud Run entrega ao container: sem assinatura."""
    if iss is not None:
        claims = {"iss": iss, **claims}
    enc = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")  # noqa: E731
    return f"Bearer {enc({'alg': 'RS256'})}.{enc(claims)}."


AUD = "https://decision-memory-abc-rj.a.run.app"


def test_le_o_email_do_token():
    assert (
        email_from_authorization(token({"email": "Ana@Exemplo.com.br", "aud": AUD}), (AUD,))
        == "ana@exemplo.com.br"
    )


def test_sem_header_nao_ha_identidade():
    assert email_from_authorization(None, (AUD,)) is None
    assert email_from_authorization("Basic xyz", (AUD,)) is None
    assert email_from_authorization("Bearer lixo", (AUD,)) is None


def test_payload_ilegivel_nao_ha_identidade():
    assert email_from_authorization("Bearer a.%%%.b", (AUD,)) is None
    lista = base64.urlsafe_b64encode(b'["a@b.c"]').decode().rstrip("=")
    assert email_from_authorization(f"Bearer a.{lista}.", (AUD,)) is None


def test_publico_errado_nao_ha_identidade():
    assert email_from_authorization(token({"email": "a@b.c", "aud": "https://outro"}), (AUD,)) is None


def test_publico_do_cliente_do_gcloud_e_aceito():
    """O token que o gcloud emite para conta de usuário (proxy, print-identity-token)
    traz como aud o id do cliente OAuth do próprio gcloud, não a URL do serviço."""
    assert GCLOUD_CLIENT_ID == "32555940559.apps.googleusercontent.com"
    claims = {"email": "ana@x.com", "aud": GCLOUD_CLIENT_ID, "azp": GCLOUD_CLIENT_ID,
              "email_verified": True}
    assert email_from_authorization(token(claims), (AUD,)) == "ana@x.com"
    assert email_from_headers({"authorization": token(claims)}, (AUD,)) == "ana@x.com"


def test_outro_cliente_oauth_nao_e_aceito():
    claims = {"email": "ana@x.com", "aud": "123-outro.apps.googleusercontent.com"}
    assert email_from_authorization(token(claims), (AUD,)) is None


def test_publico_em_lista():
    assert (
        email_from_authorization(token({"email": "a@b.c", "aud": ["https://outro", AUD]}), (AUD,))
        == "a@b.c"
    )
    assert email_from_authorization(token({"email": "a@b.c", "aud": ["https://outro"]}), (AUD,)) is None
    assert email_from_authorization(token({"email": "a@b.c", "aud": []}), (AUD,)) is None


def test_sem_publico_configurado_nao_confere():
    assert email_from_authorization(token({"email": "a@b.c", "aud": "x"}), ()) == "a@b.c"


def test_email_nao_verificado_nao_vale():
    claims = {"email": "a@b.c", "aud": AUD, "email_verified": False}
    assert email_from_authorization(token(claims), (AUD,)) is None


def test_emissor_do_google_nas_duas_grafias():
    for iss in ("accounts.google.com", "https://accounts.google.com"):
        assert email_from_authorization(token({"email": "a@b.c", "aud": AUD}, iss), (AUD,)) == "a@b.c"


def test_emissor_estranho_ou_ausente_nao_ha_identidade():
    assert email_from_authorization(token({"email": "a@b.c", "aud": AUD}, "https://evil"), (AUD,)) is None
    assert email_from_authorization(token({"email": "a@b.c", "aud": AUD}, None), (AUD,)) is None


def test_email_verified_ausente_vale():
    assert email_from_authorization(token({"email": "a@b.c", "aud": AUD}), (AUD,)) == "a@b.c"


def test_so_authorization():
    headers = {"authorization": token({"email": "ana@x.com", "aud": AUD})}
    assert email_from_headers(headers, (AUD,)) == "ana@x.com"


def test_so_x_serverless_authorization():
    headers = {"x-serverless-authorization": token({"email": "ana@x.com", "aud": AUD})}
    assert email_from_headers(headers, (AUD,)) == "ana@x.com"


def test_com_os_dois_vale_o_que_o_cloud_run_verificou():
    """Com os dois, o Cloud Run só confere o X-Serverless; o Authorization pode ser forjado."""
    headers = {
        "x-serverless-authorization": token({"email": "ana@x.com", "aud": AUD}),
        "authorization": token({"email": "vitima@x.com", "aud": AUD}),
    }
    assert email_from_headers(headers, (AUD,)) == "ana@x.com"


def test_x_serverless_ilegivel_nao_cai_para_authorization():
    headers = {
        "x-serverless-authorization": "Bearer lixo",
        "authorization": token({"email": "vitima@x.com", "aud": AUD}),
    }
    assert email_from_headers(headers, (AUD,)) is None


def test_sem_headers_nao_ha_identidade():
    assert email_from_headers({}, (AUD,)) is None


def test_acting_as_poe_e_restaura_identidade():
    assert current_email() is None and current_client() is None
    with acting_as("ana@x.com", "claude-code"):
        assert (current_email(), current_client()) == ("ana@x.com", "claude-code")
        with acting_as("bia@x.com"):
            assert (current_email(), current_client()) == ("bia@x.com", "tests")
        assert (current_email(), current_client()) == ("ana@x.com", "claude-code")
    assert current_email() is None and current_client() is None


def test_campos_de_expectativa_sao_detectados():
    args = {
        "decision_id": "d1",
        "confidence": 0.8,
        "confiança": "alta",
        "resultado_esperado": "sobe",
        "Expectativa": "x",
    }
    assert forbidden_keys(args) == sorted(
        ["confidence", "confiança", "resultado_esperado", "Expectativa"]
    )


def test_campos_aninhados_sao_detectados():
    assert forbidden_keys({"meta": {"confidence": 0.8}}) == ["meta.confidence"]
    assert forbidden_keys({"alternatives": [{"expectativa": "x"}, {"ok": 1}]}) == [
        "alternatives[0].expectativa"
    ]
    # Só chaves: valor com o termo não é recusado.
    assert forbidden_keys({"summary": "alta confiança", "tags": ["confidence"]}) == []


def test_argumentos_limpos_ou_nao_dict_passam():
    assert forbidden_keys({"decision_id": "d1", "query": "preço"}) == []
    assert forbidden_keys(None) == []
    assert forbidden_keys(["confidence"]) == []
    assert forbidden_keys("confidence") == []


def _through(middleware, arguments):
    ctx = SimpleNamespace(method="tools/call", params={"name": "x", "arguments": arguments})

    async def call_next(_ctx):
        return "chamou a ferramenta"

    return run(middleware(ctx, call_next))


def test_caractere_nulo_e_recusado_antes_da_ferramenta():
    for args in ({"query": "a\x00b"}, {"tags": ["ok", "x\x00"]},
                 {"meta": {"nota": "\x00"}}, {"chave\x00": "valor"}):
        res = _through(refuse_nul, args)
        assert isinstance(res, t.CallToolResult) and res.is_error, args
        assert "\\x00" in res.content[0].text and "Nada foi gravado" in res.content[0].text


def test_sem_caractere_nulo_segue_para_a_ferramenta():
    assert _through(refuse_nul, {"query": "checkout", "limit": 3, "tags": None}) \
        == "chamou a ferramenta"
    assert nul_paths({"a": ["x", {"b": "y\x00"}]}) == ["a[1].b"]
