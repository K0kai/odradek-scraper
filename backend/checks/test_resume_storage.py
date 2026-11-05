"""Offline regressions: no production database, AI requests or real applications."""
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import patch

from reportlab.pdfgen import canvas
from fastapi import FastAPI
from data import repository
from candidate import resume_pipeline as pipeline
from candidate.resume_storage import ensure_table, read_pdf, backfill, materialize_pdf
from web.qpid_applications import register


def pdf(text="Synthetic candidate experience developing software and accessible web applications."):
    stream = BytesIO()
    document = canvas.Canvas(stream)
    document.drawString(40, 700, text)
    document.save()
    return stream.getvalue()


class ResumeStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "test.db"
        self.files = self.root / "resumes"
        self.env = patch.dict(os.environ, {"DATABASE_URL": ""})
        self.env.start(); self.addCleanup(self.env.stop)
        self.settings = patch.multiple(repository, DB_PATH=self.database, RESUMES_DIR=str(self.files))
        self.settings.start(); self.addCleanup(self.settings.stop)
        repository.initialize()
        self.raw = pdf()

    def connect(self):
        return repository.connect()

    def upload(self, language="pt", content=None):
        with self.connect() as db:
            result = pipeline.persist_resume_upload(db, language=language, original_filename="synthetic.pdf",
                raw_bytes=content or self.raw, resumes_dir=str(self.files), now_iso="2026-11-05T10:00:00")
        return result

    def test_upload_survives_missing_filesystem_and_database_reopen(self):
        self.upload()
        self.assertEqual(list(self.files.glob("*.pdf")), [])
        with self.connect() as db:
            row = pipeline.get_resume(db, "pt")
            self.assertEqual(read_pdf(db, row, str(self.root / "deleted-directory")), self.raw)
            self.assertEqual(hashlib.sha256(self.raw).hexdigest(), row["file_sha256"])
            json.dumps(dict(row))  # Binary contents must not leak into metadata/context JSON.
            self.assertEqual(Path(materialize_pdf(db, row, self.files)).read_bytes(), self.raw)

    def test_reupload_same_pdf_reuses_analysis_and_restores_binary(self):
        self.upload()
        with self.connect() as db:
            db.execute("UPDATE resumes SET analysis_summary='Already analyzed' WHERE language='pt'")
            db.execute("DELETE FROM resume_files")
        self.assertFalse(self.upload()[2])
        with self.connect() as db:
            self.assertEqual(read_pdf(db, pipeline.get_resume(db, "pt"), self.files), self.raw)

    def test_replacing_one_language_keeps_other_and_analysis_preserves_pdf(self):
        self.upload(); self.upload("en")
        replacement = pdf("Different synthetic PDF with enough readable text for extraction and testing.")
        self.upload(content=replacement)
        with self.connect() as db, patch.object(pipeline, "analyze_resume_text", return_value={"summary":"Synthetic analysis"}):
            pipeline.run_resume_analysis(db, language="pt", provider="fake", model="fake", api_key="", now_iso="2026-11-05")
            self.assertEqual(read_pdf(db, pipeline.get_resume(db, "pt"), self.files), replacement)
            self.assertEqual(read_pdf(db, pipeline.get_resume(db, "en"), self.files), self.raw)

    def test_invalid_upload_and_failed_transaction_leave_previous_pdf(self):
        self.upload()
        for content in [b"Not a PDF", b"%PDF" + b"x" * (12 * 1024 * 1024)]:
            with self.assertRaises(ValueError): self.upload(content=content)
        with self.assertRaises(RuntimeError), self.connect() as db:
            pipeline.persist_resume_upload(db, language="pt", original_filename="new.pdf", raw_bytes=pdf("Changed synthetic data with sufficient text for a valid replacement resume."), resumes_dir=str(self.files), now_iso="now")
            raise RuntimeError("rollback")
        with self.connect() as db:
            self.assertEqual(read_pdf(db, pipeline.get_resume(db, "pt"), self.files), self.raw)

    def test_legacy_backfill_is_verified_and_does_not_change_metadata(self):
        self.upload()
        self.files.mkdir(exist_ok=True)
        (self.files / "resume_pt.pdf").write_bytes(self.raw)
        with self.connect() as db:
            before = dict(pipeline.get_resume(db, "pt")); db.execute("DELETE FROM resume_files")
            backfill(db, self.files)
            self.assertEqual(dict(pipeline.get_resume(db, "pt")), before)
        (self.files / "resume_pt.pdf").unlink()
        repository.initialize()
        with self.connect() as db:
            self.assertEqual(read_pdf(db, pipeline.get_resume(db, "pt"), self.files), self.raw)
            db.execute("UPDATE resume_files SET content=?", (b"%PDFtampered",))
            with self.assertRaises(ValueError): read_pdf(db, pipeline.get_resume(db, "pt"), self.files)

    def test_legacy_missing_or_wrong_pdf_is_not_substituted(self):
        self.upload()
        (self.files / "resume_pt.pdf").write_bytes(pdf("An unrelated PDF must never be served as the candidate's actual resume."))
        with self.connect() as db:
            db.execute("DELETE FROM resume_files")
            backfill(db, self.files)
            with self.assertRaises(FileNotFoundError): read_pdf(db, pipeline.get_resume(db, "pt"), self.files)

    def test_qpid_download_is_exact_pdf_after_local_files_are_gone(self):
        self.upload()
        with self.connect() as db:
            db.execute("INSERT INTO jobs(id,source,title,url,first_seen_at,fingerprint) VALUES(1,'test','Test','https://example.com/job','now','unique')")
        api=FastAPI(); register(api, types.SimpleNamespace(connect=self.connect))
        endpoint=next(route.endpoint for route in api.routes if getattr(route,"path","")=="/api/qpid/applications/{job_id}/resume")
        response=endpoint(1,"pt")
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.body,self.raw)
        self.assertEqual(response.headers["content-type"],"application/pdf")
        self.assertEqual(response.headers["cache-control"],"no-store")
        self.assertEqual(endpoint(999,"pt").status_code,404)
        self.assertEqual(endpoint(1,"en").status_code,404)

    def test_analysis_can_extract_again_from_database_without_original_file(self):
        self.upload()
        with self.connect() as db, patch.object(pipeline, "analyze_resume_text", return_value={"summary":"Recovered"}):
            db.execute("UPDATE resumes SET extracted_text='' WHERE language='pt'")
            pipeline.run_resume_analysis(db, language="pt", provider="fake", model="fake", api_key="", now_iso="now")
            self.assertIn("Synthetic candidate", pipeline.get_resume(db,"pt")["extracted_text"])
            self.assertEqual(read_pdf(db, pipeline.get_resume(db,"pt"), self.files),self.raw)

    def test_missing_local_pdf_and_blob_returns_known_error_from_qpid(self):
        self.upload()
        with self.connect() as db:
            db.execute("DELETE FROM resume_files")
            db.execute("INSERT INTO jobs(id,source,title,url,first_seen_at,fingerprint) VALUES(1,'test','Test','https://example.com/job','now','unique')")
        api=FastAPI();register(api,types.SimpleNamespace(connect=self.connect))
        endpoint=next(route.endpoint for route in api.routes if getattr(route,"path","")=="/api/qpid/applications/{job_id}/resume")
        response=endpoint(1,"pt")
        self.assertEqual(response.status_code,404)
        self.assertEqual(json.loads(response.body)["error"],"Arquivo de currículo indisponível.")

    def test_qpid_exposes_uploaded_text_as_evidence_without_binary_or_generated_summary(self):
        self.upload()
        with self.connect() as db:
            db.execute("UPDATE resumes SET analysis_summary='DO NOT USE GENERATED SUMMARY'")
            db.execute("INSERT INTO jobs(id,source,title,url,first_seen_at,fingerprint) VALUES(1,'test','Test','https://example.com/job','now','unique')")
        api=FastAPI();register(api,types.SimpleNamespace(connect=self.connect,settings=lambda:{}))
        endpoint=next(route.endpoint for route in api.routes if getattr(route,"path","")=="/api/qpid/applications/{job_id}")
        context=endpoint(1,"pt")
        evidence=[rule for rule in context["rules"] if rule.get("sourceOnly")]
        self.assertTrue(evidence)
        self.assertIn("Synthetic candidate",evidence[0]["value"])
        self.assertNotIn("DO NOT USE GENERATED SUMMARY",json.dumps(evidence))
        self.assertEqual(len(context["rules"]),len(context["profile"]["facts"]))
        json.dumps(context)

    def test_postgres_schema_uses_bytea_with_foreign_key(self):
        statements=[]
        ensure_table(types.SimpleNamespace(execute=lambda sql: statements.append(sql)))
        self.assertIn("BYTEA",statements[0]); self.assertIn("REFERENCES resumes(id)",statements[0])

if __name__ == "__main__": unittest.main()
