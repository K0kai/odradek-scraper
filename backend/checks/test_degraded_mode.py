"""Offline: o painel continua de pé quando o banco cai (cota esgotada, senha, rede) e se recupera sozinho."""
import time
import unittest
from unittest.mock import MagicMock, patch

import psycopg
from fastapi.testclient import TestClient

from data import db_state, dbutil
from data.dbutil import PostgresConnection, describe_db_error
from web import api_asgi, panel_queries, panel_snapshot

QUOTA = "connection failed: Your project has exceeded the compute time quota"


def quota_error():
    return psycopg.OperationalError(QUOTA)


class StateCase(unittest.TestCase):
    def setUp(self):
        db_state.reset()
        panel_snapshot.clear()
        self.addCleanup(db_state.reset)
        self.addCleanup(panel_snapshot.clear)


class PostgresConnectionTests(unittest.TestCase):
    def test_leaving_the_block_commits_and_returns_the_connection_to_the_pool(self):
        conn, pool = MagicMock(), MagicMock()
        with PostgresConnection(conn, pool) as db:
            db.execute("SELECT ? FROM t WHERE x = '?'", (1,))
        conn.execute.assert_called_once_with("SELECT %s FROM t WHERE x = '?'", (1,))  # ? traduzido, texto literal preservado
        conn.commit.assert_called_once()
        pool.putconn.assert_called_once_with(conn)
        conn.close.assert_not_called()  # volta ao pool em vez de fechar

    def test_an_error_rolls_back_still_releases_and_propagates(self):
        conn, pool = MagicMock(), MagicMock()
        with self.assertRaises(ValueError):
            with PostgresConnection(conn, pool):
                raise ValueError("boom")
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()
        pool.putconn.assert_called_once_with(conn)

    def test_a_failing_commit_still_releases_the_connection(self):
        conn, pool = MagicMock(), MagicMock()
        conn.commit.side_effect = psycopg.OperationalError("commit failed")
        with self.assertRaises(psycopg.OperationalError):
            with PostgresConnection(conn, pool):
                pass
        pool.putconn.assert_called_once_with(conn)

    def test_close_is_idempotent_and_unpooled_connections_are_really_closed(self):
        conn, pool = MagicMock(), MagicMock()
        wrapped = PostgresConnection(conn, pool)
        wrapped.close()
        wrapped.close()
        pool.putconn.assert_called_once()
        direct = MagicMock()
        with PostgresConnection(direct):
            pass
        direct.close.assert_called_once()


class ConnectionFailureTests(StateCase):
    def test_the_real_cause_survives_the_pool_and_later_calls_fail_fast(self):
        pool = MagicMock()
        pool.getconn.side_effect = psycopg.OperationalError("couldn't get a connection after 10.00 sec")
        with patch.object(dbutil, "_get_pool", return_value=pool), \
                patch.object(psycopg, "connect", side_effect=quota_error()) as direct:
            with self.assertRaises(psycopg.OperationalError) as first:
                dbutil._postgres_connection("postgresql://u:p@h/db", 60)
            self.assertEqual(describe_db_error(first.exception)[0], "quota_exceeded")  # não "pool timeout"
            self.assertTrue(db_state.is_down())
            with self.assertRaises(psycopg.OperationalError) as second:
                dbutil._postgres_connection("postgresql://u:p@h/db", 60)
            self.assertEqual(describe_db_error(second.exception)[0], "quota_exceeded")  # motivo preservado
            self.assertEqual((pool.getconn.call_count, direct.call_count), (1, 1))  # a segunda nem tentou conectar

    def test_the_database_coming_back_clears_the_state(self):
        db_state.mark_down("quota_exceeded", "msg")
        pool = MagicMock()
        with patch.object(dbutil, "_get_pool", return_value=pool), patch.object(db_state, "FAIL_FAST_SECONDS", 0.0):
            connection = dbutil._postgres_connection("postgresql://u:p@h/db", 60)
        self.assertIsInstance(connection, PostgresConnection)
        self.assertFalse(db_state.is_down())

    def test_connect_timeout_is_capped_so_a_dead_database_does_not_hold_threads_for_a_minute(self):
        pool = MagicMock()
        with patch.object(dbutil, "_get_pool", return_value=pool):
            dbutil._postgres_connection("postgresql://u:p@h/db", 60)
        self.assertEqual(pool.getconn.call_args.kwargs["timeout"], dbutil.CONNECT_TIMEOUT_CAP)

    def test_pool_can_be_disabled_with_an_env_var(self):
        conn = MagicMock()
        with patch.dict("os.environ", {"DB_POOL_DISABLED": "1"}), patch.object(dbutil, "_get_pool") as get_pool, \
                patch.object(psycopg, "connect", return_value=conn) as direct:
            wrapped = dbutil._postgres_connection("postgresql://u:p@h/db", 60)
        get_pool.assert_not_called()
        self.assertEqual(direct.call_args.kwargs["prepare_threshold"], None)
        wrapped.close()
        conn.close.assert_called_once()


class SnapshotTests(StateCase):
    def test_a_database_failure_serves_the_last_good_payload_marked_stale(self):
        loader = MagicMock(side_effect=[{"counts": {"new": 3}}, quota_error()])
        fresh = panel_snapshot.serve("dashboard", loader)
        self.assertEqual(fresh, {"counts": {"new": 3}})
        stale = panel_snapshot.serve("dashboard", loader)
        self.assertEqual(stale["counts"], {"new": 3})
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["database"], "quota_exceeded")
        self.assertTrue(stale["as_of"])
        self.assertNotIn("Your project", str(stale))  # sem repetir o texto bruto do driver

    def test_without_a_previous_payload_the_error_reaches_the_503_handler(self):
        with self.assertRaises(psycopg.OperationalError):
            panel_snapshot.serve("never-seen", MagicMock(side_effect=quota_error()))

    def test_other_errors_are_not_masked_by_a_stale_copy(self):
        panel_snapshot.serve("k", lambda: {"ok": 1})
        with self.assertRaises(ValueError):
            panel_snapshot.serve("k", MagicMock(side_effect=ValueError("bug")))

    def test_each_filter_combination_keeps_its_own_copy_and_the_cache_is_bounded(self):
        panel_snapshot.serve("jobs:1", lambda: {"page": 1})
        panel_snapshot.serve("jobs:2", lambda: {"page": 2})
        self.assertEqual(panel_snapshot.serve("jobs:2", MagicMock(side_effect=quota_error()))["page"], 2)
        for index in range(panel_snapshot.MAX_ENTRIES + 10):
            panel_snapshot.serve(f"k{index}", lambda: {"x": 1})
        self.assertLessEqual(len(panel_snapshot._last_good), panel_snapshot.MAX_ENTRIES)


class ApiTests(StateCase):
    def setUp(self):
        super().setUp()
        auth = patch.object(api_asgi, "requires_auth", return_value=False)
        auth.start()
        self.addCleanup(auth.stop)
        self.client = TestClient(api_asgi.api, raise_server_exceptions=False)  # sem lifespan

    def test_health_is_liveness_only_and_never_touches_the_database(self):
        with patch.object(api_asgi.core, "connect", side_effect=AssertionError("tocou o banco")):
            response = self.client.get("/health")
        self.assertEqual((response.status_code, response.json()["ok"]), (200, True))

    def test_health_db_reports_the_cause_without_leaking_connection_details(self):
        error = psycopg.OperationalError(QUOTA + " host=ep-secret.neon.tech user=neondb_owner")
        with patch.object(api_asgi.core, "connect", side_effect=error):
            response = self.client.get("/health/db")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["reason"], "quota_exceeded")
        self.assertNotIn("ep-secret", response.text)
        self.assertNotIn("neondb_owner", response.text)

    def test_reads_degrade_to_the_last_state_and_writes_answer_503_with_the_cause(self):
        calls = iter([{"counts": {"new": 1}, "jobs": []}])

        def dashboard():
            try:
                return next(calls)
            except StopIteration:
                raise quota_error()

        with patch.object(panel_queries, "dashboard_api_payload", dashboard):
            first = self.client.get("/api/dashboard")
            second = self.client.get("/api/dashboard")
        self.assertEqual((first.status_code, "stale" in first.json()), (200, False))
        self.assertEqual((second.status_code, second.json()["stale"], second.json()["counts"]), (200, True, {"new": 1}))
        self.assertTrue(db_state.is_down())
        with patch.object(panel_queries, "connect", side_effect=quota_error()):  # nunca o jobs.db real
            write = self.client.post("/api/triage/reviewed", json={"ids": [1]}, headers={"origin": "http://testserver"})
        self.assertEqual(write.status_code, 503)
        self.assertEqual(write.json()["database"], "quota_exceeded")

    def test_standby_sync_requires_configuration(self):
        with patch.dict("os.environ", {"DATABASE_URL_STANDBY": ""}):
            response = self.client.post("/api/db/standby-sync", headers={"origin": "http://testserver"})
        self.assertEqual(response.status_code, 400)


class BootTests(StateCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.fail_schema = True
        steps = {
            "initialize": MagicMock(side_effect=self.schema),
            "_boot_logging": MagicMock(),
            "_boot_queue": MagicMock(),
            "_boot_worth_check_scheduler": MagicMock(),
        }
        self.steps = steps
        for name, mock in steps.items():
            patcher = patch.object(api_asgi.core, name, mock)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (patch.object(api_asgi, "BOOT_RETRY_MIN_SECONDS", 0.05),
                        patch.object(api_asgi, "_flush_on_shutdown"),
                        patch.object(api_asgi.core.queue, "stop")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def schema(self):
        if self.fail_schema:
            raise quota_error()

    def test_the_port_opens_without_a_database_and_services_start_when_it_returns(self):
        with TestClient(api_asgi.api) as client:  # roda o lifespan: antes o processo morria aqui
            self.assertEqual(client.get("/health").status_code, 200)
            self.steps["_boot_queue"].assert_not_called()  # nada que dependa do banco subiu ainda
            self.fail_schema = False  # o banco voltou
            deadline = time.monotonic() + 3
            while len(api_asgi._boot_done) < 4 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(api_asgi._boot_done, {"schema", "logging", "queue", "scheduler"})
        self.steps["_boot_queue"].assert_called_once()
        self.steps["_boot_worth_check_scheduler"].assert_called_once()  # o agendador sobe uma vez só

    def test_a_healthy_database_boots_everything_synchronously_as_before(self):
        self.fail_schema = False
        with TestClient(api_asgi.api):
            self.assertEqual(api_asgi._boot_done, {"schema", "logging", "queue", "scheduler"})

    def test_steps_that_already_finished_are_not_repeated_on_retry(self):
        self.steps["_boot_logging"].side_effect = [quota_error(), None]
        self.fail_schema = False
        with TestClient(api_asgi.api):
            deadline = time.monotonic() + 3
            while len(api_asgi._boot_done) < 4 and time.monotonic() < deadline:
                time.sleep(0.02)
        self.steps["initialize"].assert_called_once()
        self.assertEqual(self.steps["_boot_logging"].call_count, 2)


if __name__ == "__main__":
    unittest.main()
