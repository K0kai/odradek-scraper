"""Estado do banco (de pé / fora do ar) compartilhado pelo painel, pelo boot e pelas rotas.

`dbutil` avisa aqui a cada tentativa de conexão. Com o banco fora, chamadas seguidas falham na hora (fail-fast) em vez
de cada requisição esperar o timeout de conexão, e o painel mostra o último estado conhecido em modo leitura.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

FAIL_FAST_SECONDS = 5.0  # depois de uma falha, novas tentativas só a cada tanto tempo

_lock = threading.Lock()
_down_since: float | None = None
_down_since_iso = ""
_reason = ""
_message = ""
_last_failure = 0.0


def mark_down(reason: str, message: str) -> None:
    global _down_since, _down_since_iso, _reason, _message, _last_failure
    with _lock:
        if _down_since is None:
            _down_since = time.monotonic()
            _down_since_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _reason, _message, _last_failure = reason, message, time.monotonic()


def mark_up() -> None:
    global _down_since, _down_since_iso, _reason, _message
    with _lock:
        _down_since, _down_since_iso, _reason, _message = None, "", "", ""


def is_down() -> bool:
    with _lock:
        return _down_since is not None


def fail_fast_reason() -> tuple[str, str] | None:
    """(motivo, mensagem) se o banco caiu há pouco e não vale tentar conectar de novo agora; senão None."""
    with _lock:
        if _down_since is not None and time.monotonic() - _last_failure < FAIL_FAST_SECONDS:
            return _reason, _message
    return None


def snapshot() -> dict:
    with _lock:
        return {"down": _down_since is not None, "reason": _reason, "message": _message, "since": _down_since_iso}


def reset() -> None:
    """Só para testes."""
    mark_up()
