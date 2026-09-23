from __future__ import annotations

import time

import psycopg
import pytest
from conftest import run
from mcp.server.mcpserver.exceptions import ToolError

from decision_memory import reads
from decision_memory.guard import fold
from decision_memory.identity import acting_as
from decision_memory.reads import terms
from decision_memory.seed import fid
from decision_memory.server import build_server


@pytest.fixture(scope="module")
def server(pool):
    return build_server(pool)


def call(server, name, args):
    return run(server.call_tool(name, args))


def test_busca_traz_a_evidencia_que_contradiz(server):
    res = call(server, "search_evidence", {"query": "checkout confirmação"})
    ids = [i["id"] for i in res.structured_content["data"]["items"]]
    assert str(fid("ev-checkout-confirm")) in ids
    assert str(fid("ev-checkout-pagina-unica")) in ids


def test_pergunta_em_linguagem_natural_encontra(server):
    res = call(server, "search_evidence", {"query": "já testamos tirar a confirmação do checkout?"})
    assert res.structured_content["data"]["items"]


def test_busca_sem_resultado_manda_dizer_isso_em_voz_alta(server):
    res = call(server, "search_evidence", {"query": "programa de fidelidade por pontos"})
    data = res.structured_content["data"]
    assert data["items"] == []
    assert "explicitamente" in data["note"]


NEGATE_EFFECT = (
    "UPDATE evidence SET normalized = jsonb_set(normalized, '{effect,point}', "
    "to_jsonb(-(normalized->'effect'->>'point')::numeric)) "
    "WHERE normalized->'effect'->>'point' IS NOT NULL"
)


def test_ordem_da_busca_nao_depende_do_efeito_nem_da_forca(server, admin_conn):
    """ADR 0003: ranquear por efeito compara grandezas de origens diferentes.

    Inverter o sinal de todo efeito inverte a ordem entre eles, e rebaixar a
    força só do primeiro colocado o tiraria do topo: se a busca olhasse para
    qualquer dos dois, a ordem mudaria.
    """
    consulta = {"query": "checkout conversão", "include": ["evidence"]}
    antes = _ids(server, consulta)
    com_efeito = [r[0] for r in admin_conn.execute(
        "SELECT id::text FROM evidence WHERE id::text = ANY(%s) "
        "AND normalized->'effect'->>'point' IS NOT NULL", (antes,))]
    pontos = {r[0] for r in admin_conn.execute(
        "SELECT normalized->'effect'->>'point' FROM evidence WHERE id::text = ANY(%s)",
        (com_efeito,))}
    assert len(com_efeito) >= 2 and len(pontos) >= 2, "o teste precisa de efeitos distintos"
    primeiro = antes[0]
    forca = admin_conn.execute("SELECT strength FROM evidence WHERE id = %s",
                               (primeiro,)).fetchone()[0]
    assert forca == "causal"
    admin_conn.execute(NEGATE_EFFECT)
    admin_conn.execute("UPDATE evidence SET strength = 'anecdotal' WHERE id = %s", (primeiro,))
    try:
        depois = _ids(server, consulta)
    finally:
        admin_conn.execute(NEGATE_EFFECT)
        admin_conn.execute("UPDATE evidence SET strength = %s WHERE id = %s", (forca, primeiro))
    assert antes == depois


def test_filtro_por_origem_e_por_tipo(server):
    res = call(server, "search_evidence",
               {"query": "checkout", "kinds": ["experiment"], "source_system": "growthbook"})
    itens = res.structured_content["data"]["items"]
    assert itens and all(i["source"]["system"] == "growthbook" for i in itens)


def test_evidencia_importada_aparece_atestada(server):
    res = call(server, "search_evidence", {"query": "checkout", "include": ["evidence"]})
    assert {i["state"] for i in res.structured_content["data"]["items"]} == {"attested"}


def test_decisao_atestada_traz_expectativa(server):
    data = call(server, "get_decision", {"slug": "remover-confirmacao-checkout"}).structured_content["data"]
    assert data["state"] == "attested"
    assert data["expectation"]["confidence"] == 0.7
    assert "contradicts" in {e["role"] for e in data["evidence"]}
    assert data["provenance"]["author_kind"] == "import"


def test_decisao_proposta_nao_traz_expectativa(server):
    data = call(server, "get_decision", {"id": str(fid("dec-push-diario"))}).structured_content["data"]
    assert data["state"] == "proposed"
    assert data["expectation"] is None


def test_decisao_inexistente_diz_o_que_fazer(server):
    with pytest.raises(Exception) as excinfo:
        call(server, "get_decision", {"id": "nao-existe"})
    assert "search_evidence" in str(excinfo.value)


def test_duas_revisoes_vencidas(server):
    data = call(server, "list_pending_reviews", {"overdue_only": True}).structured_content["data"]
    assert data["overdue_count"] == 2


def test_bloco_pending_nunca_esta_ausente(server):
    for name, args in [("search_evidence", {"query": "zzz inexistente"}),
                       ("get_decision", {"slug": "remover-confirmacao-checkout"}),
                       ("list_pending_reviews", {})]:
        assert "pending" in call(server, name, args).structured_content, name


def test_pending_prioriza_revisao_da_mesma_tag(server):
    res = call(server, "search_evidence", {"query": "onboarding"})
    due = res.structured_content["pending"]["reviews_due"]
    assert due[0]["decision_id"] == str(fid("dec-onboarding-tres-etapas"))


def test_termo_nunca_carrega_operador_de_tsquery():
    """Cada termo vai para to_tsquery: só letras e dígitos podem chegar lá."""
    for palavra in terms("checkout & !x | (y) 'aspas' a:b c*d <-> e\\f"):
        assert palavra.isalnum(), palavra


@pytest.mark.parametrize("consulta", [
    "checkout & !x | (y",
    "checkout' | 'x",
    "checkout:* & conversão:A",
    "checkout <-> confirmação",
    "'''",
    "::: *** !!!",
])
def test_operadores_de_tsquery_na_consulta_sao_inofensivos(server, consulta):
    res = call(server, "search_evidence", {"query": consulta})
    assert not res.is_error
    assert "data" in res.structured_content


def _ids(server, args):
    res = call(server, "search_evidence", args)
    return [i["id"] for i in res.structured_content["data"]["items"]]


def test_busca_ignora_acento_e_caixa(server):
    """Como no stub: consulta sem acento acha texto com acento, e vice-versa."""
    com_acento = _ids(server, {"query": "confirmação"})
    assert com_acento
    assert _ids(server, {"query": "confirmacao"}) == com_acento
    assert _ids(server, {"query": "CONFIRMAÇÃO"}) == com_acento


def test_termos_saem_sem_acento():
    assert terms("CONFIRMAÇÃO não Conversão") == ["confirmacao", "conversao"]


@pytest.mark.parametrize("collation", ["", ' COLLATE "C"'])
def test_dobra_no_banco_independe_da_collation(admin_conn, collation):
    """Com collation C, lower() não mexe em Ç nem Ã; a dobra tem de dar conta."""
    sql = reads.fold_sql(f"(%s::text{collation})")
    assert admin_conn.execute(f"SELECT {sql}", ("CONFIRMAÇÃO Ações ÚNICA",)).fetchone()[0] \
        == "confirmacao acoes unica"


def test_dobra_do_banco_e_do_python_concordam():
    assert len(reads.ACCENTED) == len(reads.PLAIN)
    for com, sem in zip(reads.ACCENTED, reads.PLAIN, strict=True):
        assert fold(com) == sem, com


def test_operadores_de_tsquery_nas_tags_sao_inofensivos(server):
    res = call(server, "search_evidence", {"query": "checkout", "tags": ["x & !y", "(z", "a:*"]})
    assert not res.is_error


@pytest.fixture
def evidencia_com_tag_composta(admin_conn):
    """Evidência cuja única ligação com "página única" é a tag hifenizada."""
    eid = admin_conn.execute(
        "INSERT INTO evidence (kind, title, summary) VALUES ('analysis', "
        "'Registro zzqk de teste', 'Texto sem as palavras da tag') RETURNING id::text"
    ).fetchone()[0]
    tid = admin_conn.execute(
        "INSERT INTO tag (name) VALUES ('pagina-unica') RETURNING id").fetchone()[0]
    admin_conn.execute("INSERT INTO evidence_tag VALUES (%s, %s)", (eid, tid))
    try:
        yield eid
    finally:
        admin_conn.execute("DELETE FROM evidence_tag WHERE evidence_id = %s", (eid,))
        admin_conn.execute("DELETE FROM evidence WHERE id = %s", (eid,))
        admin_conn.execute("DELETE FROM tag WHERE id = %s", (tid,))


def test_tag_composta_casa_por_palavra(server, evidencia_com_tag_composta):
    """Tag pode ter hífen ou espaço (spec): casa palavra a palavra, como no stub."""
    assert evidencia_com_tag_composta in _ids(server, {"query": "pagina unica"})


def test_filtro_por_tag_composta_e_pelo_nome_exato(server, evidencia_com_tag_composta):
    for tag in ("pagina-unica", " Página-Única "):
        assert _ids(server, {"query": "", "tags": [tag]}) == [evidencia_com_tag_composta], tag
    assert _ids(server, {"query": "", "tags": ["pagina"]}) == []


def test_consulta_enorme_usa_so_os_primeiros_termos(server):
    """10 mil palavras: só as MAX_TERMS primeiras contam, e responde rápido."""
    palavras = ["checkout", "confirmação"] + [f"zz{i:05d}" for i in range(10_000)]
    inicio = time.monotonic()
    enorme = _ids(server, {"query": " ".join(palavras)})
    assert time.monotonic() - inicio < 2
    assert enorme == _ids(server, {"query": " ".join(palavras[:reads.MAX_TERMS])})
    assert enorme


def test_id_vence_slug_quando_vem_os_dois(server):
    data = call(server, "get_decision", {"id": str(fid("dec-push-diario")),
                                         "slug": "remover-confirmacao-checkout"})
    assert data.structured_content["data"]["slug"] != "remover-confirmacao-checkout"
    assert data.structured_content["data"]["decision_id"] == str(fid("dec-push-diario"))


def test_dono_padrao_vem_da_identidade(server):
    with acting_as("carla@exemplo.com.br"):
        data = call(server, "list_pending_reviews", {}).structured_content["data"]
    assert [r["slug"] for r in data["reviews"]] == ["onboarding-tres-etapas"]
    with acting_as("ana@exemplo.com.br"):
        data = call(server, "list_pending_reviews", {}).structured_content["data"]
    assert {r["slug"] for r in data["reviews"]} == {"remover-confirmacao-checkout",
                                                    "manter-devolucao-30-dias"}


@pytest.mark.parametrize("erro", [psycopg.OperationalError("SELECT segredo FROM tabela_x"),
                                  RuntimeError("segredo no traceback")])
def test_erro_inesperado_vira_mensagem_generica(server, monkeypatch, caplog, erro):
    def explode(*args, **kwargs):
        raise erro

    monkeypatch.setattr(reads, "search", explode)
    with pytest.raises(ToolError) as excinfo:
        call(server, "search_evidence", {"query": "checkout"})
    mensagem = str(excinfo.value)
    assert "ref" in mensagem and "segredo" not in mensagem
    ref = mensagem.split("ref ")[1].split(")")[0]
    assert any(ref in r.getMessage() and type(erro).__name__ in r.getMessage()
               for r in caplog.records)


def test_resumo_em_texto_corta_a_consulta(server):
    consulta = "checkout " + "x" * 500
    res = call(server, "search_evidence", {"query": consulta})
    assert res.structured_content["data"]["query"] == consulta
    assert "x" * 121 not in res.content[0].text
