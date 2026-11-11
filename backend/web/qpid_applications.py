"""Qpid application contract v2. Register under the existing token middleware."""
import hashlib
import json
import re
from urllib.parse import urlsplit
from fastapi import Body, Query
from fastapi.responses import JSONResponse, Response


def ensure_tables(db):
    db.execute("""CREATE TABLE IF NOT EXISTS qpid_application_attempts (
      attempt_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
      state TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS qpid_one_active_application ON qpid_application_attempts(job_id) WHERE state != 'failed-before-click'")
    db.execute("""CREATE TABLE IF NOT EXISTS qpid_application_results (
      attempt_id TEXT PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
      state TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)""")


def register(api, core):
    def error(message, code=400):
        return JSONResponse({"error": message}, status_code=code)

    def digest(value):
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    tables = ensure_tables

    @api.get("/api/qpid/capabilities")
    def capabilities():
        return {"version": 2, "application_contract": 2, "features": ["profile-facts-v1", "application-reservation-v1", "application-result-v1", "collector-apply-limit-v1"]}

    @api.get("/api/qpid/applications")
    def candidates(limit: int = Query(30, ge=1, le=100)):
        from jobs.job_filters import _worth_match_expr
        with core.connect() as db:
            rows = db.execute("SELECT jobs.id, jobs.title, jobs.company, jobs.status, " + _worth_match_expr() + " AS match_score FROM jobs WHERE jobs.status IN ('worth','saved') ORDER BY jobs.id DESC LIMIT ?", (limit,)).fetchall()
        return {"items": [dict(row) for row in rows]}

    @api.get("/api/qpid/applications/{job_id}")
    def context(job_id: int, language: str = Query("pt", pattern="^(pt|en)$")):
        from jobs.form_rules import list_rules
        from candidate.resume_pipeline import get_resume
        cfg = core.settings()
        with core.connect() as db:
            row = db.execute("SELECT id, title, company, url, status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return error("Vaga não encontrada", 404)
            if row["status"] == "applied":
                return error("Vaga já marcada como enviada.", 409)
            rules, resume = list_rules(db), get_resume(db, language)
            letter = db.execute("SELECT body FROM cover_letters WHERE job_id=?", (job_id,)).fetchone()
            try:
                decision = db.execute("SELECT assessment_json FROM ai_decisions WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
            except Exception:
                decision = None
        resolved, facts = [], []

        def append(identifier, aliases, value, source, kind="fact", scope="profile", source_only=False, extra=None):
            item = {"id": identifier, "aliases": aliases, "value": str(value), "source": source, "kind": kind, "language": language, "scope": scope}
            if source_only:
                item["sourceOnly"] = True
            if extra:
                item.update(extra)
            item["version"] = digest(item)
            resolved.append(item)
            facts.append(dict(item))

        for raw in rules:
            rule = dict(raw)
            mode = str(rule["mode"]).lower()
            if mode not in {"text", "select", "cover_letter"}:
                continue
            value = (letter["body"] if letter else "") if mode == "cover_letter" else (cfg.get(rule["value_from"], "") if rule["value_from"] else rule["value"])
            if not str(value or "").strip():
                continue
            aliases = [part.strip() for part in str(rule["aliases"] or "").replace(";", ",").replace("\n", ",").split(",") if part.strip()]
            append(f"form-rule:{rule['id']}:{rule['key']}", aliases, value, rule.get("value_from") or "form-rule", "preference" if re.search(r"salary|availability|contract|pretens", rule["key"], re.I) else "fact", "job" if mode == "cover_letter" else "profile")
        name = str(cfg.get("candidate_name") or "").split()
        if name:
            append("candidate:first_name", ["first name", "primeiro nome"], name[0], "candidate_name")
            if len(name) > 1:
                append("candidate:last_name", ["last name", "sobrenome"], " ".join(name[1:]), "candidate_name")
        # Uploaded resume text is evidence, never a direct autofill rule or an AI summary.
        # Excerpts keep the PDF's own words, split by section/role; those that back this job's
        # verified requirement matches come first.
        if resume and str(resume["extracted_text"] or "").strip():
            from candidate.resume_profile import normalize_text, parse_profile, resume_excerpts
            excerpts = resume_excerpts(str(resume["extracted_text"]).strip(), parse_profile(resume["profile_json"]), language=language)
            backed = []
            try:
                audit = json.loads(decision["assessment_json"]) if decision and decision["assessment_json"] else {}
                for entry in list(audit.get("requirements", [])) + list(audit.get("responsibilities", [])):
                    quote = normalize_text(entry.get("resume_quote", ""))
                    if quote and entry.get("status") in ("met", "transferable", "partial"):
                        backed.append((quote, str(entry.get("text", ""))[:120]))
            except (ValueError, TypeError, AttributeError):
                backed = []
            ranked = []
            for index, excerpt in enumerate(excerpts):
                norm = normalize_text(excerpt["text"])
                matched = list(dict.fromkeys(text for quote, text in backed if quote in norm))[:3]
                ranked.append((0 if matched else 1, index, excerpt, matched))
            for number, (_, _, excerpt, matched) in enumerate(sorted(ranked, key=lambda r: (r[0], r[1]))):
                extra = {"section": excerpt["section"], "sourceLanguage": language}
                if matched:
                    extra["matched_requirements"] = matched
                append(f"resume:{language}:excerpt:{number}", [excerpt["label"]], excerpt["text"], "uploaded-resume", source_only=True, extra=extra)
        resume_version = digest(dict(resume)) if resume else None
        return {"contract_version": 2, "context_version": digest([facts, resume_version]), "job": dict(row), "rules": resolved, "profile": {"schema": 1, "version": digest([fact for fact in facts if fact["scope"] == "profile"]), "facts": facts}, "resume": bool(resume), "resume_meta": {"id": dict(resume).get("id"), "language": language, "version": resume_version} if resume else None, "language": language}

    @api.get("/api/qpid/applications/{job_id}/resume")
    def resume_file(job_id: int, language: str = Query("pt", pattern="^(pt|en)$")):
        from candidate.resume_pipeline import get_resume
        from paths import RESUMES_DIR
        with core.connect() as db:
            if not db.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone():
                return error("Vaga não encontrada", 404)
            resume = get_resume(db, language)
        if not resume:
            return error("Currículo não cadastrado neste idioma.", 404)
        from candidate.resume_storage import read_pdf
        try:
            with core.connect() as db:
                content = read_pdf(db, resume, RESUMES_DIR)
        except (OSError, ValueError):
            return error("Arquivo de currículo indisponível.", 404)
        return Response(content, media_type="application/pdf", headers={
            "Content-Disposition": 'attachment; filename="curriculo.pdf"', "Cache-Control": "no-store"
        })

    @api.post("/api/qpid/applications/{job_id}/attempt")
    def reserve(job_id: int, body: dict = Body(...)):
        attempt = body.get("attempt_id")
        if body.get("version") != 1 or not isinstance(attempt, str) or not re.fullmatch(r"[a-zA-Z0-9-]{20,80}", attempt) or not isinstance(body.get("language"), str) or body["language"] not in {"pt", "en"} or not isinstance(body.get("digest"), str) or not re.fullmatch(r"[a-f0-9]{64}", body["digest"]):
            return error("Reserva inválida.")
        candidate = context(job_id, body["language"])
        if isinstance(candidate, JSONResponse):
            return candidate
        if body.get("profile_version") != candidate["profile"]["version"]:
            return error("O perfil mudou. Reabra a candidatura e revise novamente.", 409)
        if body.get("context_version") != candidate["context_version"]:
            return error("O contexto ou currículo mudou. Reabra a candidatura e revise novamente.", 409)
        payload = json.dumps({key: body.get(key) for key in ["version", "attempt_id", "digest", "profile_version", "context_version", "language"]}, sort_keys=True)
        try:
            with core.connect() as db:
                tables(db)
                previous = db.execute("SELECT job_id, payload FROM qpid_application_attempts WHERE attempt_id=?", (attempt,)).fetchone()
                if previous:
                    if previous["job_id"] != job_id or previous["payload"] != payload:
                        return error("Reserva já utilizada com outros dados.", 409)
                    return {"ok": True, "attempt_id": attempt, "existing": True}
                db.execute("INSERT INTO qpid_application_attempts(attempt_id,job_id,state,payload,created_at) VALUES(?,?,?,?,?)", (attempt, job_id, "submitting", payload, core.now_iso()))
        except Exception:
            return error("Já existe uma tentativa pendente para esta vaga ou a reserva não pôde ser salva. Confira antes de tentar novamente.", 409)
        return {"ok": True, "attempt_id": attempt}

    @api.post("/api/qpid/applications/{job_id}/result")
    def result(job_id: int, body: dict = Body(...)):
        state, attempt = body.get("state"), body.get("attempt_id")
        if body.get("version") != 1 or not isinstance(state, str) or state not in {"submitted", "submission-unknown", "failed-before-click"} or not isinstance(attempt, str) or not re.fullmatch(r"[a-zA-Z0-9-]{20,80}", attempt) or not isinstance(body.get("evidence"), str) or body["evidence"] not in {"success-message-and-form-finished", "page-changed", "validation", "timeout", "interrupted"}:
            return error("Resultado inválido.")
        if state == "submitted" and body["evidence"] != "success-message-and-form-finished":
            return error("Envio sem evidência observada.")
        try:
            url = urlsplit(str(body.get("url", "")))
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                raise ValueError()
        except ValueError:
            return error("Endereço de resultado inválido.")
        safe = {"version": 1, "attempt_id": attempt, "state": state, "evidence": body["evidence"], "url": f"https://{url.netloc}{url.path}", "resume_hash": body.get("resume_hash") if isinstance(body.get("resume_hash"), str) and re.fullmatch(r"[a-f0-9]{64}", body["resume_hash"]) else None, "automatic": body.get("automatic") is True, "source": "qpid-observed"}
        payload = json.dumps(safe, sort_keys=True)
        with core.connect() as db:
            tables(db)
            reservation = db.execute("SELECT job_id FROM qpid_application_attempts WHERE attempt_id=?", (attempt,)).fetchone()
            if not reservation or reservation["job_id"] != job_id:
                return error("Não há reserva correspondente a este resultado.", 409)
            previous = db.execute("SELECT payload FROM qpid_application_results WHERE attempt_id=?", (attempt,)).fetchone()
            if previous:
                return {"ok": True, "existing": True} if previous["payload"] == payload else error("Resultado já registrado com outros dados.", 409)
            inserted = db.execute("INSERT INTO qpid_application_results(attempt_id,job_id,state,payload,created_at) VALUES(?,?,?,?,?) ON CONFLICT(attempt_id) DO NOTHING RETURNING attempt_id", (attempt, job_id, state, payload, core.now_iso())).fetchone()
            if not inserted:
                previous = db.execute("SELECT payload FROM qpid_application_results WHERE attempt_id=?", (attempt,)).fetchone()
                return {"ok": True, "existing": True} if previous and previous["payload"] == payload else error("Resultado concorrente já registrado com outros dados.", 409)
            db.execute("UPDATE qpid_application_attempts SET state=? WHERE attempt_id=?", (state, attempt))
            if state == "submitted":
                db.execute("UPDATE jobs SET status='applied', applied_at=COALESCE(applied_at, ?) WHERE id=?", (core.now_iso(), job_id))
        return {"ok": True, "source": "qpid-observed", "state": state}

    @api.get("/api/qpid/applications/{job_id}/results")
    def results(job_id: int):
        with core.connect() as db:
            tables(db)
            rows = db.execute("SELECT payload FROM qpid_application_results WHERE job_id=? ORDER BY created_at DESC LIMIT 20", (job_id,)).fetchall()
        return {"items": [json.loads(row["payload"]) for row in rows]}

    @api.post("/api/qpid/applications/{job_id}/confirmed")
    def confirmed(job_id: int, body: dict = Body(...)):
        if body.get("confirmed") is not True:
            return error("Confirmação explícita obrigatória.")
        with core.connect() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return error("Vaga não encontrada", 404)
            db.execute("UPDATE jobs SET status='applied', applied_at=COALESCE(applied_at, ?) WHERE id=?", (core.now_iso(), job_id))
            tables(db)
            declared = {"version": 1, "attempt_id": f"manual-{job_id}", "state": "user-declared", "source": "user-declared", "evidence": "user-confirmation"}
            db.execute("INSERT INTO qpid_application_results(attempt_id,job_id,state,payload,created_at) VALUES(?,?,?,?,?) ON CONFLICT(attempt_id) DO NOTHING", (declared["attempt_id"], job_id, declared["state"], json.dumps(declared, sort_keys=True), core.now_iso()))
        return {"ok": True, "source": "user-declared", "notice": "Envio confirmado pelo usuário no Qpid."}
