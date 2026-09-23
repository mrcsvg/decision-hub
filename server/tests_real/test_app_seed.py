from __future__ import annotations

import json
import shutil

import psycopg
import pytest

from decision_memory import seed

FIXTURES = seed.DEFAULT_FIXTURES


def _counts(conn):
    tables = ("person", "project", "evidence", "decision", "alternative", "learning",
              "review", "expectation", "provenance", "decision_evidence", "tag",
              "decision_tag", "evidence_tag", "learning_tag", "decision_learning",
              "evidence_learning")
    return {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()[0] for t in tables}


def test_carga_traz_o_corpus_inteiro(admin_conn):
    n = _counts(admin_conn)
    assert n["evidence"] == 12
    assert n["decision"] == 6
    assert n["learning"] == 4
    assert n["review"] == 5
    assert n["person"] >= 4


def test_carga_e_idempotente(admin_conn):
    antes = _counts(admin_conn)
    seed.load(admin_conn, FIXTURES, attested_by_email="ana@exemplo.com.br")
    assert _counts(admin_conn) == antes


def test_decisao_proposta_continua_sem_expectativa(admin_conn):
    row = admin_conn.execute(
        "SELECT d.state, (SELECT count(*) FROM expectation e WHERE e.decision_id = d.id) AS n "
        "FROM decision d WHERE d.id = %s",
        (seed.fid("dec-push-diario"),),
    ).fetchone()
    assert row == ("proposed", 0)


def test_evidencia_importada_nasce_atestada(admin_conn):
    sem_atestacao = admin_conn.execute(
        "SELECT count(*) FROM evidence e WHERE NOT EXISTS ("
        " SELECT 1 FROM provenance p WHERE p.object_type = 'evidence'"
        " AND p.object_id = e.id AND p.attested_at IS NOT NULL)"
    ).fetchone()[0]
    assert sem_atestacao == 0


def test_conflito_em_outra_chave_unica_levanta(admin_conn, tmp_path):
    # Fixture nova com o e-mail de uma pessoa existente: id novo, e-mail repetido.
    # A carga toda roda numa transação e é desfeita, então o banco não muda.
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    people = json.loads((fixtures / "people.json").read_text(encoding="utf-8"))
    people.append({"id": "p-ana-clone", "name": "Ana Clone", "email": "Ana@Exemplo.com.br "})
    (fixtures / "people.json").write_text(json.dumps(people), encoding="utf-8")

    antes = _counts(admin_conn)
    with pytest.raises(psycopg.errors.UniqueViolation):
        seed.load(admin_conn, fixtures, attested_by_email="ana@exemplo.com.br")
    assert _counts(admin_conn) == antes


def test_atestador_desconhecido_levanta_value_error(admin_conn):
    antes = _counts(admin_conn)
    with pytest.raises(ValueError, match="ninguem@exemplo.com.br"):
        seed.load(admin_conn, FIXTURES, attested_by_email="ninguem@exemplo.com.br")
    assert _counts(admin_conn) == antes


def test_csv_de_pessoas_normaliza_e_recusa_linha_vazia(tmp_path):
    ok = tmp_path / "pessoas.csv"
    ok.write_text("name,email\n Eva Lima , Eva@Exemplo.com \n", encoding="utf-8")
    assert seed.read_people(ok) == [("Eva Lima", "eva@exemplo.com")]

    ruim = tmp_path / "pessoas-ruim.csv"
    ruim.write_text("name,email\nEva Lima,eva@exemplo.com\nSem Email,\n", encoding="utf-8")
    with pytest.raises(ValueError, match="linha 3"):
        seed.read_people(ruim)


def _fixtures_com_tag(tmp_path, tag):
    """Cópia das fixtures com uma tag a mais na primeira evidência (mesmo id)."""
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, fixtures)
    evidence = json.loads((fixtures / "evidence.json").read_text(encoding="utf-8"))
    evidence[0]["tags"] = [*evidence[0].get("tags", []), tag]
    (fixtures / "evidence.json").write_text(json.dumps(evidence), encoding="utf-8")
    return fixtures, evidence[0]


def test_tags_das_fixtures_sao_normalizadas(admin_conn, tmp_path):
    # " CheckOut " crua violaria o CHECK de tag; dobrada, é a tag que já existe.
    fixtures, ev = _fixtures_com_tag(tmp_path, " CheckOut ")
    assert "checkout" in ev["tags"]
    antes = _counts(admin_conn)
    seed.load(admin_conn, fixtures, attested_by_email="ana@exemplo.com.br")
    assert _counts(admin_conn) == antes


def test_tag_fora_do_padrao_levanta_value_error(admin_conn, tmp_path):
    fixtures, ev = _fixtures_com_tag(tmp_path, "#growth")
    antes = _counts(admin_conn)
    with pytest.raises(ValueError, match=f"{ev['id']}.*#growth"):
        seed.load(admin_conn, fixtures, attested_by_email="ana@exemplo.com.br")
    assert _counts(admin_conn) == antes
