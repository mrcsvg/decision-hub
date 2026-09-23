"""O caminho inteiro por HTTP: transporte, middlewares e ferramenta.

São os únicos testes que passam pela cadeia de middlewares de verdade — a
chamada em processo (`server.call_tool`) a pula. Cobrem identidade vinda do
header chegando à ferramenta síncrona (que roda noutra thread), as recusas de
expectativa e de \\x00, e a proteção contra DNS rebinding.
"""

from __future__ import annotations

import base64
import json
import logging
import threading

import pytest
from conftest import ANA
from starlette.testclient import TestClient

from decision_memory import __main__ as entry
from decision_memory.app import build_app, build_http_app
from decision_memory.config import Settings
from decision_memory.guard import EXPECTATION_REFUSAL, NUL_REFUSAL
from decision_memory.server import build_server

AUD = "https://decision-memory-teste.a.run.app"
ISS = "https://accounts.google.com"
CARLA = "carla@exemplo.com.br"
HEADERS = {"accept": "application/json, text/event-stream",
           "content-type": "application/json"}
USER_AGENT = "cliente-http-teste/1.0"
# Marca das propostas deste módulo, apagadas ao final para não mudar as
# contagens dos módulos que rodam depois (test_app_seed, por exemplo).
MARK = "Proposta de teste por HTTP (zeppelin)."


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def bearer(email: str, aud: str = AUD, iss: str = ISS) -> str:
    """ID token como o Cloud Run repassa: sem assinatura."""
    return f"Bearer {_b64({'alg': 'RS256'})}.{_b64({'email': email, 'aud': aud, 'iss': iss})}."


@pytest.fixture(scope="module")
def client(pool, admin_conn):
    app = build_http_app(build_server(pool, (AUD,)), on_cloud_run=False)
    with TestClient(app, base_url="http://localhost:8080") as c:
        yield c
    with admin_conn.transaction():
        ids = [r[0] for r in admin_conn.execute(
            "SELECT id FROM decision WHERE description = %s", (MARK,))]
        admin_conn.execute("DELETE FROM provenance WHERE object_type = 'decision' "
                           "AND object_id = ANY(%s)", (ids,))
        admin_conn.execute("DELETE FROM decision WHERE id = ANY(%s)", (ids,))


def post(client, method, params, **headers):
    return client.post("/mcp", headers={**HEADERS, **headers},
                       json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


def rpc(client, method, params, **headers):
    r = post(client, method, params, **headers)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def call(client, name, args, **headers):
    return rpc(client, "tools/call", {"name": name, "arguments": args}, **headers)


def text(res) -> str:
    return res["content"][0]["text"]


def count(admin_conn, sql: str, *params) -> int:
    return admin_conn.execute(sql, params).fetchone()[0]


def decisions_titled(admin_conn, title: str) -> int:
    return count(admin_conn, "SELECT count(*) FROM decision WHERE title = %s", title)


def proposal(title: str, **extra) -> dict:
    # "zeppelin" não casa com nenhuma consulta dos testes de leitura.
    return {"title": title, "description": MARK,
            "decider_email": ANA, **extra}


def principal_of(admin_conn, decision_id: str) -> tuple[str, str]:
    return admin_conn.execute(
        "SELECT p.email, pv.source_ref FROM provenance pv "
        "JOIN person p ON p.id = pv.principal_person_id "
        "WHERE pv.object_type = 'decision' AND pv.object_id = %s", (decision_id,)).fetchone()


def test_initialize(client):
    res = rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "teste", "version": "0"}})
    assert res["serverInfo"]["name"] == "decision-memory"


def test_lista_exatamente_as_oito_ferramentas(client):
    names = {tool["name"] for tool in rpc(client, "tools/list", {})["tools"]}
    assert names == {"search_evidence", "get_decision", "propose_decision",
                     "attach_evidence", "record_learning", "list_pending_reviews",
                     "get_topic_timeline", "find_related"}


def test_leitura_sem_identidade_funciona(client):
    res = call(client, "search_evidence", {"query": "checkout"})
    assert res["isError"] is False, res
    assert res["structuredContent"]["data"]["items"]


def test_escrita_com_token_grava_em_nome_da_pessoa(client, admin_conn):
    # A ferramenta é síncrona e roda numa thread do pool; se o ContextVar posto
    # pela middleware não chegasse lá, a escrita seria recusada por falta de conta.
    res = call(client, "propose_decision", proposal("Zeppelin via HTTP com token"),
               authorization=bearer(ANA), **{"user-agent": USER_AGENT})
    assert res["isError"] is False, res
    data = res["structuredContent"]["data"]
    assert data["state"] == "proposed"
    email, source_ref = principal_of(admin_conn, data["decision_id"])
    assert email == ANA
    assert source_ref == f"mcp; client={USER_AGENT}"


def test_x_serverless_prevalece_sobre_authorization_forjado(client, admin_conn):
    res = call(client, "propose_decision", proposal("Zeppelin com Authorization forjado"),
               authorization=bearer(CARLA), **{"x-serverless-authorization": bearer(ANA)})
    assert res["isError"] is False, res
    email, _ = principal_of(admin_conn, res["structuredContent"]["data"]["decision_id"])
    assert email == ANA


def test_x_serverless_ilegivel_nao_cai_para_authorization(client, admin_conn):
    title = "Zeppelin com X-Serverless ilegível"
    res = call(client, "propose_decision", proposal(title),
               authorization=bearer(ANA), **{"x-serverless-authorization": "Bearer lixo"})
    assert res["isError"] is True
    assert "nada foi gravado" in text(res)
    assert decisions_titled(admin_conn, title) == 0


def test_identidade_nao_vaza_para_a_chamada_seguinte(client, admin_conn):
    ok = call(client, "propose_decision", proposal("Zeppelin antes do anônimo"),
              authorization=bearer(ANA))
    assert ok["isError"] is False, ok
    title = "Zeppelin anônimo logo depois"
    res = call(client, "propose_decision", proposal(title))
    assert res["isError"] is True
    assert "nada foi gravado" in text(res)
    assert decisions_titled(admin_conn, title) == 0


def test_escrita_sem_token_e_recusada(client, admin_conn):
    title = "Zeppelin anônimo"
    res = call(client, "propose_decision", proposal(title))
    assert res["isError"] is True
    assert "nada foi gravado" in text(res)
    assert decisions_titled(admin_conn, title) == 0


@pytest.mark.parametrize("token", [
    bearer(ANA, aud="https://outro-servico.a.run.app"),
    bearer(ANA, iss="https://emissor-qualquer.example"),
], ids=["aud-errado", "iss-errado"])
def test_token_de_outro_publico_ou_emissor_e_recusado(client, admin_conn, token):
    title = "Zeppelin com token alheio"
    res = call(client, "propose_decision", proposal(title), authorization=token)
    assert res["isError"] is True
    assert "nada foi gravado" in text(res)
    assert decisions_titled(admin_conn, title) == 0


def test_agente_que_tenta_passar_confianca_e_recusado(client, admin_conn):
    before = count(admin_conn, "SELECT count(*) FROM learning")
    res = call(client, "record_learning",
               {"summary": "algo zeppelin", "decision_ids": ["x"], "confiança": 0.9},
               authorization=bearer(ANA))
    assert res["isError"] is True
    assert EXPECTATION_REFUSAL in text(res)
    assert "confiança" in text(res)
    assert count(admin_conn, "SELECT count(*) FROM learning") == before


def test_confianca_aninhada_tambem_e_recusada(client, admin_conn):
    title = "Zeppelin com confiança aninhada"
    res = call(client, "propose_decision",
               proposal(title, alternatives=[{"title": "B", "reason": "caro",
                                              "confidence": "alta"}]),
               authorization=bearer(ANA))
    assert res["isError"] is True
    assert EXPECTATION_REFUSAL in text(res)
    assert "alternatives[0].confidence" in text(res)
    assert decisions_titled(admin_conn, title) == 0


def test_caractere_nulo_e_recusado(client, admin_conn):
    res = call(client, "propose_decision", proposal("Zeppelin com \x00 nulo"),
               authorization=bearer(ANA))
    assert res["isError"] is True
    assert NUL_REFUSAL in text(res)
    assert "title" in text(res)
    # O título tem \x00, que o Postgres nem aceita como parâmetro: busca pelo padrão.
    assert count(admin_conn,
                 "SELECT count(*) FROM decision WHERE title LIKE 'Zeppelin com %%nulo'") == 0


def _request_with_deadline(client, method, seconds=5.0):
    """Faz a requisição numa thread; None se ela não voltar no prazo."""
    box = {}
    worker = threading.Thread(
        target=lambda: box.update(r=client.request(method, "/mcp", headers=HEADERS)),
        daemon=True)
    worker.start()
    worker.join(seconds)
    return box.get("r")


@pytest.mark.parametrize("method", ["GET", "DELETE", "HEAD", "OPTIONS", "PUT", "PATCH"])
def test_so_post_em_mcp_o_resto_e_405_sem_pendurar(client, method):
    # Sem sessão não há fluxo SSE do servidor nem sessão a encerrar. Antes, o GET
    # abria um stream que nunca fechava; os outros métodos só servem ao POST.
    r = _request_with_deadline(client, method)
    assert r is not None, f"{method} /mcp não respondeu em 5 s"
    assert r.status_code == 405
    assert r.headers["allow"] == "POST"


def test_barra_final_tambem_e_so_post(client):
    r = client.request("GET", "/mcp/", headers=HEADERS, follow_redirects=False)
    assert r.status_code == 405
    assert r.headers["allow"] == "POST"


def test_post_continua_funcionando_depois_do_405(client):
    assert _request_with_deadline(client, "GET").status_code == 405
    assert len(rpc(client, "tools/list", {})["tools"]) == 8


def test_host_fora_de_localhost_e_recusado_fora_do_cloud_run(client):
    r = post(client, "tools/list", {}, host="decision-memory-teste.a.run.app")
    assert r.status_code == 421


def test_no_cloud_run_host_run_app_e_aceito(pool):
    # Outro servidor: o gerenciador de sessões de um app só sobe uma vez.
    app = build_http_app(build_server(pool, (AUD,)), on_cloud_run=True)
    with TestClient(app, base_url="https://decision-memory-teste.a.run.app") as c:
        r = post(c, "tools/list", {})
    assert r.status_code == 200, r.text
    assert len(r.json()["result"]["tools"]) == 8


def test_build_app_fecha_o_pool_ao_desligar(app_url):
    app = build_app(Settings(database_url=app_url, database_password=None,
                             expected_audiences=(AUD,), on_cloud_run=False))
    with TestClient(app, base_url="http://localhost:8080") as c:
        assert not app.state.pool.closed
        assert len(rpc(c, "tools/list", {})["tools"]) == 8
    assert app.state.pool.closed


def test_build_app_cala_o_log_do_sdk_abaixo_de_warning(app_url):
    # O SDK registra em INFO o texto de erro das ferramentas.
    app = build_app(Settings(database_url=app_url, database_password=None,
                             expected_audiences=(), on_cloud_run=False))
    app.state.pool.close()
    assert logging.getLogger("mcp").level == logging.WARNING


def test_no_cloud_run_uvicorn_confia_no_proxy():
    assert entry.uvicorn_options({"K_SERVICE": "decision-memory", "PORT": "9000"}) == {
        "host": "0.0.0.0", "port": 9000, "forwarded_allow_ips": "*"}
    assert entry.uvicorn_options({}) == {"host": "0.0.0.0", "port": 8080}
