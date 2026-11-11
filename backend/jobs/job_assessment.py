"""Triagem v2: requisitos da vaga × evidência do currículo, verificados pelo código.

O LLM só extrai (requisitos, responsabilidades, correspondências) e CITA. O código:
descarta o que não tem citação verificável, calcula o score, aplica bloqueios e
só então aprova. Nenhuma lista fixa de cargos e nenhum corte universal de anos:
tudo compara o que a vaga declara com o que o currículo prova.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from candidate.resume_profile import (
    NormalizedText, compact_profile_text, evidence_index, normalize_text, quote_in_text,
)

SCORING_VERSION = "v2.1"

SCORING_V2: dict[str, Any] = {
    "weights": {"must": 0.70, "nice": 0.15, "fit": 0.15},
    "status_credit": {"met": 1.0, "transferable": 0.75, "partial": 0.5, "unmet": 0.0, "unknown": 0.0},
    "fit_credit": {"same": 1.0, "adjacent": 0.7},
    "different_with_strong_transfer": 0.5,
    "years_tolerance": 1.0,
    "kitchen_sink_musts": 10,
    "inflated_majority": 0.5,
    "transfer_core_coverage": 0.6,
    "transferable_cov_relax": 0.10,
    "major_objection_penalty": 0.10,
    "major_objection_cap": 0.30,
    # otimismo (0-100) → cobertura mínima de obrigatórios core (interpolação linear)
    "optimism_curve": ((0, 1.00), (50, 0.80), (75, 0.70), (100, 0.55)),
}

_NEVER_INFLATED = {"location", "authorization", "language", "clearance"}
_CATEGORIES = ["skill", "experience_years", "seniority", "degree", "language", "location", "authorization", "clearance", "other"]
_STOPWORDS = set(
    "a o e de do da dos das em no na nos nas um uma para por com sem ou que se ao as os the and or of to in for with on at "
    "an be is are will you your our we their this that from as by".split()
)
_LANGUAGE_ALIASES = {
    "ingles": "english", "english": "english", "portugues": "portuguese", "portuguese": "portuguese",
    "espanhol": "spanish", "spanish": "spanish", "frances": "french", "french": "french",
    "alemao": "german", "german": "german", "italiano": "italian", "italian": "italian",
}

_REQ = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "text": {"type": "string"},
        "kind": {"type": "string", "enum": ["must", "nice"]},
        "category": {"type": "string", "enum": _CATEGORIES},
        "job_quote": {"type": "string"},
        "realism": {"type": "string", "enum": ["core", "inflated"]},
        "realism_basis_quote": {"type": "string"},
        "realism_reason": {"type": "string"},
    },
}

ASSESS_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "requirements": {"type": "array", "items": _REQ},
        "role_core": {
            "type": "object",
            "properties": {
                "level_label": {"type": "string"},
                "level_quote": {"type": "string"},
                "responsibilities": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}, "text": {"type": "string"}, "job_quote": {"type": "string"}},
                    },
                },
            },
        },
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requirement_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["met", "transferable", "partial", "unmet", "unknown"]},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "resume_quote": {"type": "string"},
                    "bridge": {"type": "string"},
                },
            },
        },
        "stated": {
            "type": "object",
            "properties": {
                "min_years": {"type": "number"},
                "min_years_quote": {"type": "string"},
                "workplace": {"type": "string", "enum": ["remote", "hybrid", "onsite", "unspecified"]},
                "workplace_quote": {"type": "string"},
                "job_country": {"type": "string"},
                "job_country_quote": {"type": "string"},
                "job_city": {"type": "string"},
                "job_city_quote": {"type": "string"},
                "foreign_eligibility": {"type": "string", "enum": ["open_to_candidate", "restricted", "unstated"]},
                "eligibility_quote": {"type": "string"},
                "authorization": {"type": "string", "enum": ["not_required_or_candidate_has", "candidate_lacks", "unclear"]},
                "authorization_quote": {"type": "string"},
                "required_languages": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"language": {"type": "string"}, "quote": {"type": "string"}}},
                },
            },
        },
        "role_family_fit": {"type": "string", "enum": ["same", "adjacent", "different"]},
        "fit_reason": {"type": "string"},
        "cover_letter_required": {"type": "boolean"},
    },
}

SKEPTIC_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "objections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["new_issue", "requirement_unmet", "inflated_is_core"]},
                    "requirement_id": {"type": "string"},
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                    "job_quote": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}


def clean_job_html(raw: str) -> str:
    """HTML → texto com quebras de linha de bloco preservadas e entidades resolvidas."""
    import html as html_lib

    text = re.sub(r"(?i)<\s*(br|/p|/li|/div|/h[1-6]|/tr)\s*/?>", "\n", raw or "")
    text = re.sub(r"(?i)<\s*li[^>]*>", "\n- ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def optimism_threshold(value: object) -> float:
    try:
        optimism = max(0.0, min(100.0, float(str(75 if value is None or value == "" else value))))
    except ValueError:
        optimism = 75.0
    curve = SCORING_V2["optimism_curve"]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if optimism <= x1:
            return y0 + (y1 - y0) * (optimism - x0) / (x1 - x0)
    return curve[-1][1]


def _significant_tokens(text: str) -> set[str]:
    return {t for t in normalize_text(text).split() if len(t) >= 3 and t not in _STOPWORDS}


def build_assess_prompt(job: dict, job_text: str, profile: dict, cfg: dict, language: str, preferences: str) -> str:
    facts = (cfg.get("candidate_facts_pt" if language == "pt" else "candidate_facts_en") or "").strip()
    where = ", ".join(p for p in (cfg.get("candidate_city"), cfg.get("candidate_state"), cfg.get("candidate_country")) if p)
    return f"""You audit ONE job posting against ONE candidate profile. You EXTRACT and CITE; a program verifies every quote and computes the score. Never invent.

QUOTES: every `job_quote`, `realism_basis_quote`, `level_quote`, `*_quote` must be a SHORT fragment copied VERBATIM from the JOB TEXT. `resume_quote` must be copied VERBATIM from a quote/text of the candidate PROFILE evidence you cite. Items whose quotes cannot be found are discarded.

1. requirements[]: the posting's requirements (ids Q1, Q2, ...). `kind`=must only when the posting states it as required/mandatory/"X+ years"/obligatório; when in doubt use nice. `category` as listed.
2. role_core: what the person will ACTUALLY DO day to day (responsibilities J1, J2, ... from the duties section, not the requirements list) and the posting's stated level label (intern/junior/senior...) with its quote.
3. REALISM: judge each requirement against the posting ITSELF. Mark `inflated` only when the posting contradicts or does not support it, and give `realism_basis_quote` = verbatim quote from the posting that shows the contradiction (e.g. an "Internship" title next to "5+ years", a junior role demanding architect-level skills, a requirement unrelated to any listed duty). Otherwise `core`. location/authorization/language/clearance requirements are never inflated.
4. matches[]: one per requirement id (Q*) AND per responsibility id (J*). status:
   - met: the profile directly shows it. transferable: not done exactly, but the profile shows an equivalent capability (explain in `bridge`, one sentence). partial: related but incomplete. unmet: no evidence. unknown: cannot tell.
   - `evidence_ids`: ids from the profile ([R1], [R1.2], [S3], [T1]...). `resume_quote`: verbatim from that evidence. met/transferable/partial WITHOUT valid evidence ids will be downgraded to unmet.
   - Credit transferable capabilities generously but only when truly equivalent and evidenced (e.g. internal user support -> helpdesk, spreadsheet reporting -> data analysis).
5. stated: min_years (number, 0 if none) with quote; workplace (remote/hybrid/onsite) with `workplace_quote`, or "unspecified" when the posting does not say; job_country and job_city where the job is based, each with a quote from the JOB TEXT or the Location line (empty when not stated); foreign_eligibility for the candidate living in {where or 'an unknown location'}: open_to_candidate ONLY when the posting explicitly says people there (or anyone/worldwide/"work from anywhere"/the candidate's region) can apply, restricted when it limits applicants to places that exclude the candidate, otherwise unstated; always with `eligibility_quote`. Do not guess any of these. authorization = candidate_lacks ONLY if the posting requires work authorization the candidate does not have per the profile, with quote; required_languages (languages the posting requires for the job, with quotes).
6. role_family_fit: same (same occupation family as candidate's roles), adjacent (neighbouring family, skills clearly overlap), different. Be honest; adjacent/different is acceptable when transferable capabilities exist.
7. cover_letter_required: true only if the posting explicitly asks for a cover letter.

CANDIDATE PREFERENCES (judgement guidance, never overrides the rules above):
{preferences}

CANDIDATE FACTS: {facts or '[none]'}

CANDIDATE PROFILE (evidence ids in brackets):
{compact_profile_text(profile, max_chars=6500)}

JOB
Title: {job.get('title', '')}
Company: {job.get('company', '')}
Location: {job.get('location', '')}
JOB TEXT:
{job_text[:14000]}
"""


def build_skeptic_prompt(job: dict, job_text: str, profile: dict, verified: dict) -> str:
    lines = [f"- {r['id']} [{r['kind']}/{r['realism']}] {r['text']} => {r['status']}" for r in verified["requirements"]]
    return f"""You are a SKEPTICAL reviewer. A screening already APPROVED this application. Find concrete, CITED reasons it should NOT go ahead. Do not invent problems; no quote, no objection.

Rules: `job_quote` must be copied VERBATIM from the JOB TEXT. severity: blocker = the candidate clearly cannot do/qualify for the job; major = a significant uncovered core requirement; minor = small gap.
kind: requirement_unmet (a requirement id the screening marked met/transferable/partial that the profile does not really support), new_issue (a problem the screening missed), inflated_is_core (a requirement the screening called inflated that is actually central to the duties - cite the duty).
Never use a requirement marked `inflated` as a blocker unless kind is inflated_is_core with proof.

SCREENING RESULT
{chr(10).join(lines)}

CANDIDATE PROFILE
{compact_profile_text(profile, max_chars=4500)}

JOB TEXT
{job_text[:12000]}
"""


def _credit(status: str) -> float:
    return SCORING_V2["status_credit"].get(status, 0.0)


def _verify(raw: dict, job_norm: NormalizedText, resume_norm: NormalizedText, profile: dict) -> dict:
    """Aplica as verificações de citação/evidência e normaliza a estrutura."""
    index = evidence_index(profile)
    dropped = {"requirements": 0, "responsibilities": 0, "matches_downgraded": 0, "realism_reverted": 0}

    def grounded(quote: object) -> bool:
        return isinstance(quote, str) and quote_in_text(quote, job_norm)

    requirements: list[dict] = []
    seen: set[str] = set()
    for item in raw.get("requirements") or []:
        rid = str(item.get("id") or "").strip()
        if not rid or rid in seen or not str(item.get("text") or "").strip() or not grounded(item.get("job_quote")):
            dropped["requirements"] += 1
            continue
        seen.add(rid)
        realism = item.get("realism") if item.get("realism") in ("core", "inflated") else "core"
        category = item.get("category") if item.get("category") in _CATEGORIES else "other"
        reason = str(item.get("realism_reason") or "")[:300]
        if realism == "inflated" and (category in _NEVER_INFLATED or not grounded(item.get("realism_basis_quote"))):
            realism, reason = "core", ""
            dropped["realism_reverted"] += 1
        requirements.append({
            "id": rid, "text": str(item["text"])[:300], "kind": "must" if item.get("kind") == "must" else "nice",
            "category": category, "job_quote": str(item["job_quote"])[:300], "realism": realism,
            "realism_basis_quote": str(item.get("realism_basis_quote") or "")[:300] if realism == "inflated" else "",
            "realism_reason": reason,
        })

    role_core = raw.get("role_core") or {}
    responsibilities: list[dict] = []
    for item in role_core.get("responsibilities") or []:
        rid = str(item.get("id") or "").strip()
        if rid and rid not in seen and str(item.get("text") or "").strip() and grounded(item.get("job_quote")):
            seen.add(rid)
            responsibilities.append({"id": rid, "text": str(item["text"])[:300], "job_quote": str(item["job_quote"])[:300]})
        else:
            dropped["responsibilities"] += 1
    level_label = str(role_core.get("level_label") or "")[:80] if grounded(role_core.get("level_quote")) else ""

    # kitchen sink: muitos obrigatórios de skill desligados de qualquer responsabilidade → lista de desejos
    resp_tokens = set().union(*[_significant_tokens(r["text"] + " " + r["job_quote"]) for r in responsibilities]) if responsibilities else set()
    core_skill_musts = [r for r in requirements if r["kind"] == "must" and r["realism"] == "core" and r["category"] in ("skill", "other")]
    if responsibilities and len(core_skill_musts) > SCORING_V2["kitchen_sink_musts"]:
        for req in core_skill_musts:
            if not (_significant_tokens(req["text"]) & resp_tokens):
                req["realism"] = "inflated"
                req["realism_reason"] = "lista de desejos: não ligada a nenhuma responsabilidade descrita"

    matches: dict[str, dict] = {}
    valid_ids = {r["id"] for r in requirements} | {r["id"] for r in responsibilities}
    for item in raw.get("matches") or []:
        rid = str(item.get("requirement_id") or "").strip()
        if rid not in valid_ids or rid in matches:
            continue
        status = item.get("status") if item.get("status") in SCORING_V2["status_credit"] else "unknown"
        evidence = [e for e in (item.get("evidence_ids") or []) if e in index]
        quote = str(item.get("resume_quote") or "")
        bridge = str(item.get("bridge") or "")[:300]
        note = ""
        if status in ("met", "transferable", "partial") and not evidence:
            status, note = "unmet", "sem evidência válida no perfil"
        elif status in ("met", "transferable") and not (quote_in_text(quote, resume_norm) if quote else False):
            # a citação do currículo pode estar nas próprias evidências citadas
            evidence_blob = NormalizedText.of(" ".join(f"{index[e]['text']} {' '.join(index[e]['quotes'])}" for e in evidence))
            if not (quote and quote_in_text(quote, evidence_blob)):
                status, note = "unmet", "citação do currículo não verificada"
        elif status == "transferable" and not bridge:
            status, note = "partial", "sem ponte explicada"
        if note:
            dropped["matches_downgraded"] += 1
        matches[rid] = {"status": status, "evidence_ids": evidence[:6], "resume_quote": quote[:300], "bridge": bridge, "note": note}
    for rid in valid_ids:
        matches.setdefault(rid, {"status": "unknown", "evidence_ids": [], "resume_quote": "", "bridge": "", "note": ""})
    for req in requirements:
        req.update({"status": matches[req["id"]]["status"], "evidence_ids": matches[req["id"]]["evidence_ids"],
                    "bridge": matches[req["id"]]["bridge"], "note": matches[req["id"]]["note"],
                    "resume_quote": matches[req["id"]]["resume_quote"]})
    for resp in responsibilities:
        resp.update({"status": matches[resp["id"]]["status"], "evidence_ids": matches[resp["id"]]["evidence_ids"],
                     "bridge": matches[resp["id"]]["bridge"], "note": matches[resp["id"]]["note"],
                     "resume_quote": matches[resp["id"]]["resume_quote"]})

    stated_raw = raw.get("stated") or {}
    workplace = stated_raw.get("workplace") if stated_raw.get("workplace") in ("remote", "hybrid", "onsite") else "unspecified"
    if workplace != "unspecified" and not grounded(stated_raw.get("workplace_quote")):
        workplace = "unspecified"  # modelo de trabalho sem citação verificável não conta
    stated = {
        "min_years": 0.0, "workplace": workplace,
        "workplace_quote": str(stated_raw.get("workplace_quote") or "")[:200] if workplace != "unspecified" else "",
        "job_country": "", "job_city": "", "foreign_eligibility": "unstated", "eligibility_quote": "",
        "authorization": "unclear", "authorization_quote": "", "required_languages": [],
    }
    for key in ("job_country", "job_city"):
        value = str(stated_raw.get(key) or "").strip()[:80]
        if value and grounded(stated_raw.get(f"{key}_quote")):
            stated[key] = value
    if stated_raw.get("foreign_eligibility") in ("open_to_candidate", "restricted") and grounded(stated_raw.get("eligibility_quote")):
        stated["foreign_eligibility"] = stated_raw["foreign_eligibility"]
        stated["eligibility_quote"] = str(stated_raw["eligibility_quote"])[:300]
    try:
        years = float(stated_raw.get("min_years") or 0)
    except (TypeError, ValueError):
        years = 0.0
    if years > 0 and grounded(stated_raw.get("min_years_quote")):
        stated["min_years"] = years
        stated["min_years_quote"] = str(stated_raw["min_years_quote"])[:200]
    if stated_raw.get("authorization") == "candidate_lacks" and grounded(stated_raw.get("authorization_quote")):
        stated["authorization"] = "candidate_lacks"
        stated["authorization_quote"] = str(stated_raw["authorization_quote"])[:300]
    for item in stated_raw.get("required_languages") or []:
        if isinstance(item, dict) and str(item.get("language") or "").strip() and grounded(item.get("quote")):
            stated["required_languages"].append({"language": str(item["language"])[:40], "quote": str(item["quote"])[:200]})

    fit = raw.get("role_family_fit") if raw.get("role_family_fit") in ("same", "adjacent", "different") else "different"
    return {
        "requirements": requirements, "responsibilities": responsibilities, "level_label": level_label,
        "stated": stated, "role_family_fit": fit, "fit_reason": str(raw.get("fit_reason") or "")[:400],
        "cover_letter_required": bool(raw.get("cover_letter_required")), "dropped": dropped,
    }


def _coverage(items: list[dict]) -> float:
    return sum(_credit(i["status"]) for i in items) / len(items) if items else 0.0


def _candidate_languages(profile: dict) -> set[str]:
    out = set()
    for item in profile.get("languages", []):
        key = normalize_text(item.get("language", ""))
        out.add(_LANGUAGE_ALIASES.get(key, key))
    return out


_COUNTRY_ALIASES = {
    "brasil": "brazil", "br": "brazil", "brazil": "brazil", "eua": "united states", "usa": "united states",
    "us": "united states", "united states of america": "united states", "uk": "united kingdom",
    "reino unido": "united kingdom",
}


def geo_enabled(cfg: dict) -> bool:
    """Elegibilidade geográfica opcional (padrão ligada). Desligada: modelo e país não bloqueiam."""
    return str(cfg.get("geo_eligibility_enabled", "1")).strip() != "0"


def _country_key(value: object) -> str:
    key = normalize_text(str(value or ""))
    return _COUNTRY_ALIASES.get(key, key)


def geo_blocks(stated: dict, job: dict, cfg: dict, language: str = "") -> list[str]:
    """Exterior: só remoto e aberto a estrangeiros. Presencial/híbrido: só na cidade de residência.

    Tudo vem de afirmações citadas e verificadas da vaga; sem garantia explícita no exterior, bloqueia.
    """
    if not geo_enabled(cfg):
        return []
    from jobs.form_rules import infer_candidate_country

    home_city = normalize_text(cfg.get("candidate_city") or "")
    home_country = _country_key(infer_candidate_country(cfg))
    job_country = _country_key(stated.get("job_country"))
    workplace, eligibility = stated.get("workplace", "unspecified"), stated.get("foreign_eligibility", "unstated")
    where = normalize_text(f"{job.get('location', '')} {stated.get('job_city', '')}")

    if eligibility == "restricted":
        return ["vaga restrita a candidatos de locais que excluem o candidato"]
    if job_country and home_country:
        foreign = job_country != home_country
    elif job_country:
        foreign = False
    else:
        foreign = language not in ("", "pt")  # sem país citado: vaga em outro idioma é tratada como exterior
    if foreign:
        if workplace != "remote":
            return ["vaga no exterior não remota"]
        if eligibility != "open_to_candidate":
            return ["vaga no exterior sem garantia explícita de candidatura estrangeira"]
        return []
    if workplace == "remote":
        return []
    city = home_city.title() if home_city else ""
    if workplace == "unspecified":
        job_city = normalize_text(stated.get("job_city") or "")
        if job_city and home_city and home_city not in job_city:
            return [f"vaga em {stated['job_city']} sem informar trabalho remoto (fora de {city})"]
        return []
    label = "presencial" if workplace == "onsite" else "híbrida"
    if not home_city:
        return [f"vaga {label}: configure sua cidade no Perfil para avaliar"]
    if home_city not in where:
        return [f"vaga {label} fora de {city}"]
    return []


def decide(verified: dict, profile: dict, cfg: dict, *, track: str, job: dict, language: str = "") -> dict:
    """Bloqueios + score determinístico + decisão. Não chama IA."""
    reqs, resps, stated = verified["requirements"], verified["responsibilities"], verified["stated"]
    musts = [r for r in reqs if r["kind"] == "must"]
    core_musts = [r for r in musts if r["realism"] == "core"]
    inflated = [r for r in reqs if r["realism"] == "inflated"]
    mostly_inflated = bool(musts) and len([r for r in musts if r["realism"] == "inflated"]) / len(musts) > SCORING_V2["inflated_majority"]

    if mostly_inflated or not core_musts:
        must_items, low_confidence = list(resps), not resps
        if not must_items:
            must_items = core_musts or [r for r in reqs if r["kind"] == "nice" and r["realism"] == "core"]
            low_confidence = True
    else:
        must_items, low_confidence = core_musts, False
    must_cov = _coverage(must_items)
    nice_items = [r for r in reqs if r["kind"] == "nice" and r["realism"] == "core"] + [r for r in inflated if _credit(r["status"]) > 0]
    nice_cov = _coverage(nice_items) if nice_items else must_cov

    # caminho transferível: cobertura das responsabilidades com met/transferable verificados
    resp_cov = sum(1.0 if r["status"] in ("met", "transferable") else 0.5 if r["status"] == "partial" else 0.0 for r in resps) / len(resps) if resps else must_cov
    has_transferable = any(r["status"] == "transferable" for r in resps + reqs)
    if resp_cov >= SCORING_V2["transfer_core_coverage"] and has_transferable:
        transfer_path = "strong"
    elif resp_cov >= 0.3:
        transfer_path = "partial"
    else:
        transfer_path = "none"

    fit = verified["role_family_fit"]
    effective_track = "transferable" if (track == "transferable" or fit == "different") else "core"
    if fit == "different":
        fit_credit = SCORING_V2["different_with_strong_transfer"] if transfer_path == "strong" else 0.0
    else:
        fit_credit = SCORING_V2["fit_credit"][fit]

    w = SCORING_V2["weights"]
    score = round(100 * (w["must"] * must_cov + w["nice"] * nice_cov + w["fit"] * fit_credit))

    blocks: list[str] = []
    years_reqs = [r for r in reqs if r["category"] == "experience_years"]
    years_applies = stated["min_years"] > 0 and (not years_reqs or any(r["realism"] == "core" for r in years_reqs))
    candidate_years = profile.get("derived", {}).get("total_months", 0) / 12
    if years_applies and stated["min_years"] > candidate_years + SCORING_V2["years_tolerance"]:
        blocks.append(f"experiência exigida {stated['min_years']:g}+ anos > {candidate_years:.1f} anos comprovados")
    have_languages = _candidate_languages(profile)
    if have_languages:
        for item in stated["required_languages"]:
            key = normalize_text(item["language"])
            key = _LANGUAGE_ALIASES.get(key, key)
            if key not in have_languages:
                blocks.append(f"idioma exigido ausente no currículo: {item['language']}")
    if stated["authorization"] == "candidate_lacks":
        blocks.append("exige autorização de trabalho que o candidato não tem")
    blocks.extend(geo_blocks(stated, job, cfg, language or str(job.get("language") or "")))
    if fit == "different" and transfer_path == "none":
        blocks.append("área diferente sem caminho transferível verificado")
    for req in core_musts:
        if req["category"] in ("degree", "clearance", "authorization") and req["status"] == "unmet":
            blocks.append(f"requisito eliminatório não atendido: {req['text'][:80]}")

    threshold = optimism_threshold(cfg.get("job_selection_optimism"))
    if effective_track == "transferable":
        threshold = max(0.5, threshold - SCORING_V2["transferable_cov_relax"])
        try:
            min_score = int(cfg.get("transferable_minimum_score") or 60)
        except ValueError:
            min_score = 60
        if resp_cov < SCORING_V2["transfer_core_coverage"]:
            blocks.append(f"responsabilidades centrais cobertas {resp_cov:.0%} (< {SCORING_V2['transfer_core_coverage']:.0%})")
    else:
        try:
            min_score = int(cfg.get("minimum_match_score") or 70)
        except ValueError:
            min_score = 70
    return {
        "track": effective_track, "must_cov": must_cov, "nice_cov": nice_cov, "fit_credit": fit_credit,
        "score": max(0, min(100, score)), "threshold": threshold, "min_score": max(0, min(100, min_score)),
        "blocks": blocks, "transfer_path": transfer_path, "resp_cov": resp_cov,
        "mostly_inflated": mostly_inflated, "low_confidence": low_confidence,
        "must_ids": [i["id"] for i in must_items], "inflated_ids": [r["id"] for r in inflated],
    }


def _approved(decision: dict, penalty: float = 0.0) -> bool:
    cov = max(0.0, decision["must_cov"] - penalty)
    return not decision["blocks"] and cov >= decision["threshold"] and decision["score"] >= decision["min_score"]


def _verify_skeptic(raw: dict, job_norm: NormalizedText, verified: dict) -> list[dict]:
    inflated = {r["id"] for r in verified["requirements"] if r["realism"] == "inflated"}
    out: list[dict] = []
    for item in raw.get("objections") or []:
        if not isinstance(item, dict) or not (isinstance(item.get("job_quote"), str) and quote_in_text(item["job_quote"], job_norm)):
            continue
        kind = item.get("kind") if item.get("kind") in ("new_issue", "requirement_unmet", "inflated_is_core") else "new_issue"
        severity = item.get("severity") if item.get("severity") in ("blocker", "major", "minor") else "minor"
        rid = str(item.get("requirement_id") or "")
        if rid in inflated and kind != "inflated_is_core" and severity == "blocker":
            severity = "minor"  # exigência desconsiderada não reprova sozinha
        out.append({"kind": kind, "requirement_id": rid, "severity": severity,
                    "job_quote": str(item["job_quote"])[:300], "reason": str(item.get("reason") or "")[:300]})
    return out


def build_reason(verified: dict, decision: dict, objections: list[dict], approved: bool, language: str) -> str:
    pt = language == "pt"
    musts = [r for r in verified["requirements"] if r["id"] in decision["must_ids"]] or \
            [r for r in verified["responsibilities"] if r["id"] in decision["must_ids"]]
    ok = sum(1 for r in musts if r["status"] in ("met", "transferable"))
    transf = sum(1 for r in musts if r["status"] == "transferable")
    missing = [r["text"][:60] for r in musts if r["status"] in ("unmet", "unknown")][:4]
    parts = [
        (f"Obrigatórios {ok}/{len(musts)} atendidos" if pt else f"Required {ok}/{len(musts)} met")
        + (f" ({transf} transferível/is)" if pt and transf else f" ({transf} transferable)" if transf else "")
    ]
    if missing:
        parts.append(("faltam: " if pt else "missing: ") + "; ".join(missing))
    ignored = [r for r in verified["requirements"] if r["realism"] == "inflated"]
    if ignored:
        parts.append(("ignorados (inflados): " if pt else "disregarded (inflated): ") + "; ".join(
            f"{r['text'][:50]} — {r['realism_reason'][:70]}" for r in ignored[:3]))
    if decision["blocks"]:
        parts.append(("bloqueios: " if pt else "blockers: ") + "; ".join(decision["blocks"]))
    big = [o for o in objections if o["severity"] in ("blocker", "major")]
    if big:
        parts.append(("revisão cética: " if pt else "skeptic review: ") + "; ".join(o["reason"][:100] or o["job_quote"][:60] for o in big[:3]))
    parts.append(f"score {decision['score']}/100 · cobertura {decision['must_cov']:.0%} (mín. {decision['threshold']:.0%}) · "
                 + (("trilha " + decision["track"]) if pt else ("track " + decision["track"])))
    return ". ".join(parts)[:1000]


def assess_job(
    job: dict,
    cfg: dict,
    *,
    profile: dict,
    resume_norm: NormalizedText,
    call_json: Callable[..., dict],
    language: str,
    preferences: str,
    track: str = "core",
) -> dict:
    """Passo A (auditoria) → decisão determinística → passo B cético (só se aprovada)."""
    job_text = clean_job_html(job.get("description", ""))
    job_norm = NormalizedText.of(f"{job.get('title', '')}\n{job.get('company', '')}\n{job.get('location', '')}\n{job_text}")
    raw = call_json(
        build_assess_prompt(job, job_text, profile, cfg, language, preferences), ASSESS_SCHEMA,
        max_output_tokens=8192, thinking_budget=1024,
    )
    verified = _verify(raw, job_norm, resume_norm, profile)
    decision = decide(verified, profile, cfg, track=track, job=job, language=language)
    approved = _approved(decision)
    objections: list[dict] = []
    if approved:
        skeptic = call_json(
            build_skeptic_prompt(job, job_text, profile, verified), SKEPTIC_SCHEMA,
            max_output_tokens=4096, thinking_budget=1024,
        )
        objections = _verify_skeptic(skeptic, job_norm, verified)
        flipped = False
        for obj in objections:
            if obj["kind"] == "inflated_is_core":
                for req in verified["requirements"]:
                    if req["id"] == obj["requirement_id"] and req["realism"] == "inflated":
                        req["realism"], req["realism_reason"], flipped = "core", "", True
        if flipped:
            decision = decide(verified, profile, cfg, track=track, job=job, language=language)
        penalty = min(
            SCORING_V2["major_objection_cap"],
            SCORING_V2["major_objection_penalty"] * sum(1 for o in objections if o["severity"] == "major"),
        )
        if any(o["severity"] == "blocker" for o in objections):
            decision["blocks"].append("objeção bloqueante verificada na revisão cética")
        approved = _approved(decision, penalty)
    bridges = [
        f"{r['text'][:120]} ← {r['bridge']}" for r in verified["requirements"] + verified["responsibilities"]
        if r["status"] == "transferable" and r.get("bridge")
    ][:8]
    assessment = {
        "version": SCORING_VERSION, "track": decision["track"], "decision": decision,
        "requirements": verified["requirements"], "responsibilities": verified["responsibilities"],
        "level_label": verified["level_label"], "stated": verified["stated"], "role_family_fit": verified["role_family_fit"],
        "objections": objections, "dropped": verified["dropped"], "bridges": bridges,
        "inflated_posting": decision["mostly_inflated"],
    }
    return {
        "match_score": decision["score"], "should_apply": approved,
        "letter_required": verified["cover_letter_required"],
        "reason": build_reason(verified, decision, objections, approved, language),
        "assessment": assessment, "bridges": bridges, "track": decision["track"],
    }
