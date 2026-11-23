"""Modo degradado: com o banco fora do ar, as leituras do painel devolvem o último resultado bom (somente leitura).

Cada leitura bem-sucedida fica em memória, por chave (endpoint + filtros). Se a próxima falhar por problema de
conexão, a resposta é a cópia guardada marcada com `stale`, `as_of` e o motivo; sem cópia, o erro segue para o
handler 503. Escritas nunca passam por aqui.
"""
from __future__ import annotations

import copy
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Callable

from data import db_state
from data.dbutil import describe_db_error

MAX_ENTRIES = 64

_lock = threading.Lock()
_last_good: "OrderedDict[str, tuple[str, dict]]" = OrderedDict()


def _db_errors() -> tuple[type[BaseException], ...]:
    try:
        import psycopg

        return (psycopg.OperationalError,)
    except ImportError:  # SQLite local sem psycopg
        return ()


def serve(key: str, loader: Callable[[], dict]) -> dict:
    errors = _db_errors()
    try:
        payload = loader()
    except errors as exc:  # type: ignore[misc]
        with _lock:
            hit = _last_good.get(key)
        if hit is None:
            raise
        as_of, stored = hit
        reason, message = describe_db_error(exc)
        db_state.mark_down(reason, message)
        return {**copy.deepcopy(stored), "stale": True, "as_of": as_of, "database": reason, "database_message": message}
    if isinstance(payload, dict):
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _lock:
            _last_good[key] = (stamp, copy.deepcopy(payload))
            _last_good.move_to_end(key)
            while len(_last_good) > MAX_ENTRIES:
                _last_good.popitem(last=False)
    return payload


def clear() -> None:
    """Só para testes."""
    with _lock:
        _last_good.clear()
