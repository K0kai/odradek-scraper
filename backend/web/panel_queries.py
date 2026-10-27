"""Read models returned to the web panel."""
import re
from urllib.parse import urlparse

from app import STATUSES, collector, connect, load_apify_quota_snapshot, queue, settings
from jobs.job_filters import _worth_match_expr, normalize_worth_sort, worth_date_bounds_utc, worth_order_sql
from web.panel_log import list_logs
from candidate.resume_pipeline import get_resume


JOB_SELECT = (
    "SELECT jobs.id, jobs.title, jobs.company, jobs.location, jobs.source, jobs.url, "
    "jobs.posted_at, jobs.first_seen_at, jobs.status, jobs.description, jobs.notes, "
    "cover_letters.body AS cover_letter, " + _worth_match_expr() + " AS match_score "
    "FROM jobs LEFT JOIN cover_letters ON cover_letters.job_id=jobs.id "
)


def _job_api_record(row) -> dict:
    """Keep the browser contract independent from database row implementations."""
    data = dict(row)
    data["description"] = re.sub(r"<[^>]+>", " ", data.get("description") or "")[:1800]
    data["match_score"] = int(data["match_score"]) if data.get("match_score") is not None else None
    data["has_cover_letter"] = bool(data.pop("cover_letter", None))
    if urlparse(data.get("url") or "").scheme not in {"http", "https"}:
        data["url"] = ""
    data.pop("fingerprint", None)
    return data


def dashboard_api_payload() -> dict:
    with connect() as db:
        counts = {row["status"]: row["n"] for row in db.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
        rows = db.execute(JOB_SELECT + "ORDER BY jobs.first_seen_at DESC LIMIT 20").fetchall()
        runs = [dict(row) for row in db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 8")]
    return {"counts": counts, "jobs": [{**_job_api_record(row), "description": "", "notes": ""} for row in rows], "runs": runs,
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


def queue_api_payload() -> dict:
    items = []
    for row in queue.list_jobs(limit=100):
        record = dict(row)
        progress = (record.get("progress_log") or "").splitlines()
        items.append({key: record.get(key) for key in (
            "id", "kind", "status", "attempts", "max_attempts", "next_run_at",
            "created_at", "updated_at",
        )} | {"last_step": (progress[-1] if progress else "")[:280],
             "progress": "\n".join(progress[-40:])[-4000:],
             "outcome": (record.get("last_error") or record.get("result") or "")[:500]})
    return {"counts": queue.counts(), "items": items}


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
