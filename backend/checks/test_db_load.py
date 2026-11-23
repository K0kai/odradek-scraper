"""Offline: a fila e os agendadores não consultam o banco em loop (no Neon, isso esgotou a cota de compute do mês)."""
import tempfile
import threading
import time
import unittest
from datetime import datetime as real_datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import app
from checks.test_linkedin_filters import AppDbCase
from data.dbutil import describe_db_error
from jobs import job_queue
from jobs.job_queue import JobQueue


class QueueCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # no Windows a thread pode segurar o .db um instante
        self.addCleanup(self.temp.cleanup)
        for name in ("log_event", "LOG"):  # não escreve no event_logs do banco real
            patcher = patch.object(job_queue, name, MagicMock())
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ran = []
        self.queue = JobQueue(
            db_path=str(Path(self.temp.name) / "q.db"),
            handlers={"job_apply": lambda payload: self.ran.append(payload) or "ok"},
            get_settings=lambda: {},
        )
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.queue.stop()
        if self.queue._dispatcher is not None:
            self.queue._dispatcher.join(timeout=3)
        # Os workers ainda logam depois de concluir: espera terminarem antes de desfazer os patches de log_event/LOG.
        with self.queue._lock:
            futures = list(self.queue._inflight.values())
        for future in futures:
            try:
                future.result(timeout=3)
            except Exception:
                pass

    def progress(self, job_id):
        with self.queue._connect() as db:
            return db.execute("SELECT progress_log FROM queue_jobs WHERE id=?", (job_id,)).fetchone()["progress_log"]

    def wait_for(self, condition, seconds=5.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.02)
        return condition()


class DispatcherIdleTests(QueueCase):
    def test_idle_dispatcher_sleeps_instead_of_polling_every_second(self):
        calls = []
        original = self.queue._claim_batch
        self.queue._claim_batch = lambda n: (calls.append(n), original(n))[1]
        self.queue.start()
        time.sleep(0.5)
        self.assertEqual(len(calls), 1)  # antes: uma consulta por segundo, para sempre

    def test_enqueue_wakes_the_sleeping_dispatcher_immediately(self):
        self.queue.start()
        time.sleep(0.3)  # já ocioso
        started = time.monotonic()
        self.queue.enqueue("job_apply", {"job_id": 7})
        self.assertTrue(self.wait_for(lambda: self.ran))
        self.assertLess(time.monotonic() - started, 3.0)  # sem esperar os 15 min do ciclo ocioso

    def test_idle_wait_is_capped_and_follows_the_next_scheduled_retry(self):
        self.assertEqual(self.queue._idle_wait_seconds(), job_queue.IDLE_POLL_SECONDS)
        with self.queue._connect() as db:
            db.execute(
                "INSERT INTO queue_jobs(kind,payload,status,attempts,max_attempts,next_run_at,expires_at,created_at,updated_at)"
                " VALUES('job_apply','{}','retry_wait',1,5,?,?,'now','now')",
                (job_queue._iso(job_queue._utc_now().replace(microsecond=0) + job_queue.timedelta(seconds=40)),
                 job_queue._iso(job_queue._utc_now() + job_queue.timedelta(hours=1))),
            )
        self.queue._claim_batch(3)  # nada vencido: não pega, mas aprende quando acordar
        self.assertTrue(30 <= self.queue._idle_wait_seconds() <= 45)

    def test_database_failure_backs_off_instead_of_hammering_every_second(self):
        calls = []

        def broken(limit):
            calls.append(limit)
            raise OSError("database unavailable")

        self.queue._claim_batch = broken
        self.queue.start()
        time.sleep(0.6)
        self.assertEqual(len(calls), 1)  # próxima tentativa só em >= 5 s

    def test_stop_ends_the_dispatcher_promptly_even_when_idle(self):
        self.queue.start()
        time.sleep(0.2)
        started = time.monotonic()
        self.queue.stop()
        self.queue._dispatcher.join(timeout=3)
        self.assertFalse(self.queue._dispatcher.is_alive())
        self.assertLess(time.monotonic() - started, 3.0)


class ProgressBatchingTests(QueueCase):
    def test_progress_lines_are_batched_into_few_connections_and_none_is_lost(self):
        job_id = self.queue.enqueue("job_apply", {"job_id": 1})
        connections = []
        real_connect = self.queue._connect
        self.queue._connect = lambda: (connections.append(1), real_connect())[1]
        for index in range(10):
            self.queue.append_progress(job_id, f"passo {index}")
        self.assertLessEqual(len(connections), 1)  # só a primeira linha grava na hora; antes eram 10 conexões
        self.queue.flush_progress(job_id)
        self.assertEqual(len(connections), 2)
        lines = self.progress(job_id).splitlines()
        self.assertEqual([line.split(" ", 1)[1] for line in lines], [f"passo {i}" for i in range(10)])  # ordem preservada

    def test_flush_true_writes_pending_lines_right_away(self):
        job_id = self.queue.enqueue("job_apply", {"job_id": 1})
        self.queue.append_progress(job_id, "primeira")
        self.queue.append_progress(job_id, "segunda")  # fica no buffer (menos de 5 s da gravação anterior)
        self.queue.append_progress(job_id, "concluído", flush=True)
        self.assertEqual(len(self.progress(job_id).splitlines()), 3)

    def test_failed_write_keeps_lines_for_the_next_attempt(self):
        job_id = self.queue.enqueue("job_apply", {"job_id": 1})
        real_connect = self.queue._connect
        self.queue._connect = MagicMock(side_effect=OSError("db down"))
        with self.assertRaises(OSError):
            self.queue.append_progress(job_id, "não pode se perder", flush=True)
        self.queue._connect = real_connect
        self.queue.flush_progress(job_id)
        self.assertIn("não pode se perder", self.progress(job_id))

    def test_a_job_that_runs_to_the_end_has_its_full_history_and_no_leftovers(self):
        self.queue.start()
        job_id = self.queue.enqueue("job_apply", {"job_id": 1})
        self.assertTrue(self.wait_for(lambda: "concluído" in self.progress(job_id)))
        self.assertIn("iniciando", self.progress(job_id))
        self.assertTrue(self.wait_for(
            lambda: job_id not in self.queue._progress_buffer and job_id not in self.queue._progress_flushed_at))


class DailyCheckLoopTests(AppDbCase):
    class Stop(Exception):
        pass

    def run_loop(self, claim_result, iterations=4):
        ticks = []

        class FakeDatetime:
            @staticmethod
            def now(tz=None):
                return real_datetime(2026, 11, 23, 10, 0, tzinfo=tz)

        def sleep(_seconds):
            ticks.append(1)
            if len(ticks) >= iterations:
                raise self.Stop

        claim = MagicMock(side_effect=claim_result)
        with patch.object(app, "datetime", FakeDatetime), patch.object(app.time, "sleep", sleep), \
                patch.object(app, "_claim_daily_worth_check", claim), patch.object(app, "sync_portfolio_now"), \
                patch.object(app, "log_event"):
            with self.assertRaises(self.Stop):
                app._daily_worth_check_loop()
        return claim

    def test_day_already_claimed_by_another_instance_is_not_queried_every_minute(self):
        claim = self.run_loop([False] * 4)
        self.assertEqual(claim.call_count, 1)  # antes: um UPDATE por minuto, o dia inteiro

    def test_after_a_successful_claim_the_day_is_done(self):
        claim = self.run_loop([True] + [False] * 3)
        self.assertEqual(claim.call_count, 1)


class DatabaseErrorDescriptionTests(unittest.TestCase):
    def test_quota_auth_and_connection_errors_get_distinct_safe_codes(self):
        cases = {
            "connection failed: Your project has exceeded the compute time quota": "quota_exceeded",
            'FATAL: password authentication failed for user "neondb_owner"': "auth_failed",
            "FATAL: remaining connection slots are reserved": "too_many_connections",
            "connection timeout expired": "timeout",
            'could not translate host name "ep-x.neon.tech" to address': "dns",
            "something else entirely": "connection_failed",
        }
        for text, code in cases.items():
            reason, message = describe_db_error(RuntimeError(text))
            self.assertEqual(reason, code, text)
            self.assertNotIn("neondb_owner", message)  # nunca repete o texto bruto (usuário, host)
            self.assertNotIn("ep-x", message)


if __name__ == "__main__":
    unittest.main()
