"""Read models returned to the web panel."""
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from app import STATUSES, collector, connect, load_apify_quota_snapshot, queue, settings
from integrations.ai_client import split_model_label
from jobs.job_filters import _worth_match_expr, normalize_worth_sort, worth_date_bounds_utc, worth_order_sql
from web.panel_log import list_logs
from candidate.resume_pipeline import get_resume


JOB_SELECT = (
    "SELECT jobs.id, jobs.title, jobs.company, jobs.location, jobs.source, jobs.url, "
    "jobs.posted_at, jobs.first_seen_at, jobs.status, jobs.description, jobs.notes, "
    "cover_letters.body AS cover_letter, " + _worth_match_expr() + " AS match_score, "
    "(SELECT assessment_json FROM ai_decisions WHERE ai_decisions.job_id = jobs.id ORDER BY id DESC LIMIT 1) AS assessment_json "
    "FROM jobs LEFT JOIN cover_letters ON cover_letters.job_id=jobs.id "
)


def _compact_assessment(raw: object) -> dict | None:
    """Versão enxuta da auditoria da triagem v2 para o card (checklist, bloqueios, ignorados)."""
    try:
        audit = json.loads(raw) if isinstance(raw, str) and raw.strip() else None
    except ValueError:
        return None
    if not isinstance(audit, dict) or not audit.get("requirements") and not audit.get("responsibilities"):
        return None
    decision = audit.get("decision") or {}

    def item(entry: dict) -> dict:
        return {key: str(entry.get(key) or "")[:240] for key in (
            "id", "text", "kind", "status", "realism", "realism_reason", "bridge", "resume_quote", "job_quote", "note")}

    return {
        "track": audit.get("track") or decision.get("track") or "core",
        "inflated_posting": bool(audit.get("inflated_posting")),
        "must_cov": round(float(decision.get("must_cov") or 0), 3),
        "threshold": round(float(decision.get("threshold") or 0), 3),
        "blocks": [str(b)[:200] for b in decision.get("blocks", [])][:8],
        "requirements": [item(e) for e in audit.get("requirements", [])][:30],
        "responsibilities": [item(e) for e in audit.get("responsibilities", [])][:12],
        "objections": [
            {"severity": str(o.get("severity", "")), "reason": str(o.get("reason") or o.get("job_quote") or "")[:200]}
            for o in audit.get("objections", [])
        ][:6],
    }


def _job_api_record(row) -> dict:
    """Keep the browser contract independent from database row implementations."""
    data = dict(row)
    data["assessment"] = _compact_assessment(data.pop("assessment_json", None))
    data["description"] = re.sub(r"<[^>]+>", " ", data.get("description") or "")[:1800]
    data["match_score"] = int(data["match_score"]) if data.get("match_score") is not None else None
    data["cover_letter"] = data.get("cover_letter") or ""
    data["has_cover_letter"] = bool(data["cover_letter"].strip())
    if urlparse(data.get("url") or "").scheme not in {"http", "https"}:
        data["url"] = ""
    data.pop("fingerprint", None)
    return data


def dashboard_api_payload() -> dict:
    with connect() as db:
        counts = {row["status"]: row["n"] for row in db.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
        rows = db.execute(JOB_SELECT + "ORDER BY jobs.first_seen_at DESC LIMIT 20").fetchall()
        runs = [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 8")]
    return {"counts": counts, "jobs": [{**_job_api_record(row), "description": "", "notes": "", "assessment": None} for row in rows], "runs": runs,
            "collector": collector.snapshot(), "status_labels": STATUSES}


def jobs_api_payload(*, page: int = 1, page_size: int = 15, status: str = "worth",
                     min_match: int = 0, sort: str = "", date_from: str = "", date_to: str = "") -> dict:
    page = max(1, page)
    page_size = max(5, min(50, page_size))
    min_match = max(0, min(100, min_match))
    status = status if status in STATUSES else "worth"
    sort = normalize_worth_sort(sort)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    lo, hi = worth_date_bounds_utc(date_from, date_to)
    where = "jobs.status = ?"
    params: list[object] = [status]
    if min_match:
        where += f" AND COALESCE({_worth_match_expr()}, 0) >= ?"
        params.append(min_match)
    if lo:
        where += " AND jobs.first_seen_at >= ?"
        params.append(lo)
    if hi:
        where += " AND jobs.first_seen_at < ?"
        params.append(hi)
    with connect() as db:
        total = int(db.execute(f"SELECT COUNT(*) n FROM jobs WHERE {where}", params).fetchone()["n"])
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        rows = db.execute(
            JOB_SELECT + f"WHERE {where} ORDER BY {worth_order_sql(sort)} LIMIT ? OFFSET ?",
            [*params, page_size, (page - 1) * page_size],
        ).fetchall()
    return {"items": [_job_api_record(row) for row in rows], "total": total, "page": page,
            "pages": pages, "page_size": page_size, "filters": {"status": status,
            "min_match": min_match, "sort": sort, "date_from": date_from, "date_to": date_to}}


# Abas da view "Não selecionadas": (filtro SQL, ordem).
TRIAGE_TABS = {
    # Descartadas pela IA e ainda não conferidas: a decisão mais recente reprovou a vaga. Vagas ignoradas à mão
    # (decisão aprovada ou sem decisão, como as da Índia) não entram.
    "discarded": (
        "jobs.status = 'ignored' AND COALESCE(jobs.reviewed_at, '') = '' AND "
        "(SELECT should_apply FROM ai_decisions WHERE ai_decisions.job_id = jobs.id ORDER BY id DESC LIMIT 1) = 0",
        "COALESCE(" + _worth_match_expr() + ", 0) DESC, jobs.id DESC",
    ),
    # Ainda sem decisão da IA: coletadas ou na fila de triagem.
    "pending": ("jobs.status IN ('new', 'review')", "jobs.first_seen_at DESC, jobs.id DESC"),
}


def triage_api_payload(*, tab: str = "discarded", page: int = 1, page_size: int = 15, min_match: int = 0,
                       sort: str = "", date_from: str = "", date_to: str = "", q: str = "") -> dict:
    """Mesmos filtros de "Vagas para revisar" (match mínimo, período em que foi encontrada, ordenação)
    mais busca por texto: cada palavra de `q` precisa aparecer no título, na empresa ou no local (sem diferenciar
    maiúsculas; % e _ valem como texto, não como curinga).

    `counts` (selos das abas) ignora os filtros; `total` e a paginação respeitam. Vagas aguardando triagem ainda
    não têm nota: match mínimo não se aplica a elas.
    """
    tab = tab if tab in TRIAGE_TABS else "discarded"
    page = max(1, page)
    page_size = max(5, min(50, page_size))
    min_match = max(0, min(100, min_match))
    sort = normalize_worth_sort(sort)
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    lo, hi = worth_date_bounds_utc(date_from, date_to)
    where, default_order = TRIAGE_TABS[tab]
    clauses, params = [where], []
    if min_match and tab == "discarded":
        clauses.append(f"COALESCE({_worth_match_expr()}, 0) >= ?")
        params.append(min_match)
    if lo:
        clauses.append("jobs.first_seen_at >= ?")
        params.append(lo)
    if hi:
        clauses.append("jobs.first_seen_at < ?")
        params.append(hi)
    terms = [term for term in re.split(r"\s+", (q or "").strip().lower()[:100]) if term][:6]
    for term in terms:
        clauses.append("LOWER(COALESCE(jobs.title, '') || ' ' || COALESCE(jobs.company, '') || ' ' || "
                       "COALESCE(jobs.location, '')) LIKE ? ESCAPE '!'")
        params.append("%" + term.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%")
    filtered = " AND ".join(clauses)
    order = worth_order_sql(sort) if sort else default_order
    with connect() as db:
        counts = {key: int(db.execute(f"SELECT COUNT(*) n FROM jobs WHERE {cond}").fetchone()["n"])
                  for key, (cond, _) in TRIAGE_TABS.items()}
        total = int(db.execute(f"SELECT COUNT(*) n FROM jobs WHERE {filtered}", params).fetchone()["n"])
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        rows = db.execute(JOB_SELECT + f"WHERE {filtered} ORDER BY {order} LIMIT ? OFFSET ?",
                          [*params, page_size, (page - 1) * page_size]).fetchall()
    return {"tab": tab, "items": [_job_api_record(row) for row in rows], "total": total, "page": page,
            "pages": pages, "page_size": page_size, "counts": counts,
            "filters": {"min_match": min_match, "sort": sort, "date_from": date_from, "date_to": date_to,
                        "q": " ".join(terms)}}


def mark_jobs_reviewed(ids: list[int]) -> int:
    """Marca vagas descartadas pela IA como conferidas (saem da aba). Só mexe em vagas ignoradas."""
    from app import now_iso

    ids = [int(item) for item in dict.fromkeys(ids)]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    with connect() as db:
        cursor = db.execute(
            f"UPDATE jobs SET reviewed_at=? WHERE id IN ({marks}) AND status='ignored' AND COALESCE(reviewed_at, '')=''",
            [now_iso(), *ids],
        )
        return max(cursor.rowcount, 0)


def _queue_payload_of(record: dict) -> dict:
    try:
        payload = json.loads(record.get("payload") or "{}")
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _queue_job_ids(payloads: list[dict]) -> list[int]:
    ids = []
    for payload in payloads:
        try:
            job_id = int(payload.get("job_id") or 0)
        except (TypeError, ValueError):
            continue
        if job_id:
            ids.append(job_id)
    return sorted(set(ids))


def _queue_job_context(db, job_ids: list[int]) -> tuple[dict[int, dict], dict[int, dict]]:
    """Vaga + última decisão da IA de cada job_id, em consultas únicas (sem N+1)."""
    jobs: dict[int, dict] = {}
    decisions: dict[int, dict] = {}
    if not job_ids:
        return jobs, decisions
    marks = ",".join("?" * len(job_ids))
    for row in db.execute(
        f"SELECT id, title, company, location, url, status FROM jobs WHERE id IN ({marks})", job_ids,
    ).fetchall():
        jobs[int(row["id"])] = dict(row)
    for row in db.execute(
        f"""SELECT job_id, decided_at, match_score, should_apply, reason, provider, model
            FROM ai_decisions
            WHERE id IN (SELECT MAX(id) FROM ai_decisions WHERE job_id IN ({marks}) GROUP BY job_id)""",
        job_ids,
    ).fetchall():
        decisions[int(row["job_id"])] = dict(row)
    return jobs, decisions


def _queue_subject(kind: str, payload: dict, jobs: dict[int, dict]) -> dict | None:
    if kind == "job_apply":
        try:
            job_id = int(payload.get("job_id") or 0)
        except (TypeError, ValueError):
            job_id = 0
        job = jobs.get(job_id)
        if not job:
            return {"type": "job", "job_id": job_id, "title": "", "company": "", "location": "",
                    "url": "", "job_status": "", "missing": True}
        url = job.get("url") or ""
        return {"type": "job", "job_id": job_id, "title": job.get("title") or "",
                "company": job.get("company") or "", "location": job.get("location") or "",
                "url": url if urlparse(url).scheme in {"http", "https"} else "",
                "job_status": job.get("status") or "", "missing": False}
    if kind == "resume_analysis":
        return {"type": "resume", "language": str(payload.get("language") or "")}
    if kind == "worth_reverify":
        return {"type": "worth", "scheduled_date": str(payload.get("scheduled_date") or "")}
    return None


def _queue_decision(decision: dict | None) -> dict | None:
    if not decision:
        return None
    # Decisões antigas gravaram provedor "gemini" com modelo "tokenharbor/x": corrige só na exibição.
    provider, model = split_model_label(str(decision.get("provider") or ""), str(decision.get("model") or ""))
    return {
        "score": decision.get("match_score"),
        "approved": bool(decision.get("should_apply")),
        "reason": str(decision.get("reason") or "")[:1000],
        "model": model,
        "provider": provider,
        "decided_at": decision.get("decided_at") or "",
    }


def queue_api_payload() -> dict:
    records = [dict(row) for row in queue.list_jobs(limit=100)]
    payloads = [_queue_payload_of(record) for record in records]
    with connect() as db:
        jobs, decisions = _queue_job_context(db, _queue_job_ids(payloads))
    items = []
    for record, payload in zip(records, payloads):
        progress = (record.get("progress_log") or "").splitlines()
        subject = _queue_subject(str(record.get("kind") or ""), payload, jobs)
        job_id = subject.get("job_id") if subject and subject["type"] == "job" else None
        items.append({key: record.get(key) for key in (
            "id", "kind", "status", "attempts", "max_attempts", "next_run_at",
            "created_at", "updated_at",
        )} | {"last_step": (progress[-1] if progress else "")[:280],
             "progress": "\n".join(progress[-80:])[-8000:],
             "outcome": (record.get("last_error") or record.get("result") or "")[:500],
             "subject": subject,
             "decision": _queue_decision(decisions.get(job_id)) if job_id else None})
    return {"counts": queue.counts(), "items": items}


def queue_item_detail_api_payload(queue_id: int) -> dict | None:
    """Auditoria compacta (checklist) da vaga de um item da fila; None se não houver."""
    with connect() as db:
        row = db.execute("SELECT kind, payload FROM queue_jobs WHERE id=?", (queue_id,)).fetchone()
        if not row or row["kind"] != "job_apply":
            return None
        ids = _queue_job_ids([_queue_payload_of(dict(row))])
        if not ids:
            return None
        decision = db.execute(
            "SELECT assessment_json FROM ai_decisions WHERE job_id=? ORDER BY id DESC LIMIT 1", (ids[0],),
        ).fetchone()
    if not decision:
        return None
    return {"assessment": _compact_assessment(decision["assessment_json"])}


def logs_api_payload(limit: int = 200) -> dict:
    return {"items": [dict(row) for row in list_logs(max(1, min(500, limit)))]}


def usage_api_payload() -> dict:
    from integrations.usage_tracker import STORE_KEY, load_store, summarize_ai_usage

    cfg = settings()
    provider = (cfg.get("ai_provider") or "gemini").casefold()
    store = load_store(cfg.get(STORE_KEY) or "")
    return {"provider": provider, "model": cfg.get("ai_model") or "",
            "apify": load_apify_quota_snapshot(),
            "ai": summarize_ai_usage(store, provider=provider)}


def resumes_api_payload() -> dict:
    with connect() as db:
        rows = {lang: get_resume(db, lang) for lang in ("pt", "en")}
    result = {}
    for lang, row in rows.items():
        result[lang] = None if row is None else {
            "language": lang,
            "filename": row["original_filename"],
            "status": row["analysis_status"],
            "message": row["analysis_message"],
            "error": row["analysis_error"],
            "summary": row["analysis_summary"],
            "analysis_json": row["analysis_json"],
            "analyzed_at": row["analyzed_at"],
            "provider": row["provider"],
            "model": row["model"],
        }
    return result


def filter_metrics_api_payload() -> dict:
    """Rendimento por filtro de busca: quantas vagas trouxe e quantas a triagem aprovou."""
    now = datetime.now(timezone.utc)
    since7 = (now - timedelta(days=7)).isoformat(timespec="seconds")
    since30 = (now - timedelta(days=30)).isoformat(timespec="seconds")
    with connect() as db:
        rows = db.execute(
            """SELECT j.search_filter_id AS filter_id, COUNT(*) AS collected,
                      SUM(CASE WHEN j.first_seen_at >= ? THEN 1 ELSE 0 END) AS last_7d,
                      SUM(CASE WHEN j.first_seen_at >= ? THEN 1 ELSE 0 END) AS last_30d,
                      SUM(CASE WHEN d.id IS NOT NULL THEN 1 ELSE 0 END) AS triaged,
                      SUM(CASE WHEN d.should_apply = 1 THEN 1 ELSE 0 END) AS approved,
                      SUM(CASE WHEN d.should_apply = 1 AND j.status = 'ignored' THEN 1 ELSE 0 END) AS ignored_by_user
               FROM jobs j
               LEFT JOIN ai_decisions d ON d.id = (SELECT MAX(x.id) FROM ai_decisions x WHERE x.job_id = j.id)
               WHERE j.search_filter_id <> ''
               GROUP BY j.search_filter_id""",
            (since7, since30),
        ).fetchall()
    metrics = {}
    for row in rows:
        values = {key: int(row[key] or 0) for key in ("collected", "last_7d", "last_30d", "triaged", "approved", "ignored_by_user")}
        rate = values["approved"] / values["triaged"] if values["triaged"] else None
        metrics[row["filter_id"]] = {
            **values, "approval_rate": rate,
            # Só um aviso: o painel nunca desativa filtros sozinho.
            "low_precision": values["triaged"] >= 20 and rate is not None and rate < 0.1,
        }
    return {"metrics": metrics}
