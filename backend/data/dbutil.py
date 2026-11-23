"""Database connections for local SQLite and hosted PostgreSQL deployments."""
from __future__ import annotations

import os
import sqlite3
import threading
from typing import Any


def using_postgres() -> bool:
    return bool((os.environ.get("DATABASE_URL") or "").strip())


class SQLiteConnection(sqlite3.Connection):
    """Commit or roll back, then close when used in a with block."""

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def _qmark_to_psycopg(query: str) -> str:
    """Translate SQLite-style placeholders without touching quoted SQL text."""
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(query):
        ch = query[i]
        if quote:
            out.append(ch)
            if ch == quote:
                if i + 1 < len(query) and query[i + 1] == quote:
                    out.append(query[i + 1])
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


class PostgresConnection:
    """Small compatibility wrapper for the app's existing ``db.execute`` calls.

    Em ``with``: commit/rollback e a conexão volta ao pool (ou é fechada, se não veio de um pool).
    """

    def __init__(self, connection: Any, pool: Any = None) -> None:
        self._connection = connection
        self._pool = pool

    def _release(self) -> None:
        connection, pool = self._connection, self._pool
        if connection is None:
            return
        self._connection = None
        if pool is not None:
            pool.putconn(connection)  # o pool faz rollback de transação aberta e descarta conexão quebrada
        else:
            connection.close()

    def execute(self, query: str, params: Any = None) -> Any:
        sql = _qmark_to_psycopg(query)
        if params is None:
            return self._connection.execute(sql)
        return self._connection.execute(sql, params)

    def executemany(self, query: str, params: Any) -> Any:
        return self._connection.cursor().executemany(_qmark_to_psycopg(query), params)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._release()

    def __enter__(self) -> "PostgresConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Any:
        try:
            if self._connection is not None:
                if exc_type is None:
                    self._connection.commit()
                else:
                    self._connection.rollback()
        finally:
            self._release()
        return False


def describe_db_error(exc: BaseException) -> tuple[str, str]:
    """(código, mensagem) para uma falha de conexão. Nunca devolve o texto bruto: ele pode trazer host e usuário."""
    carried = getattr(exc, "db_reason", None)
    if carried:  # fail-fast: o motivo já foi classificado na primeira falha
        return str(carried), str(getattr(exc, "db_message", "") or "Banco de dados indisponível.")
    text = str(exc).casefold()
    if any(token in text for token in ("quota", "compute time", "data transfer")):
        return "quota_exceeded", "Banco de dados indisponível: a cota do Neon (compute ou transferência) parece esgotada."
    if "password authentication failed" in text or "authentication failed" in text:
        return "auth_failed", "Banco de dados indisponível: usuário ou senha da DATABASE_URL rejeitados."
    if "too many connections" in text or "remaining connection slots" in text:
        return "too_many_connections", "Banco de dados indisponível: limite de conexões atingido."
    if "timeout" in text or "timed out" in text:
        return "timeout", "Banco de dados indisponível: tempo esgotado ao conectar."
    if "translate host" in text or "name or service not known" in text or "nodename" in text:
        return "dns", "Banco de dados indisponível: o host da DATABASE_URL não foi encontrado."
    return "connection_failed", "Banco de dados indisponível: não foi possível conectar."


POOL_MAX_SIZE = 6
POOL_MAX_IDLE_SECONDS = 60.0  # bem antes do autosuspend de 5 min do Neon: conexão ociosa não segura o compute
CONNECT_TIMEOUT_CAP = 10  # banco fora do ar não deve prender cada thread por 60 s

_pools: dict[str, Any] = {}
_pools_lock = threading.Lock()


def _pool_enabled() -> bool:
    return (os.environ.get("DB_POOL_DISABLED") or "").strip().casefold() not in {"1", "true", "yes"}


def _connect_kwargs(connect_timeout: int) -> dict[str, Any]:
    from psycopg.rows import dict_row

    # prepare_threshold=None: sem prepared statements, exigência do pooler em transaction mode (Supabase/Neon pooled).
    return {"row_factory": dict_row, "prepare_threshold": None, "connect_timeout": connect_timeout}


def _get_pool(database_url: str) -> Any:
    with _pools_lock:
        pool = _pools.get(database_url)
        if pool is None:
            from psycopg_pool import ConnectionPool

            try:
                max_size = max(1, int(os.environ.get("DB_POOL_MAX") or POOL_MAX_SIZE))
            except ValueError:
                max_size = POOL_MAX_SIZE
            pool = ConnectionPool(
                database_url,
                min_size=0,  # nada conecta no boot: o banco só acorda quando alguém precisa
                max_size=max_size,
                max_idle=POOL_MAX_IDLE_SECONDS,
                timeout=CONNECT_TIMEOUT_CAP,
                kwargs=_connect_kwargs(CONNECT_TIMEOUT_CAP),
                check=ConnectionPool.check_connection,  # descarta a conexão que o servidor já fechou
                open=False,
            )
            pool.open()
            _pools[database_url] = pool
        return pool


def close_pools() -> None:
    """Fecha os pools (shutdown, ou depois de trocar a DATABASE_URL)."""
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        try:
            pool.close(timeout=2)
        except Exception:
            pass


def _postgres_connection(database_url: str, timeout: float) -> PostgresConnection:
    import psycopg
    from data import db_state

    fast = db_state.fail_fast_reason()
    if fast is not None:  # caiu há instantes: não faz cada requisição esperar o timeout de novo
        error = psycopg.OperationalError("database unavailable (recent failure, retrying shortly)")
        error.db_reason, error.db_message = fast  # type: ignore[attr-defined]
        raise error
    connect_timeout = max(1, min(CONNECT_TIMEOUT_CAP, int(timeout)))
    try:
        if not _pool_enabled():
            connection = PostgresConnection(psycopg.connect(database_url, **_connect_kwargs(connect_timeout)))
        else:
            pool = _get_pool(database_url)
            try:
                connection = PostgresConnection(pool.getconn(timeout=connect_timeout), pool)
            except Exception as pool_error:
                if not isinstance(pool_error, psycopg.OperationalError):
                    raise
                # O pool só diz "couldn't get a connection"; a conexão direta devolve a causa real (cota, senha, DNS...).
                connection = PostgresConnection(psycopg.connect(database_url, **_connect_kwargs(min(5, connect_timeout))))
    except psycopg.OperationalError as exc:
        reason, message = describe_db_error(exc)
        db_state.mark_down(reason, message)
        raise
    db_state.mark_up()
    return connection


def open_connection(path: str, *, timeout: float = 60.0) -> Any:
    """Use Neon/Postgres when DATABASE_URL is set; otherwise use local SQLite."""
    database_url = (os.environ.get("DATABASE_URL") or "").strip()
    if database_url:
        return _postgres_connection(database_url, timeout)
    return open_sqlite(path, timeout=timeout)


def open_sqlite(path: str, *, timeout: float = 60.0) -> sqlite3.Connection:
    """SQLite local (sempre, independente de DATABASE_URL): usado pelo app sem Postgres e pela ferramenta de cópia."""
    db = sqlite3.connect(path, timeout=timeout, factory=SQLiteConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=60000")
    db.execute("PRAGMA synchronous=NORMAL")
    return db
