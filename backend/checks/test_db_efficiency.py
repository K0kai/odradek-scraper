"""Offline: caches, lotes e consultas enxutas que mantêm o uso do banco em rede mínimo (Neon/Supabase)."""
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from checks.test_linkedin_filters import AppDbCase
from data import repository
from data.secrets import SecretStore
from integrations import usage_tracker
from jobs import job_queue
from jobs.job_queue import JobQueue
from web import panel_log, panel_queries


def counting(connect):
    """Envolve uma função connect() e conta quantas conexões foram abertas."""
    calls = []

    def wrapper(*args, **kwargs):
        calls.append(1)
        return connect(*args, **kwargs)

    wrapper.calls = calls
    return wrapper


class SettingsCacheTests(AppDbCase):
    """Só vale para banco em rede: using_postgres() é simulado, a conexão continua sendo o SQLite do teste."""

    def setUp(self):
        super().setUp()
        patcher = patch.object(repository, "using_postgres", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        repository.invalidate_settings_cache()
        self.addCleanup(repository.invalidate_settings_cache)
        self.connect = counting(repository.connect)
        connect_patch = patch.object(repository, "connect", self.connect)
        connect_patch.start()
        self.addCleanup(connect_patch.stop)

    def test_settings_are_read_once_and_then_served_from_memory(self):
        first = repository.settings()
        for _ in range(5):
            repository.settings()
        self.assertEqual(len(self.connect.calls), 1)  # antes: uma conexão e a tabela inteira por leitura
        first["keywords"] = "mutado"
        self.assertNotEqual(repository.settings()["keywords"], "mutado")  # devolve cópias

    def test_writes_update_the_cache_without_rereading_the_table(self):
        repository.settings()
        repository.set_setting("keywords", "novo valor")
        self.assertEqual(repository.settings()["keywords"], "novo valor")
        self.assertEqual(len(self.connect.calls), 2)  # 1 leitura + 1 gravação, nenhuma releitura
        repository.set_settings_many({"a_key": "1", "b_key": "2"})
        self.assertEqual((repository.get_setting("a_key"), repository.get_setting("b_key")), ("1", "2"))
        self.assertEqual(len(self.connect.calls), 3)  # as duas chaves saíram numa conexão só

    def test_get_setting_uses_the_warm_cache_and_reads_a_single_row_when_cold(self):
        self.assertEqual(repository.get_setting("auto_apply", "x"), repository.DEFAULT_SETTINGS["auto_apply"])
        self.assertEqual(repository.get_setting("missing_key", "padrão"), "padrão")
        cold = len(self.connect.calls)
        repository.settings()
        repository.get_setting("auto_apply")
        repository.get_setting("keywords")
        self.assertEqual(len(self.connect.calls), cold + 1)  # só a leitura completa; os get_setting vieram do cache

    def test_database_outage_serves_the_last_known_settings(self):
        known = repository.settings()
        with patch.object(repository, "SETTINGS_CACHE_SECONDS", 0.0), \
                patch.object(repository, "connect", MagicMock(side_effect=OSError("db down"))):
            self.assertEqual(repository.settings(), known)  # cache vencido + banco fora: usa o último valor conhecido
        repository.invalidate_settings_cache()
        with patch.object(repository, "connect", MagicMock(side_effect=OSError("db down"))):
            with self.assertRaises(OSError):
                repository.settings()  # sem nada em memória, o erro aparece


class SecretCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        from data.dbutil import open_sqlite

        self.path = str(Path(self.temp.name) / "s.db")
        with open_sqlite(self.path) as db:
            db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.opened = []
        self.db_down = False
        env = patch.dict(os.environ, {"SECRET_ENCRYPTION_KEY": "", "GEMINI_API_KEY": "", "GOOGLE_API_KEY": "",
                                      "APIFY_TOKEN": "", "APIFY_API_TOKEN": ""})
        env.start()
        self.addCleanup(env.stop)
        # O fallback final é o keyring do sistema, onde ficam as chaves reais do usuário: nunca consultar nos testes.
        keyring = patch.object(SecretStore, "_secret_from_keyring", return_value="")
        keyring.start()
        self.addCleanup(keyring.stop)

    def connect(self):
        if self.db_down:
            raise OSError("db down")
        from data.dbutil import open_sqlite

        self.opened.append(1)
        return open_sqlite(self.path)

    def test_database_lookup_is_cached_and_set_invalidates_it(self):
        from cryptography.fernet import Fernet

        with patch.dict(os.environ, {"SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
            store = SecretStore(self.connect, cache_ttl=300.0)
            store.set("gemini", "chave-1")
            self.opened.clear()
            for _ in range(5):
                self.assertEqual(store.get("gemini"), "chave-1")
            self.assertEqual(len(self.opened), 1)  # antes: uma conexão por chamada
            store.set("gemini", "chave-2")
            self.assertEqual(store.get("gemini"), "chave-2")

    def test_without_cache_ttl_every_call_reads_the_database(self):
        store = SecretStore(self.connect)
        store.get("gemini")
        store.get("gemini")
        self.assertEqual(len(self.opened), 2)

    def test_environment_is_evaluated_every_time_and_a_dead_database_falls_back_to_it(self):
        store = SecretStore(self.connect, cache_ttl=300.0)
        self.db_down = True
        with patch.dict(os.environ, {"GEMINI_API_KEY": "da-env"}):
            self.assertEqual(store.get("gemini"), "da-env")  # banco fora e sem cache: usa a variável de ambiente
        self.assertEqual(store.get("gemini"), "")


class UsageBatchingTests(unittest.TestCase):
    def setUp(self):
        saved = (usage_tracker._PERSIST, usage_tracker._BATCHED, usage_tracker._LAST_FLUSH)
        self.addCleanup(lambda: (setattr(usage_tracker, "_PERSIST", saved[0]), setattr(usage_tracker, "_BATCHED", saved[1]),
                                 setattr(usage_tracker, "_LAST_FLUSH", saved[2]), usage_tracker._PENDING.clear()))
        usage_tracker._PENDING.clear()
        self.raw = ""
        self.reads, self.writes = [], []

        def get_raw():
            self.reads.append(1)
            return self.raw

        def set_raw(value):
            self.writes.append(1)
            self.raw = value

        usage_tracker.configure_persistence(get_raw, set_raw, batched=True)

    def record(self, **extra):
        usage_tracker.try_record_ai_result(provider="gemini", model="m", ok=True, **extra)

    def test_calls_are_counted_in_memory_and_written_in_one_read_plus_one_write(self):
        for _ in range(7):
            self.record()
        self.assertEqual((len(self.reads), len(self.writes)), (0, 0))  # antes: 2 conexões por chamada de IA
        usage_tracker.flush_pending()
        self.assertEqual((len(self.reads), len(self.writes)), (1, 1))
        store = usage_tracker.load_store(self.raw)
        self.assertEqual(sum(bucket["calls_ok"] for bucket in store["months"].values()), 7)
        usage_tracker.flush_pending()  # nada pendente: não toca no banco
        self.assertEqual((len(self.reads), len(self.writes)), (1, 1))

    def test_the_flush_interval_triggers_a_write_on_its_own(self):
        usage_tracker._LAST_FLUSH = time.monotonic() - usage_tracker.FLUSH_INTERVAL_SECONDS - 1
        self.record()
        self.assertEqual(len(self.writes), 1)

    def test_a_failed_write_keeps_the_counts_for_the_next_flush(self):
        self.record()
        self.record()
        failing = MagicMock(side_effect=OSError("db down"))
        usage_tracker._PERSIST = (lambda: "", failing)
        with self.assertRaises(OSError):
            usage_tracker.flush_pending()
        usage_tracker._PERSIST = (lambda: self.raw, lambda value: setattr(self, "raw", value))
        usage_tracker.flush_pending()
        store = usage_tracker.load_store(self.raw)
        self.assertEqual(sum(bucket["calls_ok"] for bucket in store["months"].values()), 2)

    def test_the_timestamp_of_the_call_is_kept_not_the_flush_time(self):
        from datetime import datetime, timezone

        with patch.object(usage_tracker, "_utc_now", return_value=datetime(2026, 1, 31, 23, 59, tzinfo=timezone.utc)):
            self.record()
        usage_tracker.flush_pending()
        self.assertIn("gemini|2026-01-31", usage_tracker.load_store(self.raw)["days"])


class LogBatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.path = str(Path(self.temp.name) / "logs.db")
        patches = [
            patch.object(panel_log, "DB_PATH", self.path),
            patch.object(panel_log, "_batched", return_value=True),
            patch.object(panel_log, "_ensure_flusher"),  # sem thread: o teste chama flush_logs() quando quer
            patch.object(panel_log, "MAX_ROWS", 10),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        panel_log._pending.clear()
        self.addCleanup(panel_log._pending.clear)
        panel_log._inserted_since_prune = panel_log.PRUNE_EVERY
        panel_log.ensure_log_table()
        self.connect = counting(panel_log._connect)
        patcher = patch.object(panel_log, "_connect", self.connect)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_events_wait_in_memory_and_go_out_in_one_connection(self):
        for index in range(8):
            panel_log.log_event("info", "t", f"evento {index}")
        self.assertEqual(len(self.connect.calls), 0)  # antes: uma conexão por evento
        self.assertEqual(len(panel_log.pending_logs()), 8)
        self.assertTrue(panel_log.flush_logs())
        self.assertEqual(len(self.connect.calls), 1)
        rows = panel_log.list_logs(50)
        self.assertEqual([row["message"] for row in rows][:2], ["evento 7", "evento 6"])  # mais novo primeiro

    def test_the_table_is_pruned_to_the_newest_rows_without_a_full_scan_each_time(self):
        for index in range(25):
            panel_log.log_event("info", "t", f"evento {index}")
        panel_log.flush_logs()
        rows = panel_log.list_logs(50)
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["message"], "evento 24")
        self.assertEqual(rows[-1]["message"], "evento 15")

    def test_database_outage_keeps_events_and_the_tab_shows_this_session(self):
        panel_log.log_event("warning", "t", "durante a queda")
        with patch.object(panel_log, "_connect", MagicMock(side_effect=OSError("db down"))):
            self.assertFalse(panel_log.flush_logs())
            shown = panel_log.list_logs(10)
        self.assertEqual([row["message"] for row in shown], ["durante a queda"])
        self.assertTrue(panel_log.flush_logs())  # banco de volta: nada se perdeu
        self.assertEqual(panel_log.list_logs(10)[0]["message"], "durante a queda")

    def test_a_full_buffer_wakes_the_flusher_immediately(self):
        panel_log._flush_now.clear()
        for index in range(panel_log.FLUSH_AT_COUNT):
            panel_log.log_event("info", "t", f"e{index}")
        self.assertTrue(panel_log._flush_now.is_set())
        panel_log._flush_now.clear()
        panel_log._flush_wake.clear()


class QueueSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        for name in ("log_event", "LOG"):
            patcher = patch.object(job_queue, name, MagicMock())
            patcher.start()
            self.addCleanup(patcher.stop)
        self.started, self.release = [], threading.Event()
        self.behavior = lambda payload: "ok"

        def handler(payload):
            self.started.append(payload["job_id"])
            return self.behavior(payload)

        self.queue = JobQueue(
            db_path=str(Path(self.temp.name) / "q.db"), handlers={"job_apply": handler},
            get_settings=lambda: {"queue_max_workers": "1"},
        )
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.release.set()
        self.queue.stop()
        if self.queue._dispatcher is not None:
            self.queue._dispatcher.join(timeout=3)
        with self.queue._lock:
            futures = list(self.queue._inflight.values())
        for future in futures:
            try:
                future.result(timeout=3)
            except Exception:
                pass

    def wait_for(self, condition, seconds=5.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.02)
        return condition()

    def test_a_finishing_job_wakes_the_dispatcher_so_the_next_one_starts_without_polling(self):
        claims = []
        original = self.queue._claim_batch
        self.queue._claim_batch = lambda n: (claims.append(n), original(n))[1]

        def hold(payload):
            if payload["job_id"] == 1:
                self.release.wait(5)
            return "ok"

        self.behavior = hold
        self.queue.start()
        self.queue.enqueue("job_apply", {"job_id": 1}, dedupe_key="apply:1")
        self.queue.enqueue("job_apply", {"job_id": 2}, dedupe_key="apply:2")
        self.assertTrue(self.wait_for(lambda: self.started == [1]))
        time.sleep(1.5)  # com 1 worker ocupado o dispatcher não deve consultar o banco em loop
        self.assertEqual(self.started, [1])
        busy_claims = len(claims)
        self.assertLessEqual(busy_claims, 4)  # antes: ~1 por segundo enquanto houvesse um job rodando
        released = time.monotonic()
        self.release.set()
        self.assertTrue(self.wait_for(lambda: self.started == [1, 2]))
        self.assertLess(time.monotonic() - released, 3.0)  # o fim do job 1 acordou o dispatcher

    def test_an_ai_quota_failure_pauses_that_kind_instead_of_failing_the_whole_queue(self):
        def quota_error(payload):
            raise RuntimeError("429 quota exceeded")

        self.behavior = quota_error
        self.queue.start()
        self.queue.enqueue("job_apply", {"job_id": 1}, dedupe_key="apply:1")
        self.queue.enqueue("job_apply", {"job_id": 2}, dedupe_key="apply:2")
        self.queue.enqueue("job_apply", {"job_id": 3}, dedupe_key="apply:3")
        self.assertTrue(self.wait_for(lambda: self.started == [1]))
        time.sleep(1.5)
        self.assertEqual(self.started, [1])  # os outros ficam esperando em vez de falharem em série
        self.assertIn("job_apply", self.queue._active_pauses())
        self.assertGreaterEqual(self.queue._idle_wait_seconds(), job_queue.KIND_PAUSE_MIN_SECONDS - 5)

    def test_terminal_items_older_than_the_retention_window_are_purged(self):
        old = job_queue._iso(job_queue._utc_now() - job_queue.timedelta(days=30))
        recent = job_queue._iso(job_queue._utc_now())
        with self.queue._connect() as db:
            for status, stamp in (("succeeded", old), ("failed", old), ("succeeded", recent), ("pending", old)):
                db.execute(
                    "INSERT INTO queue_jobs(kind,payload,status,attempts,max_attempts,next_run_at,expires_at,created_at,updated_at)"
                    " VALUES('job_apply','{}',?,0,5,?,?,?,?)", (status, stamp, recent, stamp, stamp))
        self.assertEqual(self.queue.purge_terminal_older_than(14), 2)  # só terminados e antigos
        self.assertEqual(sorted(self.queue.counts().items()), [("pending", 1), ("succeeded", 1)])


class LeanPayloadTests(AppDbCase):
    def setUp(self):
        super().setUp()
        self.connect = counting(panel_queries.connect)
        patcher = patch.object(panel_queries, "connect", self.connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        panel_queries.queue._ensure_schema()

    def test_dashboard_uses_one_connection_and_leaves_out_heavy_columns(self):
        self.seed_job(id=1, description="x" * 5000, notes="nota longa")
        from jobs.copilot_asks import create_ask

        create_ask(repository.connect, job_id=None, question="Qual é o seu pronome?", now_iso="now")
        self.connect.calls.clear()
        payload = panel_queries.dashboard_api_payload()
        self.assertEqual(len(self.connect.calls), 1)
        job = payload["jobs"][0]
        self.assertEqual((job["title"], job["description"], job["notes"], job["cover_letter"]), ("Estágio em Suporte de TI", "", "", ""))
        self.assertEqual(payload["pending_ask"]["question"], "Qual é o seu pronome?")  # sem poll próprio do assistente
        self.assertEqual(payload["counts"]["new"], 1)

    def test_queue_list_is_one_connection_with_a_short_tail_and_full_history_on_demand(self):
        self.seed_job(id=1)
        queue = panel_queries.queue
        item_id = queue.enqueue("job_apply", {"job_id": 1}, dedupe_key="apply:1")
        lines = [f"10:00:{index % 60:02d} passo {index} " + "x" * 60 for index in range(120)]
        with queue._connect() as db:
            db.execute("UPDATE queue_jobs SET progress_log=? WHERE id=?", ("\n".join(lines), item_id))
        self.connect.calls.clear()
        payload = panel_queries.queue_api_payload()
        self.assertEqual(len(self.connect.calls), 1)  # lista + contagens + contexto da vaga numa conexão
        item = next(entry for entry in payload["items"] if entry["id"] == item_id)
        self.assertLessEqual(len(item["progress"]), job_queue.PROGRESS_TAIL_CHARS)
        self.assertTrue(item["progress_more"])
        self.assertEqual(item["last_step"], lines[-1])
        self.assertTrue(all(line in lines for line in item["progress"].splitlines()))  # sem linha cortada no início
        self.assertEqual(payload["counts"]["pending"], 1)
        full = panel_queries.queue_progress_api_payload(item_id)["progress"].splitlines()
        self.assertEqual((len(full), full[-1]), (80, lines[-1]))
        self.assertIsNone(panel_queries.queue_progress_api_payload(999999))

    def test_short_histories_are_returned_whole(self):
        queue = panel_queries.queue
        item_id = queue.enqueue("job_apply", {"job_id": 5}, dedupe_key="apply:5")
        with queue._connect() as db:
            db.execute("UPDATE queue_jobs SET progress_log='a\nb' WHERE id=?", (item_id,))
        item = next(entry for entry in panel_queries.queue_api_payload()["items"] if entry["id"] == item_id)
        self.assertEqual((item["progress"], item["progress_more"], item["last_step"]), ("a\nb", False, "b"))

    def test_resumes_payload_keeps_its_contract_without_the_extracted_text(self):
        self.seed_resume("pt")
        payload = panel_queries.resumes_api_payload()
        self.assertEqual((payload["pt"]["filename"], payload["pt"]["status"], payload["pt"]["summary"]), ("cv.pdf", "ok", "Resumo"))
        self.assertIsNone(payload["en"])
        self.assertEqual(set(payload["pt"]), {"language", "filename", "status", "message", "error", "summary",
                                              "analysis_json", "analyzed_at", "provider", "model"})


class UsagePayloadTests(AppDbCase):
    def test_usage_screen_flushes_pending_counts_before_reading(self):
        saved = (usage_tracker._PERSIST, usage_tracker._BATCHED, usage_tracker._LAST_FLUSH)
        self.addCleanup(lambda: (setattr(usage_tracker, "_PERSIST", saved[0]), setattr(usage_tracker, "_BATCHED", saved[1]),
                                 setattr(usage_tracker, "_LAST_FLUSH", saved[2]), usage_tracker._PENDING.clear()))
        usage_tracker.configure_persistence(
            lambda: repository.get_setting(usage_tracker.STORE_KEY, ""),
            lambda value: repository.set_setting(usage_tracker.STORE_KEY, value), batched=True)
        usage_tracker.try_record_ai_result(provider="gemini", model="m", ok=True)
        usage_tracker.try_record_ai_result(provider="gemini", model="m", ok=True)
        with patch.object(panel_queries, "fresh_apify_quota", return_value={}), \
                patch.object(panel_queries, "tokenharbor_quota_snapshot", return_value={"configured": False}):
            payload = panel_queries.usage_api_payload()
        self.assertEqual(payload["standby"], {"configured": False, "last": None})
        calls = json.dumps(payload["ai"])
        self.assertIn("2", calls)


if __name__ == "__main__":
    unittest.main()
