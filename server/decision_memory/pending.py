"""O bloco `pending`: o lembrete que viaja junto de toda resposta.

É a mitigação nº 1 do ADR 0002: o servidor não controla quando é chamado, então
o lembrete vai na resposta. Curto, específico e ligado ao que acabou de
acontecer. Vem vazio, nunca ausente.
"""

from __future__ import annotations

from typing import Any

from .config import today
from .db import evidence_attested
from .models import Pending, ReviewDue

MAX_REVIEWS_DUE = 3

# Evidência não tem coluna de estado: a regra do ADR 0005 está em db.py.
UNATTESTED_COUNT_SQL = f"""
SELECT (SELECT count(*) FROM decision WHERE state = 'proposed')
     + (SELECT count(*) FROM learning WHERE state = 'proposed')
     + (SELECT count(*) FROM evidence e WHERE NOT {evidence_attested('e.id')}) AS n
"""

# Só revisões já vencidas: as que ainda não venceram não são cobrança, são ruído.
OVERDUE_SQL = """
SELECT d.id::text AS decision_id, d.title, r.due_on,
       ARRAY(SELECT t.name FROM decision_tag dt JOIN tag t ON t.id = dt.tag_id
              WHERE dt.decision_id = d.id) AS tags
  FROM review r JOIN decision d ON d.id = r.decision_id
 WHERE r.done_on IS NULL AND r.due_on <= %(today)s
"""


def _hint(due: list[ReviewDue], unattested: int, shared_tags: set[str]) -> str | None:
    """Uma frase para o agente.

    `shared_tags` são as tags do contexto que alguma revisão vencida também tem.
    Diferente do stub, que citava a primeira tag do contexto mesmo sem relação
    com as revisões, aqui só se cita tag que de fato liga as duas coisas.
    """
    if due:
        tag = next(iter(sorted(shared_tags)), None)
        many = len(due) > 1
        escopo = f" {'relacionadas' if many else 'relacionada'} à tag {tag}" if tag else ""
        plural = "revisões vencidas" if many else "revisão vencida"
        return f"Há {len(due)} {plural}{escopo}. Mencione ao usuário antes de prosseguir."
    if unattested:
        return (
            f"{unattested} registros aguardam atestação de uma pessoa. "
            "Confiança e resultado esperado são preenchidos nesse momento, não aqui."
        )
    return None


def build(
    conn: Any,
    *,
    context_tags: set[str] | None = None,
    on_this_record: list[str] | None = None,
) -> Pending:
    """Monta o bloco. `conn` usa dict_row, como as conexões do pool."""
    context_tags = context_tags or set()
    reference = today()
    scored: list[tuple[int, int, str, ReviewDue, set[str]]] = []
    for row in conn.execute(OVERDUE_SQL, {"today": reference}).fetchall():
        shared = context_tags & set(row["tags"])
        overdue = (reference - row["due_on"]).days
        review = ReviewDue(
            decision_id=row["decision_id"],
            title=row["title"],
            due_on=row["due_on"].isoformat(),
            overdue_days=overdue,
        )
        scored.append((1 if shared else 0, overdue, row["decision_id"], review, shared))
    # Quem compartilha tag primeiro, depois a mais vencida; o id desempata para
    # que a ordem não dependa do plano de execução.
    scored.sort(key=lambda r: (-r[0], -r[1], r[2]))
    top = scored[:MAX_REVIEWS_DUE]
    due = [r[3] for r in top]
    shared_tags = set().union(*(r[4] for r in top))
    unattested = conn.execute(UNATTESTED_COUNT_SQL).fetchone()["n"]
    return Pending(
        on_this_record=on_this_record or [],
        reviews_due=due,
        unattested_count=unattested,
        hint=_hint(due, unattested, shared_tags),
    )
