"""Registro das tentativas de escrita.

O stub não tem banco. Toda escrita vira uma linha JSONL, e esse arquivo é o
dado do experimento: mostra o que o agente tentou gravar, quando e com quais
campos. Nada sai do processo e nada chega a fonte externa alguma.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_LOG = Path("run") / "tool-calls.jsonl"
_lock = threading.Lock()


def log_path() -> Path:
    return Path(os.environ.get("DECISION_MEMORY_STUB_LOG", DEFAULT_LOG))


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def record(event: str, payload: dict[str, Any]) -> None:
    """Acrescenta uma linha ao log. Falha de escrita nunca derruba a ferramenta."""
    line = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "event": event,
        **payload,
    }
    path = log_path()
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass
