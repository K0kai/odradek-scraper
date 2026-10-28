"""HTTP API and static frontend for Odradek."""
from __future__ import annotations

import logging
import os
import re
from contextlib import asynccontextmanager
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import app as core
from web import panel_queries, panel_settings
from jobs.form_rules import list_rules, save_rules_from_form
from jobs.bulk_ignore import apply as apply_bulk_ignore, criteria_from_body, preview as preview_bulk_ignore
from jobs.job_queue import KIND_RESUME
from web.panel_log import clear_logs, log_event
from candidate.resume_pipeline import AiUnavailableError, get_resume, persist_resume_upload
from web.panel_auth import configured, requires_auth, same_origin_post
from web.panel_session import (clear_session_cookie, issue_session, login_form, login_page,
                           safe_next, secure_cookie, session_cookie, session_expiry, verify_login)


_dist = Path(core.FRONTEND_DIST)
_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    core.initialize()
    core._boot_logging()
    core._boot_queue()
    core._boot_worth_check_scheduler()
    try:
        yield
    finally:
        core.queue.stop()


api = FastAPI(title="Odradek API", lifespan=lifespan)


@api.middleware("http")
async def guard(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "POST"}:
        return JSONResponse({"error": "Método não permitido"}, status_code=405)
    if request.method == "POST" and not same_origin_post(
        request.headers.get("origin"), request.headers.get("host"),
        request.headers.get("sec-fetch-site"), request.headers.get("referer"), request.url.scheme,
    ):
        return JSONResponse({"error": "Origem não permitida"}, status_code=403)
    public_path = request.url.path in {"/health", "/odradek-icon.png", "/favicon.ico"}
    login_path = request.url.path in {"/login", "/logout"}
    if not public_path and requires_auth():
        if not configured():
            return JSONResponse({"error": "Configure PANEL_PASSWORD para acessar o painel hospedado."}, status_code=503)
        if not login_path and session_expiry(request.headers.get("cookie")) is None:
            if request.method in {"GET", "HEAD"} and request.url.path in {"/", "/new", "/new/"}:
                target = request.url.path + ("?" + request.url.query if request.url.query else "")
                return RedirectResponse("/login?" + urlencode({"next": safe_next(target)}), status_code=303,
                                        headers={"Cache-Control": "no-store"})
            return JSONResponse({"error": "Sessão expirada. Entre novamente."}, status_code=401,
                                headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    if request.url.path.startswith("/ui/assets/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif request.url.path.startswith("/api/") or request.url.path in {"/", "/new", "/new/", "/login", "/logout"}:
        response.headers["Cache-Control"] = "no-store"
    return response


@api.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    return response


@api.get("/login", include_in_schema=False)
def login(request: Request):
    target = safe_next(request.query_params.get("next"))
    if not requires_auth() or session_expiry(request.headers.get("cookie")) is not None:
        return RedirectResponse(target, status_code=303)
    return HTMLResponse(login_page(target), headers={"Cache-Control": "no-store"})


@api.post("/login", include_in_schema=False)
async def login_submit(request: Request):
    try:
        password, target = login_form(await _bounded_body(request, max_bytes=8192))
    except (ValueError, UnicodeError):
        return HTMLResponse(login_page(error="Formulário inválido."), status_code=400)
    result = verify_login(password, request.client.host if request.client else "unknown")
    if result != "ok":
        message = "Muitas tentativas. Aguarde 15 minutos." if result == "blocked" else "Senha incorreta."
        return HTMLResponse(login_page(target, message), status_code=429 if result == "blocked" else 401)
    response = RedirectResponse(target, status_code=303)
    response.headers["Set-Cookie"] = session_cookie(issue_session(), secure=secure_cookie(request.url.scheme))
    response.headers["Cache-Control"] = "no-store"
    return response


@api.post("/logout", include_in_schema=False)
def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    response.headers["Set-Cookie"] = clear_session_cookie(secure=secure_cookie(request.url.scheme))
    response.headers["Cache-Control"] = "no-store"
    return response


@api.get("/api/session", include_in_schema=False)
def session(request: Request):
    return {"auth_required": requires_auth(), "expires_at": session_expiry(request.headers.get("cookie"))}


@api.get("/health", include_in_schema=False)
def health():
    try:
        with core.connect() as db:
            db.execute("SELECT 1")
        return {"ok": True, "service": "odradek-scraper", "database": "ok"}
    except Exception:
        return JSONResponse({"ok": False, "service": "odradek-scraper", "database": "unavailable"}, status_code=503)


@api.get("/", include_in_schema=False)
@api.get("/new", include_in_schema=False)
@api.get("/new/", include_in_schema=False)
def new_panel():
    file = _dist / "index.html"
    if not file.is_file():
        return Response("Frontend ainda não compilado.", status_code=503)
    return FileResponse(file, media_type="text/html", headers={"Cache-Control": "no-store"})


api.mount("/ui/assets", StaticFiles(directory=str(_dist / "assets"), check_dir=False), name="assets")


@api.get("/api/dashboard")
def dashboard() -> dict:
    return panel_queries.dashboard_api_payload()


@api.get("/api/jobs")
def jobs(page: int = 1, page_size: int = 15, status: str = "worth", min_match: int = 0,
         sort: str = "", date_from: str = "", date_to: str = "") -> dict:
    try:
        return panel_queries.jobs_api_payload(page=page, page_size=page_size, status=status,
                                     min_match=min_match, sort=sort, date_from=date_from, date_to=date_to)
    except ValueError:
        return JSONResponse({"error": "Parâmetros de filtro inválidos"}, status_code=400)


@api.post("/api/jobs/bulk-ignore/preview")
def bulk_ignore_preview(body: dict = Body(...)):
    try:
        criteria = criteria_from_body(body)
    except ValueError as exc:
        return _error(str(exc))
    with core.connect() as db:
        count = preview_bulk_ignore(db, criteria)
    return {"count": count}


@api.post("/api/jobs/bulk-ignore/apply")
def bulk_ignore_apply(body: dict = Body(...)):
    try:
        expected_count = body.get("expected_count")
        if type(expected_count) is not int or expected_count < 1:
            raise ValueError("Confira a quantidade antes de ignorar.")
        criteria = criteria_from_body({key: value for key, value in body.items() if key != "expected_count"})
    except ValueError as exc:
        return _error(str(exc))
    with core.connect() as db:
        if preview_bulk_ignore(db, criteria) != expected_count:
            return _error("A quantidade de vagas mudou. Confira novamente antes de ignorar.", 409)
        count = apply_bulk_ignore(db, criteria)
    return {"count": count, "notice": f"{count} vaga(s) ignorada(s)."}


@api.get("/api/queue")
def queue() -> dict:
    return panel_queries.queue_api_payload()


@api.get("/api/logs")
def logs(limit: int = 200) -> dict:
    return panel_queries.logs_api_payload(limit)


@api.get("/api/usage")
def usage() -> dict:
    return panel_queries.usage_api_payload()


@api.get("/api/resumes")
def resumes() -> dict:
    return panel_queries.resumes_api_payload()


@api.get("/api/copilot-asks/pending")
def pending_ask() -> dict:
    from jobs.copilot_asks import get_pending_ask
    return {"ask": get_pending_ask(core.connect)}


@api.get("/api/settings/{group}")
def settings_group(group: str) -> dict:
    if group not in panel_settings.SETTINGS_API_GROUPS:
        return JSONResponse({"error": "Grupo de configuração inválido"}, status_code=404)
    return panel_settings.settings_api_payload(group)


@api.get("/api/form-rules")
def form_rules() -> dict:
    with core.connect() as db:
        rows = list_rules(db)
    return {"items": [{key: row[key] for key in ("key", "aliases", "mode", "value_from", "value")} for row in rows]}


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _bounded_body(request: Request, *, max_bytes: int) -> bytes:
    size = 0
    chunks = []
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("Arquivo ou formulário muito grande")
        chunks.append(chunk)
    return b"".join(chunks)


async def _form_data(request: Request, *, max_bytes: int = 16 * 1024 * 1024):
    """Read bounded form data without accepting unbounded request bodies."""
    body = await _bounded_body(request, max_bytes=max_bytes)
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/x-www-form-urlencoded"):
        parsed = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}, {}
    if not content_type.startswith("multipart/form-data") or "boundary=" not in content_type:
        raise ValueError("Formulário inválido")
    head = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=policy.default).parsebytes(head + body)
    if not message.is_multipart():
        raise ValueError("Upload multipart inválido")
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        content = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files[name] = (filename or "upload.bin", content)
        else:
            fields[name] = content.decode("utf-8")
    return fields, files


@api.get("/odradek-icon.png", include_in_schema=False)
@api.get("/favicon.ico", include_in_schema=False)
def icon():
    return FileResponse(core.FAVICON_PATH, media_type="image/png")


@api.get("/cover-letter.pdf", include_in_schema=False)
def cover_letter(job_id: int):
    if job_id <= 0:
        return _error("Vaga inválida")
    with core.connect() as db:
        row = db.execute("SELECT body FROM cover_letters WHERE job_id=?", (job_id,)).fetchone()
    if not row or not (row["body"] or "").strip():
        return _error("Carta não encontrada", 404)
    from candidate.cover_letter_pdf import render_cover_letter_pdf
    data = render_cover_letter_pdf(row["body"])
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="cover-letter-{job_id}.pdf"',
        "Cache-Control": "no-store",
    })


@api.get("/api/autofill-profile", include_in_schema=False)
def autofill_profile():
    cfg = core.settings()
    name = (cfg.get("candidate_name") or "").strip()
    parts = name.split()
    return {"profile": {
        "firstName": parts[0] if parts else "",
        "lastName": " ".join(parts[1:]),
        "fullName": name,
        "email": (cfg.get("candidate_email") or "").strip(),
        "phone": (cfg.get("candidate_phone") or "").strip(),
        "city": (cfg.get("candidate_city") or "").strip(),
        "state": (cfg.get("candidate_state") or "").strip(),
        "country": (cfg.get("candidate_country") or "").strip(),
        "zip": (cfg.get("candidate_postal_code") or "").strip(),
        "linkedin": (cfg.get("candidate_linkedin") or "").strip(),
    }}


@api.get("/api/autofill-rules", include_in_schema=False)
def autofill_rules():
    extension_keys = {
        "full_name": "fullName", "email": "email", "phone": "phone", "linkedin": "linkedin",
        "city": "city", "street_address": "streetAddress", "postal_code": "zip",
        "state": "state", "country": "country", "current_company": "currentCompany",
        "cpf": "cpf", "salary": "salaryExpectation", "contract_type": "contractType",
    }
    with core.connect() as db:
        rules = list_rules(db)
    fields = []
    for rule in rules:
        key = extension_keys.get(str(rule["key"]).casefold())
        if not key or str(rule["mode"]).casefold() == "skip":
            continue
        aliases = [part.strip() for part in re.split(r"[,\n;]", str(rule["aliases"] or "")) if part.strip()]
        fields.append({"key": key, "aliases": aliases})
    return {"fields": fields}


@api.post("/api/collector/{action}")
def collector_action(action: str):
    if action == "start":
        started = core.collector.start()
        notice = "Coleta iniciada." if started else "A coleta já está em execução."
    elif action == "stop":
        core.collector.stop()
        notice = "Solicitação para parar enviada."
    else:
        return _error("Ação inválida", 404)
    return {"ok": True, "notice": notice, "collector": core.collector.snapshot()}


@api.post("/api/jobs/{job_id}/{action}")
def update_job(job_id: int, action: str, body: dict = Body(...)):
    if action not in {"status", "notes"}:
        return _error("Ação inválida", 404)
    with core.connect() as db:
        if not db.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone():
            return _error("Vaga não encontrada", 404)
        if action == "status":
            status = body.get("status")
            if status not in {"applied", "ignored", "saved", "worth"}:
                return _error("Status inválido")
            db.execute("UPDATE jobs SET status=?, applied_at=COALESCE(?, applied_at) WHERE id=?",
                       (status, core.now_iso() if status == "applied" else None, job_id))
            notice = "Status atualizado."
        else:
            notes = body.get("notes")
            if not isinstance(notes, str) or len(notes) > 30000:
                return _error("Nota inválida")
            db.execute("UPDATE jobs SET notes=? WHERE id=?", (notes, job_id))
            notice = "Anotação salva."
    return {"ok": True, "notice": notice, "id": job_id}


@api.post("/api/queue/{action}")
def queue_action(action: str):
    if action == "retry-all":
        notice = f"{core.queue.retry_all()} job(s) reenfileirado(s)."
    elif action == "evaluate-new":
        notice = core.enqueue_new_jobs_for_triage()
    elif action == "clear":
        notice = f"{core.queue.clear_terminal()} job(s) removido(s)."
    else:
        return _error("Ação inválida", 404)
    return {"ok": True, "notice": notice}


@api.post("/api/queue/{job_id}/{action}")
def queue_item_action(job_id: int, action: str):
    if action not in {"cancel", "retry"}:
        return _error("Ação inválida", 404)
    ok = core.queue.cancel(job_id) if action == "cancel" else core.queue.retry_now(job_id)
    return JSONResponse({"ok": ok, "notice": "Fila atualizada." if ok else "O job não pode ser alterado nesse estado."},
                        status_code=200 if ok else 409)


@api.post("/api/logs/clear")
def clear_panel_logs():
    return {"ok": True, "notice": f"{clear_logs()} log(s) removido(s)."}


@api.post("/api/usage/refresh")
def refresh_usage():
    snapshot = core.refresh_apify_quota_snapshot()
    return {"ok": not bool(snapshot.get("error")),
            "notice": snapshot.get("error") or "Cotas Apify atualizadas.",
            "usage": panel_queries.usage_api_payload()}


@api.post("/api/settings/{group}")
def save_settings(group: str, body: dict = Body(...)):
    if group not in panel_settings.SETTINGS_API_GROUPS:
        return _error("Grupo de configuração inválido", 404)
    try:
        notice = panel_settings.save_settings_api_group(group, body)
    except ValueError as exc:
        return _error(str(exc) or "Configuração inválida")
    except RuntimeError as exc:
        return _error(str(exc), 503)
    return {"ok": True, "notice": notice, "values": panel_settings.settings_api_payload(group)["values"]}


@api.post("/api/linkedin-filters/generate")
def generate_linkedin_filters(body: dict = Body(...)):
    if any(not isinstance(value, str) or len(value) > 30000 for value in body.values()):
        return _error("Valor inválido para a configuração de busca")
    form = {key: value for key, value in body.items() if key in panel_settings.SETTINGS_API_GROUPS["search"]}
    try:
        generated = panel_settings.generate_linkedin_filters(form)
    except ValueError as exc:
        return _error(str(exc))
    except Exception as exc:
        log_event("warning", "apify", f"Falha ao gerar filtros LinkedIn: {exc}")
        return _error("Não foi possível gerar filtros com IA.", 503)
    return {"ok": True, "notice": f"A IA gerou {len(generated)} filtros LinkedIn.",
            "values": panel_settings.settings_api_payload("search")["values"]}


@api.post("/api/form-rules")
def save_form_rules(body: dict = Body(...)):
    items = body.get("items")
    if not isinstance(items, list) or not (1 <= len(items) <= 100):
        return _error("Use de 1 a 100 regras")
    form: dict[str, str] = {}
    seen = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            return _error(f"Regra {index} inválida")
        key = item.get("key")
        if not isinstance(key, str) or not key.strip() or len(key) > 100 or key.casefold() in seen:
            return _error(f"Chave vazia, repetida ou longa na regra {index}")
        seen.add(key.casefold())
        mode = item.get("mode") or "text"
        if mode not in {"text", "select", "salary", "file", "cover_letter", "skip"}:
            return _error(f"Modo inválido na regra {index}")
        for field in ("key", "aliases", "mode", "value_from", "value"):
            value = item.get(field) or ""
            if not isinstance(value, str) or len(value) > 10000:
                return _error(f"Campo {field} inválido na regra {index}")
            form[f"rule_{field}_{index}"] = value
    with core.connect() as db:
        save_rules_from_form(db, form)
    return {"ok": True, "notice": "Regras de formulário salvas."}


@api.post("/api/smtp/test")
def smtp_test():
    ok, notice = panel_settings.send_smtp_test()
    return JSONResponse({"ok": ok, "notice": notice}, status_code=200 if ok else 400)


def _persist_resume(language: str, filename: str, raw: bytes, force: bool) -> str:
    with core.connect() as db:
        message, kind, needs_analysis = persist_resume_upload(
            db, language=language, original_filename=filename, raw_bytes=raw,
            resumes_dir=core.RESUMES_DIR, now_iso=core.now_iso(), force_reanalyze=force,
        )
    if needs_analysis:
        queue_id = core.queue.enqueue(
            KIND_RESUME,
            {"language": language, "force": True, "requested_at": core.now_iso()},
            dedupe_key=None if force else f"resume:{language}",
        )
        message = f"{message} Job de fila #{queue_id}."
    log_event(kind if kind != "info" else "info", "resume", message)
    return message


@api.post("/api/resumes/upload")
async def upload_resume(request: Request):
    try:
        fields, files = await _form_data(request)
        if "file" not in files:
            raise ValueError("Selecione um arquivo PDF.")
        language = (fields.get("language") or "").strip().casefold()
        filename, raw = files["file"]
        message = await run_in_threadpool(_persist_resume, language, filename, raw,
                                          fields.get("force_reanalyze") == "1")
    except (ValueError, UnicodeError) as exc:
        return _error(str(exc))
    except Exception:
        _log.exception("Falha no upload do currículo")
        return _error("Falha no upload do currículo.", 500)
    return {"ok": True, "notice": message}


def _reanalyze_resume(language: str) -> str:
    if language not in {"pt", "en"}:
        raise ValueError("Idioma inválido para reanálise.")
    with core.connect() as db:
        row = get_resume(db, language)
        if not row:
            raise ValueError(f"Não há currículo {language.upper()} salvo.")
        if not (row["extracted_text"] or "").strip() and not (
            row["stored_path"] and os.path.exists(row["stored_path"])
        ):
            raise ValueError(f"Currículo {language.upper()} sem arquivo/texto para reanalisar.")
        db.execute(
            """UPDATE resumes SET analysis_status='pending', analysis_error='',
               analysis_message=?, updated_at=? WHERE language=?""",
            (f"Currículo {language.upper()}: reanálise forçada na fila.", core.now_iso(), language),
        )
        actives = db.execute(
            """SELECT id FROM queue_jobs WHERE kind=? AND status IN ('pending','running','retry_wait')
               AND payload LIKE ?""",
            (KIND_RESUME, f'%"language": "{language}"%'),
        ).fetchall()
    for active in actives:
        core.queue.cancel(int(active["id"]))
    queue_id = core.queue.enqueue(
        KIND_RESUME, {"language": language, "force": True, "requested_at": core.now_iso()},
        dedupe_key=None,
    )
    message = f"Reanálise do currículo {language.upper()} enfileirada (job #{queue_id}). Acompanhe em Filas/Logs."
    log_event("info", "resume", message)
    return message


@api.post("/api/resumes/reanalyze")
async def reanalyze_resume(request: Request):
    try:
        fields, _ = await _form_data(request, max_bytes=8192)
        message = await run_in_threadpool(_reanalyze_resume, (fields.get("language") or "").strip().casefold())
    except (ValueError, UnicodeError) as exc:
        return _error(str(exc))
    return {"ok": True, "notice": message}


def _answer_assistant(message: str, history: str) -> str:
    from candidate.assistant_chat import answer_form_question
    cfg = core.settings()
    provider = (cfg.get("ai_provider") or "gemini").strip()
    model = (cfg.get("ai_model") or "gemini-2.5-flash").strip()
    with core.connect() as db:
        answer = answer_form_question(
            question=message, cfg=cfg, db=db, provider=provider,
            model=model, api_key=core.get_ai_key(provider), history=history,
        )
    log_event("info", "assistant", "Resposta gerada no Assistente.")
    return answer


@api.post("/api/assistant")
async def assistant(request: Request):
    try:
        fields, _ = await _form_data(request, max_bytes=256 * 1024)
        answer = await run_in_threadpool(_answer_assistant,
                                         (fields.get("message") or "").strip(), fields.get("history") or "[]")
    except (AiUnavailableError, ValueError, UnicodeError) as exc:
        return _error(str(exc), 503 if isinstance(exc, AiUnavailableError) else 400)
    except Exception:
        _log.exception("Falha no Assistente")
        return _error("Falha ao gerar resposta.", 500)
    return {"ok": True, "answer": answer}


def _answer_copilot(ask_id: int, answer: str, upload: tuple[str, bytes] | None):
    from jobs.copilot_asks import answer_ask, get_ask, save_ask_upload
    if ask_id <= 0 or (not answer and not upload):
        raise ValueError("Informe a resposta ou anexe o arquivo.")
    pending = get_ask(core.connect, ask_id)
    if pending and (pending.get("kind") or "text") == "file" and not upload:
        raise ValueError("Esta pergunta exige um arquivo anexado.")
    file_path = ""
    if upload:
        filename, raw = upload
        file_path = save_ask_upload(
            uploads_dir=core.COPILOT_UPLOADS_DIR, ask_id=ask_id,
            filename=filename, raw_bytes=raw,
        )
    ok = answer_ask(core.connect, ask_id, answer, now_iso=core.now_iso(), file_path=file_path)
    if not ok:
        return None
    log_event("info", "copilot", f"Ask #{ask_id} respondida no painel" +
              (" (com arquivo)." if file_path else "."))
    return "Resposta enviada."


@api.post("/api/copilot-asks/answer")
async def copilot_answer(request: Request):
    try:
        fields, files = await _form_data(request)
        raw_id = fields.get("id") or ""
        ask_id = int(raw_id) if raw_id.isdigit() else 0
        notice = await run_in_threadpool(_answer_copilot, ask_id, (fields.get("answer") or "").strip(),
                                         files.get("file"))
    except (ValueError, UnicodeError) as exc:
        return _error(str(exc))
    if notice is None:
        return _error("Pergunta já respondida ou expirada.", 409)
    return {"ok": True, "notice": notice}


@api.post("/api/copilot-asks/{action}")
async def copilot_action(action: str, request: Request):
    from jobs.copilot_asks import cancel_ask, manual_ask
    if action not in {"cancel", "manual"}:
        return _error("Ação inválida", 404)
    try:
        fields, _ = await _form_data(request, max_bytes=8192)
        raw_id = fields.get("id") or ""
        ask_id = int(raw_id) if raw_id.isdigit() else 0
    except (ValueError, UnicodeError) as exc:
        return _error(str(exc))
    if ask_id <= 0:
        return _error("Pergunta inválida")
    ok = cancel_ask(core.connect, ask_id) if action == "cancel" else manual_ask(core.connect, ask_id)
    if ok:
        log_event("info", "copilot", f"Ask #{ask_id} {action} no painel.")
    return {"ok": ok, "notice": ("Pergunta cancelada." if action == "cancel" else "Modo manual ativado.")
            if ok else "Pergunta já fechada."}
