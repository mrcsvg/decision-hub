from __future__ import annotations

import threading
import time

import pytest
from conftest import ANA, run

from decision_memory import writes
from decision_memory.identity import acting_as
from decision_memory.seed import fid
from decision_memory.server import build_server


@pytest.fixture(scope="module")
def server(pool):
    return build_server(pool)


def call(server, name, args, as_email=ANA):
    with acting_as(as_email):
        return run(server.call_tool(name, args))


# Títulos com "zeppelin" não casam com nenhuma consulta dos testes de leitura.
PROPOSTA = {
    "title": "Adotar entrega zeppelin no mesmo dia",
    "description": "Entregar no mesmo dia para pedidos até as 14h (teste-escrita).",
    "decider_email": ANA,
}


def proposta(server, title: str, **extra) -> str:
    res = call(server, "propose_decision", {**PROPOSTA, "title": title, **extra})
    return res.structured_content["data"]["decision_id"]


def count(admin_conn, sql: str, *params) -> int:
    return admin_conn.execute(sql, params).fetchone()[0]


def test_proposta_nasce_proposed_e_lista_o_que_falta(server, admin_conn):
    res = call(server, "propose_decision", PROPOSTA).structured_content
    data = res["data"]
    assert data["state"] == "proposed"
    assert data["attest_url"] is None
    assert set(data["missing"]) == {"alternatives", "evidence", "context"}
    assert len(res["pending"]["on_this_record"]) == 3
    kind, model, principal, ref, attested = admin_conn.execute(
        "SELECT pv.author_kind::text, pv.model, p.email, pv.source_ref, pv.attested_at "
        "FROM provenance pv JOIN person p ON p.id = pv.principal_person_id "
        "WHERE pv.object_type = 'decision' AND pv.object_id = %s", (data["decision_id"],)
    ).fetchone()
    assert (kind, model, principal) == ("agent", "unknown", ANA)
    assert ref == "mcp; client=tests"
    assert attested is None
    state = admin_conn.execute("SELECT state::text FROM decision WHERE id = %s",
                               (data["decision_id"],)).fetchone()[0]
    assert state == "proposed"


def test_proposta_completa_grava_alternativas_tags_e_evidencia(server, admin_conn):
    did = proposta(
        server, "Decisão zeppelin completa", context="Pedidos atrasavam na capital.",
        alternatives=[{"description": "Entrega em dois dias",
                       "rejection_reason": "Perde para a concorrência."}],
        tags=["  Teste-Escrita ", "", "ZEPPELIN", "zeppelin", "Conversão"],
        evidence=[{"evidence_id": str(fid("ev-checkout-confirm")), "role": "supports",
                   "weight": 0.5}],
    )
    tags = {r[0] for r in admin_conn.execute(
        "SELECT t.name FROM decision_tag x JOIN tag t ON t.id = x.tag_id "
        "WHERE x.decision_id = %s", (did,))}
    assert tags == {"teste-escrita", "zeppelin", "conversao"}
    assert count(admin_conn, "SELECT count(*) FROM alternative WHERE decision_id = %s", did) == 1
    role = admin_conn.execute("SELECT role::text FROM decision_evidence WHERE decision_id = %s",
                              (did,)).fetchone()[0]
    assert role == "supports"


def test_sem_identidade_nao_escreve(server, admin_conn):
    title = "Decisão zeppelin sem identidade"
    with pytest.raises(Exception) as excinfo:
        call(server, "propose_decision", {**PROPOSTA, "title": title}, as_email=None)
    assert "nada foi gravado" in str(excinfo.value)
    assert count(admin_conn, "SELECT count(*) FROM decision WHERE title = %s", title) == 0


def test_conta_nao_cadastrada_nao_escreve(server, admin_conn):
    title = "Decisão zeppelin de conta estranha"
    with pytest.raises(Exception) as excinfo:
        call(server, "propose_decision", {**PROPOSTA, "title": title},
             as_email="estranha@exemplo.com")
    assert "não está cadastrada" in str(excinfo.value)
    assert count(admin_conn, "SELECT count(*) FROM decision WHERE title = %s", title) == 0


def test_email_da_conta_casa_sem_caixa(server):
    """A identidade chega em minúsculas pela middleware, mas o banco pode não estar."""
    did = proposta(server, "Decisão zeppelin em caixa alta")
    with acting_as(ANA.upper()):
        res = run(server.call_tool("record_learning", {
            "summary": "Caixa do e-mail não muda quem é a pessoa zeppelin",
            "decision_ids": [did]}))
    assert res.structured_content["data"]["state"] == "proposed"


def test_decisor_desconhecido_nao_cria_registro(server, admin_conn):
    with pytest.raises(Exception) as excinfo:
        call(server, "propose_decision", {**PROPOSTA, "title": "Decisão zeppelin fantasma",
                                          "decider_email": "ninguem@exemplo.com"})
    assert str(excinfo.value).endswith(
        "Pessoa não encontrada. Confirme o e-mail com o usuário; o registro não foi criado.")
    assert count(admin_conn, "SELECT count(*) FROM decision "
                             "WHERE title = 'Decisão zeppelin fantasma'") == 0


def test_data_invalida_explica_o_formato(server):
    with pytest.raises(Exception) as excinfo:
        call(server, "propose_decision", {**PROPOSTA, "decided_on": "ontem"})
    assert "AAAA-MM-DD" in str(excinfo.value)


def test_erro_no_meio_desfaz_tudo(server, admin_conn):
    with pytest.raises(Exception) as excinfo:
        call(server, "propose_decision", {
            **PROPOSTA, "title": "Decisão zeppelin com evidência inválida",
            "tags": ["zeppelin-desfeito"],
            "alternatives": [{"description": "a", "rejection_reason": "b"}],
            "evidence": [{"evidence_id": "00000000-0000-0000-0000-000000000000",
                          "role": "supports"}]})
    assert "Evidência não encontrada" in str(excinfo.value)
    assert count(admin_conn, "SELECT count(*) FROM decision "
                             "WHERE title = 'Decisão zeppelin com evidência inválida'") == 0
    assert count(admin_conn, "SELECT count(*) FROM tag WHERE name = 'zeppelin-desfeito'") == 0


def test_idempotency_key_devolve_o_mesmo_registro(server, admin_conn):
    args = {**PROPOSTA, "title": "Decisão zeppelin idempotente", "idempotency_key": "k-123"}
    a = call(server, "propose_decision", args).structured_content["data"]
    b = call(server, "propose_decision", args).structured_content["data"]
    assert a["decision_id"] == b["decision_id"]
    assert count(admin_conn, "SELECT count(*) FROM decision "
                             "WHERE title = 'Decisão zeppelin idempotente'") == 1


def test_idempotency_key_concorrente_nao_duplica_nem_falha(server, admin_conn, monkeypatch):
    """Duas chamadas simultâneas, mesma pessoa e chave: um registro, mesma resposta.

    O atraso em `_slug` abre a janela entre conferir a chave e gravá-la; sem o
    advisory lock, as duas passariam pela conferência e a segunda cairia na
    chave primária de idempotency_key.
    """
    slug = writes._slug

    def slow_slug(conn, title):
        time.sleep(0.3)
        return slug(conn, title)

    monkeypatch.setattr(writes, "_slug", slow_slug)
    args = {**PROPOSTA, "title": "Decisão zeppelin concorrente", "idempotency_key": "k-corrida"}
    barrier = threading.Barrier(2)
    results: list = [None, None]

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results[i] = call(server, "propose_decision", args).structured_content["data"]
        except Exception as exc:  # noqa: BLE001 - o teste compara o que voltou
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert all(isinstance(r, dict) for r in results), results
    assert results[0]["decision_id"] == results[1]["decision_id"]
    assert count(admin_conn, "SELECT count(*) FROM decision "
                             "WHERE title = 'Decisão zeppelin concorrente'") == 1


def test_slug_repetido_ganha_sufixo(server):
    a = call(server, "propose_decision", {**PROPOSTA, "title": "Título zeppelin repetido"})
    b = call(server, "propose_decision", {**PROPOSTA, "title": "Título zeppelin repetido"})
    sa, sb = (r.structured_content["data"]["slug"] for r in (a, b))
    assert sa != sb
    assert sa.startswith("titulo-zeppelin-repetido") and sb.startswith("titulo-zeppelin-repetido-")


def test_evidencia_ja_existente_e_reaproveitada(server, admin_conn):
    did = proposta(server, "Decisão zeppelin para anexar")
    antes = count(admin_conn, "SELECT count(*) FROM evidence")
    data = call(server, "attach_evidence", {
        "decision_id": did, "role": "contradicts",
        "evidence": {"kind": "experiment", "title": "outro título", "strength": "causal",
                     "source_system": "internal.lab", "external_id": "exp-2025-0311"},
    }).structured_content["data"]
    assert data["reused_existing"] is True
    assert data["already_linked"] is False
    assert data["role"] == "contradicts"
    assert data["evidence_id"] == str(fid("ev-checkout-pagina-unica"))
    assert count(admin_conn, "SELECT count(*) FROM evidence") == antes


def test_evidencia_nova_de_agente_aparece_proposta_na_busca(server, admin_conn):
    did = proposta(server, "Decisão zeppelin com evidência nova")
    data = call(server, "attach_evidence", {
        "decision_id": did, "role": "supports",
        "evidence": {"kind": "analysis", "title": "Análise de entregas dirigivel",
                     "strength": "correlational"},
    }).structured_content
    assert data["data"]["state"] == "proposed"
    assert data["data"]["reused_existing"] is False
    assert data["pending"]["on_this_record"] == [
        "Só há evidência favorável registrada até agora: houve algo que contradisse a escolha?"]
    kind = admin_conn.execute(
        "SELECT author_kind::text FROM provenance WHERE object_type = 'evidence' "
        "AND object_id = %s", (data["data"]["evidence_id"],)).fetchone()[0]
    assert kind == "agent"
    busca = call(server, "search_evidence", {"query": "dirigivel"}).structured_content["data"]
    achados = {i["id"]: i["state"] for i in busca["items"]}
    assert achados[data["data"]["evidence_id"]] == "proposed"


def test_experimento_sem_origem_entra_por_agente(server):
    did = proposta(server, "Decisão zeppelin com experimento informal")
    data = call(server, "attach_evidence", {
        "decision_id": did, "role": "contradicts",
        "evidence": {"kind": "experiment", "title": "Teste zeppelin de corredor",
                     "strength": "anecdotal", "url": "https://exemplo.com/planilha"},
    }).structured_content["data"]
    assert data["state"] == "proposed"
    assert data["reused_existing"] is False


def test_vinculo_repetido_nao_muda_nada(server, admin_conn):
    did = str(fid("dec-remover-confirmacao"))
    eid = str(fid("ev-checkout-confirm"))
    data = call(server, "attach_evidence", {
        "decision_id": did, "role": "discarded", "evidence_id": eid, "weight": 0.1,
    }).structured_content["data"]
    res = call(server, "attach_evidence", {
        "decision_id": did, "role": "supports", "evidence_id": eid,
    }).structured_content
    assert res["pending"]["on_this_record"] == [], "vínculo repetido não cobra nada"
    assert data["already_linked"] is True
    assert data["role"] == "supports", "o papel original não pode ser trocado por agente"
    role, weight = admin_conn.execute(
        "SELECT role::text, weight FROM decision_evidence WHERE decision_id = %s "
        "AND evidence_id = %s", (did, eid)).fetchone()
    assert (role, float(weight)) == ("supports", 0.9)


@pytest.mark.parametrize("kind", ["experiment", "analysis"])
def test_identidade_de_origem_nova_nao_entra_por_agente(server, admin_conn, kind):
    """Qualquer tipo: (source_system, external_id) inédito só entra pela ingestão."""
    did = str(fid("dec-remover-confirmacao"))
    with pytest.raises(Exception) as excinfo:
        call(server, "attach_evidence", {
            "decision_id": did, "role": "supports",
            "evidence": {"kind": kind, "title": "x zeppelin", "strength": "causal",
                         "source_system": "growthbook", "external_id": f"inedito_{kind}"}})
    assert "ingestão" in str(excinfo.value)
    assert count(admin_conn, "SELECT count(*) FROM evidence "
                             "WHERE external_id = %s OR title = 'x zeppelin'", f"inedito_{kind}") == 0


def test_licao_precisa_de_origem(server):
    with pytest.raises(Exception) as excinfo:
        call(server, "record_learning", {"summary": "algo"})
    assert "origem" in str(excinfo.value)


def test_licao_nasce_proposta(server, admin_conn):
    did = proposta(server, "Decisão zeppelin que ensinou algo")
    eid = str(fid("ev-checkout-confirm"))
    res = call(server, "record_learning", {
        "summary": "Clientes aceitam frete zeppelin mais caro quando a entrega é no mesmo dia",
        "decision_ids": [did, did], "evidence_ids": [eid], "tags": ["Teste-Escrita"],
    }).structured_content
    data = res["data"]
    assert data["state"] == "proposed"
    assert data["linked_decisions"] == [did]
    assert data["linked_evidence"] == [eid]
    assert res["pending"]["on_this_record"] == []
    lid = data["learning_id"]
    state, recorded_on = admin_conn.execute(
        "SELECT state::text, recorded_on::text FROM learning WHERE id = %s", (lid,)).fetchone()
    assert (state, recorded_on) == ("proposed", "2026-09-22")
    assert count(admin_conn, "SELECT count(*) FROM provenance WHERE object_type = 'learning' "
                             "AND object_id = %s AND author_kind = 'agent'", lid) == 1
    assert count(admin_conn, "SELECT count(*) FROM learning_tag x JOIN tag t ON t.id = x.tag_id "
                             "WHERE x.learning_id = %s AND t.name = 'teste-escrita'", lid) == 1


def test_licao_curta_pede_condicao(server):
    did = proposta(server, "Decisão zeppelin com lição curta")
    res = call(server, "record_learning", {"summary": "zeppelin funciona",
                                           "decision_ids": [did]}).structured_content
    assert res["pending"]["on_this_record"] == [
        "Lição muito curta para viajar entre projetos: em que condição ela vale?"]


# ---------------------------------------------------------------- validação


def erro(server, name, args) -> str:
    """Mensagem do erro; nunca pode ser o erro interno genérico."""
    with pytest.raises(Exception) as excinfo:
        call(server, name, args)
    message = str(excinfo.value)
    assert "Erro interno" not in message, message
    return message


@pytest.fixture(scope="module")
def decisao(server):
    return proposta(server, "Decisão zeppelin para validar tipos")


@pytest.mark.parametrize("weight", ["0.5", True, 1.5, -0.1])
def test_weight_invalido_e_recusado(server, decisao, weight):
    message = erro(server, "attach_evidence", {
        "decision_id": decisao, "role": "supports",
        "evidence_id": str(fid("ev-checkout-confirm")), "weight": weight})
    assert "weight" in message


@pytest.mark.parametrize("weight", [0, 1])
def test_weight_nos_limites_e_aceito(server, weight):
    did = proposta(server, f"Decisão zeppelin com peso {weight}")
    data = call(server, "attach_evidence", {
        "decision_id": did, "role": "supports",
        "evidence_id": str(fid("ev-checkout-confirm")), "weight": weight,
    }).structured_content["data"]
    assert data["already_linked"] is False


@pytest.mark.parametrize(("evidence", "trecho"), [
    ({"kind": "rumor", "title": "t", "strength": "causal"}, "kind"),
    ({"kind": "analysis", "title": "t", "strength": "forte"}, "strength"),
    ({"kind": "analysis", "title": 3, "strength": "causal"}, "title"),
    ({"kind": "analysis", "title": "  ", "strength": "causal"}, "title"),
    ({"kind": "analysis", "title": "t", "strength": "causal", "url": ["x"]}, "url"),
    ({"kind": "analysis", "title": "t", "strength": "causal", "summary": 1}, "summary"),
    ({"kind": "analysis", "title": "t", "strength": "causal", "source_system": "x"},
     "external_id"),
    ({"kind": "analysis", "title": "t", "strength": "causal", "source_system": 1,
      "external_id": "x"}, "source_system"),
])
def test_evidencia_nova_malformada_e_recusada(server, decisao, evidence, trecho):
    message = erro(server, "attach_evidence", {
        "decision_id": decisao, "role": "supports", "evidence": evidence})
    assert trecho in message


def test_note_precisa_ser_texto(server, decisao):
    message = erro(server, "attach_evidence", {
        "decision_id": decisao, "role": "supports",
        "evidence_id": str(fid("ev-checkout-confirm")), "note": 7})
    assert "note" in message


def test_evidence_id_e_evidence_juntos_sao_recusados(server, decisao):
    message = erro(server, "attach_evidence", {
        "decision_id": decisao, "role": "supports",
        "evidence_id": str(fid("ev-checkout-confirm")),
        "evidence": {"kind": "analysis", "title": "t", "strength": "causal"}})
    assert "um dos dois" in message


def test_chaves_inesperadas_na_evidencia_sao_ignoradas(server, admin_conn, decisao):
    data = call(server, "attach_evidence", {
        "decision_id": decisao, "role": "contradicts",
        "evidence": {"kind": "experiment", "title": "Planilha zeppelin", "strength": "anecdotal",
                     "conformance_level": 2, "normalized": {"x": 1}, "imported_at": "2026-01-01"},
    }).structured_content["data"]
    row = admin_conn.execute(
        "SELECT conformance_level, normalized, imported_at, source_system FROM evidence "
        "WHERE id = %s", (data["evidence_id"],)).fetchone()
    assert tuple(row) == (None, None, None, None)


@pytest.mark.parametrize(("args", "trecho"), [
    ({"title": "   "}, "title"),
    ({"description": ""}, "description"),
    ({"alternatives": [{"description": "a", "rejection_reason": " "}]}, "rejection_reason"),
    ({"alternatives": [{"description": "a"}]}, "rejection_reason"),
    ({"evidence": [{"evidence_id": 3, "role": "supports"}]}, "evidence_id"),
    ({"evidence": [{"evidence_id": str(fid("ev-checkout-confirm")), "role": "apoia"}]}, "role"),
    ({"evidence": [{"evidence_id": str(fid("ev-checkout-confirm")), "role": "supports",
                    "weight": "alto"}]}, "weight"),
    ({"evidence": ["ev-checkout-confirm"]}, "evidence"),
    ({"tags": ["ok", 1]}, "tags"),
])
def test_proposta_malformada_e_recusada_sem_gravar(server, admin_conn, args, trecho):
    title = args.get("title", "Decisão zeppelin malformada")
    message = erro(server, "propose_decision", {**PROPOSTA, "title": title, **args})
    assert trecho in message
    assert count(admin_conn, "SELECT count(*) FROM decision "
                             "WHERE title = 'Decisão zeppelin malformada'") == 0


@pytest.mark.parametrize(("args", "trecho"), [
    ({"summary": "  "}, "summary"),
    ({"decision_ids": [1]}, "decision_ids"),
    ({"evidence_ids": "abc"}, "evidence_ids"),
])
def test_licao_malformada_e_recusada(server, decisao, args, trecho):
    message = erro(server, "record_learning",
                   {"summary": "Uma lição zeppelin", "decision_ids": [decisao], **args})
    assert trecho in message


# ---------------------------------------------------- cobrança e reaproveitamento


def test_favoravel_nao_cobra_contraria_quando_ela_ja_existe(server):
    did = proposta(server, "Decisão zeppelin já contestada", evidence=[
        {"evidence_id": str(fid("ev-checkout-pagina-unica")), "role": "contradicts"}])
    res = call(server, "attach_evidence", {
        "decision_id": did, "role": "supports",
        "evidence_id": str(fid("ev-checkout-confirm"))}).structured_content
    assert res["pending"]["on_this_record"] == []


def test_idempotencia_devolve_o_registro_gravado_nao_o_novo(server, admin_conn):
    args = {**PROPOSTA, "title": "Decisão zeppelin gravada primeiro",
            "context": "Havia atraso.", "idempotency_key": "k-gravado",
            "alternatives": [{"description": "a", "rejection_reason": "b"}]}
    a = call(server, "propose_decision", args).structured_content
    assert a["data"]["reused"] is False
    assert a["data"]["missing"] == ["evidence"]
    b = call(server, "propose_decision", {
        **PROPOSTA, "title": "Decisão zeppelin reenviada", "idempotency_key": "k-gravado"})
    data = b.structured_content["data"]
    assert data["reused"] is True
    assert data["decision_id"] == a["data"]["decision_id"]
    assert data["slug"] == a["data"]["slug"]
    assert data["state"] == "proposed"
    assert data["missing"] == ["evidence"], "o que falta é do registro gravado"
    assert b.structured_content["pending"]["on_this_record"] == [writes.PROMPTS["evidence"]]
    assert "não foi aplicado" in b.content[0].text
    assert count(admin_conn, "SELECT count(*) FROM decision "
                             "WHERE title = 'Decisão zeppelin reenviada'") == 0


def test_slug_nao_termina_em_hifen():
    """Corte em 80 caracteres logo antes de um espaço deixaria o hífen no fim."""
    title = "a" * 79 + " zeppelin"
    assert writes.slug_base(title) == "a" * 79
