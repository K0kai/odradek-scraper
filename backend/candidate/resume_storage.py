"""Durable PDF storage: BYTEA on PostgreSQL, BLOB on local SQLite."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import sqlite3

MAX_PDF_BYTES = 12 * 1024 * 1024

def ensure_table(db):
    binary_type = "BLOB" if isinstance(db, sqlite3.Connection) else "BYTEA"
    db.execute(f"""CREATE TABLE IF NOT EXISTS resume_files (
        resume_id INTEGER PRIMARY KEY REFERENCES resumes(id) ON DELETE CASCADE,
        content {binary_type} NOT NULL
    )""")

def validate_pdf(content, expected_digest):
    content = bytes(content)
    if not content.startswith(b"%PDF") or not 0 < len(content) <= MAX_PDF_BYTES:
        raise ValueError("Arquivo de currículo inválido ou maior que 12 MB.")
    if hashlib.sha256(content).hexdigest() != expected_digest:
        raise ValueError("O PDF não corresponde à versão cadastrada do currículo.")
    return content

def save_pdf(db, resume, content):
    content = validate_pdf(content, resume["file_sha256"])
    db.execute("""INSERT INTO resume_files(resume_id,content) VALUES(?,?)
        ON CONFLICT(resume_id) DO UPDATE SET content=excluded.content""", (resume["id"], content))

def read_pdf(db, resume, resumes_dir):
    row = db.execute("SELECT content FROM resume_files WHERE resume_id=?", (resume["id"],)).fetchone()
    if row is not None:
        return validate_pdf(row["content"], resume["file_sha256"])
    # Import old filesystem-only records without changing their identity/version.
    root = Path(resumes_dir).resolve()
    candidates = [Path(resume["stored_path"]).resolve(), root / f"resume_{resume['language']}.pdf"]
    for path in candidates:
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > MAX_PDF_BYTES:
            continue
        try:
            content = validate_pdf(path.read_bytes(), resume["file_sha256"])
        except ValueError:
            continue
        save_pdf(db, resume, content)
        return content
    raise FileNotFoundError("Arquivo de currículo indisponível. Reenvie o PDF neste idioma para armazená-lo no banco.")

def backfill(db, resumes_dir):
    for row in db.execute("SELECT resumes.* FROM resumes LEFT JOIN resume_files ON resume_files.resume_id=resumes.id WHERE resume_files.resume_id IS NULL").fetchall():
        try:
            read_pdf(db, row, resumes_dir)
        except (FileNotFoundError, OSError):
            pass

def materialize_pdf(db, resume, resumes_dir):
    content = read_pdf(db, resume, resumes_dir)
    root = Path(resumes_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"resume_{resume['language']}_{resume['file_sha256']}.pdf"
    if not path.is_file() or path.read_bytes() != content:
        import tempfile
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=root, delete=False) as handle:
                temporary = handle.name
                handle.write(content)
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)
    return str(path)
