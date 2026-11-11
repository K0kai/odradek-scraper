# Upload, extração e análise única de currículos PDF.
from __future__ import annotations

from io import BytesIO
from candidate.resume_storage import save_pdf, read_pdf
import hashlib
import json
import os
import re
import sqlite3
from typing import Callable


class AiUnavailableError(RuntimeError):
    """Chave ausente, quota esgotada ou falha de autenticação/billing da IA."""


class AiResponseError(AiUnavailableError):
    """O modelo respondeu, mas com JSON inválido/truncado mesmo após retry.

    Subclasse de AiUnavailableError: a fila tenta de novo em vez de bloquear a vaga.
    """


def _clean_pdf_pages(pages: list[str]) -> str:
    """Preserva linhas/seções; remove cabeçalhos/rodapés repetidos e junta hifenização."""
    page_lines = [
        [re.sub(r" {2,}", " ", line.replace("\t", " ").replace(" ", " ")).strip() for line in (page or "").splitlines()]
        for page in pages
    ]
    repeated: set[str] = set()
    if len(page_lines) >= 3:
        counts: dict[str, int] = {}
        for lines in page_lines:
            for line in {ln.casefold() for ln in lines if ln and len(ln) < 80}:
                counts[line] = counts.get(line, 0) + 1
        repeated = {line for line, n in counts.items() if n >= max(3, int(len(page_lines) * 0.6 + 0.999))}
    merged: list[str] = []
    for lines in page_lines:
        for line in lines:
            if line and line.casefold() in repeated:
                continue
            if (
                line and merged and re.search(r"[A-Za-zÀ-ÿ]-$", merged[-1]) and re.match(r"[a-zà-ÿ]", line)
            ):
                merged[-1] = merged[-1][:-1] + line
                continue
            if not line and (not merged or not merged[-1]):
                continue
            merged.append(line)
        if merged and merged[-1]:
            merged.append("")
    return "\n".join(merged).strip()


def extract_pdf_text(path: str | BytesIO) -> str:
    from pypdf import PdfReader

    reader = PdfReader(path)
    text = _clean_pdf_pages([page.extract_text() or "" for page in reader.pages])
    if len(re.sub(r"\s+", " ", text)) < 40:
        raise ValueError("Não foi possível extrair texto útil deste PDF. Use um PDF com texto selecionável.")
    return text[:50000]


def get_resume(db: sqlite3.Connection, language: str) -> sqlite3.Row | None:
    return db.execute("SELECT * FROM resumes WHERE language=?", (language,)).fetchone()


def resume_summaries(db: sqlite3.Connection) -> dict[str, str]:
    rows = db.execute("SELECT language, analysis_summary FROM resumes").fetchall()
    return {row["language"]: row["analysis_summary"] or "" for row in rows}


def _experience_is_current(item: object) -> bool:
    """Detecta papel atual a partir de período/texto (Atual, Present, Current…)."""
    if isinstance(item, dict):
        bits = [
            str(item.get("period") or ""),
            str(item.get("role") or ""),
            str(item.get("employer") or ""),
            str(item.get("is_current") or ""),
        ]
        hay = " ".join(bits)
    else:
        hay = str(item or "")
    return bool(
        re.search(
            r"\batual\b|\bpresent\b|\bcurrent\b|\bhoje\b|\buntil\s+now\b|"
            r"\bto\s+present\b|\baté\s+o\s+momento\b|is_current\s*[:=]\s*true",
            hay,
            re.I,
        )
    )


def _format_experience_item(item: object) -> str:
    if isinstance(item, dict):
        role = str(item.get("role") or "").strip()
        employer = str(item.get("employer") or "").strip()
        period = str(item.get("period") or "").strip()
        head = " — ".join(p for p in (role, employer) if p) or str(item)[:220]
        if period:
            head = f"{head} ({period})"
        if _experience_is_current(item):
            head = f"[CURRENT ROLE] {head}"
        parts = [head]
        for key in ("responsibilities", "highlights"):
            val = item.get(key)
            if isinstance(val, list):
                for line in val:
                    text = str(line).strip()
                    if text:
                        parts.append(f"  - {text[:300]}")
            elif str(val or "").strip():
                parts.append(f"  - {str(val).strip()[:300]}")
        return "\n".join(parts)
    text = str(item).strip()
    if _experience_is_current(text):
        return f"[CURRENT ROLE] {text}"
    return text


_DOSSIER_LABELS = {
    "pt": {
        "headline": "Headline", "profile": "Perfil profissional", "level": "Senioridade",
        "years": "Anos de experiência", "tech": "Competências técnicas", "tools": "Ferramentas e plataformas",
        "soft": "Soft skills", "experience": "Experiência profissional (todas as funções)",
        "projects": "Projetos e automações relevantes", "achievements": "Conquistas / resultados mensuráveis",
        "education": "Formação", "certs": "Certificações", "languages": "Idiomas", "domains": "Domínios / indústrias",
        "auth": "Notas de autorização de trabalho", "location": "Localidade / mobilidade", "coverage": "Cobertura da extração",
    },
    "en": {
        "headline": "Headline", "profile": "Professional profile", "level": "Seniority",
        "years": "Years of experience", "tech": "Technical skills", "tools": "Tools and platforms",
        "soft": "Soft skills", "experience": "Professional experience (all roles)",
        "projects": "Relevant projects and automations", "achievements": "Achievements / measurable results",
        "education": "Education", "certs": "Certifications", "languages": "Languages", "domains": "Domains / industries",
        "auth": "Work authorization notes", "location": "Location / mobility", "coverage": "Extraction coverage",
    },
}


def compose_analysis_dossier(analysis: dict, language: str) -> str:
    """Dossiê estruturado (rótulos no idioma do currículo). Experiência vem antes das listas de skills,
    para que cortes por tamanho percam skills repetidas e não o histórico profissional."""
    label = _DOSSIER_LABELS["pt" if language == "pt" else "en"]
    language_name = "Português" if language == "pt" else "English"
    lines: list[str] = []
    headline = str(analysis.get("headline") or analysis.get("summary") or "").strip()
    if headline:
        lines.append(f"{label['headline']}: {headline}")
    profile = str(analysis.get("professional_profile") or "").strip()
    if profile:
        lines.append(f"{label['profile']}:\n{profile}")
    seniority = str(analysis.get("seniority") or "").strip()
    years = str(analysis.get("years_of_experience") or "").strip()
    if seniority or years:
        lines.append(f"{label['level']}: {seniority or '—'} | {label['years']}: {years or '—'}")

    def _section(title: str, value: object) -> None:
        if isinstance(value, list):
            items = [str(v).strip() for v in value if str(v).strip()]
            if items:
                lines.append(title + ":\n- " + "\n- ".join(items))
        else:
            text_v = str(value or "").strip()
            if text_v:
                lines.append(f"{title}:\n{text_v}")

    exp_items = analysis.get("experience") if isinstance(analysis.get("experience"), list) else []
    if exp_items:
        formatted = [_format_experience_item(item) for item in exp_items]
        formatted = [f for f in formatted if f.strip()]
        if formatted:
            lines.append(f"{label['experience']}:\n- " + "\n- ".join(formatted))
    _section(label["tech"], analysis.get("technical_skills") or analysis.get("skills"))
    _section(label["tools"], analysis.get("tools"))
    _section(label["soft"], analysis.get("soft_skills"))
    _section(label["projects"], analysis.get("projects"))
    _section(label["achievements"], analysis.get("achievements"))
    _section(label["education"], analysis.get("education"))
    _section(label["certs"], analysis.get("certifications"))
    _section(label["languages"], analysis.get("languages"))
    _section(label["domains"], analysis.get("domains"))
    _section(label["auth"], analysis.get("work_authorization_notes"))
    _section(label["location"], analysis.get("location_notes"))
    coverage = str(analysis.get("coverage_notes") or "").strip()
    if coverage:
        lines.append(f"{label['coverage']}:\n{coverage}")
    dossier = "\n\n".join(lines).strip()
    if not dossier:
        dossier = headline or f"Análise sem conteúdo estruturado ({language_name})."
    return dossier[:20000]


def analyze_resume_text(
    text: str,
    language: str,
    *,
    provider: str,
    model: str,
    api_key: str,
    call_json: Callable[..., dict] | None = None,
) -> dict:
    """Extrai o perfil canônico ancorado e devolve o formato legado de analysis_json.

    A chave "_profile" carrega o perfil verificado (persistido em resumes.profile_json).
    """
    from candidate.resume_profile import (
        PROFILE_SCHEMA, build_profile_prompt, ground_profile, legacy_analysis,
    )

    language_name = "Portuguese" if language == "pt" else "English"
    prompt = build_profile_prompt(text, language_name)
    if call_json is not None:
        raw = call_json(provider, model, api_key, prompt)
    else:
        if not api_key:
            raise AiUnavailableError("Configure a chave da API de IA antes de analisar o currículo.")
        from integrations.ai_client import call_ai_json

        raw = call_ai_json(
            prompt=prompt, schema=PROFILE_SCHEMA, provider=provider.casefold().strip(), model=model,
            api_key=api_key, max_output_tokens=12000, thinking_budget=2048, timeout=180,
        )
    if not isinstance(raw, dict):
        raise RuntimeError("A análise do currículo não retornou JSON objeto.")
    profile = ground_profile(raw, text)
    if not profile["roles"] and not profile["skills"]:
        raise RuntimeError("A análise não retornou fatos verificáveis no texto do PDF.")
    analysis = legacy_analysis(profile, language)
    analysis["summary"] = compose_analysis_dossier(analysis, language)
    if not str(analysis.get("headline") or "").strip():
        analysis["headline"] = analysis["summary"].split("\n", 1)[0][:240]
    analysis["_profile"] = profile
    return analysis


def _upsert_resume(
    db: sqlite3.Connection,
    *,
    language: str,
    original_filename: str,
    stored_path: str,
    digest: str,
    extracted_text: str,
    analysis_json: str,
    analysis_summary: str,
    analyzed_at: str | None,
    provider: str,
    model: str,
    analysis_status: str,
    analysis_error: str,
    analysis_message: str,
    now_iso: str,
    profile_json: str | None = None,
    profile_version: str | None = None,
) -> None:
    # None = preserva o perfil já gravado (upload pendente de nova análise).
    existing_row = get_resume(db, language)
    if profile_json is None:
        profile_json = (existing_row["profile_json"] if existing_row else "") or ""
    if profile_version is None:
        profile_version = (existing_row["profile_version"] if existing_row else "") or ""
    db.execute(
        """INSERT INTO resumes(
             language,original_filename,stored_path,file_sha256,extracted_text,analysis_json,analysis_summary,
             analyzed_at,provider,model,analysis_status,analysis_error,analysis_message,created_at,updated_at,
             profile_json,profile_version)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(language) DO UPDATE SET
             original_filename=excluded.original_filename,
             stored_path=excluded.stored_path,
             file_sha256=excluded.file_sha256,
             extracted_text=excluded.extracted_text,
             analysis_json=excluded.analysis_json,
             analysis_summary=excluded.analysis_summary,
             analyzed_at=excluded.analyzed_at,
             provider=excluded.provider,
             model=excluded.model,
             analysis_status=excluded.analysis_status,
             analysis_error=excluded.analysis_error,
             analysis_message=excluded.analysis_message,
             profile_json=excluded.profile_json,
             profile_version=excluded.profile_version,
             updated_at=excluded.updated_at
        """,
        (
            language,
            original_filename,
            stored_path,
            digest,
            extracted_text,
            analysis_json,
            analysis_summary,
            analyzed_at,
            provider,
            model,
            analysis_status,
            analysis_error[:1000],
            analysis_message[:500],
            now_iso,
            now_iso,
            profile_json,
            profile_version,
        ),
    )


def persist_resume_upload(
    db: sqlite3.Connection,
    *,
    language: str,
    original_filename: str,
    raw_bytes: bytes,
    resumes_dir: str,
    now_iso: str,
    force_reanalyze: bool = False,
) -> tuple[str, str, bool]:
    """Salva o PDF no banco e extrai texto. Não chama a IA.

    Retorna (mensagem, notice_kind, needs_analysis).
    """
    if language not in {"pt", "en"}:
        raise ValueError("Idioma do currículo deve ser pt ou en.")
    if not raw_bytes.startswith(b"%PDF"):
        raise ValueError("Envie um arquivo PDF válido.")
    if len(raw_bytes) > 12 * 1024 * 1024:
        raise ValueError("PDF maior que 12 MB.")

    stored_path = os.path.join(resumes_dir, f"resume_{language}.pdf")
    digest = hashlib.sha256(raw_bytes).hexdigest()
    existing = get_resume(db, language)
    same_analyzed = (
        existing
        and existing["file_sha256"] == digest
        and (existing["analysis_summary"] or "").strip()
        and not force_reanalyze
    )
    if same_analyzed:
        message = (
            f"Currículo {language.upper()}: PDF idêntico ao já analisado. "
            "Análise reutilizada. Use «Reanalisar currículo» ou marque «Forçar nova análise»."
        )
        db.execute(
            """UPDATE resumes SET original_filename=?, stored_path=?, analysis_status='reused',
                   analysis_error='', analysis_message=?, updated_at=? WHERE language=?""",
            (original_filename, stored_path, message, now_iso, language),
        )
        save_pdf(db, get_resume(db, language), raw_bytes)
        return message, "info", False

    extracted = extract_pdf_text(BytesIO(raw_bytes))
    if force_reanalyze and existing and existing["file_sha256"] == digest:
        message = (
            f"Currículo {language.upper()}: mesmo PDF, mas reanálise forçada foi enfileirada."
        )
    else:
        message = (
            f"Currículo {language.upper()}: PDF salvo. Análise da IA enfileirada "
            "(será retentada automaticamente se a API estiver ocupada)."
        )
    _upsert_resume(
        db,
        language=language,
        original_filename=original_filename,
        stored_path=stored_path,
        digest=digest,
        extracted_text=extracted,
        analysis_json=(existing["analysis_json"] if existing else "") or "",
        analysis_summary=(existing["analysis_summary"] if existing else "") or "",
        analyzed_at=existing["analyzed_at"] if existing else None,
        provider="",
        model="",
        analysis_status="pending",
        analysis_error="",
        analysis_message=message,
        now_iso=now_iso,
    )
    save_pdf(db, get_resume(db, language), raw_bytes)
    return message, "info", True


def run_resume_analysis(
    db: sqlite3.Connection,
    *,
    language: str,
    provider: str,
    model: str,
    api_key: str,
    now_iso: str,
) -> str:
    """Executa a análise de IA do currículo já persistido. Pode levantar AiUnavailableError."""
    row = get_resume(db, language)
    if not row:
        raise ValueError(f"Nenhum currículo {language} salvo para analisar.")
    text = (row["extracted_text"] or "").strip()
    if not text:
        from paths import RESUMES_DIR
        text = extract_pdf_text(BytesIO(read_pdf(db, row, RESUMES_DIR)))
    analysis = analyze_resume_text(text, language, provider=provider, model=model, api_key=api_key)
    profile = analysis.pop("_profile", None)
    summary = str(analysis.get("summary") or "").strip()
    message = f"Currículo {language.upper()}: análise da IA concluída ({provider}/{model})."
    _upsert_resume(
        db,
        language=language,
        original_filename=row["original_filename"],
        stored_path=row["stored_path"],
        digest=row["file_sha256"],
        extracted_text=text,
        analysis_json=json.dumps(analysis, ensure_ascii=False),
        analysis_summary=summary,
        analyzed_at=now_iso,
        provider=provider,
        model=model,
        analysis_status="ok",
        analysis_error="",
        analysis_message=message,
        now_iso=now_iso,
        profile_json=json.dumps(profile, ensure_ascii=False) if profile else "",
        profile_version=str(profile.get("version", "")) if profile else "",
    )
    return message


def mark_resume_analysis_error(db: sqlite3.Connection, language: str, error: str, now_iso: str) -> None:
    row = get_resume(db, language)
    if not row:
        return
    db.execute(
        """UPDATE resumes SET analysis_status='error', analysis_error=?, analysis_message=?, updated_at=?
           WHERE language=?""",
        (error[:1000], f"Currículo {language.upper()}: falha na análise — {error}"[:500], now_iso, language),
    )
