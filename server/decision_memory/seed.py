"""Carga do corpus de fixtures no Postgres. Idempotente.

Os ids são uuid5 do id da fixture, então a mesma fixture vira sempre o mesmo
registro e rodar de novo não duplica nada. Tudo entra com author_kind =
'import'; o que está atestado nas fixtures recebe attested_by/attested_at.

Uso (com o Cloud SQL Auth Proxy na porta 5433):

    PGPASSWORD=... python -m decision_memory.seed \\
        --database-url "host=127.0.0.1 port=5433 dbname=decision_memory user=dm_admin" \\
        --attested-by voce@exemplo.com --people pessoas.csv

`--people` é um CSV com cabeçalho `name,email`. Tem e-mail de gente real: não
versione.

Só o conflito de id é tolerado (é o que torna a carga idempotente). Colisão em
outra chave única — um e-mail já cadastrado com outro id, uma evidência com o
mesmo (source_system, external_id) — levanta erro e desfaz a carga inteira.
"""

from __future__ import annotations

import argparse
import csv
import json
import uuid
from pathlib import Path
from typing import Any

import psycopg
from mcp.server.mcpserver.exceptions import ToolError
from psycopg.types.json import Jsonb

from .writes import normalize_tags

NAMESPACE = uuid.UUID("7f1b0c3e-5d2a-4e8b-9c61-2a4f3e9d8b10")
DEFAULT_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def fid(fixture_id: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"fixtures:{fixture_id}")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def person_id(email: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"person:{normalize_email(email)}")


def read_people(path: Path) -> list[tuple[str, str]]:
    """Lê o CSV `name,email` de pessoas reais. Linha sem nome ou sem e-mail é erro."""
    people: list[tuple[str, str]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        # Linha 1 é o cabeçalho.
        for line, row in enumerate(csv.DictReader(handle), start=2):
            name = (row.get("name") or "").strip()
            email = normalize_email(row.get("email") or "")
            if not name or not email:
                raise ValueError(f"{path}, linha {line}: nome e e-mail são obrigatórios")
            people.append((name, email))
    return people


def _read(fixtures: Path, name: str) -> list[dict[str, Any]]:
    return json.loads((fixtures / f"{name}.json").read_text(encoding="utf-8"))


def _provenance(cur, object_type: str, object_id: uuid.UUID, fixture_id: str,
                attested_by: uuid.UUID | None) -> None:
    cur.execute(
        """
        INSERT INTO provenance (object_type, object_id, author_kind, source_ref,
                                attested_by, attested_at)
        SELECT %(t)s, %(o)s, 'import', %(ref)s, %(by)s,
               CASE WHEN %(by)s::uuid IS NULL THEN NULL ELSE now() END
         WHERE NOT EXISTS (SELECT 1 FROM provenance
                            WHERE object_type = %(t)s AND object_id = %(o)s
                              AND source_ref = %(ref)s)
        """,
        {"t": object_type, "o": object_id, "ref": f"fixtures:{fixture_id}", "by": attested_by},
    )


def _tags(cur, link_table: str, fk: str, object_id: uuid.UUID, fixture_id: str,
          names: list[str]) -> None:
    """Tags normalizadas como na escrita por agente (writes.normalize_tags): a
    mesma dobra, e fora do padrão do contrato é erro, que desfaz a carga."""
    try:
        names = normalize_tags(names)
    except ToolError as exc:
        raise ValueError(f"{fixture_id}: {exc}") from None
    for name in names:
        cur.execute("INSERT INTO tag (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))
        cur.execute(
            f"INSERT INTO {link_table} ({fk}, tag_id) SELECT %s, id FROM tag WHERE name = %s "
            "ON CONFLICT DO NOTHING",
            (object_id, name),
        )


def load(conn: psycopg.Connection, fixtures: Path = DEFAULT_FIXTURES, *,
         attested_by_email: str, extra_people: list[tuple[str, str]] = ()) -> None:
    with conn.transaction(), conn.cursor() as cur:
        for p in _read(fixtures, "people"):
            cur.execute(
                "INSERT INTO person (id, name, email) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (fid(p["id"]), p["name"], normalize_email(p["email"])),
            )
        for name, email in extra_people:
            cur.execute(
                "INSERT INTO person (id, name, email) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (person_id(email), name, normalize_email(email)),
            )
        row = cur.execute(
            "SELECT id FROM person WHERE lower(email) = lower(%s)", (attested_by_email,)
        ).fetchone()
        if row is None:
            raise ValueError(f"atestador {attested_by_email}: pessoa não cadastrada")
        attester = row[0]

        for p in _read(fixtures, "projects"):
            cur.execute(
                "INSERT INTO project (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (fid(p["id"]), p["name"]),
            )

        for e in _read(fixtures, "evidence"):
            cur.execute(
                """
                INSERT INTO evidence (id, kind, title, summary, url, source_system, external_id,
                                      strength, conformance_level, normalized, imported_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO NOTHING
                """,
                (fid(e["id"]), e["kind"], e["title"], e.get("summary"), e.get("url"),
                 e.get("source_system"), e.get("external_id"), e.get("strength"),
                 e.get("conformance_level"), Jsonb(e["record"]) if e.get("record") else None),
            )
            _tags(cur, "evidence_tag", "evidence_id", fid(e["id"]), e["id"], e.get("tags", []))
            _provenance(cur, "evidence", fid(e["id"]), e["id"], attester)

        decisions = _read(fixtures, "decisions")
        for d in decisions:
            did = fid(d["id"])
            cur.execute(
                """
                INSERT INTO decision (id, slug, title, context, description, door, decided_on,
                                      decider_person_id, project_id, state)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (did, d["slug"], d["title"], d.get("context"), d["description"], d["door"],
                 d["decided_on"], fid(d["decider"]),
                 fid(d["project"]) if d.get("project") else None, d["state"]),
            )
            for i, alt in enumerate(d.get("alternatives", [])):
                cur.execute(
                    "INSERT INTO alternative (id, decision_id, description, rejection_reason) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                    (fid(f"{d['id']}:alt:{i}"), did, alt["description"], alt["rejection_reason"]),
                )
            for link in d.get("evidence", []):
                cur.execute(
                    "INSERT INTO decision_evidence (decision_id, evidence_id, role, weight, note) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (did, fid(link["evidence_id"]), link["role"], link.get("weight"),
                     link.get("note")),
                )
            _tags(cur, "decision_tag", "decision_id", did, d["id"], d.get("tags", []))
            _provenance(cur, "decision", did, d["id"],
                        attester if d["state"] == "attested" else None)

        # Expectativas antes das revisões: o banco recusa expectativa depois de
        # revisão realizada. INSERT ... WHERE NOT EXISTS, e não ON CONFLICT, porque
        # o gatilho BEFORE INSERT dispara antes do teste de conflito — numa
        # segunda carga ele recusaria a linha que já existe.
        for d in decisions:
            x = d.get("expectation")
            if not x:
                continue
            cur.execute(
                """
                INSERT INTO expectation (decision_id, recorded_at, recorded_by, confidence,
                                         expected_metric, expected_magnitude, due_on)
                SELECT %(d)s, %(at)s, %(by)s, %(c)s, %(m)s, %(mag)s, %(due)s
                 WHERE NOT EXISTS (SELECT 1 FROM expectation
                                    WHERE decision_id = %(d)s AND recorded_at = %(at)s)
                """,
                {"d": fid(d["id"]), "at": x["recorded_at"], "by": fid(x["recorded_by"]),
                 "c": x["confidence"], "m": x["expected_metric"],
                 "mag": x["expected_magnitude"], "due": x["due_on"]},
            )

        for ln in _read(fixtures, "learnings"):
            lid = fid(ln["id"])
            cur.execute(
                "INSERT INTO learning (id, summary, recorded_on, state) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (lid, ln["summary"], ln["recorded_on"], ln["state"]),
            )
            for dec in ln.get("from_decisions", []):
                cur.execute(
                    "INSERT INTO decision_learning (decision_id, learning_id) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (fid(dec), lid),
                )
            for ev in ln.get("from_evidence", []):
                cur.execute(
                    "INSERT INTO evidence_learning (evidence_id, learning_id) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    (fid(ev), lid),
                )
            _tags(cur, "learning_tag", "learning_id", lid, ln["id"], ln.get("tags", []))
            _provenance(cur, "learning", lid, ln["id"],
                        attester if ln["state"] == "attested" else None)

        deciders = {fid(d["id"]): fid(d["decider"]) for d in decisions}
        for r in _read(fixtures, "reviews"):
            did = fid(r["decision_id"])
            cur.execute(
                "INSERT INTO review (id, decision_id, due_on, done_on, verdict, notes, reviewed_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (fid(r["id"]), did, r["due_on"], r.get("done_on"), r.get("verdict"),
                 r.get("notes"), deciders[did] if r.get("done_on") else None),
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--attested-by", required=True,
                        help="e-mail de quem atesta a importação; precisa estar cadastrado")
    parser.add_argument("--people", type=Path, help="CSV name,email com as pessoas reais")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    args = parser.parse_args()

    try:
        extra = read_people(args.people) if args.people else []
        with psycopg.connect(args.database_url) as conn:
            load(conn, args.fixtures, attested_by_email=args.attested_by, extra_people=extra)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    except psycopg.errors.UniqueViolation as exc:
        detail = exc.diag.message_detail or exc.diag.message_primary
        raise SystemExit(f"carga desfeita: conflito na chave única "
                         f"{exc.diag.constraint_name} ({detail})") from exc
    print(f"Carga concluída ({len(extra)} pessoa(s) além das fixtures).")


if __name__ == "__main__":
    main()
