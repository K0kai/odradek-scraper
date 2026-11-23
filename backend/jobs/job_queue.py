# Fila persistente com workers paralelos e retry para operações de IA.
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from web.panel_log import log_event
from data.dbutil import using_postgres

LOG = logging.getLogger("odradek-scraper")

KIND_RESUME = "resume_analysis"
KIND_APPLY = "job_apply"
KIND_WORTH_REVERIFY = "worth_reverify"
ACTIVE_STATUSES = ("pending", "running", "retry_wait")
TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")
# Teto absoluto do jitter de retry (evita intervalos de horas).
BACKOFF_CAP_SECONDS = 20 * 60
# Fila vazia: o dispatcher não consulta o banco a cada segundo (no Neon isso mantém o compute ligado 24h e
# esgota a cota do mês). Dorme até o próximo job agendado, no máximo este tempo; enqueue()/retry acordam na hora.
IDLE_POLL_SECONDS = 3600  # só uma rede de segurança: enqueue, retry e o fim de um job acordam o dispatcher na hora
MIN_WAIT_SECONDS = 1.0
# Falha de cota/indisponibilidade da IA: pausa os jobs desse tipo por este intervalo (limites), em vez de cada job
# da fila abrir ~15 conexões só para falhar de novo.
KIND_PAUSE_MIN_SECONDS = 30
KIND_PAUSE_MAX_SECONDS = 600
ERROR_BACKOFF_MAX_SECONDS = 300  # banco fora do ar: espera crescente em vez de martelar a cada segundo
PROGRESS_FLUSH_SECONDS = 5.0  # linhas de progresso são agrupadas: 1 gravação a cada 5 s em vez de 1 conexão por linha
WORKERS_CACHE_SECONDS = 30.0


PROGRESS_TAIL_CHARS = 600
# Listagem do painel: sem o progress_log inteiro (até 12 KB por item, ~1 MB a cada consulta), só a cauda.
QUEUE_LIST_SQL = (
    "SELECT id, kind, payload, status, attempts, max_attempts, next_run_at, expires_at, last_error, result, "
    "created_at, updated_at, LENGTH(progress_log) AS progress_len, "
    f"CASE WHEN LENGTH(progress_log) > {PROGRESS_TAIL_CHARS} "
    f"THEN SUBSTR(progress_log, LENGTH(progress_log) - {PROGRESS_TAIL_CHARS - 1}) ELSE progress_log END AS progress_tail "
    "FROM queue_jobs ORDER BY id DESC LIMIT ?"
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def backoff_seconds(attempt: int, *, rate_limited: bool = False) -> int:
    """Backoff exponencial com jitter aleatório por job (espalha retries sincronizados).

    Sem jitter, vários 429 voltam no mesmo segundo e martelam a IA de novo.
    Teto: 20 minutos.
    """
    floor = 60 if rate_limited else 20
    # attempt 1 → ~60–180s (429) ou ~20–60s; dobra a cada tentativa até o teto.
    base = floor * (2 ** max(0, attempt - 1))
    base = min(BACKOFF_CAP_SECONDS, base)
    low = floor
    high = min(BACKOFF_CAP_SECONDS, max(low + 15, int(base * 1.75)))
    return random.randint(low, high)


def is_rate_limit_error(exc: BaseException) -> bool:
    msg = str(exc).casefold()
    tokens = ("429", "rate limit", "quota", "resource_exhausted", "resource exhausted", "too many requests")
    return any(t in msg for t in tokens)


def is_retryable_error(exc: BaseException) -> bool:
    name = exc.__class__.__name__.casefold()
    if "nonretryable" in name:
        return False
    if "aiunavailable" in name or "airesponse" in name:
        return True
    if "operationalerror" in name and "locked" in str(exc).casefold():
        return True
    msg = str(exc).casefold()
    # Never retry browser/login steps that require a person.
    linkedin_human = (
        "login", "checkpoint", "captcha", "authwall", "perfil chrome", "manual",
    )
    if any(token in msg for token in linkedin_human):
        return False
    tokens = (
        "429", "503", "502", "504", "quota", "rate limit", "resource_exhausted",
        "unavailable", "overload", "temporar", "try again", "retry", "high demand",
        "ia indisponível", "resource exhausted", "database is locked", "database locked",
    )
    return any(token in msg for token in tokens)


class NonRetryableError(Exception):
    """Queue should mark failed immediately (no retry_wait)."""


def ensure_queue_schema(db) -> None:
    """Tabela da fila no SQLite (também usada ao preparar um SQLite de destino para cópia)."""
    db.execute(
        """CREATE TABLE IF NOT EXISTS queue_jobs (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL,
          payload TEXT NOT NULL,
          status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          max_attempts INTEGER NOT NULL DEFAULT 40,
          next_run_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          last_error TEXT NOT NULL DEFAULT '',
          result TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )"""
    )
    db.execute("CREATE INDEX IF NOT EXISTS queue_jobs_status_idx ON queue_jobs(status, next_run_at)")
    db.execute("CREATE INDEX IF NOT EXISTS queue_jobs_kind_idx ON queue_jobs(kind)")
    cols = {row["name"] for row in db.execute("PRAGMA table_info(queue_jobs)")}
    if "progress_log" not in cols:
        db.execute("ALTER TABLE queue_jobs ADD COLUMN progress_log TEXT NOT NULL DEFAULT ''")



class JobQueue:
    """Fila SQLite + pool de workers (padrão 3) para jobs de IA."""

    def __init__(
        self,
        *,
        db_path: str,
        handlers: dict[str, Callable[[dict[str, Any]], str]],
        get_settings: Callable[[], dict[str, str]],
        connect_fn: Callable[[], sqlite3.Connection] | None = None,
    ) -> None:
        self.db_path = db_path
        self.handlers = handlers
        self.get_settings = get_settings
        self._connect_fn = connect_fn
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()  # enqueue/retry acordam o dispatcher ocioso
        self._next_due_iso: str | None = None
        self._paused_until: dict[str, float] = {}  # kind -> time.monotonic() em que volta a ser executado
        self._progress_lock = threading.Lock()
        self._progress_buffer: dict[int, list[str]] = {}
        self._progress_flushed_at: dict[int, float] = {}
        self._dispatcher: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._inflight: dict[int, Future] = {}
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        if self._connect_fn:
            return self._connect_fn()
        from data.dbutil import open_connection

        return open_connection(self.db_path, timeout=60.0)

    def _ensure_schema(self) -> None:
        # No Postgres o schema vem de data/schema_postgres.sql (aplicado por repository.initialize).
        if using_postgres():
            return
        with self._connect() as db:
            ensure_queue_schema(db)

    def _limits(self) -> tuple[int, int, int]:
        cfg = self.get_settings()
        try:
            workers = max(1, min(8, int(cfg.get("queue_max_workers", "3") or 3)))
        except ValueError:
            workers = 3
        try:
            max_attempts = max(1, min(200, int(cfg.get("queue_max_attempts", "40") or 40)))
        except ValueError:
            max_attempts = 40
        try:
            ttl_hours = max(1, min(168, int(cfg.get("queue_ttl_hours", "24") or 24)))
        except ValueError:
            ttl_hours = 24
        return workers, max_attempts, ttl_hours

    def start(self) -> None:
        with self._lock:
            if self._dispatcher and self._dispatcher.is_alive():
                return
            workers, _, _ = self._limits()
            self._stop.clear()
            recovered = self.recover_stale_running()
            respread = self.respread_retry_waits()
            self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="queue-worker")
            self._dispatcher = threading.Thread(target=self._dispatch_loop, daemon=True, name="queue-dispatcher")
            self._dispatcher.start()
            msg = f"Fila iniciada com até {workers} worker(s)."
            if recovered:
                msg += f" {recovered} job(s) interrompido(s) recolocados como pendentes."
            if respread:
                msg += f" {respread} retry(s) reespalhado(s) com jitter."
            log_event("info", "queue", msg)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            ex = self._executor
            self._executor = None
        if ex:
            ex.shutdown(wait=False, cancel_futures=False)
        log_event("info", "queue", "Fila parada.")

    def recover_stale_running(self) -> int:
        """Jobs 'running' sem processo vivo (ex.: após restart).

        Legacy browser-application jobs are cancelled; other interrupted work is requeued.
        """
        now = _utc_now()
        _, _, ttl_hours = self._limits()
        with self._connect() as db:
            cur_legacy = db.execute(
                """UPDATE queue_jobs
                   SET status='cancelled', updated_at=?,
                       last_error='Candidatura pelo navegador removida; use a extensão no Chrome para formulários.'
                   WHERE kind='linkedin_apply' AND status IN ('pending','running','retry_wait')""",
                (_iso(now),),
            )
            cancelled_legacy = int(cur_legacy.rowcount or 0)
            cur = db.execute(
                """UPDATE queue_jobs
                   SET status='pending', next_run_at=?, expires_at=?, updated_at=?,
                       last_error=CASE
                         WHEN last_error='' THEN 'Interrompido (reinício do app); reenfileirado.'
                         ELSE last_error
                       END
                   WHERE status='running'""",
                (_iso(now), _iso(now + timedelta(hours=ttl_hours)), _iso(now)),
            )
            return int(cur.rowcount or 0) + cancelled_legacy

    def respread_retry_waits(self) -> int:
        """Reespalha next_run_at de jobs em retry_wait para quebrar sincronização (ex.: 429 em massa)."""
        now = _utc_now()
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, attempts, last_error FROM queue_jobs WHERE status='retry_wait'"
            ).fetchall()
            n = 0
            for row in rows:
                rate_limited = is_rate_limit_error(Exception(row["last_error"] or ""))
                delay = backoff_seconds(max(1, int(row["attempts"] or 1)), rate_limited=rate_limited)
                db.execute(
                    "UPDATE queue_jobs SET next_run_at=?, updated_at=? WHERE id=? AND status='retry_wait'",
                    (_iso(now + timedelta(seconds=delay)), _iso(now), int(row["id"])),
                )
                n += 1
            return n

    def enqueue(self, kind: str, payload: dict[str, Any], *, dedupe_key: str | None = None) -> int:
        if kind not in self.handlers:
            raise ValueError(f"Tipo de job desconhecido: {kind}")
        _, max_attempts, ttl_hours = self._limits()
        now = _utc_now()
        payload_json = json.dumps(payload, ensure_ascii=False)
        if dedupe_key:
            payload = dict(payload)
            payload["_dedupe"] = dedupe_key
            payload_json = json.dumps(payload, ensure_ascii=False)
            with self._connect() as db:
                existing = db.execute(
                    """SELECT id FROM queue_jobs
                       WHERE kind=? AND status IN ('pending','running','retry_wait')
                         AND payload LIKE ?
                       ORDER BY id DESC LIMIT 1""",
                    (kind, f'%"{dedupe_key}"%'),
                ).fetchone()
                if existing:
                    log_event("info", "queue", f"Job #{existing['id']} ({kind}) já na fila; reutilizando.")
                    self._wake.set()
                    return int(existing["id"])
        with self._connect() as db:
            row = db.execute(
                """INSERT INTO queue_jobs(
                     kind,payload,status,attempts,max_attempts,next_run_at,expires_at,last_error,result,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?) RETURNING id""",
                (
                    kind,
                    payload_json,
                    "pending",
                    0,
                    max_attempts,
                    _iso(now),
                    _iso(now + timedelta(hours=ttl_hours)),
                    "",
                    "",
                    _iso(now),
                    _iso(now),
                ),
            ).fetchone()
            job_id = int(row["id"])
        log_event("info", "queue", f"Enfileirado #{job_id} ({kind}).")
        self._wake.set()
        return job_id

    def append_progress(self, job_id: int | None, message: str, *, max_chars: int = 12000, flush: bool = False) -> None:
        """Append-only na fila. Não apaga result/last_error. job_id None = no-op.

        As linhas ficam em memória e são gravadas em lote (a cada PROGRESS_FLUSH_SECONDS, ou já com flush=True):
        cada gravação abre uma conexão com o banco, e uma linha por conexão consumia compute demais no Neon.
        """
        if job_id is None:
            return
        try:
            qid = int(job_id)
        except (TypeError, ValueError):
            return
        text = re.sub(r"\s+", " ", str(message or "").strip())
        if not text:
            return
        line = f"{_utc_now().strftime('%H:%M:%S')} {text[:1000]}"
        with self._progress_lock:
            self._progress_buffer.setdefault(qid, []).append(line)
            due = flush or time.monotonic() - self._progress_flushed_at.get(qid, 0.0) >= PROGRESS_FLUSH_SECONDS
        if due:
            self.flush_progress(qid, max_chars=max_chars)

    def flush_progress(self, job_id: int, *, max_chars: int = 12000) -> None:
        """Grava as linhas pendentes de um job numa única conexão."""
        with self._progress_lock:
            lines = self._progress_buffer.pop(job_id, [])
            self._progress_flushed_at[job_id] = time.monotonic()
        if not lines:
            return
        try:
            with self._connect() as db:
                row = db.execute("SELECT progress_log FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
                if not row:
                    return
                prev = str(row["progress_log"] or "")
                merged = (prev + ("\n" if prev else "") + "\n".join(lines)).strip()
                if len(merged) > max_chars:
                    merged = merged[-max_chars:]
                    # evita cortar no meio da primeira linha visível
                    nl = merged.find("\n")
                    if nl > 0:
                        merged = merged[nl + 1 :]
                db.execute(
                    "UPDATE queue_jobs SET progress_log=?, updated_at=? WHERE id=?",
                    (merged, _iso(_utc_now()), job_id),
                )
        except Exception:
            with self._progress_lock:  # banco indisponível: guarda as linhas para a próxima tentativa
                self._progress_buffer[job_id] = lines + self._progress_buffer.get(job_id, [])
            raise

    def list_jobs(self, *, limit: int = 100, include_terminal: bool = True) -> list[sqlite3.Row]:
        limit = max(1, min(300, limit))
        with self._connect() as db:
            if include_terminal:
                return list(db.execute("SELECT * FROM queue_jobs ORDER BY id DESC LIMIT ?", (limit,)))
            return list(
                db.execute(
                    "SELECT * FROM queue_jobs WHERE status IN ('pending','running','retry_wait') ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            )

    def has_active_kind(self, kind: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM queue_jobs WHERE kind=? AND status IN ('pending','running','retry_wait') LIMIT 1",
                (kind,),
            ).fetchone()
        return row is not None

    def cancel(self, job_id: int) -> bool:
        now = _iso(_utc_now())
        with self._connect() as db:
            cur = db.execute(
                """UPDATE queue_jobs SET status='cancelled', updated_at=?, last_error=COALESCE(NULLIF(last_error,''),'Cancelado manualmente')
                   WHERE id=? AND status IN ('pending','retry_wait','running')""",
                (now, job_id),
            )
            ok = cur.rowcount > 0
        if ok:
            log_event("warning", "queue", f"Job #{job_id} cancelado.")
        return ok

    def is_cancelled(self, job_id: int) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT status FROM queue_jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row) and str(row["status"]) == "cancelled"

    def retry_now(self, job_id: int) -> bool:
        now = _utc_now()
        _, max_attempts, ttl_hours = self._limits()
        with self._lock:
            fut = self._inflight.get(job_id)
            if fut is not None and not fut.done():
                return False
        with self._connect() as db:
            cur = db.execute(
                """UPDATE queue_jobs
                   SET status='pending', next_run_at=?, expires_at=?, updated_at=?,
                       last_error='', attempts=0, max_attempts=?, result=''
                   WHERE id=? AND kind!='linkedin_apply'
                     AND status IN ('retry_wait','failed','cancelled','running')""",
                (_iso(now), _iso(now + timedelta(hours=ttl_hours)), _iso(now), max_attempts, job_id),
            )
            ok = cur.rowcount > 0
        if ok:
            log_event("info", "queue", f"Job #{job_id} reenfileirado para execução imediata.")
            self._wake.set()
        return ok

    def retry_all(self, *, statuses: tuple[str, ...] = ("failed", "cancelled", "retry_wait", "running")) -> int:
        """Reenfileira em lote jobs reprocessáveis (útil após falhas em massa / restart)."""
        now = _utc_now()
        _, max_attempts, ttl_hours = self._limits()
        with self._lock:
            live = {jid for jid, fut in self._inflight.items() if not fut.done()}
        placeholders = ",".join("?" * len(statuses))
        with self._connect() as db:
            rows = db.execute(
                f"SELECT id FROM queue_jobs WHERE kind!='linkedin_apply' AND status IN ({placeholders})",
                statuses,
            ).fetchall()
            ids = [int(row["id"]) for row in rows if int(row["id"]) not in live]
            if not ids:
                return 0
            id_ph = ",".join("?" * len(ids))
            cur = db.execute(
                f"""UPDATE queue_jobs
                    SET status='pending', next_run_at=?, expires_at=?, updated_at=?,
                        last_error='', attempts=0, max_attempts=?, result=''
                    WHERE id IN ({id_ph})""",
                [_iso(now), _iso(now + timedelta(hours=ttl_hours)), _iso(now), max_attempts, *ids],
            )
            n = int(cur.rowcount or 0)
        if n:
            log_event("info", "queue", f"{n} job(s) reenfileirado(s) em lote.")
            self._wake.set()
        return n

    def purge_terminal_older_than(self, days: int = 14) -> int:
        """Retenção: itens terminados (com histórico de até 12 KB cada) não ficam para sempre na tabela."""
        cutoff = _iso(_utc_now() - timedelta(days=days))
        with self._connect() as db:
            cur = db.execute(
                "DELETE FROM queue_jobs WHERE status IN ('succeeded','failed','cancelled') AND updated_at < ?",
                (cutoff,),
            )
            return int(cur.rowcount or 0)

    def clear_terminal(self) -> int:
        with self._connect() as db:
            cur = db.execute(
                "DELETE FROM queue_jobs WHERE status IN ('succeeded','failed','cancelled')"
            )
            n = int(cur.rowcount or 0)
        log_event("info", "queue", f"Removidos {n} job(s) concluídos/falhos/cancelados.")
        return n

    def counts(self) -> dict[str, int]:
        with self._connect() as db:
            return self.counts_in(db)

    @staticmethod
    def counts_in(db) -> dict[str, int]:
        rows = db.execute("SELECT status, COUNT(*) AS n FROM queue_jobs GROUP BY status").fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def _active_pauses(self) -> dict[str, float]:
        now = time.monotonic()
        with self._lock:
            for kind in [kind for kind, until in self._paused_until.items() if until <= now]:
                del self._paused_until[kind]
            return dict(self._paused_until)

    def _pause_kind(self, kind: str, seconds: float) -> None:
        seconds = max(KIND_PAUSE_MIN_SECONDS, min(KIND_PAUSE_MAX_SECONDS, seconds))
        with self._lock:
            self._paused_until[kind] = max(self._paused_until.get(kind, 0.0), time.monotonic() + seconds)

    def _claim_batch(self, limit: int) -> list[sqlite3.Row]:
        now = _utc_now()
        now_s = _iso(now)
        claimed: list[sqlite3.Row] = []
        paused = sorted(self._active_pauses())
        skip_sql = f" AND kind NOT IN ({','.join('?' * len(paused))})" if paused else ""
        with self._connect() as db:
            # Expire old jobs first.
            db.execute(
                """UPDATE queue_jobs SET status='failed', last_error='Expirado (TTL da fila)', updated_at=?
                   WHERE status IN ('pending','retry_wait') AND expires_at < ?""",
                (now_s, now_s),
            )
            rows = db.execute(
                f"""SELECT * FROM queue_jobs
                   WHERE status IN ('pending','retry_wait') AND next_run_at <= ?{skip_sql}
                   ORDER BY id ASC LIMIT ?""",
                (now_s, *paused, limit),
            ).fetchall()
            for row in rows:
                if len(claimed) >= limit:
                    break
                kind = str(row["kind"])
                cur = db.execute(
                    """UPDATE queue_jobs SET status='running', updated_at=?
                       WHERE id=? AND status IN ('pending','retry_wait')""",
                    (now_s, row["id"]),
                )
                if cur.rowcount:
                    claimed.append(row)
            # Mesma conexão: quando o dispatcher pode dormir até o próximo job agendado (retry_wait/pending futuro).
            due = db.execute(
                f"SELECT MIN(next_run_at) AS due FROM queue_jobs WHERE status IN ('pending','retry_wait'){skip_sql}",
                tuple(paused),
            ).fetchone()
            self._next_due_iso = (due["due"] if due else None) or None
        return claimed

    def _idle_wait_seconds(self) -> float:
        """Quanto o dispatcher pode dormir: até o próximo job agendado ou o fim de uma pausa, no máximo IDLE_POLL_SECONDS."""
        wait = float(IDLE_POLL_SECONDS)
        due = _parse_iso(self._next_due_iso)
        if due is not None:
            wait = min(wait, (due - _utc_now()).total_seconds() + 0.5)
        pauses = self._active_pauses()
        if pauses:
            wait = min(wait, min(pauses.values()) - time.monotonic() + 0.5)
        return max(MIN_WAIT_SECONDS, wait)

    def _dispatch_loop(self) -> None:
        workers, workers_read_at = 3, float("-inf")
        error_delay = 0.0
        while not self._stop.is_set():
            self._wake.clear()  # antes de olhar o banco: um enqueue durante o corpo do loop não se perde
            delay = float(IDLE_POLL_SECONDS)
            try:
                # A configuração é uma consulta ao banco: relê só de vez em quando, não a cada iteração.
                if time.monotonic() - workers_read_at >= WORKERS_CACHE_SECONDS:
                    workers, _, _ = self._limits()
                    workers_read_at = time.monotonic()
                with self._lock:
                    # Drop finished futures
                    done = [jid for jid, fut in self._inflight.items() if fut.done()]
                    for jid in done:
                        self._inflight.pop(jid, None)
                    inflight = len(self._inflight)
                    capacity = max(0, workers - inflight)
                    executor = self._executor
                if capacity and executor is not None:
                    for row in self._claim_batch(capacity):
                        job_id = int(row["id"])
                        fut = executor.submit(self._run_job, dict(row))
                        with self._lock:
                            self._inflight[job_id] = fut
                # Sem polling: dorme até o próximo job agendado; um worker que termina, enqueue e retry acordam.
                delay = self._idle_wait_seconds()
                error_delay = 0.0
            except Exception:
                workers_read_at = float("-inf")
                error_delay = min(ERROR_BACKOFF_MAX_SECONDS, max(5.0, error_delay * 2))
                delay = error_delay
                LOG.exception("Falha no dispatcher da fila (nova tentativa em %.0fs)", delay)
                try:
                    log_event("error", "queue", f"Falha no dispatcher da fila (nova tentativa em {delay:.0f}s).")
                except Exception:
                    pass
            self._wake.wait(delay)

    def _run_job(self, row: dict[str, Any]) -> None:
        job_id = int(row["id"])
        try:
            self._run_job_inner(row)
        finally:
            try:
                self.flush_progress(job_id)  # nenhuma linha fica pendente quando o job termina (ou é cancelado)
            except Exception:
                pass
            with self._progress_lock:
                self._progress_buffer.pop(job_id, None)
                self._progress_flushed_at.pop(job_id, None)
            with self._lock:
                self._inflight.pop(job_id, None)  # libera a vaga antes de acordar o dispatcher
            self._wake.set()  # há capacidade livre: o próximo job da fila pode começar já

    def _run_job_inner(self, row: dict[str, Any]) -> None:
        job_id = int(row["id"])
        kind = str(row["kind"])
        attempts = int(row["attempts"]) + 1
        max_attempts = int(row["max_attempts"])
        now = _utc_now()
        try:
            payload = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {"_raw": payload}
        payload = dict(payload)
        payload["_queue_job_id"] = job_id
        # Se cancelaram entre claim e start, não começa.
        if self.is_cancelled(job_id):
            log_event("warning", "queue", f"Job #{job_id} ({kind}) já cancelado; não executa.")
            return
        handler = self.handlers.get(kind)
        if not handler:
            self._finish(job_id, "failed", attempts, f"Handler ausente para {kind}", "")
            return
        if attempts > 1:
            self.append_progress(job_id, f"— retry {attempts}/{max_attempts} —")
        else:
            self.append_progress(job_id, f"iniciando ({kind})")
        log_event("info", "queue", f"Executando #{job_id} ({kind}), tentativa {attempts}/{max_attempts}.")
        try:
            result = handler(payload) or "ok"
            self.append_progress(job_id, f"concluído: {str(result)[:200]}", flush=True)
            self._finish(job_id, "succeeded", attempts, "", str(result)[:1000])
            log_event("success", "queue", f"Job #{job_id} ({kind}) concluído.")
        except Exception as exc:
            err = f"{exc.__class__.__name__}: {exc}"
            expires = _parse_iso(str(row.get("expires_at") or ""))
            retryable = is_retryable_error(exc)
            if self.is_cancelled(job_id):
                log_event("warning", "queue", f"Job #{job_id} ({kind}) cancelado durante execução.")
                return
            if retryable and attempts < max_attempts and (expires is None or now < expires):
                delay = backoff_seconds(attempts, rate_limited=is_rate_limit_error(exc))
                if is_rate_limit_error(exc) or "aiunavailable" in exc.__class__.__name__.casefold():
                    self._pause_kind(kind, delay)  # a IA está sem cota/fora: não deixa o resto da fila falhar em série
                next_run = _iso(now + timedelta(seconds=delay))
                self.append_progress(job_id, f"retry em ~{delay}s: {err[:180]}", flush=True)
                with self._connect() as db:
                    db.execute(
                        """UPDATE queue_jobs SET status='retry_wait', attempts=?, next_run_at=?, last_error=?, updated_at=?
                           WHERE id=? AND status='running'""",
                        (attempts, next_run, err[:1000], _iso(now), job_id),
                    )
                log_event(
                    "warning",
                    "queue",
                    f"Job #{job_id} ({kind}) em retry ({attempts}/{max_attempts}) em ~{delay}s: {err}",
                )
            else:
                self.append_progress(job_id, f"falhou: {err[:200]}", flush=True)
                self._finish(job_id, "failed", attempts, err[:1000], "")
                log_event("error", "queue", f"Job #{job_id} ({kind}) falhou em definitivo: {err}")

    def _finish(self, job_id: int, status: str, attempts: int, last_error: str, result: str) -> None:
        now = _iso(_utc_now())
        with self._connect() as db:
            # Não sobrescreve cancelamento/manual mid-flight.
            db.execute(
                """UPDATE queue_jobs SET status=?, attempts=?, last_error=?, result=?, updated_at=?
                   WHERE id=? AND status='running'""",
                (status, attempts, last_error, result, now, job_id),
            )
