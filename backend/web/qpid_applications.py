"""Explicit Qpid access to one application at a time. No browser runs on Render."""
from pathlib import Path
from fastapi import Body, Query
from fastapi.responses import FileResponse, JSONResponse

def register(api, core):
    def error(message, code=400):
        return JSONResponse({"error": message}, status_code=code)

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
            if not row: return error("Vaga não encontrada", 404)
            if row["status"] == "applied": return error("Vaga já marcada como enviada.", 409)
            rules = list_rules(db)
            resume = get_resume(db, language)
            letter = db.execute("SELECT body FROM cover_letters WHERE job_id=?", (job_id,)).fetchone()
        resolved = []
        for rule in rules:
            mode = str(rule["mode"]).lower()
            if mode not in {"text", "select", "cover_letter"}: continue
            value = (letter["body"] if letter else "") if mode == "cover_letter" else (cfg.get(rule["value_from"], "") if rule["value_from"] else rule["value"])
            if not str(value or "").strip(): continue
            aliases = [part.strip() for part in str(rule["aliases"] or "").replace(";", ",").replace("\n", ",").split(",") if part.strip()]
            resolved.append({"aliases": aliases, "value": str(value)})
        name = str(cfg.get("candidate_name") or "").split()
        if name:
            resolved += [{"aliases": ["first name", "primeiro nome"], "value": name[0]}, {"aliases": ["last name", "sobrenome"], "value": " ".join(name[1:])}]
        return {"job": dict(row), "rules": resolved, "resume": bool(resume), "language": language}

    @api.get("/api/qpid/applications/{job_id}/resume")
    def resume_file(job_id: int, language: str = Query("pt", pattern="^(pt|en)$")):
        from candidate.resume_pipeline import get_resume
        from paths import RESUMES_DIR
        with core.connect() as db:
            if not db.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone(): return error("Vaga não encontrada", 404)
            resume = get_resume(db, language)
        if not resume: return error("Currículo não cadastrado neste idioma.", 404)
        target = Path(resume["stored_path"]).resolve()
        if not target.is_relative_to(Path(RESUMES_DIR).resolve()) or not target.is_file() or target.stat().st_size > 15 * 1024 * 1024:
            return error("Arquivo de currículo indisponível.", 404)
        return FileResponse(target, media_type="application/pdf", filename="curriculo.pdf")

    @api.post("/api/qpid/applications/{job_id}/confirmed")
    def confirmed(job_id: int, body: dict = Body(...)):
        if body.get("confirmed") is not True: return error("Confirmação explícita obrigatória.")
        with core.connect() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row: return error("Vaga não encontrada", 404)
            db.execute("UPDATE jobs SET status='applied', applied_at=COALESCE(applied_at, ?) WHERE id=?", (core.now_iso(), job_id))
        return {"ok": True, "notice": "Envio confirmado pelo usuário no Qpid."}
