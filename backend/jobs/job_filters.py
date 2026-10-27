"""Job-list sorting, date bounds and match expression."""
from datetime import datetime, timedelta, timezone

from dates_br import BRASILIA


WORTH_SORT_MATCH_ASC = "match_asc"
WORTH_SORT_MATCH_DESC = "match_desc"
WORTH_SORT_SEEN_ASC = "seen_asc"
WORTH_SORT_SEEN_DESC = "seen_desc"
WORTH_SORT_POSTED_ASC = "posted_asc"
WORTH_SORT_POSTED_DESC = "posted_desc"
# Legado do select antigo.
WORTH_SORT_TOKEN = {
    WORTH_SORT_MATCH_ASC,
    WORTH_SORT_MATCH_DESC,
    WORTH_SORT_SEEN_ASC,
    WORTH_SORT_SEEN_DESC,
    WORTH_SORT_POSTED_ASC,
    WORTH_SORT_POSTED_DESC,
}


def parse_worth_sorts(sort: str | None) -> list[str]:
    """Lista ordenada de tokens ativos (primeiro = prioridade). Combináveis."""
    raw = (sort or "").strip().casefold()
    if not raw:
        return []
    if raw in {"match", "score_desc"}:
        return [WORTH_SORT_MATCH_DESC]
    if "," not in raw and "+" not in raw and raw in WORTH_SORT_TOKEN:
        return [raw]
    sep = "," if "," in raw else "+"
    out: list[str] = []
    seen_cols: set[str] = set()
    for part in raw.split(sep):
        token = part.strip().casefold()
        if token in {"match", "score_desc"}:
            token = WORTH_SORT_MATCH_DESC
        if token not in WORTH_SORT_TOKEN:
            continue
        col = token.rsplit("_", 1)[0]
        if col in seen_cols:
            continue
        seen_cols.add(col)
        out.append(token)
    return out


def encode_worth_sorts(tokens: list[str]) -> str:
    return ",".join(tokens)


def normalize_worth_sort(sort: str | None) -> str:
    """Normaliza string de sort (pode ser multi: match_desc,seen_asc)."""
    return encode_worth_sorts(parse_worth_sorts(sort))




def _worth_sort_clause(kind: str) -> str:
    if kind == WORTH_SORT_SEEN_DESC:
        return "jobs.first_seen_at DESC"
    if kind == WORTH_SORT_SEEN_ASC:
        return "jobs.first_seen_at ASC"
    if kind == WORTH_SORT_MATCH_ASC:
        return "COALESCE(match_score, 0) ASC"
    if kind == WORTH_SORT_MATCH_DESC:
        return "COALESCE(match_score, 0) DESC"
    if kind == WORTH_SORT_POSTED_ASC:
        return (
            "(CASE WHEN COALESCE(jobs.posted_at,'')='' THEN 1 ELSE 0 END) ASC, "
            "jobs.posted_at ASC"
        )
    if kind == WORTH_SORT_POSTED_DESC:
        return (
            "(CASE WHEN COALESCE(jobs.posted_at,'')='' THEN 1 ELSE 0 END) ASC, "
            "jobs.posted_at DESC"
        )
    return ""


def worth_order_sql(sort: str | None) -> str:
    clauses = [_worth_sort_clause(t) for t in parse_worth_sorts(sort)]
    clauses = [c for c in clauses if c]
    if not clauses:
        return "jobs.id DESC"
    return ", ".join(clauses) + ", jobs.id DESC"


def _parse_worth_day(raw: str | None) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        y, m, d = (int(p) for p in text.split("-", 2))
        return datetime(y, m, d, tzinfo=BRASILIA)
    except (TypeError, ValueError):
        return None


def worth_date_bounds_utc(
    date_from: str | None,
    date_to: str | None,
) -> tuple[str | None, str | None]:
    """Converte dias YYYY-MM-DD (Brasília) em limites ISO UTC [lo, hi)."""
    start_local = _parse_worth_day(date_from)
    end_local = _parse_worth_day(date_to)
    lo = start_local.astimezone(timezone.utc).isoformat(timespec="seconds") if start_local else None
    hi = None
    if end_local is not None:
        hi = (end_local + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
    return lo, hi


def _worth_match_expr() -> str:
    return """(
                        SELECT match_score FROM ai_decisions
                        WHERE ai_decisions.job_id = jobs.id
                        ORDER BY id DESC LIMIT 1
                      )"""
