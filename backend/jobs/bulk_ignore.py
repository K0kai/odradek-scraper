"""Preview and ignore jobs awaiting review with explicit criteria."""
from datetime import date

from jobs.job_filters import _worth_match_expr, worth_date_bounds_utc


FIELDS = ("country", "found_from", "found_to", "posted_from", "posted_to", "match_min", "match_max")
DATE_FIELDS = ("found_from", "found_to", "posted_from", "posted_to")


def criteria_from_body(body: dict) -> dict:
    if not isinstance(body, dict) or any(key not in (*FIELDS, "all") for key in body):
        raise ValueError("Filtros inválidos.")
    values = {key: body.get(key, "") for key in FIELDS}
    if any(not isinstance(value, str) for value in values.values()) or type(body.get("all", False)) is not bool:
        raise ValueError("Filtros inválidos.")
    values = {key: value.strip() for key, value in values.items()}
    if len(values["country"]) > 100:
        raise ValueError("Nome do país muito longo.")
    for key in DATE_FIELDS:
        value = values[key]
        if value:
            try:
                if date.fromisoformat(value).isoformat() != value:
                    raise ValueError
            except ValueError:
                raise ValueError("Use datas no formato AAAA-MM-DD.") from None
    for prefix in ("found", "posted"):
        if values[f"{prefix}_from"] and values[f"{prefix}_to"] and values[f"{prefix}_from"] > values[f"{prefix}_to"]:
            raise ValueError("A data inicial deve ser anterior à final.")
    for key in ("match_min", "match_max"):
        if values[key] and (not values[key].isdigit() or not 0 <= int(values[key]) <= 100):
            raise ValueError("O match deve estar entre 0 e 100.")
    if values["match_min"] and values["match_max"] and int(values["match_min"]) > int(values["match_max"]):
        raise ValueError("O match mínimo deve ser menor ou igual ao máximo.")
    values["all"] = body.get("all", False)
    if values["all"] and any(values[key] for key in FIELDS):
        raise ValueError("Use 'todas' sem outros filtros.")
    if not values["all"] and not any(values[key] for key in FIELDS):
        raise ValueError("Informe ao menos um filtro ou selecione todas.")
    return values


def _where(criteria: dict) -> tuple[str, list[object]]:
    clauses = ["status = 'worth'"]
    params: list[object] = []
    if criteria["country"]:
        country = criteria["country"].lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("LOWER(location) LIKE ? ESCAPE '\\'")
        params.append(f"%{country}%")
    lo, hi = worth_date_bounds_utc(criteria["found_from"], criteria["found_to"])
    if lo:
        clauses.append("first_seen_at >= ?")
        params.append(lo)
    if hi:
        clauses.append("first_seen_at < ?")
        params.append(hi)
    if criteria["posted_from"]:
        clauses.append("SUBSTR(posted_at, 1, 10) >= ?")
        params.append(criteria["posted_from"])
    if criteria["posted_to"]:
        clauses.append("SUBSTR(posted_at, 1, 10) <= ?")
        params.append(criteria["posted_to"])
    if criteria["match_min"]:
        clauses.append(f"COALESCE({_worth_match_expr()}, 0) >= ?")
        params.append(int(criteria["match_min"]))
    if criteria["match_max"]:
        clauses.append(f"COALESCE({_worth_match_expr()}, 0) <= ?")
        params.append(int(criteria["match_max"]))
    return " AND ".join(clauses), params


def preview(db, criteria: dict) -> int:
    where, params = _where(criteria)
    return int(db.execute(f"SELECT COUNT(*) AS n FROM jobs WHERE {where}", params).fetchone()["n"])


def apply(db, criteria: dict) -> int:
    where, params = _where(criteria)
    return int(db.execute(f"UPDATE jobs SET status='ignored' WHERE {where}", params).rowcount)
