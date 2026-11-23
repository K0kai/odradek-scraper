"""Offline: cópia entre bancos, schema Postgres versionado e standby."""
import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg

import app
from checks.test_linkedin_filters import AppDbCase
from data import dbcopy, repository
from data.dbutil import open_sqlite

PDF = b"%PDF-1.4 conteudo de teste"


def new_sqlite(path: Path, resumes_dir: Path) -> None:
    """Cria um SQLite com o schema completo do app (inclusive resume_files e as tabelas do qpid)."""
    resumes_dir.mkdir(parents=True, exist_ok=True)
    with patch.object(repository, "RESUMES_DIR", str(resumes_dir)):
        db = open_sqlite(str(path))
        try:
            dbcopy._prepare_sqlite_target(db)
            db.commit()
        finally:
            db.close()


def seed(path: Path) -> None:
    db = open_sqlite(str(path))
    try:
        db.execute("INSERT INTO settings(key,value) VALUES('custom_key','valor') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        for job_id in (7, 9):
            db.execute(
                "INSERT INTO jobs(id,source,title,company,location,description,url,first_seen_at,status,fingerprint,language,language_confidence)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, "t", f"Vaga {job_id}", "Acme", "Remoto", "desc", f"https://x/{job_id}", "now", "worth", f"fp{job_id}", "pt", 0.9))
        db.execute("INSERT INTO ai_decisions(id,job_id,decided_at,match_score,should_apply,letter_required,reason,provider,model)"
                   " VALUES(31,7,'now',80,1,0,'motivo','gemini','m')")
        db.execute("INSERT INTO cover_letters(job_id,language,provider,model,body,created_at,updated_at) VALUES(7,'pt','g','m','Carta','now','now')")
        db.execute("INSERT INTO resumes(id,language,original_filename,stored_path,file_sha256,created_at,updated_at)"
                   " VALUES(3,'pt','cv.pdf','x',?, 'now','now')", (hashlib.sha256(PDF).hexdigest(),))
        db.execute("INSERT INTO resume_files(resume_id,content) VALUES(3,?)", (PDF,))
        db.execute("INSERT INTO event_logs(created_at,level,source,message) VALUES('now','info','t','log')")
        db.execute("INSERT INTO queue_jobs(kind,payload,status,next_run_at,expires_at,created_at,updated_at,progress_log)"
                   " VALUES('job_apply','{\"job_id\":7}','succeeded','now','now','now','now','a\nb')")
        db.commit()
    finally:
        db.close()


def snapshot(path: Path) -> dict:
    db = sqlite3.connect(str(path))
    try:
        return {table: db.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()
                for table in dbcopy.TABLE_ORDER}
    finally:
        db.close()


class CopyCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict("os.environ", {"DATABASE_URL": ""})
        env.start()
        self.addCleanup(env.stop)
        self.source = self.root / "source" / "jobs.db"
        self.source.parent.mkdir()
        new_sqlite(self.source, self.root / "source" / "resumes")
        seed(self.source)
        self.target = self.root / "target.db"


class CopyTests(CopyCase):
    def test_everything_is_copied_with_ids_and_binary_files_preserved(self):
        counts = dbcopy.copy_database(str(self.source), str(self.target))
        self.assertEqual((counts["jobs"], counts["ai_decisions"], counts["resume_files"], counts["queue_jobs"]), (2, 1, 1, 1))
        copied = sqlite3.connect(str(self.target))
        try:
            self.assertEqual([row[0] for row in copied.execute("SELECT id FROM jobs ORDER BY id")], [7, 9])
            self.assertEqual(copied.execute("SELECT id, job_id FROM ai_decisions").fetchone(), (31, 7))  # FK ainda aponta certo
            self.assertEqual(bytes(copied.execute("SELECT content FROM resume_files").fetchone()[0]), PDF)
            self.assertEqual(copied.execute("SELECT value FROM settings WHERE key='custom_key'").fetchone()[0], "valor")
            self.assertEqual(copied.execute("SELECT progress_log FROM queue_jobs").fetchone()[0], "a\nb")
        finally:
            copied.close()

    def test_a_second_copy_replaces_the_target_instead_of_duplicating(self):
        dbcopy.copy_database(str(self.source), str(self.target))
        extra = open_sqlite(str(self.target))
        extra.execute("INSERT INTO jobs(id,source,title,url,first_seen_at,fingerprint) VALUES(99,'t','Sobra','https://x/99','now','fp99')")
        extra.commit()
        extra.close()
        dbcopy.copy_database(str(self.source), str(self.target))
        self.assertEqual(snapshot(self.target), snapshot(self.source))  # a linha extra do destino sumiu

    def test_a_failure_in_the_middle_leaves_the_target_as_it_was(self):
        dbcopy.copy_database(str(self.source), str(self.target))
        before = snapshot(self.target)
        original = dbcopy._rows

        def flaky(kind, src, table, columns):
            if table == "cover_letters":
                raise RuntimeError("falha no meio")
            return original(kind, src, table, columns)

        update = open_sqlite(str(self.source))
        update.execute("UPDATE jobs SET title='Mudou' WHERE id=7")
        update.commit()
        update.close()
        with patch.object(dbcopy, "_rows", flaky), self.assertRaises(RuntimeError):
            dbcopy.copy_database(str(self.source), str(self.target))
        self.assertEqual(snapshot(self.target), before)

    def test_dry_run_counts_the_source_and_writes_nothing(self):
        counts = dbcopy.copy_database(str(self.source), str(self.target), dry_run=True)
        self.assertEqual((counts["jobs"], counts["resume_files"]), (2, 1))
        self.assertFalse(self.target.exists())

    def test_guards_refuse_dangerous_or_pointless_copies(self):
        with self.assertRaises(ValueError):
            dbcopy.copy_database(str(self.source), str(self.source))
        with self.assertRaises(FileNotFoundError):
            dbcopy.copy_database(str(self.root / "nao-existe.db"), str(self.target))
        url = "postgresql://user:pass@host/db"
        with patch.dict("os.environ", {"DATABASE_URL": url}), patch.object(dbcopy, "_open_source") as opened:
            with self.assertRaises(ValueError) as error:
                dbcopy.copy_database(str(self.source), url)  # destino = banco ativo do app
            opened.assert_not_called()  # recusou antes de abrir qualquer conexão
            self.assertNotIn("pass", str(error.exception))

    def test_old_sqlite_databases_get_their_loose_pdfs_imported(self):
        old = self.root / "old" / "jobs.db"
        old.parent.mkdir()
        new_sqlite(old, self.root / "old" / "unused")
        db = open_sqlite(str(old))
        db.execute("INSERT INTO resumes(id,language,original_filename,stored_path,file_sha256,created_at,updated_at)"
                   " VALUES(1,'pt','cv.pdf',?,?, 'now','now')",
                   (str(self.root / "old" / "resumes" / "resume_pt.pdf"), hashlib.sha256(PDF).hexdigest()))
        db.commit()
        db.close()
        (self.root / "old" / "resumes").mkdir()
        (self.root / "old" / "resumes" / "resume_pt.pdf").write_bytes(PDF)
        dbcopy.copy_database(str(old), str(self.target))
        copied = sqlite3.connect(str(self.target))
        try:
            self.assertEqual(bytes(copied.execute("SELECT content FROM resume_files WHERE resume_id=1").fetchone()[0]), PDF)
        finally:
            copied.close()


class PostgresSchemaTests(CopyCase):
    @staticmethod
    def pg_tables() -> dict[str, list[str]]:
        text = "\n".join(repository.postgres_schema_statements())
        tables = {}
        for name, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\)", text, re.S):
            columns = []
            for line in body.splitlines():
                token = line.strip().split(" ", 1)[0].strip(",")
                if token and token.upper() not in {"PRIMARY", "UNIQUE", "FOREIGN", "CONSTRAINT"}:
                    columns.append(token)
            tables[name] = columns
        return tables

    def test_the_versioned_schema_covers_every_table_and_every_sqlite_column(self):
        pg = self.pg_tables()
        self.assertEqual(set(pg), set(dbcopy.TABLE_ORDER))
        self.assertEqual(set(pg), set(repository.POSTGRES_REQUIRED_TABLES))
        db = sqlite3.connect(str(self.source))
        try:
            for table in dbcopy.TABLE_ORDER:
                sqlite_columns = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                self.assertLessEqual(sqlite_columns, set(pg[table]), f"{table}: coluna do SQLite ausente no schema Postgres")
        finally:
            db.close()

    def test_autoincrement_ids_are_identity_columns_so_inserts_without_an_id_work(self):
        text = "\n".join(repository.postgres_schema_statements())
        for name, body in re.findall(r"CREATE TABLE IF NOT EXISTS (\w+) \((.*?)\n\)", text, re.S):
            for line in body.splitlines():
                if line.strip().startswith("id "):
                    self.assertIn("GENERATED BY DEFAULT AS IDENTITY", line, name)

    def test_statements_are_safe_for_the_placeholder_translator_and_split_cleanly(self):
        statements = repository.postgres_schema_statements()
        self.assertGreaterEqual(len(statements), 25)
        for statement in statements:
            self.assertNotIn("?", statement)
            self.assertNotIn("%", statement)
            self.assertNotIn(";", statement.rstrip(";"))
        partial = [s for s in statements if "qpid_one_active_application" in s]
        self.assertIn("WHERE state != 'failed-before-click'", partial[0])  # índice parcial intacto

    def test_an_empty_postgres_gets_the_whole_schema_applied(self):
        executed = []
        db = MagicMock()
        db.execute.side_effect = lambda sql, *a: executed.append(sql) or MagicMock(fetchall=lambda: [])
        self.assertEqual(repository.missing_postgres_tables(db), sorted(repository.POSTGRES_REQUIRED_TABLES))
        executed.clear()
        repository.apply_postgres_schema(db)
        self.assertEqual(len(executed), len(repository.postgres_schema_statements()))


class StandbyTests(AppDbCase):
    URL_A, URL_B = "postgresql://u:p@primary/db", "postgresql://u:p@standby/db"

    def test_sync_requires_both_databases_and_refuses_to_copy_onto_itself(self):
        with patch.dict("os.environ", {"DATABASE_URL": self.URL_A, "DATABASE_URL_STANDBY": ""}):
            with self.assertRaises(ValueError):
                dbcopy.sync_standby()
        with patch.dict("os.environ", {"DATABASE_URL": "", "DATABASE_URL_STANDBY": self.URL_B}):
            with self.assertRaises(ValueError):
                dbcopy.sync_standby()
        with patch.dict("os.environ", {"DATABASE_URL": self.URL_A, "DATABASE_URL_STANDBY": self.URL_A}):
            with self.assertRaises(ValueError):
                dbcopy.sync_standby()

    def test_sync_copies_from_the_active_database_to_the_standby(self):
        with patch.dict("os.environ", {"DATABASE_URL": self.URL_A, "DATABASE_URL_STANDBY": self.URL_B}), \
                patch.object(dbcopy, "copy_database", return_value={"jobs": 5, "settings": 3}) as copy:
            summary = dbcopy.sync_standby()
        self.assertEqual(copy.call_args.args, (self.URL_A, self.URL_B))
        self.assertEqual((summary["rows"], summary["tables"]["jobs"]), (8, 5))

    def test_the_result_is_recorded_for_the_usage_screen(self):
        with patch.dict("os.environ", {"DATABASE_URL_STANDBY": self.URL_B}), \
                patch.object(dbcopy, "sync_standby", return_value={"tables": {"jobs": 2}, "rows": 2, "seconds": 1.5}):
            result = app.run_standby_sync()
            status = app.standby_status()
        self.assertTrue(result["ok"])
        self.assertEqual((status["configured"], status["last"]["rows"], status["last"]["ok"]), (True, 2, True))

    def test_a_failure_is_recorded_without_leaking_connection_details(self):
        error = psycopg.OperationalError('connection to server at "ep-secret.neon.tech" failed: exceeded the compute time quota user=neondb_owner')
        with patch.dict("os.environ", {"DATABASE_URL_STANDBY": self.URL_B}), patch.object(dbcopy, "sync_standby", side_effect=error):
            result = app.run_standby_sync()
            raw = repository.get_setting("standby_last_sync_json")
        self.assertFalse(result["ok"])
        self.assertEqual(json.loads(raw)["ok"], False)
        for text in (result["error"], raw):
            self.assertNotIn("ep-secret", text)
            self.assertNotIn("neondb_owner", text)
        self.assertIn("cota", result["error"])  # a causa legível chega ao painel

    def test_the_daily_claim_is_atomic(self):
        self.assertTrue(app._claim_daily_standby_sync("2026-11-24"))
        self.assertFalse(app._claim_daily_standby_sync("2026-11-24"))  # outra instância não repete
        self.assertTrue(app._claim_daily_standby_sync("2026-11-25"))


if __name__ == "__main__":
    unittest.main()
