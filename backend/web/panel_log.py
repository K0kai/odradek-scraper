# Log persistente para o painel local (visível na aba Logs).
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from paths import DB_PATH as DEFAULT_DB_PATH

DB_PATH = str(DEFAULT_DB_PATH)
MAX_ROWS = 800
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    from data.dbutil import open_connection

    return open_connection(DB_PATH, timeout=60.0)


def ensure_log_table(db: sqlite3.Connection | None = None) -> None:
    own = db is None
    if own:
        db = _connect()
    assert db is not None
    db.execute(
        """CREATE TABLE IF NOT EXISTS event_logs (
          id INTEGER PRIMARY KEY,
          created_at TEXT NOT NULL,
          level TEXT NOT NULL,
          source TEXT NOT NULL,
          message TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS event_logs_created_idx ON event_logs(created_at DESC)")
    if own:
        db.commit()
        db.close()


_PRUNE_SQL = """DELETE FROM event_logs WHERE id <= (
                     SELECT id FROM event_logs ORDER BY id DESC LIMIT 1 OFFSET ?
                   )"""  # mantém as MAX_ROWS mais novas; sem linhas além disso a subconsulta é NULL e nada é apagado

# Banco em rede: cada evento abria uma conexão (e varria a tabela inteira para podar). Os eventos agora ficam em
# memória e saem juntos, numa conexão só, a cada FLUSH_SECONDS (ou ao juntar FLUSH_AT_COUNT). SQLite local grava na hora.
FLUSH_SECONDS = 5.0
FLUSH_AT_COUNT = 25
RETRY_AFTER_FAILURE_SECONDS = 30.0
PRUNE_EVERY = 50
MAX_PENDING = 2000
_pending: list[tuple[str, str, str, str]] = []
_pending_lock = threading.Lock()
_flush_wake = threading.Event()  # há eventos pendentes
_flush_now = threading.Event()  # o buffer encheu: grava sem esperar a janela
_flusher: threading.Thread | None = None
_inserted_since_prune = PRUNE_EVERY  # força uma poda no primeiro flush do processo


def _batched() -> bool:
    from data.dbutil import using_postgres

    return using_postgres()


def log_event(level: str, source: str, message: str) -> None:
    """Registra um evento para a aba Logs. Níveis: info, success, warning, error."""
    level = (level or "info").casefold().strip()
    if level not in {"info", "success", "warning", "error", "debug"}:
        level = "info"
    source = (source or "app")[:80]
    message = (message or "").strip()[:2000]
    if not message:
        return
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if _batched():
        with _pending_lock:
            _pending.append((stamp, level, source, message))
            del _pending[:-MAX_PENDING]
            full = len(_pending) >= FLUSH_AT_COUNT
        _ensure_flusher()
        _flush_wake.set()
        if full:
            _flush_now.set()
        return
    with _lock:
        try:
            with _connect() as db:
                db.execute(
                    "INSERT INTO event_logs(created_at,level,source,message) VALUES(?,?,?,?)",
                    (stamp, level, source, message),
                )
                db.execute(_PRUNE_SQL, (MAX_ROWS,))
        except Exception:
            pass


def _ensure_flusher() -> None:
    global _flusher
    with _pending_lock:
        if _flusher is not None and _flusher.is_alive():
            return
        _flusher = threading.Thread(target=_flush_loop, daemon=True, name="log-flusher")
        _flusher.start()


def _flush_loop() -> None:
    while True:
        _flush_wake.wait()  # nada pendente: dorme sem tocar no banco
        _flush_wake.clear()
        _flush_now.wait(FLUSH_SECONDS)  # janela para juntar o que chegar; o buffer cheio encerra antes
        _flush_now.clear()
        if not flush_logs():
            time.sleep(RETRY_AFTER_FAILURE_SECONDS)  # banco fora: não martela


def flush_logs() -> bool:
    """Grava os eventos pendentes numa conexão só. False se o banco falhou (os eventos são mantidos)."""
    global _inserted_since_prune
    with _pending_lock:
        batch = list(_pending)
        _pending.clear()
    if not batch:
        return True
    try:
        with _connect() as db:
            db.executemany("INSERT INTO event_logs(created_at,level,source,message) VALUES(?,?,?,?)", batch)
            _inserted_since_prune += len(batch)
            if _inserted_since_prune >= PRUNE_EVERY:
                db.execute(_PRUNE_SQL, (MAX_ROWS,))
                _inserted_since_prune = 0
        return True
    except Exception:
        with _pending_lock:  # mantém na fila (limitada) para a próxima tentativa
            _pending[:0] = batch
            del _pending[:-MAX_PENDING]
        return False


def pending_logs(limit: int = 200) -> list[dict]:
    """Eventos ainda não gravados (mais novos primeiro), no formato das linhas da tabela."""
    with _pending_lock:
        items = list(_pending)
    return [
        {"id": -(index + 1), "created_at": stamp, "level": level, "source": source, "message": message}
        for index, (stamp, level, source, message) in enumerate(reversed(items))
    ][:limit]


def list_logs(limit: int = 200) -> list[sqlite3.Row]:
    limit = max(1, min(500, int(limit)))
    if _batched():
        flush_logs()  # a aba Logs mostra tudo, inclusive o que ainda estava no buffer
        try:
            with _connect() as db:
                return list(db.execute("SELECT * FROM event_logs ORDER BY id DESC LIMIT ?", (limit,)))
        except Exception:
            return pending_logs(limit)  # banco fora: pelo menos o que aconteceu nesta sessão
    with _connect() as db:
        return list(db.execute("SELECT * FROM event_logs ORDER BY id DESC LIMIT ?", (limit,)))


def clear_logs() -> int:
    with _lock:
        with _connect() as db:
            ensure_log_table(db)
            cur = db.execute("DELETE FROM event_logs")
            return int(cur.rowcount or 0)


class PanelLogHandler(logging.Handler):
    """Encaminha records do logging stdlib para a aba Logs."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level_name = record.levelname.casefold()
            if level_name == "warning":
                level = "warning"
            elif level_name in {"error", "critical"}:
                level = "error"
            elif level_name == "debug":
                level = "debug"
            else:
                level = "info"
            log_event(level, record.name or "logger", record.getMessage())
        except Exception:
            self.handleError(record)


def attach_to_logger(logger_name: str = "odradek-scraper") -> None:
    logger = logging.getLogger(logger_name)
    for existing in list(logger.handlers):
        if isinstance(existing, PanelLogHandler):
            return
    handler = PanelLogHandler()
    handler.setLevel(logging.INFO)
    logger.addHandler(handler)
