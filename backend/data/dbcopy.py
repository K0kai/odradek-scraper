"""Copia o banco inteiro entre SQLite e PostgreSQL (qualquer combinação) e mantém o standby em dia.

Usos: migrar do Neon para a Supabase, restaurar a partir do standby, exportar um backup SQLite e a cópia diária
automática para `DATABASE_URL_STANDBY`. O destino é substituído por inteiro numa transação: se algo falhar, ele fica
como estava. A origem é lida num instantâneo consistente (REPEATABLE READ somente leitura no Postgres).
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Callable

from data.dbutil import open_sqlite

# Ordem que respeita as chaves estrangeiras (pais antes dos filhos).
TABLE_ORDER = (
    "settings",
    "jobs",
    "runs",
    "ai_decisions",
    "cover_letters",
    "resumes",
    "resume_files",
    "form_field_rules",
    "applications",
    "qpid_application_attempts",
    "qpid_application_results",
    "form_answers",
    "ai_answer_cache",
    "copilot_asks",
    "queue_jobs",
    "event_logs",
)
STANDBY_ENV = "DATABASE_URL_STANDBY"


def is_postgres_url(spec: str) -> bool:
    return spec.strip().casefold().startswith(("postgres://", "postgresql://"))


def _sqlite_path(spec: str) -> str:
    return spec[len("sqlite:"):] if spec.casefold().startswith("sqlite:") else spec


def same_endpoint(a: str, b: str) -> bool:
    a, b = a.strip(), b.strip()
    if is_postgres_url(a) and is_postgres_url(b):
        return a == b
    if not is_postgres_url(a) and not is_postgres_url(b):
        return Path(_sqlite_path(a)).resolve() == Path(_sqlite_path(b)).resolve()
    return False


def _open_source(spec: str):
    if is_postgres_url(spec):
        import psycopg

        conn = psycopg.connect(spec, connect_timeout=15)
        conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ  # leitura consistente entre as tabelas
        conn.read_only = True
        return "postgres", conn
    path = Path(_sqlite_path(spec)).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Banco SQLite de origem não encontrado: {path}")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    return "sqlite", conn


def _tables(kind: str, conn) -> set[str]:
    if kind == "sqlite":
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return {row[0] for row in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}


def _columns(kind: str, conn, table: str) -> list[str]:
    if kind == "sqlite":
        return [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
    return [row[0] for row in conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s ORDER BY ordinal_position", (table,))]


def _prepare_postgres_target(conn) -> None:
    from data import repository

    if repository.missing_postgres_tables(conn):
        repository.apply_postgres_schema(conn)
    repository.ensure_new_columns(conn, postgres=True)


def _prepare_sqlite_target(db) -> None:
    from data import repository

    from jobs.job_queue import ensure_queue_schema

    repository.initialize_sqlite(db)
    ensure_queue_schema(db)
    try:
        from web.qpid_applications import ensure_tables

        ensure_tables(db)
    except ImportError:  # sem fastapi instalado: as tabelas do qpid ficam de fora, o restante é copiado
        pass


def copy_database(source: str, target: str, *, dry_run: bool = False, force: bool = False,
                  log: Callable[[str], None] = lambda _message: None) -> dict[str, int]:
    """Copia todas as tabelas de `source` para `target`, substituindo o conteúdo do destino. Devolve {tabela: linhas}."""
    source, target = source.strip(), target.strip()
    if not source or not target:
        raise ValueError("Informe a origem e o destino.")
    if same_endpoint(source, target):
        raise ValueError("A origem e o destino são o mesmo banco.")
    active = (os.environ.get("DATABASE_URL") or "").strip()
    if not force and not dry_run and active and same_endpoint(target, active):
        raise ValueError("O destino é o banco ativo do app (DATABASE_URL). Use --force se é isso mesmo que você quer.")

    source_kind, src = _open_source(source)
    counts: dict[str, int] = {}
    try:
        source_tables = _tables(source_kind, src)
        tables = [name for name in TABLE_ORDER if name in source_tables]
        for name in sorted(set(TABLE_ORDER) - source_tables):
            log(f"Origem sem a tabela {name} (ignorada).")
        if dry_run:
            for table in tables:
                counts[table] = int(src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                log(f"{table}: {counts[table]} linha(s)")
            return counts
        if is_postgres_url(target):
            _copy_to_postgres(source_kind, src, tables, target, counts, log)
        else:
            _copy_to_sqlite(source_kind, src, tables, _sqlite_path(target), counts, log)
    finally:
        src.close()
    if source_kind == "sqlite":
        # SQLite antigos guardam só o caminho do PDF: importa os arquivos que ainda existem ao lado do banco.
        legacy = Path(_sqlite_path(source)).expanduser().resolve().parent / "resumes"
        if legacy.is_dir():
            _backfill_legacy_pdfs(target, str(legacy), log)
    return counts


def _backfill_legacy_pdfs(target: str, resumes_dir: str, log: Callable[[str], None]) -> None:
    from candidate.resume_storage import backfill
    from data.dbutil import PostgresConnection

    if is_postgres_url(target):
        import psycopg
        from psycopg.rows import dict_row

        with psycopg.connect(target, connect_timeout=15, row_factory=dict_row) as conn:
            backfill(PostgresConnection(conn), resumes_dir)
    else:
        db = open_sqlite(str(Path(_sqlite_path(target)).expanduser().resolve()))
        try:
            backfill(db, resumes_dir)
            db.commit()
        finally:
            db.close()
    log("PDFs de currículo antigos importados de " + resumes_dir)


def _rows(source_kind: str, src, table: str, columns: list[str]) -> list[tuple]:
    quoted = ", ".join(f'"{column}"' for column in columns)
    return [tuple(row) for row in src.execute(f'SELECT {quoted} FROM "{table}"')]


def _copy_to_postgres(source_kind, src, tables, target, counts, log) -> None:
    import psycopg

    with psycopg.connect(target, connect_timeout=15) as conn:  # uma transação: tudo ou nada
        _prepare_postgres_target(conn)
        present = _tables("postgres", conn)
        copy_tables = [table for table in tables if table in present]
        if copy_tables:
            names = ", ".join(f'"{table}"' for table in copy_tables)
            conn.execute(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE")
        with conn.cursor() as cur:
            for table in copy_tables:
                target_columns = set(_columns("postgres", conn, table))
                columns = [column for column in _columns(source_kind, src, table) if column in target_columns]
                rows = _rows(source_kind, src, table, columns) if columns else []
                if rows:
                    quoted = ", ".join(f'"{column}"' for column in columns)
                    placeholders = ", ".join(["%s"] * len(columns))
                    cur.executemany(f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})', rows)
                    if "id" in columns:
                        cur.execute(
                            "SELECT setval(pg_get_serial_sequence(%s, 'id'), "
                            f'GREATEST(COALESCE(MAX(id), 1), 1), MAX(id) IS NOT NULL) FROM "{table}"',
                            (f'"{table}"',),
                        )
                counts[table] = len(rows)
                log(f"{table}: {len(rows)} linha(s)")


def _copy_to_sqlite(source_kind, src, tables, path, counts, log) -> None:
    target_path = Path(path).expanduser().resolve()
    target_path.parent.mkdir(parents=True, exist_ok=True)
    db = open_sqlite(str(target_path))
    try:
        _prepare_sqlite_target(db)
        present = _tables("sqlite", db)
        copy_tables = [table for table in tables if table in present]
        for table in reversed(copy_tables):
            db.execute(f'DELETE FROM "{table}"')
        for table in copy_tables:
            target_columns = set(_columns("sqlite", db, table))
            columns = [column for column in _columns(source_kind, src, table) if column in target_columns]
            rows = _rows(source_kind, src, table, columns) if columns else []
            if rows:
                quoted = ", ".join(f'"{column}"' for column in columns)
                db.executemany(f'INSERT INTO "{table}" ({quoted}) VALUES ({",".join("?" * len(columns))})', rows)
            counts[table] = len(rows)
            log(f"{table}: {len(rows)} linha(s)")
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def sync_standby(*, log: Callable[[str], None] = lambda _message: None) -> dict:
    """Copia o banco ativo (DATABASE_URL) para o standby (DATABASE_URL_STANDBY). Devolve um resumo serializável."""
    source = (os.environ.get("DATABASE_URL") or "").strip()
    standby = (os.environ.get(STANDBY_ENV) or "").strip()
    if not standby:
        raise ValueError(f"Configure {STANDBY_ENV} para ter um banco standby.")
    if not source:
        raise ValueError("DATABASE_URL não configurada: não há banco ativo para copiar.")
    started = time.monotonic()
    counts = copy_database(source, standby, log=log)
    return {"tables": counts, "rows": sum(counts.values()), "seconds": round(time.monotonic() - started, 1)}
