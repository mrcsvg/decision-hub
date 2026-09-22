"""Construção do bloco `pending`.

É a mitigação nº 1 do ADR 0002: o lembrete viaja junto da resposta, porque o
servidor não controla quando é chamado. Por isso o bloco é curto, específico e
ligado ao que acabou de acontecer — nunca uma lista genérica.
"""

from __future__ import annotations

from datetime import date

from .corpus import Corpus, today
from .models import Pending, ReviewDue

MAX_REVIEWS_DUE = 3


def _overdue_days(due_on: str, reference: date) -> int:
    return (reference - date.fromisoformat(due_on)).days


def reviews_due(corpus: Corpus, context_tags: set[str] | None = None) -> list[ReviewDue]:
    """Revisões em aberto, priorizando as que compartilham tag com o contexto."""
    reference = today()
    context_tags = context_tags or set()
    scored: list[tuple[int, int, ReviewDue]] = []

    for review in corpus.open_reviews():
        decision = corpus.decision_by_id(review["decision_id"])
        if decision is None:
            continue
        overdue = _overdue_days(review["due_on"], reference)
        if overdue < 0:
            continue  # ainda não venceu: não é cobrança, é ruído
        shares_tag = 1 if context_tags & set(decision.get("tags", [])) else 0
        scored.append(
            (
                shares_tag,
                overdue,
                ReviewDue(
                    decision_id=decision["id"],
                    title=decision["title"],
                    due_on=review["due_on"],
                    overdue_days=overdue,
                ),
            )
        )

    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return [row[2] for row in scored[:MAX_REVIEWS_DUE]]


def _hint(due: list[ReviewDue], unattested: int, context_tags: set[str]) -> str | None:
    if due:
        tag = next(iter(sorted(context_tags)), None)
        escopo = f" relacionada à tag {tag}" if tag else ""
        plural = "revisões vencidas" if len(due) > 1 else "revisão vencida"
        return (
            f"Há {len(due)} {plural}{escopo}. Mencione ao usuário antes de prosseguir."
        )
    if unattested:
        return (
            f"{unattested} registros aguardam atestação de uma pessoa. "
            "Confiança e resultado esperado são preenchidos nesse momento, não aqui."
        )
    return None


def build(
    corpus: Corpus,
    *,
    context_tags: set[str] | None = None,
    on_this_record: list[str] | None = None,
) -> Pending:
    context_tags = context_tags or set()
    due = reviews_due(corpus, context_tags)
    unattested = corpus.unattested_count()
    return Pending(
        on_this_record=on_this_record or [],
        reviews_due=due,
        unattested_count=unattested,
        hint=_hint(due, unattested, context_tags),
    )
