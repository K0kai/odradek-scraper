"""Perfil canônico do currículo: fatos com citação verbatim verificada pelo código.

O LLM extrai; o código (1) descarta qualquer fato cuja citação não exista no texto
do PDF, (2) calcula meses de experiência/uso por skill a partir de datas, e
(3) atribui IDs estáveis (R1, R1.1, S1, T1...) que a triagem cita como evidência.
Nada aqui chama IA ou banco, exceto merged_profile (leitura).
"""
from __future__ import annotations

import difflib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date

PROFILE_VERSION = "2"

_SKILL_CATEGORIES = ["language", "framework", "tool", "cloud", "db", "domain", "soft", "other"]

_FACT = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "quote": {"type": "string"}},
}

PROFILE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "professional_profile": {"type": "string"},
        "seniority": {"type": "string"},
        "roles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "employer": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "quote": {"type": "string"},
                    "responsibilities": {"type": "array", "items": _FACT},
                    "skills_used": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "skills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "canonical": {"type": "string"},
                    "category": {"type": "string", "enum": _SKILL_CATEGORIES},
                    "quotes": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "transferable_skills": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "capability": {"type": "string"},
                    "from_roles": {"type": "array", "items": {"type": "string"}},
                    "quotes": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "education": {"type": "array", "items": _FACT},
        "certifications": {"type": "array", "items": _FACT},
        "projects": {"type": "array", "items": _FACT},
        "achievements": {"type": "array", "items": _FACT},
        "languages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"language": {"type": "string"}, "level": {"type": "string"}, "quote": {"type": "string"}},
            },
        },
        "location_notes": {"type": "string"},
        "work_authorization_notes": {"type": "string"},
    },
}


def build_profile_prompt(text: str, language_name: str) -> str:
    return f"""You extract a structured, EVIDENCE-BACKED profile from a resume written primarily in {language_name}.

HARD RULES
- Every fact must carry a `quote`: a SHORT contiguous fragment (max ~200 characters) copied VERBATIM from the resume text below. Do not paraphrase inside quotes. Facts without a verbatim quote will be discarded by the program.
- Never invent employers, dates, skills, tools, metrics or claims. If unsure, omit the item.
- Include EVERY role (newest first), including non-IT roles such as administrative assistant. Do not drop any.
- roles[].start / roles[].end use "YYYY-MM" (or "YYYY" if only the year is known, written as "YYYY-01" for start and "YYYY-12" for end). Use "present" as end for an ongoing role. Use "" when the resume gives no date. Never guess dates.
- roles[].quote = the line(s) naming the title/employer/period. roles[].skills_used = canonical skill names used in that role, only if the resume ties them to it.
- skills[]: every technology, language, framework, tool, platform, database, methodology or domain explicitly named. `canonical` = lowercase, no version, no punctuation variants (e.g. "node.js" -> "nodejs", "Postgres" -> "postgresql"). `quotes` = where it appears.
- transferable_skills[]: abstract capabilities the candidate DEMONSTRATED in any role, including non-technical ones (e.g. user support, document/process control, spreadsheets and reporting, routine automation, communication with clients/suppliers, scheduling, financial/administrative routines). `from_roles` = titles of the roles where shown. Each needs a verbatim quote.
- languages[]: spoken/written languages with the stated level only if present.
- headline: one positioning sentence in {language_name}. professional_profile: 2-4 dense paragraphs in {language_name} covering the FULL career arc (all roles).
- seniority: a short free-text label of the candidate's realistic level, based on actual dated experience.
- Do NOT compute years of experience; the program does it from dates.

Resume text:
{text[:40000]}
"""


# --- normalização e verificação de citações -------------------------------------------------

def normalize_text(value: str) -> str:
    """casefold, sem acentos, apenas alfanuméricos separados por 1 espaço."""
    decomposed = unicodedata.normalize("NFKD", str(value or "").casefold())
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9+#]+", " ", stripped).strip()


@dataclass(frozen=True)
class NormalizedText:
    """Texto normalizado pronto para checagens repetidas de citação."""

    text: str
    tokens: tuple[str, ...]
    positions: dict

    @classmethod
    def of(cls, raw: str) -> "NormalizedText":
        norm = normalize_text(raw)
        tokens = tuple(norm.split())
        positions: dict[str, list[int]] = {}
        for index, token in enumerate(tokens):
            positions.setdefault(token, []).append(index)
        return cls(f" {norm} ", tokens, positions)


def quote_in_text(quote: str, source: NormalizedText, *, threshold: float = 0.9) -> bool:
    """Citação existe no texto: substring normalizada ou aproximação por janela de tokens."""
    norm = normalize_text(quote)
    if len(norm) < 3:
        return False
    if f" {norm} " in source.text:
        return True
    q_tokens = norm.split()
    if len(q_tokens) < 3:
        return False
    n = len(q_tokens)
    starts: set[int] = set()
    for token in q_tokens[:3]:
        for pos in source.positions.get(token, ()):  # janela pode começar até 2 tokens antes
            starts.update(range(max(0, pos - 2), pos + 1))
    best = 0.0
    for start in sorted(starts):
        window = list(source.tokens[start:start + n + 3])
        matcher = difflib.SequenceMatcher(None, q_tokens, window, autojunk=False)
        if matcher.real_quick_ratio() < threshold * 0.8:
            continue
        best = max(best, matcher.ratio())
        if best >= threshold:
            return True
    return False


def term_in_text(term: str, source: NormalizedText) -> bool:
    norm = normalize_text(term)
    return bool(norm) and f" {norm} " in source.text


# --- seções ----------------------------------------------------------------------------------

_SECTION_KINDS = {
    "experience": [
        "experiencia", "experiencia profissional", "experiencia de trabalho", "historico profissional",
        "work experience", "professional experience", "experience", "employment history", "work history",
        "career history", "atuacao profissional",
    ],
    "education": ["formacao", "formacao academica", "educacao", "escolaridade", "education", "academic background", "cursos", "courses"],
    "skills": [
        "habilidades", "competencias", "skills", "technical skills", "competencias tecnicas",
        "tecnologias", "conhecimentos", "hard skills", "soft skills", "ferramentas", "tools",
    ],
    "projects": ["projetos", "projects", "projetos relevantes", "portfolio"],
    "certifications": ["certificacoes", "certificados", "certifications", "licencas", "licenses"],
    "languages": ["idiomas", "languages", "linguas"],
    "summary": ["resumo", "resumo profissional", "summary", "professional summary", "perfil", "perfil profissional", "profile", "objetivo", "objective", "sobre mim", "about", "about me"],
    "contact": ["contato", "contact", "dados pessoais", "personal information"],
    "achievements": ["conquistas", "achievements", "realizacoes", "premios", "awards"],
    "other": ["informacoes adicionais", "additional information", "voluntariado", "volunteering", "publicacoes", "publications", "interesses", "interests", "referencias", "references"],
}
_HEADING_LOOKUP = {name: kind for kind, names in _SECTION_KINDS.items() for name in names}


@dataclass
class Section:
    title: str
    kind: str
    start: int
    end: int
    text: str


def _heading_kind(line: str) -> str | None:
    stripped = line.strip().strip(":-–—•*#|").strip()
    if not stripped or len(stripped) > 45 or len(stripped.split()) > 5:
        return None
    if stripped[-1] in ".,;":
        return None
    return _HEADING_LOOKUP.get(normalize_text(stripped))


def split_sections(text: str) -> list[Section]:
    """Divide o texto em seções por cabeçalhos conhecidos (PT/EN). Texto antes do primeiro vira 'header'."""
    sections: list[Section] = []
    current_title, current_kind, current_start = "", "header", 0
    cursor = 0
    for line in text.splitlines(keepends=True):
        kind = _heading_kind(line)
        if kind:
            body = text[current_start:cursor]
            if body.strip():
                sections.append(Section(current_title, current_kind, current_start, cursor, body))
            current_title, current_kind, current_start = line.strip().strip(":"), kind, cursor
        cursor += len(line)
    tail = text[current_start:]
    if tail.strip():
        sections.append(Section(current_title, current_kind, current_start, len(text), tail))
    return sections


# --- datas e derivados -----------------------------------------------------------------------

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})$")


def _clean_date(value: object, *, allow_present: bool) -> str:
    raw = str(value or "").strip().casefold()
    if allow_present and raw in {"present", "atual", "current", "now", "hoje"}:
        return "present"
    match = _DATE_RE.match(raw)
    if match and 1 <= int(match.group(2)) <= 12 and 1950 <= int(match.group(1)) <= 2100:
        return raw
    if re.fullmatch(r"\d{4}", raw) and 1950 <= int(raw) <= 2100:
        return f"{raw}-01" if not allow_present else f"{raw}-12"
    return ""


def _month_index(value: str, today: date) -> int | None:
    if value == "present":
        return today.year * 12 + today.month - 1
    match = _DATE_RE.match(value)
    return int(match.group(1)) * 12 + int(match.group(2)) - 1 if match else None


def _interval(role: dict, today: date) -> tuple[int, int] | None:
    start = _month_index(role.get("start", ""), today)
    end = _month_index(role.get("end", ""), today)
    if start is None or end is None or end < start:
        return None
    return start, end + 1  # fim exclusivo: jan..jan = 1 mês


def union_months(intervals: list[tuple[int, int]]) -> int:
    total, last_end = 0, None
    for start, end in sorted(intervals):
        if last_end is None or start > last_end:
            total += end - start
            last_end = end
        elif end > last_end:
            total += end - last_end
            last_end = end
    return total


def months_for_roles(profile: dict, role_ids: list[str], *, today: date | None = None) -> int:
    """Meses de experiência da união dos cargos indicados (sem dupla contagem)."""
    today = today or date.today()
    wanted = set(role_ids)
    intervals = [_interval(role, today) for role in profile.get("roles", []) if role["id"] in wanted]
    return union_months([iv for iv in intervals if iv])


def _index_to_ym(index: int) -> str:
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _compact(value: object) -> str:
    """Chave de comparação de nomes de skill sem espaços: 'Node.js' e 'nodejs' coincidem."""
    return normalize_text(str(value or "")).replace(" ", "")


def compute_derived(profile: dict, *, today: date | None = None) -> dict:
    today = today or date.today()
    intervals: dict[str, tuple[int, int]] = {}
    for role in profile.get("roles", []):
        interval = _interval(role, today)
        if interval:
            intervals[role["id"]] = interval
    role_norm: dict[str, NormalizedText] = {}
    for role in profile.get("roles", []):
        blob = " ".join(
            [" ".join(role.get("skills_used", []))]
            + [f"{r.get('text', '')} {r.get('quote', '')}" for r in role.get("responsibilities", [])]
        )
        role_norm[role["id"]] = NormalizedText.of(blob)
    skills: dict[str, dict] = {}
    # Projetos pessoais (portfólio) dão evidência de uso da skill, mas ficam fora de months/total_months.
    project_intervals = {}
    for project in profile.get("projects", []):
        interval = _interval(project, today) if project.get("start") else None
        if interval:
            project_intervals[project["id"]] = interval
    project_stacks = {
        p["id"]: {_compact(s) for s in p.get("stack", [])}
        for p in profile.get("projects", []) if p["id"] in project_intervals
    }
    for skill in profile.get("skills", []):
        canonical = skill.get("canonical") or normalize_text(skill.get("name", ""))
        used = [
            role["id"] for role in profile.get("roles", [])
            if role["id"] in intervals and (
                canonical in {normalize_text(s) for s in role.get("skills_used", [])}
                or term_in_text(canonical, role_norm[role["id"]])
                or term_in_text(skill.get("name", ""), role_norm[role["id"]])
            )
        ]
        months = union_months([intervals[i] for i in used])
        last = max((intervals[i][1] - 1 for i in used), default=None)
        keys = {_compact(canonical), _compact(skill.get("name", ""))} - {""}
        project_used = [pid for pid, stack in project_stacks.items() if keys & stack]
        project_last = max((project_intervals[i][1] - 1 for i in project_used), default=None)
        skills[skill["id"]] = {
            "canonical": canonical,
            "months": months,
            "last_used": _index_to_ym(last) if last is not None else "",
            "role_ids": used,
            "project_months": union_months([project_intervals[i] for i in project_used]),
            "project_ids": project_used,
            "project_last_used": _index_to_ym(project_last) if project_last is not None else "",
        }
    return {
        "total_months": union_months(list(intervals.values())),
        "skills": skills,
        "role_months": {rid: end - start for rid, (start, end) in intervals.items()},
        "current_role_ids": [r["id"] for r in profile.get("roles", []) if r.get("end") == "present"],
    }


# --- ancoragem -------------------------------------------------------------------------------

def _str(value: object, limit: int = 600) -> str:
    return " ".join(str(value or "").split())[:limit]


def ground_profile(raw: dict, source_text: str, *, today: date | None = None) -> dict:
    """Valida o perfil extraído pelo LLM contra o texto do PDF e atribui IDs estáveis.

    Fatos sem citação encontrada são descartados e contados em coverage.
    """
    source = NormalizedText.of(source_text)
    dropped: dict[str, int] = {}
    kept = 0

    def grounded(quote: object) -> bool:
        return isinstance(quote, str) and quote_in_text(quote, source)

    def drop(kind: str) -> None:
        dropped[kind] = dropped.get(kind, 0) + 1

    profile: dict = {
        "version": PROFILE_VERSION,
        "headline": _str(raw.get("headline"), 400),
        "professional_profile": _str(raw.get("professional_profile"), 4000),
        "seniority": _str(raw.get("seniority"), 120),
        "location_notes": _str(raw.get("location_notes"), 600),
        "work_authorization_notes": _str(raw.get("work_authorization_notes"), 600),
        "roles": [], "skills": [], "transferable_skills": [], "education": [],
        "certifications": [], "projects": [], "achievements": [], "languages": [],
    }
    role_ids_by_title: dict[str, str] = {}
    for role in raw.get("roles") or []:
        if not isinstance(role, dict) or not (_str(role.get("title")) or _str(role.get("employer"))):
            continue
        if not grounded(role.get("quote")) and not grounded(f"{role.get('title', '')} {role.get('employer', '')}"):
            drop("roles")
            continue
        rid = f"R{len(profile['roles']) + 1}"
        responsibilities = []
        for item in role.get("responsibilities") or []:
            if isinstance(item, dict) and _str(item.get("text")) and grounded(item.get("quote")):
                responsibilities.append({
                    "id": f"{rid}.{len(responsibilities) + 1}",
                    "text": _str(item["text"], 400), "quote": _str(item["quote"], 400),
                })
            else:
                drop("responsibilities")
        entry = {
            "id": rid, "title": _str(role.get("title"), 160), "employer": _str(role.get("employer"), 160),
            "start": _clean_date(role.get("start"), allow_present=False),
            "end": _clean_date(role.get("end"), allow_present=True),
            "quote": _str(role.get("quote"), 400),
            "responsibilities": responsibilities,
            "skills_used": [_str(s, 80) for s in (role.get("skills_used") or []) if _str(s)][:40],
        }
        profile["roles"].append(entry)
        role_ids_by_title[normalize_text(entry["title"])] = rid
        kept += 1

    for skill in raw.get("skills") or []:
        if not isinstance(skill, dict) or not _str(skill.get("name")):
            continue
        quotes = [_str(q, 300) for q in (skill.get("quotes") or []) if grounded(q)]
        if not quotes and not term_in_text(skill["name"], source):
            drop("skills")
            continue
        category = skill.get("category") if skill.get("category") in _SKILL_CATEGORIES else "other"
        canonical = normalize_text(skill.get("canonical") or skill["name"])
        if any(s["canonical"] == canonical for s in profile["skills"]):
            continue
        profile["skills"].append({
            "id": f"S{len(profile['skills']) + 1}", "name": _str(skill["name"], 80),
            "canonical": canonical, "category": category, "quotes": quotes[:3],
        })
        kept += 1

    for item in raw.get("transferable_skills") or []:
        if not isinstance(item, dict) or not _str(item.get("capability")):
            continue
        quotes = [_str(q, 300) for q in (item.get("quotes") or []) if grounded(q)]
        if not quotes:
            drop("transferable_skills")
            continue
        from_roles = [role_ids_by_title[k] for t in (item.get("from_roles") or [])
                      if (k := normalize_text(str(t))) in role_ids_by_title]
        profile["transferable_skills"].append({
            "id": f"T{len(profile['transferable_skills']) + 1}", "capability": _str(item["capability"], 200),
            "from_role_ids": from_roles, "quotes": quotes[:3],
        })
        kept += 1

    for key, prefix in (("education", "E"), ("certifications", "C"), ("projects", "P"), ("achievements", "A")):
        for item in raw.get(key) or []:
            if isinstance(item, dict) and _str(item.get("text")) and grounded(item.get("quote")):
                profile[key].append({
                    "id": f"{prefix}{len(profile[key]) + 1}", "text": _str(item["text"], 400),
                    "quote": _str(item["quote"], 400),
                })
                kept += 1
            else:
                drop(key)
    for item in raw.get("languages") or []:
        if isinstance(item, dict) and _str(item.get("language")) and grounded(item.get("quote")):
            profile["languages"].append({
                "id": f"L{len(profile['languages']) + 1}", "language": _str(item["language"], 60),
                "level": _str(item.get("level"), 60), "quote": _str(item["quote"], 300),
            })
            kept += 1
        else:
            drop("languages")

    profile["derived"] = compute_derived(profile, today=today)
    profile["coverage"] = {"kept": kept, "dropped": dropped, "ungrounded": sum(dropped.values())}
    return profile


# --- fusão PT/EN e renderizações -------------------------------------------------------------

def _role_key(role: dict) -> str:
    return f"{normalize_text(role.get('employer', '')) or normalize_text(role.get('title', ''))}|{role.get('start', '')}"


def _item_marker(item: dict) -> str:
    return _compact(item.get("canonical") or item.get("text") or item.get("language") or item.get("capability", ""))


def _find_role(roles: list[dict], role: dict) -> dict | None:
    """Mesmo cargo vindo de outra fonte: mesmo empregador+início, ou mesmo início com empregador/título contido."""
    for existing in roles:
        if _role_key(existing) == _role_key(role):
            return existing
    start = role.get("start", "")
    employer, title = normalize_text(role.get("employer", "")), normalize_text(role.get("title", ""))
    if not start:
        return None
    for existing in roles:
        if existing.get("start") != start:
            continue
        other_employer = normalize_text(existing.get("employer", ""))
        if employer and other_employer and (employer in other_employer or other_employer in employer):
            return existing
        if title and title == normalize_text(existing.get("title", "")):
            return existing
    return None


def _enrich_project(projects: list[dict], incoming: dict) -> bool:
    """Projeto datado do portfólio que já aparece no texto de um projeto do PDF: enriquece o existente."""
    title = normalize_text(incoming.get("title", ""))
    if len(title) < 4:
        return False
    for existing in projects:
        if existing.get("title") == incoming.get("title") and existing.get("start"):
            return True  # já fundido
        if f" {title} " in f" {normalize_text(existing.get('text', ''))} ":
            for key in ("title", "slug", "headline", "start", "end", "stack", "source", "url"):
                if incoming.get(key):
                    existing[key] = json.loads(json.dumps(incoming[key]))
            return True
    return False


def merge_profiles(profiles: list[dict], *, today: date | None = None) -> dict | None:
    """Une perfis (PT/EN) sem duplicar: o primeiro da lista é o preferencial."""
    profiles = [p for p in profiles if p and p.get("roles") is not None]
    if not profiles:
        return None
    if len(profiles) == 1:
        return profiles[0]
    base = json.loads(json.dumps(profiles[0]))
    for other in profiles[1:]:
        for role in other["roles"]:
            existing = _find_role(base["roles"], role)
            if existing is None:
                base["roles"].append(json.loads(json.dumps(role)))
                continue
            known = {normalize_text(r["text"]) for r in existing["responsibilities"]}
            for resp in role["responsibilities"]:
                if normalize_text(resp["text"]) not in known:
                    existing["responsibilities"].append(dict(resp))
            for skill in role["skills_used"]:
                if normalize_text(skill) not in {normalize_text(s) for s in existing["skills_used"]}:
                    existing["skills_used"].append(skill)
        for key in ("skills", "education", "certifications", "projects", "achievements", "languages", "transferable_skills"):
            known_items = {_item_marker(i) for i in base[key]}
            for item in other.get(key, []):
                marker = _item_marker(item)
                if key == "projects" and _enrich_project(base[key], item):
                    continue
                if marker and marker not in known_items:
                    base[key].append(json.loads(json.dumps(item)))
                    known_items.add(marker)
        for key in ("headline", "professional_profile", "seniority", "location_notes", "work_authorization_notes"):
            base[key] = base.get(key) or other.get(key, "")
    # IDs estáveis reatribuídos sobre o conjunto fundido
    for index, role in enumerate(base["roles"], start=1):
        role["id"] = f"R{index}"
        for sub, resp in enumerate(role["responsibilities"], start=1):
            resp["id"] = f"R{index}.{sub}"
    for key, prefix in (("skills", "S"), ("transferable_skills", "T"), ("education", "E"),
                        ("certifications", "C"), ("projects", "P"), ("achievements", "A"), ("languages", "L")):
        for index, item in enumerate(base[key], start=1):
            item["id"] = f"{prefix}{index}"
    for item in base["transferable_skills"]:
        item["from_role_ids"] = []  # IDs de origem não sobrevivem à fusão
    base["derived"] = compute_derived(base, today=today)
    base["coverage"] = {
        "kept": sum(p.get("coverage", {}).get("kept", 0) for p in profiles),
        "dropped": {}, "ungrounded": sum(p.get("coverage", {}).get("ungrounded", 0) for p in profiles),
    }
    return base


def format_months(months: int) -> str:
    years, rest = divmod(max(0, months), 12)
    if years and rest:
        return f"{years}y{rest}m"
    return f"{years}y" if years else f"{rest}m"


def compact_profile_text(profile: dict, *, max_chars: int = 6000) -> str:
    """Resumo com IDs para a triagem. Ordem: papéis → skills → transferíveis → resto."""
    derived = profile.get("derived", {})
    skill_stats = derived.get("skills", {})
    lines = [f"TOTAL PROFESSIONAL EXPERIENCE (computed from dated roles): {format_months(derived.get('total_months', 0))}"]
    if profile.get("seniority"):
        lines.append(f"SELF-DESCRIBED LEVEL: {profile['seniority']}")
    # Idiomas, local e autorização são curtos e decisivos: ficam no topo, longe do corte por max_chars.
    if profile.get("languages"):
        lines.append("LANGUAGES: " + "; ".join(f"[{i['id']}] {i['language']} {i['level']}".strip() for i in profile["languages"]))
    if profile.get("location_notes"):
        lines.append(f"LOCATION: {profile['location_notes']}")
    if profile.get("work_authorization_notes"):
        lines.append(f"WORK AUTHORIZATION: {profile['work_authorization_notes']}")
    lines.append("ROLES")
    for role in profile.get("roles", []):
        months = derived.get("role_months", {}).get(role["id"])
        span = f"{role.get('start') or '?'} → {role.get('end') or '?'}" + (f", {format_months(months)}" if months else "")
        lines.append(f"[{role['id']}] {role.get('title', '')} — {role.get('employer', '')} ({span})")
        for resp in role.get("responsibilities", [])[:8]:
            lines.append(f"  [{resp['id']}] {resp['text'][:220]}")
    lines.append("SKILLS (id name: months used, last used)")
    undated: list[str] = []
    for skill in profile.get("skills", []):
        stat = skill_stats.get(skill["id"], {})
        if not stat.get("months") and not stat.get("project_months"):
            undated.append(f"[{skill['id']}] {skill['name']}")  # sem datas: uma linha só, para não gastar o prompt
            continue
        usage = f"{format_months(stat['months'])}, last {stat['last_used']}" if stat.get("months") else "no dated professional usage"
        if stat.get("project_months"):
            ids = ",".join(stat.get("project_ids", []))
            usage += f"; portfolio projects {format_months(stat['project_months'])} ({ids}), last {stat['project_last_used']}"
        lines.append(f"[{skill['id']}] {skill['name']}: {usage}")
    if undated:
        lines.append("LISTED SKILLS, NO DATED USAGE: " + ", ".join(undated))
    if profile.get("transferable_skills"):
        lines.append("TRANSFERABLE CAPABILITIES")
        for item in profile["transferable_skills"]:
            lines.append(f"[{item['id']}] {item['capability']}")
    for key, label in (("education", "EDUCATION"), ("certifications", "CERTIFICATIONS"), ("projects", "PROJECTS"), ("achievements", "ACHIEVEMENTS")):
        if profile.get(key):
            lines.append(label)
            for item in profile[key]:
                if key == "projects" and item.get("start"):
                    span = f"{item['start']} → {item.get('end') or '?'}"
                    stack = ", ".join(item.get("stack", [])[:8])
                    lines.append(f"[{item['id']}] {item.get('title') or 'Project'} ({span}; {stack}) — {(item.get('headline') or item['text'])[:200]}")
                else:
                    lines.append(f"[{item['id']}] {item['text'][:200]}")
    return "\n".join(lines)[:max_chars]


def evidence_index(profile: dict) -> dict[str, dict]:
    """id → {kind, text, quotes} para verificar evidence_ids citados pela triagem."""
    index: dict[str, dict] = {}
    for role in profile.get("roles", []):
        index[role["id"]] = {"kind": "role", "text": f"{role.get('title', '')} {role.get('employer', '')}", "quotes": [role.get("quote", "")]}
        for resp in role.get("responsibilities", []):
            index[resp["id"]] = {"kind": "responsibility", "text": resp["text"], "quotes": [resp["quote"]]}
    for skill in profile.get("skills", []):
        index[skill["id"]] = {"kind": "skill", "text": skill["name"], "quotes": skill.get("quotes", [])}
    for item in profile.get("transferable_skills", []):
        index[item["id"]] = {"kind": "transferable", "text": item["capability"], "quotes": item.get("quotes", [])}
    for key in ("education", "certifications", "projects", "achievements"):
        for item in profile.get(key, []):
            index[item["id"]] = {"kind": key, "text": item["text"], "quotes": [item["quote"]]}
    for section in ("roles", "skills", "projects", "education"):
        for item in profile.get(section, []):
            if item.get("source") and item["id"] in index:
                index[item["id"]].update(source=item["source"], url=item.get("url", ""))
    for item in profile.get("languages", []):
        index[item["id"]] = {"kind": "language", "text": f"{item['language']} {item['level']}", "quotes": [item["quote"]]}
    return index


_LEGACY_CATEGORY = {"tool": "tools", "cloud": "tools", "soft": "soft_skills", "domain": "domains"}


def legacy_analysis(profile: dict, language: str) -> dict:
    """Formato antigo de analysis_json (painel, assistente, regras de formulário)."""
    out: dict = {
        "headline": profile.get("headline", ""),
        "professional_profile": profile.get("professional_profile", ""),
        "seniority": profile.get("seniority", ""),
        "technical_skills": [], "tools": [], "soft_skills": [], "domains": [],
        "experience": [], "projects": [p["text"] for p in profile.get("projects", [])],
        "achievements": [a["text"] for a in profile.get("achievements", [])],
        "education": [e["text"] for e in profile.get("education", [])],
        "certifications": [c["text"] for c in profile.get("certifications", [])],
        "languages": [f"{i['language']} {i['level']}".strip() for i in profile.get("languages", [])],
        "work_authorization_notes": profile.get("work_authorization_notes", ""),
        "location_notes": profile.get("location_notes", ""),
    }
    for skill in profile.get("skills", []):
        out[_LEGACY_CATEGORY.get(skill["category"], "technical_skills")].append(skill["name"])
    total = profile.get("derived", {}).get("total_months", 0)
    if total:
        years = total / 12
        out["years_of_experience"] = (f"{years:.1f} anos" if language == "pt" else f"{years:.1f} years") + " (calculado das datas)"
    else:
        out["years_of_experience"] = ""
    for role in profile.get("roles", []):
        period = f"{role.get('start') or '?'} – {role.get('end') or '?'}"
        out["experience"].append({
            "role": role.get("title", ""), "employer": role.get("employer", ""), "period": period,
            "is_current": role.get("end") == "present",
            "responsibilities": [r["text"] for r in role.get("responsibilities", [])], "highlights": [],
        })
    current = next((r for r in profile.get("roles", []) if r.get("end") == "present"), None)
    if current:
        out["current_employer"] = current.get("employer", "")
    cov = profile.get("coverage", {})
    out["coverage_notes"] = (
        f"{cov.get('kept', 0)} fatos verificados no texto do PDF; {cov.get('ungrounded', 0)} descartados por falta de citação."
        if language == "pt" else
        f"{cov.get('kept', 0)} facts verified against the PDF text; {cov.get('ungrounded', 0)} discarded for lacking a quote."
    )
    return out


def parse_profile(raw: str | None) -> dict | None:
    try:
        data = json.loads(raw or "")
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) and data.get("version") == PROFILE_VERSION else None


def merged_profile(db, *, prefer: str = "pt") -> tuple[dict, NormalizedText] | None:
    """Perfil fundido PT+EN e o texto normalizado (de ambos) para checar citações."""
    from candidate.portfolio_source import load_portfolio  # import tardio: portfolio_source importa este módulo

    rows = db.execute("SELECT language, profile_json, extracted_text FROM resumes").fetchall()
    ordered = sorted(rows, key=lambda r: 0 if r["language"] == prefer else 1)
    profiles, texts = [], []
    for row in ordered:
        profile = parse_profile(row["profile_json"])
        if profile:
            profiles.append(profile)
            texts.append(row["extracted_text"] or "")
    portfolio = load_portfolio(db, prefer)  # por último: o PDF tem precedência na fusão
    if portfolio:
        profiles.append(portfolio[0])
        texts.append(portfolio[1])
    merged = merge_profiles(profiles)
    if not merged:
        return None
    if "derived" not in merged:  # só portfólio: nada passou por ground_profile
        merged["derived"] = compute_derived(merged)
        merged["coverage"] = {"kept": 0, "dropped": {}, "ungrounded": 0}
    return merged, NormalizedText.of("\n".join(texts))


# --- trechos do currículo para o Qpid ----------------------------------------------------------

_BULLET_START = ("-", "•", "*", "–", "·", "▪")
_SECTION_LABELS = {"pt": {"header": "Cabeçalho"}, "en": {"header": "Header"}}


def _pack_lines(lines: list[str], max_chars: int) -> list[str]:
    """Agrupa linhas inteiras até max_chars; só quebra por palavras uma linha maior que o limite."""
    pieces: list[str] = []
    current = ""
    for line in lines:
        while len(line) > max_chars:
            cut = line.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:cut].strip())
            line = line[cut:].strip()
        if current and len(current) + 1 + len(line) > max_chars:
            pieces.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    if current.strip():
        pieces.append(current)
    return [p.strip() for p in pieces if p.strip()]


def resume_excerpts(text: str, profile: dict | None = None, *, max_chars: int = 900, language: str = "pt") -> list[dict]:
    """Trechos do TEXTO ORIGINAL do PDF, por seção e por cargo, sem cruzar fronteiras de seção.

    O perfil só ajuda a achar onde cada cargo começa; o conteúdo e o rótulo vêm do próprio texto.
    """
    role_markers = [
        (normalize_text(r.get("title", "")), normalize_text(r.get("employer", "")))
        for r in (profile or {}).get("roles", []) if len(normalize_text(r.get("title", ""))) >= 4
    ]

    def starts_role(line: str) -> bool:
        stripped = line.strip()
        if not stripped or stripped.startswith(_BULLET_START) or len(stripped) > 140:
            return False
        norm = normalize_text(stripped)
        return any(title in norm for title, _employer in role_markers)

    out: list[dict] = []
    for section in split_sections(text):
        lines = [ln.strip() for ln in section.text.splitlines()]
        heading = lines[0] if section.kind != "header" and lines else ""
        body = lines[1:] if heading else lines
        blocks: list[list[str]] = []
        current: list[str] = []
        for line in body:
            if section.kind == "experience" and role_markers and current and starts_role(line):
                blocks.append(current)
                current = []
            if line or current:
                current.append(line)
        if current:
            blocks.append(current)
        title = section.title or _SECTION_LABELS.get(language, _SECTION_LABELS["pt"]).get(section.kind, section.kind)
        for number, block in enumerate(blocks or [[]]):
            content = ([heading] if heading and number == 0 else []) + [ln for ln in block if ln]
            if not content:
                continue
            first = next((ln for ln in block if ln), "")
            pieces = _pack_lines(content, max_chars)
            for part, piece in enumerate(pieces, start=1):
                label = f"{title} — {first[:80]}" if first else title
                if len(pieces) > 1:
                    label += f" ({part}/{len(pieces)})"
                out.append({"section": section.kind, "label": label, "text": piece})
    return out
