"""Portfólio público (`/ai/profile.json`) como segunda fonte de evidência do perfil.

O contrato (schema 1, que o Qpid também consome) só cresce de forma aditiva; o que importa aqui é cada
registro trazer `facts`. Sem IA: o endpoint já traz `facts` estruturados (datas YYYY-MM, stack, bullets), então o perfil sai
no mesmo formato v2 de `resume_profile.ground_profile`. Cada fato cita um trecho do próprio registro,
e o texto de todos os registros acompanha o perfil para a checagem de citações da triagem.
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

from candidate.resume_profile import PROFILE_VERSION, normalize_text

BUILDER_VERSION = "1"  # muda quando build_portfolio_profile muda: força reconstruir o snapshot salvo
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 500
USER_AGENT = "job-scraper-portfolio-sync/1"

_KIND_TO_CATEGORY = {
    "language": "language", "framework": "framework", "tool": "tool",
    "practice": "domain", "ai_tool": "tool",
}
_LEVEL_RE = re.compile(r"^(?P<name>.+?)\s*\((?P<level>[^)]*)\)\s*$")

SETTING_KEYS = ("portfolio_version", "portfolio_profile_json", "portfolio_text", "portfolio_synced_at", "portfolio_error")


class PortfolioError(RuntimeError):
    """Falha ao buscar ou validar o portfólio (a mensagem é mostrada no painel)."""


# --- busca e validação ---------------------------------------------------------------------------

def validate_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise PortfolioError("Resposta do portfólio não é um objeto JSON.")
    schema = payload.get("schema")
    if not isinstance(schema, int) or schema < 1:
        raise PortfolioError("Resposta do portfólio sem `schema` válido.")
    if not isinstance(payload.get("version"), str) or not payload["version"]:
        raise PortfolioError("Resposta do portfólio sem `version`.")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise PortfolioError("Resposta do portfólio sem registros.")
    clean = []
    for record in records[:MAX_RECORDS]:
        if not isinstance(record, dict):
            continue
        if not all(isinstance(record.get(k), str) and record[k] for k in ("id", "kind", "language", "text")):
            continue
        if not isinstance(record.get("facts"), dict):
            continue
        clean.append(record)
    if not clean:
        raise PortfolioError("O portfólio não traz `facts` estruturados nos registros: atualize o endpoint /ai/profile.json.")
    return {"schema": schema, "version": payload["version"], "records": clean}


def fetch_portfolio(url: str, timeout: int = 20) -> dict:
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PortfolioError("URL do portfólio inválida: use http(s)://...")
    request = urllib.request.Request(url.strip(), headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(MAX_BODY_BYTES + 1)
    except Exception as exc:  # rede, HTTP, DNS, timeout
        raise PortfolioError(f"Não foi possível buscar o portfólio: {exc}") from exc
    if len(body) > MAX_BODY_BYTES:
        raise PortfolioError("Resposta do portfólio maior que 2 MB.")
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PortfolioError("O portfólio não retornou JSON válido.") from exc
    return validate_payload(data)


# --- montagem do perfil --------------------------------------------------------------------------

def _clip(value: object, limit: int) -> str:
    """Recorta em limite de palavra, para que o trecho continue sendo substring do texto original."""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > 0 else limit].strip()


def _end(value: object) -> str:
    return str(value) if value else "present"


def _str_list(value: object, limit: int = 40) -> list[str]:
    return [" ".join(str(i).split()) for i in value if str(i).strip()][:limit] if isinstance(value, list) else []


def build_portfolio_profile(payload: dict, language: str) -> dict:
    """Perfil no formato v2 a partir dos registros do idioma. Fatos levam `source` e `url`."""
    records = [r for r in payload["records"] if r["language"] == language]
    profile: dict = {
        "version": PROFILE_VERSION, "source": "portfolio",
        "headline": "", "professional_profile": "", "seniority": "",
        "location_notes": "", "work_authorization_notes": "",
        "roles": [], "skills": [], "transferable_skills": [], "education": [],
        "certifications": [], "projects": [], "achievements": [], "languages": [],
    }
    skill_by_key: dict[str, dict] = {}

    def add_skill(name: str, category: str, quote: str) -> None:
        name = " ".join(name.split())[:80]
        canonical = normalize_text(name)
        if not name or not canonical or canonical in skill_by_key:
            return
        item = {"id": f"S{len(profile['skills']) + 1}", "name": name, "canonical": canonical,
                "category": category, "quotes": [quote], "source": "portfolio"}
        profile["skills"].append(item)
        skill_by_key[canonical] = item

    stack_sources: list[tuple[list[str], str]] = []  # (stack, quote) de cargos e projetos, para skills sem categoria

    for record in records:
        kind, facts, url = record["kind"], record["facts"], record.get("url", "")
        text = _clip(record["text"], 4000)
        if kind == "profile":
            profile["professional_profile"] = text
            profile["location_notes"] = _clip(facts.get("residence"), 200)  # residência, não cidadania nem autorização
        elif kind == "skills":
            items = _str_list(facts.get("items"), 80)
            if facts.get("kind") == "spoken":
                for entry in items:
                    match = _LEVEL_RE.match(entry)
                    name, level = (match["name"].strip(), match["level"].strip()) if match else (entry, "")
                    profile["languages"].append({
                        "id": f"L{len(profile['languages']) + 1}", "language": name[:60], "level": level[:60],
                        "quote": entry[:300], "source": "portfolio", "url": url,
                    })
            else:
                category = _KIND_TO_CATEGORY.get(str(facts.get("kind")), "other")
                for entry in items:
                    add_skill(entry, category, _clip(text, 300))
        elif kind == "experience":
            role_id = f"R{len(profile['roles']) + 1}"
            responsibilities = [
                {"id": f"{role_id}.{n}", "text": _clip(b, 400), "quote": _clip(b, 400)}
                for n, b in enumerate(_str_list(facts.get("bullets"), 30), start=1)
            ]
            stack = _str_list(facts.get("stack"))
            profile["roles"].append({
                "id": role_id, "title": _clip(facts.get("role"), 160), "employer": _clip(facts.get("company"), 160),
                "start": str(facts.get("start") or ""), "end": _end(facts.get("end")),
                "quote": _clip(text, 200), "responsibilities": responsibilities, "skills_used": stack,
                "source": "portfolio", "url": url,
            })
            stack_sources.append((stack, _clip(text, 300)))
        elif kind == "project" and record["id"].count(":") == 2:  # lang:project:slug; ignora :bullet:N e :story
            stack = _str_list(facts.get("stack"))
            profile["projects"].append({
                "id": f"P{len(profile['projects']) + 1}", "text": _clip(text, 400), "quote": _clip(text, 200),
                "title": _clip(facts.get("title") or record.get("title"), 160), "slug": str(facts.get("slug") or ""),
                "headline": _clip(facts.get("headline"), 240),
                "start": str(facts.get("start") or ""), "end": _end(facts.get("end")),
                "stack": stack, "source": "portfolio", "url": url,
            })
            stack_sources.append((stack, _clip(text, 300)))
        elif kind == "education":
            profile["education"].append({
                "id": f"E{len(profile['education']) + 1}", "text": _clip(text, 400), "quote": _clip(text, 200),
                "start": str(facts.get("start") or ""), "end": _end(facts.get("end")),
                "source": "portfolio", "url": url,
            })

    # itens de stack que não aparecem nas categorias de skills viram skill "other"
    for stack, quote in stack_sources:
        for name in stack:
            add_skill(name, "other", quote)
    return profile


def portfolio_text(payload: dict) -> str:
    """Texto de todos os registros (PT e EN) para verificar citações de qualquer idioma."""
    return "\n".join(_clip(r["text"], 4000) for r in payload["records"])


# --- snapshot no banco ---------------------------------------------------------------------------

def _get(db, key: str) -> str:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return (row["value"] if row else "") or ""


def _put(db, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def sync_portfolio(db, url: str, *, force: bool = False, fetch=fetch_portfolio, now_iso: str | None = None) -> dict:
    """Baixa o portfólio se a `version` mudou. Nunca levanta; em falha mantém o último snapshot bom."""
    url = (url or "").strip()
    if not url:
        return {"status": "disabled", "version": _get(db, "portfolio_version"), "message": "URL do portfólio não configurada."}
    try:
        payload = fetch(url)
        key = f"{payload['version']}+b{BUILDER_VERSION}"
        if not force and key == _get(db, "portfolio_version") and _get(db, "portfolio_profile_json"):
            _put(db, "portfolio_error", "")
            return {"status": "unchanged", "version": key, "message": "Portfólio já está atualizado."}
        snapshot = {lang: build_portfolio_profile(payload, lang) for lang in ("pt", "en")}
        if not any(p["roles"] or p["projects"] or p["skills"] for p in snapshot.values()):
            raise PortfolioError("O portfólio não trouxe cargos, projetos nem skills.")
    except Exception as exc:  # inclui PortfolioError e falhas inesperadas de formato
        message = str(exc)[:500] or exc.__class__.__name__
        _put(db, "portfolio_error", message)
        return {"status": "error", "version": _get(db, "portfolio_version"), "message": message}
    _put(db, "portfolio_profile_json", json.dumps(snapshot, ensure_ascii=False))
    _put(db, "portfolio_text", portfolio_text(payload))
    _put(db, "portfolio_version", key)
    _put(db, "portfolio_synced_at", now_iso or datetime.now(timezone.utc).isoformat(timespec="seconds"))
    _put(db, "portfolio_error", "")
    counts = {k: len(snapshot["pt"][k]) for k in ("roles", "projects", "skills")}
    return {"status": "updated", "version": key,
            "message": f"Portfólio sincronizado: {counts['roles']} cargo(s), {counts['projects']} projeto(s), {counts['skills']} skill(s)."}


def load_portfolio(db, language: str = "pt") -> tuple[dict, str] | None:
    """(perfil do idioma, texto) do último snapshot salvo, ou None."""
    raw = _get(db, "portfolio_profile_json")
    if not raw:
        return None
    try:
        snapshot = json.loads(raw)
    except ValueError:
        return None
    profile = snapshot.get(language) or snapshot.get("pt") or snapshot.get("en")
    if not isinstance(profile, dict) or profile.get("version") != PROFILE_VERSION:
        return None
    return profile, _get(db, "portfolio_text")


def portfolio_status(db) -> dict:
    snapshot = load_portfolio(db)
    profile = snapshot[0] if snapshot else {}
    return {
        "url": _get(db, "portfolio_profile_url"), "version": _get(db, "portfolio_version"),
        "synced_at": _get(db, "portfolio_synced_at"), "error": _get(db, "portfolio_error"),
        "counts": {k: len(profile.get(k, [])) for k in ("roles", "projects", "skills", "education", "languages")},
    }
