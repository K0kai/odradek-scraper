# Odradek Scraper: dados, coleta, busca e preparação de candidaturas.
# O host e a porta podem ser sobrescritos pelo ambiente de deploy (ex.: Render).
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen

from dbutil import open_connection, using_postgres
from apply_channels import apply_via_email, record_blocked, send_smtp_email
from dates_br import BRASILIA, format_cycle_range, parse_utc

from form_rules import ensure_default_rules
from job_queue import (
    KIND_APPLY,
    KIND_RESUME,
    KIND_WORTH_REVERIFY,
    JobQueue,
)
from resume_pipeline import (
    AiUnavailableError,
    get_resume,
    mark_resume_analysis_error,
    resume_match_snippets,
    resume_summaries,
    run_resume_analysis,
)
from panel_log import attach_to_logger, ensure_log_table, list_logs, log_event
from paths import PROJECT_ROOT, DB_PATH as DEFAULT_DB_PATH, FRONTEND_DIST as DEFAULT_FRONTEND_DIST, ICON_PATH


DB_PATH = str(DEFAULT_DB_PATH)
RESUMES_DIR = os.environ.get("RESUMES_DIR", str(PROJECT_ROOT / "resumes"))
COPILOT_UPLOADS_DIR = os.environ.get(
    "COPILOT_UPLOADS_DIR", str(PROJECT_ROOT / "copilot_uploads")
)
FAVICON_PATH = str(ICON_PATH)
FRONTEND_DIST = str(DEFAULT_FRONTEND_DIST)
HOST = os.environ.get("HOST", "0.0.0.0" if os.environ.get("RENDER", "").casefold() == "true" else "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
LOG = logging.getLogger("odradek-scraper")

# Estrutura persistente. As vagas são deduplicadas pela impressão digital; cartas e execuções
# ficam separadas para poder registrar versões e resultados sem alterar os dados da vaga.
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,
  source_id TEXT,
  title TEXT NOT NULL,
  company TEXT NOT NULL DEFAULT '',
  location TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL,
  posted_at TEXT,
  first_seen_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new',
  applied_at TEXT,
  notes TEXT NOT NULL DEFAULT '',
  fingerprint TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_seen_idx ON jobs(first_seen_at DESC);
CREATE INDEX IF NOT EXISTS jobs_status_seen_idx ON jobs(status, first_seen_at DESC);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  state TEXT NOT NULL,
  found_count INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ai_decisions (
  id INTEGER PRIMARY KEY,
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  decided_at TEXT NOT NULL,
  match_score INTEGER NOT NULL,
  should_apply INTEGER NOT NULL,
  letter_required INTEGER NOT NULL,
  reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ai_decisions_job_latest_idx ON ai_decisions(job_id, id DESC);
"""

DEFAULT_SETTINGS = {
    "keywords": "software engineer, desenvolvedor, developer, backend, frontend, full stack, data engineer, devops",
    "locations": "remote, remoto, brazil, brasil, worldwide, anywhere",
    "sources": "remotive,remoteok",
    "ai_provider": "gemini",
    "ai_model": "gemini-2.5-flash",
    "candidate_name": "",
    "candidate_email": "",
    "candidate_phone": "",
    "candidate_linkedin": "",
    "candidate_city": "",
    "candidate_street": "",
    "candidate_postal_code": "",
    "candidate_state": "",
    "candidate_country": "",
    "candidate_current_company": "",
    "candidate_cpf": "",
    # Expectativa salarial — o robô escolhe BRL×USD pela moeda do campo/vaga
    "salary_expectation_brl": "",
    "salary_expectation_usd": "",
    "salary_currency_preference": "auto",
    "salary_pj_multiplier": "1.3",
    # Preferência de contratação (radio Contractor/Employee); 'ask' = deixar com você
    "candidate_contract_type": "employee",
    "candidate_profile_pt": "",
    "candidate_profile_en": "",
    "candidate_facts_pt": "",
    "candidate_facts_en": "",
    "resume_pt_path": "",
    "resume_en_path": "",
    "auto_apply": "0",
    "browser_engine": "pydoll",
    "minimum_match_score": "65",
    "job_selection_optimism": "75",
    "job_selection_prompt": "",
    "maximum_applications_per_run": "5",
    "adzuna_countries": "br,us,gb,ca",
    "apify_monthly_credit_limit_usd": "5",
    "apify_job_count": "25",
    "apify_actors_json": "[{\"id\": \"curious_coder~linkedin-jobs-scraper\", \"label\": \"LinkedIn Jobs\", \"enabled\": true, \"input_mode\": \"linkedin_search\", \"count\": 25}]",
    "apify_linkedin_filter_json": "",
    "apify_linkedin_filter_hash": "",
    "apify_linkedin_filters_json": "",
    # Vazio = DEFAULT_LINKEDIN_FILTER_PROMPT (editável em Busca & coleta)
    "linkedin_filter_prompt": "",
    "ai_usage_json": "",
    "apify_quota_snapshot_json": "",
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_from": "",
    "smtp_use_tls": "1",
    "queue_max_workers": "3",
    "queue_max_attempts": "40",
    "queue_ttl_hours": "24",
    "worth_reverify_last_daily": "",
    # Som no painel quando o copiloto pede um dado (Web Audio)
    "copilot_ask_sound": "1",
    "copilot_ask_sound_volume": "0.55",
    # Perfil de diversidade (identidade NUNCA vem de IA; 'not_informed' = não informar)
    "candidate_gender": "not_informed",
    "candidate_race": "not_informed",
    "candidate_lgbtq": "not_informed",
    "candidate_pcd": "no",
    "candidate_diversity_note": "",
}
STATUSES = {"new": "Nova", "review": "Na fila", "worth": "Vale a pena olhar", "saved": "Salva", "prepared": "Carta preparada", "applied": "Aplicada", "ignored": "Ignorada", "blocked": "Envio indisponível"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    return open_connection(DB_PATH, timeout=60.0)


def initialize() -> None:
    """Cria o banco e aplica pequenas migrações compatíveis com versões anteriores."""
    os.makedirs(RESUMES_DIR, exist_ok=True)
    if using_postgres():
        required_tables = {
            "settings", "jobs", "runs", "ai_decisions", "cover_letters", "resumes",
            "form_field_rules", "applications", "form_answers", "ai_answer_cache",
            "copilot_asks", "queue_jobs", "event_logs",
        }
        with connect() as db:
            rows = db.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            ).fetchall()
            available = {row["table_name"] for row in rows}
            missing = sorted(required_tables - available)
            if missing:
                raise RuntimeError(
                    "Neon schema is incomplete. Run the PostgreSQL schema SQL in the Neon console; "
                    "missing tables: " + ", ".join(missing)
                )
            for key, value in DEFAULT_SETTINGS.items():
                db.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING",
                    (key, value),
                )
            ensure_default_rules(db)
            db.execute(
                "CREATE INDEX IF NOT EXISTS ai_decisions_job_latest_idx "
                "ON ai_decisions(job_id, id DESC)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS jobs_status_seen_idx "
                "ON jobs(status, first_seen_at DESC)"
            )
            db.execute(
                "UPDATE settings SET value='65' WHERE key='minimum_match_score' AND value='80'"
            )
            db.execute(
                "UPDATE settings SET value='' WHERE key='apify_linkedin_filter_hash' "
                "AND COALESCE((SELECT value FROM settings WHERE key='apify_linkedin_geo_v2'), '') <> '1'"
            )
            db.execute(
                "INSERT INTO settings(key,value) VALUES('apify_linkedin_geo_v2','1') "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"
            )
            ensure_log_table(db)
        return

    with connect() as db:
        db.executescript(SCHEMA)
        # Additions are applied in place so an existing jobs.db remains usable.
        columns = {row["name"] for row in db.execute("PRAGMA table_info(jobs)")}
        if "language" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN language TEXT NOT NULL DEFAULT 'unknown'")
        if "language_confidence" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN language_confidence REAL NOT NULL DEFAULT 0")
        db.execute("""CREATE TABLE IF NOT EXISTS cover_letters (
          id INTEGER PRIMARY KEY,
          job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
          language TEXT NOT NULL,
          provider TEXT NOT NULL,
          model TEXT NOT NULL,
          body TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS resumes (
          id INTEGER PRIMARY KEY,
          language TEXT NOT NULL UNIQUE,
          original_filename TEXT NOT NULL DEFAULT '',
          stored_path TEXT NOT NULL,
          file_sha256 TEXT NOT NULL,
          extracted_text TEXT NOT NULL DEFAULT '',
          analysis_json TEXT NOT NULL DEFAULT '',
          analysis_summary TEXT NOT NULL DEFAULT '',
          analyzed_at TEXT,
          provider TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '',
          analysis_status TEXT NOT NULL DEFAULT 'none',
          analysis_error TEXT NOT NULL DEFAULT '',
          analysis_message TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )""")
        resume_cols = {row["name"] for row in db.execute("PRAGMA table_info(resumes)")}
        if "analysis_status" not in resume_cols:
            db.execute("ALTER TABLE resumes ADD COLUMN analysis_status TEXT NOT NULL DEFAULT 'none'")
        if "analysis_error" not in resume_cols:
            db.execute("ALTER TABLE resumes ADD COLUMN analysis_error TEXT NOT NULL DEFAULT ''")
        if "analysis_message" not in resume_cols:
            db.execute("ALTER TABLE resumes ADD COLUMN analysis_message TEXT NOT NULL DEFAULT ''")
        # Backfill: resumes with summary count as successful analysis.
        db.execute(
            """UPDATE resumes SET analysis_status='ok',
                   analysis_message=COALESCE(NULLIF(analysis_message,''), 'Análise concluída com sucesso.')
               WHERE IFNULL(analysis_summary,'') != '' AND analysis_status IN ('none', '')"""
        )
        db.execute("""CREATE TABLE IF NOT EXISTS form_field_rules (
          id INTEGER PRIMARY KEY,
          key TEXT NOT NULL,
          aliases TEXT NOT NULL DEFAULT '',
          mode TEXT NOT NULL DEFAULT 'text',
          value_from TEXT NOT NULL DEFAULT '',
          value TEXT NOT NULL DEFAULT '',
          sort_order INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS applications (
          id INTEGER PRIMARY KEY,
          job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
          channel TEXT NOT NULL,
          recipient TEXT NOT NULL DEFAULT '',
          resume_id INTEGER,
          cover_letter_id INTEGER,
          status TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '',
          attempted_at TEXT NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS form_answers (
          id INTEGER PRIMARY KEY,
          job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
          question TEXT NOT NULL,
          answer TEXT NOT NULL,
          provider TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        )""")
        # Cache GLOBAL de respostas de IA por pergunta normalizada ('open' ou 'choices').
        db.execute("""CREATE TABLE IF NOT EXISTS ai_answer_cache (
          id INTEGER PRIMARY KEY,
          question_norm TEXT NOT NULL UNIQUE,
          question_raw TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT 'open',
          answer TEXT NOT NULL,
          provider TEXT NOT NULL DEFAULT '',
          model TEXT NOT NULL DEFAULT '',
          hits INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )""")
        from copilot_asks import ensure_table as ensure_copilot_asks

        ensure_copilot_asks(db)
        decision_cols = {row["name"] for row in db.execute("PRAGMA table_info(ai_decisions)")}
        if "resume_language" not in decision_cols:
            db.execute("ALTER TABLE ai_decisions ADD COLUMN resume_language TEXT NOT NULL DEFAULT ''")
        if "apply_channel" not in decision_cols:
            db.execute("ALTER TABLE ai_decisions ADD COLUMN apply_channel TEXT NOT NULL DEFAULT ''")
        if "apply_result" not in decision_cols:
            db.execute("ALTER TABLE ai_decisions ADD COLUMN apply_result TEXT NOT NULL DEFAULT ''")
        ensure_default_rules(db)
        for key, value in DEFAULT_SETTINGS.items():
            db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING",
                (key, value),
            )
        # Default antigo (80) era rígido demais com a triagem generosa.
        db.execute(
            "UPDATE settings SET value='65' WHERE key='minimum_match_score' AND value='80'"
        )
        # Uma vez: regenerar filtro LinkedIn com geo remote global + híbrido BH.
        row = db.execute(
            "SELECT value FROM settings WHERE key='apify_linkedin_geo_v2'"
        ).fetchone()
        if not row or row["value"] != "1":
            db.execute("UPDATE settings SET value='' WHERE key='apify_linkedin_filter_hash'")
            db.execute(
                "INSERT INTO settings(key,value) VALUES('apify_linkedin_geo_v2','1') "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
            )
        ensure_log_table(db)


def settings() -> dict[str, str]:
    """Lê as preferências salvas no SQLite."""
    with connect() as db:
        return {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM settings")}


def save_settings(data: dict[str, str]) -> None:
    """Atualiza somente os campos enviados pelo formulário atual."""
    with connect() as db:
        for key in DEFAULT_SETTINGS:
            if key in data:
                db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, data.get(key, "")))


def set_setting(key: str, value: str) -> None:
    with connect() as db:
        db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _boot_usage_tracker() -> None:
    from usage_tracker import STORE_KEY, configure_persistence

    configure_persistence(
        lambda: settings().get(STORE_KEY, "") or "",
        lambda value: set_setting(STORE_KEY, value),
    )


_boot_usage_tracker()


def detect_language(text: str) -> tuple[str, float]:
    """Detect English/Portuguese locally, using Lingua when installed."""
    sample = re.sub(r"<[^>]+>", " ", text or "")
    sample = re.sub(r"\s+", " ", sample).strip()[:12000]
    if len(sample) < 30:
        return "unknown", 0.0
    try:
        from lingua import Language, LanguageDetectorBuilder
        detector = LanguageDetectorBuilder.from_languages(Language.ENGLISH, Language.PORTUGUESE).build()
        scores = detector.compute_language_confidence_values(sample)
        if not scores:
            return "unknown", 0.0
        best = scores[0]
        confidence = float(best.value)
        return ("en" if best.language == Language.ENGLISH else "pt", confidence) if confidence >= 0.65 else ("unknown", confidence)
    except ImportError:
        # A conservative fallback keeps the app usable before optional model install.
        normalized = " " + re.sub(r"[^a-záàâãéêíóôõúüç ]", " ", sample.casefold()) + " "
        pt = sum(normalized.count(f" {word} ") for word in ("de", "para", "com", "uma", "não", "você", "experiência", "responsabilidades", "requisitos", "benefícios", "trabalho"))
        en = sum(normalized.count(f" {word} ") for word in ("the", "and", "with", "for", "you", "experience", "responsibilities", "requirements", "benefits", "work", "team"))
        total = pt + en
        if total < 2 or pt == en:
            return "unknown", 0.0
        return ("pt", min(0.9, 0.55 + pt / (total + 2) * 0.4)) if pt > en else ("en", min(0.9, 0.55 + en / (total + 2) * 0.4))


def ai_generate_letter(provider: str, model: str, language: str, job: dict, cfg: dict[str, str]) -> str:
    """Pede somente a carta; o modelo não recebe, cria ou altera o arquivo de currículo."""
    from experience_priority import EXPERIENCE_PRIORITY_RULE

    provider = provider.casefold().strip()
    language_name = "Portuguese" if language == "pt" else "English"
    facts_key = "candidate_facts_pt" if language == "pt" else "candidate_facts_en"
    facts = cfg.get(facts_key, "").strip()
    name = cfg.get("candidate_name", "").strip()
    prompt = f"""Write a concise, specific cover letter in {language_name} for this job application.
Use only the candidate facts provided below. Never invent experience, qualifications, results, employers, dates, or skills. If facts are sparse, keep the letter brief and make no unsupported claims. Do not claim the candidate already applied. Return only the letter, with no subject line or commentary.

{EXPERIENCE_PRIORITY_RULE}

Candidate name: {name or '[candidate name]'}
Candidate-provided facts:
{facts or '[No candidate facts provided.]'}
Resume analysis summary (authoritative; do not invent beyond this):
{cfg.get("resume_summary_pt" if language == "pt" else "resume_summary_en", "") or "[No resume analysis provided.]"}

Job title: {job['title']}
Company: {job['company']}
Location / eligibility: {job['location']}
Job description:
{re.sub(r'<[^>]+>', ' ', job['description'])[:10000]}
"""
    if provider == "openai":
        api_key = get_ai_key(provider)
        endpoint = "https://api.openai.com/v1/responses"
        payload = json.dumps({"model": model, "input": prompt, "store": False, "max_output_tokens": 700}).encode("utf-8")
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    elif provider == "gemini":
        api_key = get_ai_key(provider)
        endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote_plus(model)}:generateContent?key={quote_plus(api_key)}"
        payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"maxOutputTokens": 700, "temperature": 0.4}}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
    else:
        raise ValueError("Provedor de IA deve ser Gemini ou OpenAI.")
    if not api_key:
        raise ValueError("Configure a chave da API na seção de IA.")
    request = Request(endpoint, data=payload, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        quota = exc.code in {401, 403, 429, 502, 503, 504} or "quota" in body.casefold()
        _record_ai_usage(provider, model, None, ok=False, quota_error=quota)
        if quota:
            raise AiUnavailableError(f"IA indisponível ao gerar carta (HTTP {exc.code}).") from exc
        raise RuntimeError(f"Falha ao chamar a API de IA (HTTP {exc.code}).") from exc
    except Exception as exc:
        msg = str(exc).casefold()
        quota = any(t in msg for t in ("429", "quota", "unavailable", "timeout"))
        _record_ai_usage(provider, model, None, ok=False, quota_error=quota)
        if quota:
            raise AiUnavailableError(f"IA indisponível ao gerar carta: {exc}") from exc
        raise RuntimeError(f"Falha ao chamar a API de IA ({exc.__class__.__name__}).") from exc
    _record_ai_usage(provider, model, result if isinstance(result, dict) else None, ok=True, quota_error=False)
    if provider == "openai":
        parts = [part.get("text", "") for item in result.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text"]
        letter = "\n".join(parts).strip()
    else:
        letter = "\n".join(part.get("text", "") for item in result.get("candidates", []) for part in item.get("content", {}).get("parts", [])).strip()
    if not letter:
        raise RuntimeError("A API não retornou uma carta de apresentação.")
    return letter


def _record_ai_usage(
    provider: str,
    model: str,
    result: dict | None,
    *,
    ok: bool,
    quota_error: bool,
) -> None:
    try:
        from usage_tracker import try_record_ai_result

        try_record_ai_result(
            provider=provider,
            model=model,
            result=result,
            ok=ok,
            quota_error=quota_error,
        )
    except Exception:
        pass


SECRET_ENV_VARS = {
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "adzuna_app_id": ("ADZUNA_APP_ID",),
    "adzuna_app_key": ("ADZUNA_APP_KEY",),
    "apify_token": ("APIFY_TOKEN", "APIFY_API_TOKEN"),
    "smtp_password": ("SMTP_PASSWORD",),
}
SECRET_SETTING_PREFIX = "__encrypted_secret__:"
SECRET_MASTER_KEY_ENV = "SECRET_ENCRYPTION_KEY"


def _secret_from_environment(name: str) -> str:
    for env_name in SECRET_ENV_VARS.get(name.casefold(), ()):
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    return ""


def _secret_from_keyring(name: str) -> str:
    try:
        import keyring
        return keyring.get_password("job-scraper", name) or ""
    except Exception:
        return ""


def _secret_from_database(name: str) -> str | None:
    with connect() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key=?",
            (SECRET_SETTING_PREFIX + name.casefold(),),
        ).fetchone()
    if not row:
        return None
    master_key = os.environ.get(SECRET_MASTER_KEY_ENV, "").strip()
    if not master_key:
        raise RuntimeError(f"Configure {SECRET_MASTER_KEY_ENV} para descriptografar os segredos salvos.")
    try:
        from cryptography.fernet import Fernet, InvalidToken

        return Fernet(master_key.encode("ascii")).decrypt(row["value"].encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise RuntimeError(
            f"Não foi possível descriptografar o segredo '{name}'. Confira {SECRET_MASTER_KEY_ENV}; "
            "ela precisa ser a mesma usada quando o segredo foi salvo."
        ) from exc


def _secret_save(name: str, value: str, *, delete_empty: bool = False) -> None:
    if not value.strip() and not delete_empty:
        return
    master_key = os.environ.get(SECRET_MASTER_KEY_ENV, "").strip()
    if master_key:
        try:
            from cryptography.fernet import Fernet

            encrypted = Fernet(master_key.encode("ascii")).encrypt(value.strip().encode("utf-8")).decode("ascii")
        except Exception as exc:
            raise RuntimeError(f"{SECRET_MASTER_KEY_ENV} inválida. Gere uma chave Fernet válida.") from exc
        db_key = SECRET_SETTING_PREFIX + name.casefold()
        with connect() as db:
            if not value.strip() and delete_empty:
                db.execute("DELETE FROM settings WHERE key=?", (db_key,))
            elif value.strip():
                db.execute(
                    "INSERT INTO settings(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
                    (db_key, encrypted),
                )
        return
    if os.environ.get("RENDER"):
        raise RuntimeError(
            f"Configure {SECRET_MASTER_KEY_ENV} no Render para salvar segredos criptografados no banco."
        )
    try:
        import keyring

        if value.strip():
            keyring.set_password("job-scraper", name, value.strip())
        elif delete_empty:
            keyring.delete_password("job-scraper", name)
    except Exception as exc:
        raise RuntimeError(
            f"Não foi possível acessar o cofre local. Configure {SECRET_MASTER_KEY_ENV} para usar o banco criptografado."
        ) from exc


def get_ai_key(provider: str) -> str:
    return secret_get(provider)


def save_ai_key(provider: str, key: str) -> None:
    _secret_save(provider, key)


def secret_get(name: str) -> str:
    stored = _secret_from_database(name)
    if stored is not None:
        return stored
    return _secret_from_environment(name) or _secret_from_keyring(name)


def secret_set(name: str, value: str) -> None:
    _secret_save(name, value)


def create_cover_letter(job_id: int, resume_language: str | None = None) -> int:
    cfg = settings()
    with connect() as db:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        summaries = resume_summaries(db)
    if not job:
        raise ValueError("Vaga não encontrada.")
    language = resume_language or job["language"]
    if language not in ("pt", "en"):
        raise ValueError("Idioma da vaga incerto. Corrija o idioma antes de gerar a carta.")
    cfg = dict(cfg)
    cfg["resume_summary_pt"] = summaries.get("pt", "")
    cfg["resume_summary_en"] = summaries.get("en", "")
    provider = cfg.get("ai_provider", "gemini").casefold()
    model = cfg.get("ai_model", "gemini-2.5-flash").strip()
    letter = ai_generate_letter(provider, model, language, dict(job), cfg)
    stamp = now_iso()
    with connect() as db:
        db.execute("""INSERT INTO cover_letters(job_id,language,provider,model,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?)
          ON CONFLICT(job_id) DO UPDATE SET language=excluded.language,provider=excluded.provider,model=excluded.model,body=excluded.body,updated_at=excluded.updated_at""", (job_id, language, provider, model, letter, stamp, stamp))
        db.execute("UPDATE jobs SET status='prepared' WHERE id=? AND status IN ('new','review')", (job_id,))
        row = db.execute("SELECT id FROM cover_letters WHERE job_id=?", (job_id,)).fetchone()
    return int(row["id"])


DEFAULT_JOB_SELECTION_PROMPT = """You are a fair, practical job-fit evaluator. Assess whether this candidate has a credible path to succeed in this role.

- Compare the role's core responsibilities and required skills with the candidate's actual resume evidence.
- Give meaningful credit to transferable skills, adjacent experience, and demonstrated ability to learn.
- Treat missing secondary or preferred tools as a modest gap, not an automatic rejection.
- Do not invent employers, degrees, skills, or experience absent from the candidate information.
- Reject only for a genuine hard blocker or a fundamentally unrelated career path.
- Base the score and recommendation on the optimism calibration supplied below.
"""


def effective_job_selection_prompt(cfg: dict[str, str] | None = None) -> str:
    cfg = cfg or {}
    custom = str(cfg.get("job_selection_prompt") or "").strip()
    return custom if custom else DEFAULT_JOB_SELECTION_PROMPT


def job_selection_optimism_instruction(value: object) -> tuple[int, str]:
    try:
        optimism = max(0, min(100, int(float(str(value or "75")))))
    except (TypeError, ValueError):
        optimism = 75
    if optimism <= 20:
        guidance = "Be highly selective: recommend only clear, well-supported matches; score adjacent or stretch roles conservatively."
    elif optimism <= 40:
        guidance = "Be somewhat selective: prioritize strong matches, while allowing relevant transferable experience."
    elif optimism <= 60:
        guidance = "Use balanced judgment: count relevant overlap and transferable skills, but reflect meaningful gaps."
    elif optimism <= 80:
        guidance = "Lean optimistic: give strong credit to adjacent experience and transferable skills; keep plausible roles in consideration."
    else:
        guidance = "Be very inclusive: favor a credible path to success, avoid over-penalizing missing secondary skills, and keep stretch roles in consideration unless a hard blocker exists."
    return optimism, guidance


def ai_assess_job(job: dict, cfg: dict[str, str]) -> dict:
    """Classifica aderência usando a análise de currículo já salva (sem reenviar o PDF)."""
    language = job.get("language", "unknown")
    if language not in ("pt", "en"):
        raise ValueError("Idioma incerto; a IA não vai escolher um currículo por suposição.")
    facts = cfg.get("candidate_facts_pt" if language == "pt" else "candidate_facts_en", "").strip()
    with connect() as db:
        summaries = resume_summaries(db)
        snippets = resume_match_snippets(db)
    resume_summary_pt = summaries.get("pt", "").strip()
    resume_summary_en = summaries.get("en", "").strip()
    snippet_pt = snippets.get("pt", "").strip()
    snippet_en = snippets.get("en", "").strip()
    if language == "pt" and not resume_summary_pt and not resume_summary_en and not snippet_pt and not snippet_en:
        raise ValueError("Faça upload e análise de pelo menos um currículo antes da triagem automática.")
    if language == "en" and not resume_summary_en and not resume_summary_pt and not snippet_en and not snippet_pt:
        raise ValueError("Faça upload e análise de pelo menos um currículo antes da triagem automática.")

    optimism, optimism_guidance = job_selection_optimism_instruction(cfg.get("job_selection_optimism"))
    prompt = f"""{effective_job_selection_prompt(cfg)}

OPTIMISM CALIBRATION: {optimism}/100. {optimism_guidance}
Keep the score consistent with this calibration. Set should_apply=true when the role meets the configured standard and has no hard blocker. Identify whether the job description asks for a cover letter.

Return ONLY JSON with keys:
match_score (integer 0-100),
should_apply (boolean),
cover_letter_required (boolean),
reason (short string in {('Portuguese' if language == 'pt' else 'English')}),
recommended_resume_language (either "en" or "pt").

Candidate facts: {facts or '[none provided]'}
Resume profile snippet (PT): {snippet_pt or '[none provided]'}
Resume profile snippet (EN): {snippet_en or '[none provided]'}
Resume summary (PT): {(resume_summary_pt[:2500] if resume_summary_pt else '[none provided]')}
Resume summary (EN): {(resume_summary_en[:2500] if resume_summary_en else '[none provided]')}
Job title: {job['title']}
Company: {job['company']}
Location/eligibility: {job['location']}
Description: {re.sub(r'<[^>]+>', ' ', job['description'])[:10000]}"""

    provider = cfg.get("ai_provider", "gemini").casefold()
    model = cfg.get("ai_model", "gemini-2.5-flash").strip()
    api_key = get_ai_key(provider)
    if not api_key:
        raise AiUnavailableError("Configure a chave da API de IA para ativar o julgamento automático.")
    try:
        if provider == "openai":
            endpoint = "https://api.openai.com/v1/responses"
            payload = json.dumps(
                {
                    "model": model,
                    "input": prompt,
                    "text": {"format": {"type": "json_object"}},
                    "store": False,
                    "max_output_tokens": 400,
                    "temperature": 0.7,
                }
            ).encode("utf-8")
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        elif provider == "gemini":
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{quote_plus(model)}:generateContent?key={quote_plus(api_key)}"
            payload = json.dumps(
                {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {
                        "maxOutputTokens": 400,
                        "temperature": 0.7,
                        "responseMimeType": "application/json",
                    },
                }
            ).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        else:
            raise ValueError("Provedor de IA inválido.")
        request = Request(endpoint, data=payload, headers=headers, method="POST")
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        from ai_client import ai_http_error_detail

        detail = ai_http_error_detail(exc, provider=provider, model=model, operation="Triagem da vaga", api_key=api_key)
        quota = exc.code in {401, 403, 429, 502, 503, 504} or "quota" in detail.casefold()
        _record_ai_usage(provider, model, None, ok=False, quota_error=quota)
        log_event("error", "auto-apply", f"Vaga #{job.get('id', '?')}: {detail}")
        if quota:
            raise AiUnavailableError(detail) from exc
        raise RuntimeError(detail) from exc
    _record_ai_usage(provider, model, result if isinstance(result, dict) else None, ok=True, quota_error=False)
    if provider == "openai":
        raw = "\n".join(part.get("text", "") for item in result.get("output", []) for part in item.get("content", []) if part.get("type") == "output_text")
    else:
        raw = "\n".join(part.get("text", "") for item in result.get("candidates", []) for part in item.get("content", {}).get("parts", []))
    decision = json.loads(raw.strip())

    recommended_language = decision.get("recommended_resume_language")
    if recommended_language not in ("en", "pt"):
        match_score = int(decision.get("match_score", 0))
        if language == "en" and match_score >= 65 and resume_summary_en:
            recommended_language = "en"
        elif resume_summary_pt:
            recommended_language = "pt"
        elif resume_summary_en:
            recommended_language = "en"
        else:
            recommended_language = language
    if recommended_language == "pt" and not resume_summary_pt and resume_summary_en:
        recommended_language = "en"
    if recommended_language == "en" and not resume_summary_en and resume_summary_pt:
        recommended_language = "pt"

    score = max(0, min(100, int(decision.get("match_score", 0))))
    should_apply = bool(decision.get("should_apply"))
    # Soft floor: same-field optimistic calibration if the model still underrates.
    if should_apply and score < 50:
        score = 50
    elif score >= 50:
        should_apply = True
    return {
        "match_score": score,
        "should_apply": should_apply,
        "letter_required": bool(decision.get("cover_letter_required")),
        "reason": str(decision.get("reason", ""))[:1000],
        "provider": provider,
        "model": model,
        "resume_language": recommended_language,
    }


def is_linkedin_job(job: dict) -> bool:
    blob = " ".join(
        str(job.get(key) or "")
        for key in ("url", "source", "description", "company")
    ).casefold()
    return "linkedin.com" in blob or "linkedin" in str(job.get("source") or "").casefold()


def mark_worth_looking(job_id: int, *, score: int, reason: str, detail: str) -> None:
    note = (
        f"Vale a pena olhar (score {score}/100). {detail} "
        f"Motivo da IA: {reason}"
    )[:900]
    with connect() as db:
        db.execute(
            "UPDATE jobs SET status='worth', notes=? WHERE id=?",
            (note, job_id),
        )
        db.execute(
            "UPDATE ai_decisions SET apply_channel=?, apply_result=? WHERE id=(SELECT MAX(id) FROM ai_decisions WHERE job_id=?)",
            ("manual", detail[:500], job_id),
        )
    log_event("info", "worth", f"Vaga #{job_id} enviada para 'Vale a pena olhar' — {detail}")


def process_auto_job(job_id: int) -> str:

    """Triagem com análise do currículo; candidatura automática fica limitada a e-mail."""
    cfg = settings()
    with connect() as db:
        job_row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job_row:
        return "vaga removida"
    job = dict(job_row)
    title = job.get("title", "")
    log_event("info", "auto-apply", f"Triagem da vaga #{job_id}: {title}")
    try:
        decision = ai_assess_job(job, cfg)
    except AiUnavailableError:
        # Propagates to the job queue for retry/backoff.
        raise
    except ValueError as exc:
        with connect() as db:
            db.execute("UPDATE jobs SET status='blocked', notes=? WHERE id=?", (str(exc), job_id))
        log_event("warning", "auto-apply", f"Vaga #{job_id}: {exc}")
        return f"vaga {job_id}: {exc}"

    minimum = max(0, min(100, int(cfg.get("minimum_match_score", "80") or 80)))
    qualifies = decision["should_apply"] and decision["match_score"] >= minimum
    resume_language = decision["resume_language"]
    with connect() as db:
        db.execute(
            """INSERT INTO ai_decisions(job_id,decided_at,match_score,should_apply,letter_required,reason,resume_language,apply_channel,apply_result)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (job_id, now_iso(), decision["match_score"], int(qualifies), int(decision["letter_required"]), decision["reason"], resume_language, "", ""),
        )
    log_event(
        "info",
        "auto-apply",
        f"Vaga #{job_id}: score {decision['match_score']}/100, should_apply={decision['should_apply']}, CV={resume_language}. {decision['reason']}",
    )
    if not qualifies:
        with connect() as db:
            db.execute("UPDATE jobs SET status='ignored', notes=? WHERE id=?", (f"IA: score {decision['match_score']}/100. {decision['reason']}", job_id))
        log_event("info", "auto-apply", f"Vaga #{job_id} ignorada (score abaixo do mínimo ou should_apply=false).")
        return f"IA descartou vaga {job_id} ({decision['match_score']}/100)"

    with connect() as db:
        resume = get_resume(db, resume_language)
    if not resume or not (resume["analysis_summary"] or "").strip():
        with connect() as db:
            db.execute("UPDATE jobs SET status='blocked', notes=? WHERE id=?", (f"Sem currículo analisado em {resume_language}.", job_id))
        return f"vaga {job_id}: currículo {resume_language} ausente"

    try:
        cover_id = create_cover_letter(job_id, resume_language=resume_language)
    except AiUnavailableError:
        raise
    except Exception as exc:
        with connect() as db:
            db.execute("UPDATE jobs SET status='blocked', notes=? WHERE id=?", (f"Falha ao gerar carta: {exc}", job_id))
        return f"vaga {job_id}: falha na carta"

    with connect() as db:
        letter_row = db.execute("SELECT * FROM cover_letters WHERE job_id=?", (job_id,)).fetchone()
    cover_body = letter_row["body"] if letter_row else ""
    stamp = now_iso()
    score = int(decision["match_score"])

    # Candidaturas via página web ficam para o usuário no Chrome com a extensão.
    if is_linkedin_job(job):
        mark_worth_looking(
            job_id,
            score=score,
            reason=decision["reason"],
            detail="Vaga do LinkedIn — carta preparada. Abra no Chrome e use Odradek Autofill para preencher manualmente.",
        )
        return f"vaga {job_id}: vale a pena olhar (LinkedIn, score {score})"

    ok, detail = apply_via_email(
        connect,
        job,
        cfg,
        cover_letter=cover_body,
        resume_path=resume["stored_path"],
        resume_id=int(resume["id"]),
        cover_letter_id=cover_id,
        smtp_password=secret_get("smtp_password"),
        now_iso=stamp,
    )
    channel = "email"
    if not ok:
        record_blocked(connect, job_id, detail, now_iso(), int(resume["id"]), cover_id)
        mark_worth_looking(
            job_id,
            score=score,
            reason=decision["reason"],
            detail=f"Envio por e-mail indisponível ({detail}). Abra a vaga no Chrome e use Odradek Autofill.",
        )
        return f"vaga {job_id}: vale a pena olhar — {detail}"
    with connect() as db:
        db.execute("UPDATE jobs SET status='applied', applied_at=?, notes=? WHERE id=?", (now_iso(), detail[:900], job_id))
        db.execute(
            "UPDATE ai_decisions SET apply_channel=?, apply_result=? WHERE id=(SELECT MAX(id) FROM ai_decisions WHERE job_id=?)",
            (channel, detail[:500], job_id),
        )
    log_event("success", "auto-apply", f"Vaga #{job_id} aplicada via {channel}: {detail}")
    return f"vaga {job_id}: aplicada via {channel} — {detail}"


def enqueue_new_jobs_for_triage() -> str:
    """Enfileira vagas com status 'new' para triagem IA. Não exige auto_apply."""
    cfg = settings()
    with connect() as db:
        rows = db.execute("SELECT id FROM jobs WHERE status='new' ORDER BY id ASC").fetchall()
    if not rows:
        return "nenhuma vaga nova para enfileirar"
    queued = 0
    for row in rows:
        job_id = int(row["id"])
        queue.enqueue(KIND_APPLY, {"job_id": job_id}, dedupe_key=f"apply:{job_id}")
        with connect() as db:
            db.execute(
                "UPDATE jobs SET status='review', notes=? WHERE id=? AND status='new'",
                ("Na fila de triagem (processada pelos workers da fila).", job_id),
            )
        queued += 1
    workers = cfg.get("queue_max_workers", "3")
    return f"{queued} vaga(s) enfileirada(s) para triagem (até {workers} worker(s) em paralelo)"


def process_auto_apply_batch() -> str:
    """Usado pelo ciclo de coleta quando auto_apply=1; delega ao enqueue manual."""
    return enqueue_new_jobs_for_triage()


def handle_resume_analysis_job(payload: dict) -> str:
    language = str(payload.get("language") or "").casefold()
    cfg = settings()
    provider = cfg.get("ai_provider", "gemini").casefold()
    model = cfg.get("ai_model", "gemini-2.5-flash").strip()
    stamp = now_iso()
    try:
        with connect() as db:
            return run_resume_analysis(
                db,
                language=language,
                provider=provider,
                model=model,
                api_key=get_ai_key(provider),
                now_iso=stamp,
            )
    except AiUnavailableError:
        raise
    except Exception as e:
        with connect() as db:
            mark_resume_analysis_error(db, language, f"{e.__class__.__name__}: {e}", stamp)
        raise


def handle_job_apply_job(payload: dict) -> str:
    job_id = int(payload.get("job_id") or 0)
    if not job_id:
        raise ValueError("job_id ausente no payload da fila.")
    return process_auto_job(job_id)


def handle_worth_reverify_job(payload: dict) -> str:
    from worth_reverify import format_summary, reverify_worth_jobs

    summary = reverify_worth_jobs(connect)
    msg = format_summary(summary)
    log_event("info", "worth-check", msg)
    return msg


def _claim_daily_worth_check(today: str) -> bool:
    """Atomically claim today's run, even when Render starts multiple instances."""
    with connect() as db:
        cur = db.execute(
            "UPDATE settings SET value=? WHERE key='worth_reverify_last_daily' AND value < ?",
            (today, today),
        )
        return cur.rowcount == 1


def _daily_worth_check_loop() -> None:
    """Queue one browser-free liveness pass per Brasília calendar day, after 03:00."""
    while True:
        try:
            today = datetime.now(BRASILIA).date().isoformat()
            if datetime.now(BRASILIA).hour >= 3 and _claim_daily_worth_check(today):
                try:
                    with connect() as db:
                        count = int(db.execute(
                            "SELECT COUNT(*) AS n FROM jobs WHERE status='worth'"
                        ).fetchone()["n"] or 0)
                except Exception:
                    with connect() as db:
                        db.execute(
                            "UPDATE settings SET value='' "
                            "WHERE key='worth_reverify_last_daily' AND value=?",
                            (today,),
                        )
                    raise
                if count:
                    try:
                        qid = queue.enqueue(
                            KIND_WORTH_REVERIFY,
                            {"scheduled_date": today},
                            dedupe_key=f"daily:{today}",
                        )
                    except Exception:
                        with connect() as db:
                            db.execute(
                                "UPDATE settings SET value='' "
                                "WHERE key='worth_reverify_last_daily' AND value=?",
                                (today,),
                            )
                        raise
                    log_event(
                        "info", "worth-check",
                        f"Verificação diária enfileirada (fila #{qid}, {count} vaga(s)).",
                    )
                else:
                    log_event("info", "worth-check", "Verificação diária: nenhuma vaga para checar.")
        except Exception as exc:
            LOG.exception("Falha ao agendar verificação diária de vagas: %s", exc)
        time.sleep(60)


def _boot_worth_check_scheduler() -> None:
    threading.Thread(
        target=_daily_worth_check_loop,
        daemon=True,
        name="daily-worth-check-scheduler",
    ).start()


queue = JobQueue(
    db_path=DB_PATH,
    handlers={
        KIND_RESUME: handle_resume_analysis_job,
        KIND_APPLY: handle_job_apply_job,
        KIND_WORTH_REVERIFY: handle_worth_reverify_job,
    },
    get_settings=settings,
    connect_fn=connect,
)


def terms(value: str) -> list[str]:
    return [part.strip().casefold() for part in value.split(",") if part.strip()]


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}{parsed.path.rstrip('/')}"


def fingerprint(job: dict) -> str:
    stable_url = normalize_url(job.get("url", ""))
    text = "|".join((stable_url, job.get("company", "").casefold().strip(), job.get("title", "").casefold().strip(), job.get("location", "").casefold().strip()))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_json(url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None, timeout: int = 25) -> object:
    """Busca uma resposta JSON com timeout e identificação do cliente."""
    request_headers = {"User-Agent": "JobScraperLocal/0.1 (personal job search)", "Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    request = Request(url, data=data, headers=request_headers, method="POST" if data is not None else "GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except URLError as exc:
        raise RuntimeError(f"Falha de rede ({exc.reason})") from exc
    if not raw.strip():
        return {}
    return json.loads(raw)


def fetch_remotive() -> list[dict]:
    """Adapta os anúncios do feed público Remotive ao formato comum do banco."""
    data = fetch_json("https://remotive.com/api/remote-jobs?category=software-dev&limit=100")
    results = []
    for item in data.get("jobs", []):
        results.append({"source": "Remotive", "source_id": str(item.get("id", "")), "title": item.get("title", ""), "company": item.get("company_name", ""), "location": item.get("candidate_required_location", "Remote"), "description": item.get("description", ""), "url": item.get("url", ""), "posted_at": item.get("publication_date")})
    return results


def fetch_remoteok() -> list[dict]:
    """Adapta o feed JSON do Remote OK ao formato comum do banco."""
    data = fetch_json("https://remoteok.com/api")
    results = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        results.append({"source": "Remote OK", "source_id": str(item.get("id")), "title": item.get("position", ""), "company": item.get("company", ""), "location": item.get("location", "Worldwide"), "description": item.get("description", ""), "url": item.get("url", ""), "posted_at": item.get("date")})
    return results


def fetch_adzuna() -> list[dict]:
    """Consulta Adzuna em países escolhidos; as credenciais vêm do cofre do Windows."""
    app_id, app_key = secret_get("adzuna_app_id"), secret_get("adzuna_app_key")
    if not app_id or not app_key:
        raise ValueError("Configure Adzuna app_id e app_key no painel.")
    cfg = settings()
    search_terms = terms(cfg.get("keywords", ""))[:2]
    countries = [code.strip().lower() for code in cfg.get("adzuna_countries", "br,us,gb,ca").split(",") if code.strip()]
    results = []
    for country in countries:
        for search in search_terms:
            query = f"https://api.adzuna.com/v1/api/jobs/{quote_plus(country)}/search/1?app_id={quote_plus(app_id)}&app_key={quote_plus(app_key)}&results_per_page=50&what={quote_plus(search)}&content-type=application/json"
            data = fetch_json(query)
            for item in data.get("results", []):
                results.append({"source": "Adzuna", "source_id": str(item.get("id", "")), "title": item.get("title", ""), "company": (item.get("company") or {}).get("display_name", ""), "location": (item.get("location") or {}).get("display_name", ""), "description": item.get("description", ""), "url": item.get("redirect_url", ""), "posted_at": item.get("created")})
    return results


APIFY_API = "https://api.apify.com/v2"
DEFAULT_APIFY_ACTORS: list[dict] = [
    {
        "id": "curious_coder~linkedin-jobs-scraper",
        "label": "LinkedIn Jobs",
        "enabled": True,
        "input_mode": "linkedin_search",
        "count": 25,
    }
]


def apify_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def apify_monthly_usage_usd(token: str) -> tuple[float, str]:
    """Consulta o uso do ciclo mensal atual da conta Apify (créditos em USD)."""
    payload = fetch_json(f"{APIFY_API}/users/me/usage/monthly", headers=apify_headers(token), timeout=30)
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    used = float(data.get("totalUsageCreditsUsdAfterVolumeDiscount") or data.get("totalUsageCreditsUsdBeforeVolumeDiscount") or 0)
    cycle = data.get("usageCycle") or {}
    label = format_cycle_range(cycle.get("startAt", ""), cycle.get("endAt", ""))
    return used, label


def load_apify_quota_snapshot() -> dict:
    from usage_tracker import APIFY_SNAPSHOT_KEY

    raw = (settings().get(APIFY_SNAPSHOT_KEY) or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def refresh_apify_quota_snapshot() -> dict:
    """Busca limites oficiais da Apify e grava cache em settings."""
    from usage_tracker import APIFY_SNAPSHOT_KEY, parse_apify_limits

    token = secret_get("apify_token")
    cfg = settings()
    try:
        local_limit = float(str(cfg.get("apify_monthly_credit_limit_usd", "5")).replace(",", "."))
    except ValueError:
        local_limit = 5.0
    if not token:
        snap = {
            "used_usd": 0.0,
            "api_limit_usd": 0.0,
            "local_limit_usd": local_limit,
            "cycle_label": "—",
            "pct_of_local": 0.0,
            "pct_of_api": 0.0,
            "fetched_at": "",
            "error": "Token Apify não configurado (IA & integrações).",
        }
        set_setting(APIFY_SNAPSHOT_KEY, json.dumps(snap, ensure_ascii=False))
        return snap
    try:
        payload = fetch_json(f"{APIFY_API}/users/me/limits", headers=apify_headers(token), timeout=30)
        snap = parse_apify_limits(payload if isinstance(payload, dict) else {}, local_limit_usd=local_limit)
        # Alinha também o last_usage legado usado pelo coletor.
        set_setting("apify_last_usage_usd", f"{snap['used_usd']:.4f}")
        set_setting("apify_last_usage_cycle", snap.get("cycle_label") or "")
    except Exception as exc:
        snap = {
            "used_usd": 0.0,
            "api_limit_usd": 0.0,
            "local_limit_usd": local_limit,
            "cycle_label": "—",
            "pct_of_local": 0.0,
            "pct_of_api": 0.0,
            "fetched_at": "",
            "error": f"Falha ao consultar Apify: {exc}",
        }
    set_setting(APIFY_SNAPSHOT_KEY, json.dumps(snap, ensure_ascii=False))
    return snap


def normalize_apify_actor_id(actor_id: str) -> str:
    raw = (actor_id or "").strip()
    if "/" in raw and "~" not in raw:
        owner, name = raw.split("/", 1)
        return f"{owner.strip()}~{name.strip()}"
    return raw


def load_apify_actors(cfg: dict[str, str] | None = None) -> list[dict]:
    cfg = cfg or settings()
    raw = (cfg.get("apify_actors_json") or "").strip()
    if not raw:
        return [dict(item) for item in DEFAULT_APIFY_ACTORS]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON de actors Apify inválido: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise ValueError("apify_actors_json deve ser uma lista JSON com pelo menos um actor.")
    actors: list[dict] = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise ValueError(f"Actor Apify #{idx + 1} deve ser um objeto JSON.")
        actor_id = normalize_apify_actor_id(str(item.get("id") or ""))
        if not actor_id:
            raise ValueError(f"Actor Apify #{idx + 1} sem id.")
        label = str(item.get("label") or actor_id).strip()
        enabled = bool(item.get("enabled", True))
        mode = str(item.get("input_mode") or "linkedin_search").strip().casefold()
        if mode not in {"linkedin_search", "custom"}:
            mode = "custom" if item.get("input_template") or item.get("input") else "linkedin_search"
        try:
            count = max(1, min(100, int(item.get("count") or cfg.get("apify_job_count", "25") or 25)))
        except (TypeError, ValueError):
            count = 25
        template = item.get("input_template") if "input_template" in item else item.get("input")
        actors.append(
            {
                "id": actor_id,
                "label": label,
                "enabled": enabled,
                "input_mode": mode,
                "count": count,
                "input_template": template if isinstance(template, (dict, list)) else None,
            }
        )
    return actors


def _substitute_apify_value(value: object, ctx: dict[str, str]) -> object:
    if isinstance(value, str):
        out = value
        for key, replacement in ctx.items():
            out = out.replace("{{" + key + "}}", replacement)
        return out
    if isinstance(value, list):
        return [_substitute_apify_value(item, ctx) for item in value]
    if isinstance(value, dict):
        return {str(k): _substitute_apify_value(v, ctx) for k, v in value.items()}
    return value


# LinkedIn Jobs search: f_E = experience, f_WT = workplace type, f_TPR = time posted.
LINKEDIN_EXPERIENCE_CODES = {1, 2, 3, 4, 5, 6}
LINKEDIN_WORKPLACE_CODES = {1, 2, 3}  # 1 on-site, 2 remote, 3 hybrid
#: Past 24 hours — máxima prioridade (vagas do mesmo dia).
LINKEDIN_TPR_DAY = "r86400"
#: Past week — teto duro de 7 dias (nunca buscar mais antigo que isso).
LINKEDIN_TPR_WEEK = "r604800"
LINKEDIN_MAX_AGE_DAYS = 7
#: Quantos keywords a IA pode gravar / usar nas URLs (antes era 4–5 e estreita demais).
LINKEDIN_FILTER_MAX_KEYWORDS = 12
LINKEDIN_FILTER_MAX_COUNT = 12
LINKEDIN_URL_MAX_KEYWORDS = 8
LINKEDIN_SEARCH_MAX_URLS = 8

# Placeholders obrigatórios no texto editável: {pref_keywords} {facts_pt} {facts_en} {resume_pt} {resume_en}
DEFAULT_LINKEDIN_FILTER_PROMPT = """You design LinkedIn Jobs search filters for Apify (curious_coder linkedin-jobs-scraper).
The scraper takes LinkedIn search URLs. Cover the BREADTH of THIS candidate's real skills and roles — do NOT collapse to only 4–5 trendy skills.

LinkedIn experience codes (f_E): 1 Internship, 2 Entry, 3 Associate, 4 Mid-Senior, 5 Director, 6 Executive.
LinkedIn workplace codes (f_WT): 1 On-site, 2 Remote, 3 Hybrid.

GEO / WORKPLACE RULES (mandatory — override panel location preferences):
- Do NOT prioritize Brazil, Brasil, or Brazilian cities as the main search target.
- Remote (f_WT=2) is the PRIMARY target: the candidate accepts remote work from ANYWHERE worldwide.
- For remote searches, prefer location strings like "Remote", "Worldwide", "United States", "Europe", or leave broad global remote — NOT "Brazil" as the default.
- Hybrid (f_WT=3) is allowed ONLY when paired with location "Belo Horizonte" (or "Belo Horizonte, Minas Gerais" / "Belo Horizonte, Brazil").
- Do NOT use f_WT=1 (On-site) unless the resume explicitly requires it (default: omit on-site).
- Typical good combo: workplace_types [2, 3] with locations including "Remote" and "Belo Horizonte" (hybrid applies to BH only in intent).
- Never make "Brazil" the only or primary location.

KEYWORD BREADTH (mandatory):
- Produce 6–12 distinct job-search phrases spread across DIFFERENT skills, stacks, domains, and role titles from the resume. Organize them into 4–8 separate filters, each with only 1–3 related phrases. Each filter is sent as its own Apify run, so cover different role/skill combinations without duplicating them.
- One solid skill match is enough to justify a keyword. Do NOT require the intersection of many skills; e.g. include separate phrases for Python, React, SQL, DevOps, etc. when present on the resume.
- Prefer English for global remote reach; PT only if clearly Brazil-hybrid BH search.
- Market demand may influence ORDER, but NEVER drop a real resume skill just because it is less trendy.
- Do NOT invent employers or skills; only use the resume summaries and facts.

Other rules:
- Prefer 2–4 experience codes centered on the candidate's seniority (usually one level below + main; avoid Director/Executive unless clearly supported).
- RECENCY (mandatory — the URL builder enforces this; mention it in reason):
  * Only consider jobs from the last 7 days (LinkedIn f_TPR=r604800).
  * Strongly prioritize jobs posted today / last 24 hours (f_TPR=r86400) over older ones in the week.
- OPEN JOBS ONLY (intent): design searches for roles that still accept applications. Prefer fresher postings; never aim for filled/closed/expired listings.
- Return ONLY JSON with key filters, an array of 4–8 objects. Each object has:
  name (short unique label in Portuguese),
  keywords (1–3 related search phrases),
  locations (string array),
  experience_levels (integer array of f_E codes),
  workplace_types (integer array of f_WT codes),
  reason (short string in Portuguese explaining the role/skills covered + remote global + hybrid BH + recency).

Panel keyword preferences (roles/skills only — ignore geo bias here): {pref_keywords}
Candidate facts PT: {facts_pt}
Candidate facts EN: {facts_en}
Resume summary PT: {resume_pt}
Resume summary EN: {resume_en}
"""


def effective_linkedin_filter_prompt(cfg: dict[str, str] | None = None) -> str:
    """Prompt efetivo: custom do painel, senão o default amplo."""
    cfg = cfg or {}
    custom = str(cfg.get("linkedin_filter_prompt") or "").strip()
    return custom if custom else DEFAULT_LINKEDIN_FILTER_PROMPT


def render_linkedin_filter_prompt(
    template: str,
    *,
    pref_keywords: str,
    facts_pt: str,
    facts_en: str,
    resume_pt: str,
    resume_en: str,
) -> str:
    """Substitui placeholders sem .format() (o template pode ter chaves JSON)."""
    pairs = {
        "pref_keywords": pref_keywords or "[none]",
        "facts_pt": facts_pt or "[none]",
        "facts_en": facts_en or "[none]",
        "resume_pt": resume_pt or "[none]",
        "resume_en": resume_en or "[none]",
    }
    out = template or DEFAULT_LINKEDIN_FILTER_PROMPT
    for key, value in pairs.items():
        out = out.replace("{" + key + "}", value)
    return out


def _resume_filter_fingerprint(cfg: dict[str, str] | None = None) -> str:
    cfg = cfg or settings()
    prompt = effective_linkedin_filter_prompt(cfg)
    with connect() as db:
        summaries = resume_summaries(db)
        rows = db.execute(
            "SELECT language, analyzed_at, file_sha256 FROM resumes ORDER BY language"
        ).fetchall()
    blob = json.dumps(
        {
            "summaries": summaries,
            "prompt": prompt,
            "preferred_keywords": cfg.get("keywords", ""),
            "candidate_facts_pt": cfg.get("candidate_facts_pt", ""),
            "candidate_facts_en": cfg.get("candidate_facts_en", ""),
            "meta": [
                {
                    "language": r["language"],
                    "analyzed_at": r["analyzed_at"],
                    "sha": r["file_sha256"],
                }
                for r in rows
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def _fallback_linkedin_filters(cfg: dict[str, str]) -> list[dict]:
    keywords = terms(cfg.get("keywords", "")) or ["software engineer"]
    groups = [keywords[i : i + 2] for i in range(0, min(len(keywords), 12), 2)] or [["software engineer"]]
    return [
        {
            "id": f"fallback-{i + 1}",
            "name": f"Busca {i + 1}: {', '.join(group[:2])}",
            "enabled": True,
            "keywords": group,
            "locations": ["Remote", "Belo Horizonte"],
            "experience_levels": [3, 4],
            "workplace_types": [2, 3],
            "reason": "Filtro alternativo baseado nas preferências, remoto global e híbrido em Belo Horizonte.",
            "source": "fallback",
        }
        for i, group in enumerate(groups[:LINKEDIN_FILTER_MAX_COUNT])
    ]


def _fallback_linkedin_filter(cfg: dict[str, str]) -> dict:
    return _fallback_linkedin_filters(cfg)[0]


def _normalize_linkedin_filter(raw: dict, cfg: dict[str, str]) -> dict:
    fallback = _fallback_linkedin_filter(cfg)
    keywords = raw.get("keywords") if isinstance(raw.get("keywords"), list) else []
    keywords = [str(k).strip() for k in keywords if str(k).strip()][:LINKEDIN_FILTER_MAX_KEYWORDS]
    if not keywords:
        keywords = list(fallback["keywords"])
    locations = raw.get("locations") if isinstance(raw.get("locations"), list) else []
    locations = [str(x).strip() for x in locations if str(x).strip()][:4]
    if not locations:
        locations = list(fallback["locations"])
    experience_raw = raw.get("experience_levels")
    experience = []
    for item in (experience_raw if isinstance(experience_raw, list) else fallback["experience_levels"]):
        try:
            code = int(item)
        except (TypeError, ValueError):
            continue
        if code in LINKEDIN_EXPERIENCE_CODES and code not in experience:
            experience.append(code)
    workplace_raw = raw.get("workplace_types")
    workplace = []
    for item in (workplace_raw if isinstance(workplace_raw, list) else fallback["workplace_types"]):
        try:
            code = int(item)
        except (TypeError, ValueError):
            continue
        if code in LINKEDIN_WORKPLACE_CODES and code not in workplace:
            workplace.append(code)
    reason = str(raw.get("reason") or "").strip()[:400]
    out = {
        "keywords": keywords,
        "locations": locations,
        "experience_levels": experience,
        "workplace_types": workplace,
        "reason": reason or fallback["reason"],
        "source": str(raw.get("source") or "ai"),
    }
    name = str(raw.get("name") or "").strip()[:80]
    if name:
        out["name"] = name
    return out


def _normalize_linkedin_filters(
    raw: object,
    cfg: dict[str, str],
    *,
    fallback_if_empty: bool = True,
) -> list[dict]:
    if isinstance(raw, dict):
        candidates = raw.get("filters") if isinstance(raw.get("filters"), list) else [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        candidates = []
    filters: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(candidates[:LINKEDIN_FILTER_MAX_COUNT]):
        if not isinstance(item, dict):
            continue
        normalized = _normalize_linkedin_filter(item, cfg)
        signature = json.dumps(
            {key: normalized[key] for key in ("keywords", "locations", "experience_levels", "workplace_types")},
            ensure_ascii=False,
            sort_keys=True,
        )
        if signature in seen:
            continue
        seen.add(signature)
        stable_id = hashlib.sha256(f"{index}:{signature}".encode("utf-8")).hexdigest()[:12]
        name = normalized.get("name") or f"Busca {index + 1}"
        filters.append({
            **normalized,
            "id": str(item.get("id") or f"li-{stable_id}")[:64],
            "name": name,
            "enabled": bool(item.get("enabled", True)),
        })
    return filters or (_fallback_linkedin_filters(cfg) if fallback_if_empty else [])


def _validate_linkedin_filter_form(raw: str, cfg: dict[str, str]) -> str:
    try:
        submitted = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON dos filtros LinkedIn inválido: {exc}") from exc
    if not isinstance(submitted, list) or len(submitted) > LINKEDIN_FILTER_MAX_COUNT:
        raise ValueError(f"Use uma lista com até {LINKEDIN_FILTER_MAX_COUNT} filtros LinkedIn.")
    for index, item in enumerate(submitted, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Filtro #{index} inválido.")
        if not str(item.get("name") or "").strip():
            raise ValueError(f"Dê um nome ao filtro #{index}.")
        keywords = item.get("keywords")
        if not isinstance(keywords, list) or not any(str(x).strip() for x in keywords):
            raise ValueError(f"Adicione ao menos uma keyword ao filtro #{index}.")
    filters = _normalize_linkedin_filters(submitted, cfg, fallback_if_empty=False)
    return json.dumps(filters, ensure_ascii=False)


def ai_generate_linkedin_search_filters(cfg: dict[str, str]) -> list[dict]:
    """Pede à IA um conjunto de filtros LinkedIn independentes com base no currículo."""
    with connect() as db:
        summaries = resume_summaries(db)
    resume_pt = (summaries.get("pt") or "").strip()
    resume_en = (summaries.get("en") or "").strip()
    if not resume_pt and not resume_en:
        raise ValueError("Analise um currículo antes de gerar o filtro LinkedIn/Apify.")

    facts_pt = (cfg.get("candidate_facts_pt") or "").strip()
    facts_en = (cfg.get("candidate_facts_en") or "").strip()
    pref_keywords = cfg.get("keywords", "")
    provider = cfg.get("ai_provider", "gemini").casefold()
    model = cfg.get("ai_model", "gemini-2.5-flash").strip()
    api_key = get_ai_key(provider)
    if not api_key:
        raise AiUnavailableError("Configure a chave da API de IA para gerar o filtro LinkedIn.")

    prompt = render_linkedin_filter_prompt(
        effective_linkedin_filter_prompt(cfg),
        pref_keywords=pref_keywords,
        facts_pt=facts_pt,
        facts_en=facts_en,
        resume_pt=resume_pt[:6000],
        resume_en=resume_en[:6000],
    )
    prompt += (
        "\n\nMANDATORY OUTPUT OVERRIDE: Return JSON exactly as "
        '{"filters":[{"name":"...","keywords":["..."],"locations":["..."],'
        '"experience_levels":[3,4],"workplace_types":[2,3],"reason":"..."}]}. '
        "Create multiple independent filters (ideally 4–8), with only 1–3 related keywords per filter."
    )
    try:
        if provider == "openai":
            endpoint = "https://api.openai.com/v1/responses"
            payload = json.dumps(
                {
                    "model": model,
                    "input": prompt,
                    "text": {"format": {"type": "json_object"}},
                    "store": False,
                    "max_output_tokens": 1800,
                }
            ).encode("utf-8")
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        elif provider == "gemini":
            endpoint = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{quote_plus(model)}:generateContent?key={quote_plus(api_key)}"
            )
            payload = json.dumps(
                {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"maxOutputTokens": 1800, "responseMimeType": "application/json"},
                }
            ).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        else:
            raise ValueError("Provedor de IA inválido.")
        request = Request(endpoint, data=payload, headers=headers, method="POST")
        with urlopen(request, timeout=60) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        quota = exc.code in {401, 403, 429} or "quota" in body.casefold()
        _record_ai_usage(provider, model, None, ok=False, quota_error=quota)
        if quota:
            raise AiUnavailableError(f"IA indisponível ao gerar filtro LinkedIn (HTTP {exc.code}).") from exc
        raise
    except URLError as exc:
        _record_ai_usage(provider, model, None, ok=False, quota_error=True)
        raise AiUnavailableError(f"IA indisponível ao gerar filtro LinkedIn: {exc}") from exc

    _record_ai_usage(provider, model, result if isinstance(result, dict) else None, ok=True, quota_error=False)
    if provider == "openai":
        raw_text = "\n".join(
            part.get("text", "")
            for item in result.get("output", [])
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        )
    else:
        raw_text = "\n".join(
            part.get("text", "")
            for item in result.get("candidates", [])
            for part in item.get("content", {}).get("parts", [])
        )
    decision = json.loads(raw_text.strip())
    if not isinstance(decision, dict):
        raise ValueError("Resposta da IA para filtro LinkedIn inválida.")
    raw_filters = decision.get("filters") if isinstance(decision.get("filters"), list) else [decision]
    expanded_filters: list[dict] = []
    for index, item in enumerate(raw_filters):
        if not isinstance(item, dict):
            continue
        item_keywords = item.get("keywords") if isinstance(item.get("keywords"), list) else []
        groups = [item_keywords[i : i + 3] for i in range(0, len(item_keywords), 3)]
        if len(groups) > 1:
            for part, group in enumerate(groups, start=1):
                expanded_filters.append({
                    **item,
                    "name": f"{item.get('name') or f'Busca {index + 1}'} {part}",
                    "keywords": group,
                })
        else:
            expanded_filters.append(item)
    if not expanded_filters:
        raise ValueError("A IA não retornou nenhum filtro LinkedIn válido.")
    filters = _normalize_linkedin_filters(expanded_filters, cfg)
    for index, item in enumerate(filters, start=1):
        item["source"] = "ai"
        item["id"] = str(item.get("id") or f"ai-{index}")
    return filters


def get_linkedin_search_filters(cfg: dict[str, str], *, force_refresh: bool = False) -> list[dict]:
    """Retorna filtros em cache ou gera uma coleção quando currículo/prompt mudarem."""
    fingerprint = _resume_filter_fingerprint(cfg)
    cached_hash = (cfg.get("apify_linkedin_filter_hash") or "").strip()
    cached_raw = (cfg.get("apify_linkedin_filters_json") or "").strip()
    if not force_refresh and cached_hash == fingerprint and cached_raw:
        try:
            filters = _normalize_linkedin_filters(
                json.loads(cached_raw), cfg, fallback_if_empty=False
            )
            return filters
        except json.JSONDecodeError:
            pass

    legacy_filter: dict | None = None
    if not cached_raw:
        try:
            old = json.loads((cfg.get("apify_linkedin_filter_json") or "").strip() or "null")
            if isinstance(old, dict):
                legacy_filter = old
        except json.JSONDecodeError:
            pass
    try:
        generated = ai_generate_linkedin_search_filters(cfg)
    except (AiUnavailableError, ValueError, json.JSONDecodeError) as exc:
        log_event("warning", "apify", f"Filtros LinkedIn via IA indisponíveis ({exc}); usando fallback.")
        if legacy_filter:
            generated = _normalize_linkedin_filters([legacy_filter], cfg)
            generated[0]["source"] = "legacy"
        else:
            generated = _fallback_linkedin_filters(cfg)

    old_enabled: dict[str, bool] = {}
    if cached_raw:
        try:
            old_enabled = {
                str(item.get("name") or "").strip().casefold(): bool(item.get("enabled", True))
                for item in _normalize_linkedin_filters(json.loads(cached_raw), cfg)
            }
        except json.JSONDecodeError:
            pass
    if old_enabled:
        for item in generated:
            if item["name"].casefold() in old_enabled:
                item["enabled"] = old_enabled[item["name"].casefold()]
    payload = _normalize_linkedin_filters(generated, cfg)
    set_setting("apify_linkedin_filters_json", json.dumps(payload, ensure_ascii=False))
    set_setting("apify_linkedin_filter_hash", fingerprint)
    log_event(
        "info",
        "apify",
        f"{len(payload)} filtros LinkedIn disponíveis; "
        f"{sum(1 for item in payload if item['enabled'])} ativos.",
    )
    return payload


def get_linkedin_search_filter(cfg: dict[str, str], *, force_refresh: bool = False) -> dict:
    """Compatibilidade dos placeholders customizados: usa o primeiro filtro ativo."""
    filters = get_linkedin_search_filters(cfg, force_refresh=force_refresh)
    return next((item for item in filters if item.get("enabled")), filters[0] if filters else _fallback_linkedin_filter(cfg))


def build_linkedin_search_urls(filter_data: dict, *, max_urls: int = 4) -> list[str]:
    """Monta URLs de busca LinkedIn com recência obrigatória.

    Toda URL leva ``f_TPR``: metade (arredondando pra cima) prioriza o mesmo dia
    (``r86400``); o restante completa a janela de 7 dias (``r604800``). Nunca
    busca sem teto de tempo — evita vagas velhas no scrape.
    """
    keywords = [str(k) for k in (filter_data.get("keywords") or []) if str(k).strip()][:LINKEDIN_URL_MAX_KEYWORDS]
    locations = [str(x) for x in (filter_data.get("locations") or []) if str(x).strip()][:4]
    if not keywords:
        keywords = ["software engineer"]
    if not locations:
        locations = ["Remote", "Belo Horizonte"]
    experience = [int(x) for x in (filter_data.get("experience_levels") or []) if int(x) in LINKEDIN_EXPERIENCE_CODES]
    workplace = [int(x) for x in (filter_data.get("workplace_types") or []) if int(x) in LINKEDIN_WORKPLACE_CODES]
    f_e = "%2C".join(str(x) for x in experience) if experience else ""

    def _is_bh(loc: str) -> bool:
        low = loc.casefold()
        return "belo horizonte" in low or low in {"bh", "bh, mg", "bh - mg"}

    def _is_remote_loc(loc: str) -> bool:
        low = loc.casefold()
        return any(
            token in low
            for token in ("remote", "remoto", "worldwide", "anywhere", "global", "europe", "united states", "usa", "eua")
        )

    # Pares intencionais: remote → qualquer lugar remoto; hybrid → só BH.
    pairs: list[tuple[str, str]] = []
    if 2 in workplace:
        remote_locs = [loc for loc in locations if _is_remote_loc(loc)] or ["Remote"]
        for loc in remote_locs[:2]:
            pairs.append((loc, "2"))
    if 3 in workplace:
        bh_locs = [loc for loc in locations if _is_bh(loc)] or ["Belo Horizonte"]
        for loc in bh_locs[:1]:
            pairs.append((loc, "3"))
    if 1 in workplace and not pairs:
        pairs.append((locations[0], "1"))
    if not workplace:
        pairs = [(loc, "") for loc in locations]
    elif not pairs:
        pairs = [("Remote", "2"), ("Belo Horizonte", "3")]

    def _url(keyword: str, location: str, f_wt: str, tpr: str) -> str:
        url = (
            f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(keyword)}"
            f"&location={quote_plus(location)}&position=1&pageNum=0"
            f"&f_TPR={tpr}"
        )
        if f_e:
            url += f"&f_E={f_e}"
        if f_wt:
            url += f"&f_WT={f_wt}"
        return url

    day_urls: list[str] = []
    week_urls: list[str] = []
    for keyword in keywords:
        for location, f_wt in pairs:
            day_urls.append(_url(keyword, location, f_wt, LINKEDIN_TPR_DAY))
            week_urls.append(_url(keyword, location, f_wt, LINKEDIN_TPR_WEEK))

    # Prioridade: mesmo dia primeiro (≥ metade dos slots); resto ≤7 dias.
    day_slots = max(1, (max_urls + 1) // 2)
    urls: list[str] = []
    for u in day_urls[:day_slots]:
        if u not in urls:
            urls.append(u)
        if len(urls) >= max_urls:
            return urls
    for u in week_urls:
        if u not in urls:
            urls.append(u)
        if len(urls) >= max_urls:
            return urls
    for u in day_urls[day_slots:]:
        if u not in urls:
            urls.append(u)
        if len(urls) >= max_urls:
            return urls
    return urls


def build_apify_run_input(
    actor: dict,
    cfg: dict[str, str],
    *,
    linkedin_filter: dict | None = None,
) -> dict:
    keywords = terms(cfg.get("keywords", ""))[:3] or ["software engineer"]
    locations = terms(cfg.get("locations", ""))[:3] or ["remote"]
    count = int(actor.get("count") or 25)
    mode = str(actor.get("input_mode") or "linkedin_search")
    if mode == "linkedin_search":
        filter_data = linkedin_filter or get_linkedin_search_filter(cfg)
        urls = build_linkedin_search_urls(filter_data, max_urls=LINKEDIN_SEARCH_MAX_URLS)
        return {"urls": urls, "count": count, "scrapeCompany": False}

    template = actor.get("input_template")
    if not isinstance(template, (dict, list)):
        raise ValueError(
            f"Actor {actor.get('label')} em modo custom precisa de input_template (objeto JSON)."
        )
    # Prefer AI LinkedIn filter keywords when available for template placeholders.
    try:
        linkedin_filter = get_linkedin_search_filter(cfg)
        keywords = list(linkedin_filter.get("keywords") or keywords)[:3] or keywords
        locations = list(linkedin_filter.get("locations") or locations)[:3] or locations
    except Exception:
        pass
    ctx = {
        "count": str(count),
        "keywords": ", ".join(keywords),
        "locations": ", ".join(locations),
        "keyword": keywords[0],
        "location": locations[0],
        "keyword0": keywords[0],
        "keyword1": keywords[1] if len(keywords) > 1 else keywords[0],
        "location0": locations[0],
        "location1": locations[1] if len(locations) > 1 else locations[0],
    }
    built = _substitute_apify_value(template, ctx)
    if isinstance(built, list):
        return {"items": built}
    if not isinstance(built, dict):
        raise ValueError(f"input_template do actor {actor.get('label')} deve resultar em objeto JSON.")
    return built


def _apify_item_url(item: dict) -> str:
    """Extrai URL aplicável de payloads LinkedIn/Indeed (incl. viewJobLink relativo)."""
    candidates = [
        item.get("originalApplyUrl"),
        item.get("link"),
        item.get("url"),
        item.get("applyUrl"),
        item.get("jobUrl"),
        item.get("externalApplyLink"),
        item.get("viewJobLink"),
        item.get("jobLink"),
    ]
    for raw in candidates:
        url = str(raw or "").strip()
        if not url:
            continue
        if url.startswith("http://") or url.startswith("https://"):
            return url
        if url.startswith("/"):
            base = ""
            for key in ("companyOverviewLink", "companyUrl", "url", "link"):
                hint = str(item.get(key) or "").strip()
                if hint.startswith("http://") or hint.startswith("https://"):
                    parsed = urlparse(hint)
                    if parsed.scheme and parsed.netloc:
                        base = f"{parsed.scheme}://{parsed.netloc}"
                        break
            if not base:
                # Indeed default quando só vem path relativo.
                base = "https://www.indeed.com"
            return base.rstrip("/") + url
    return ""


def _linkedin_posted_age_days(posted_at: object) -> float | None:
    """Idade em dias da postagem, ou None se não der pra inferir.

    Aceita ISO / timestamp e textos relativos comuns do LinkedIn
    ('Just now', '2 hours ago', '3 days ago', '1 week ago', ...).
    """
    if posted_at is None or posted_at == "":
        return None
    if isinstance(posted_at, (int, float)):
        ts = float(posted_at)
        if ts > 10_000_000_000:
            ts /= 1000.0
        try:
            when = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return max(0.0, (datetime.now(timezone.utc) - when).total_seconds() / 86400.0)

    raw = str(posted_at).strip()
    parsed = parse_utc(raw)
    if parsed is not None:
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds() / 86400.0)

    low = raw.casefold()
    if any(tok in low for tok in ("just now", "agora", "moments ago", "seconds ago", "second ago")):
        return 0.0
    m = re.search(r"(\d+)\s*(minute|minutos?|hour|horas?|day|dias?|week|semanas?|month|meses?)", low)
    if not m:
        m2 = re.search(r"(\d+)\s*([mhdw])\b", low)
        if not m2:
            return None
        n = int(m2.group(1))
        unit = m2.group(2)
        if unit == "m":
            return n / (60 * 24)
        if unit == "h":
            return n / 24.0
        if unit == "d":
            return float(n)
        if unit == "w":
            return float(n * 7)
        return None
    n = int(m.group(1))
    unit = m.group(2)
    if unit.startswith("min"):
        return n / (60 * 24)
    if unit.startswith("hour") or unit.startswith("hora"):
        return n / 24.0
    if unit.startswith("day") or unit.startswith("dia"):
        return float(n)
    if unit.startswith("week") or unit.startswith("semana"):
        return float(n * 7)
    if unit.startswith("month") or unit.startswith("mes"):
        return float(n * 30)
    return None


def _linkedin_within_max_age(posted_at: object, *, max_days: int = LINKEDIN_MAX_AGE_DAYS) -> bool:
    """True se a vaga cabe na janela (ou a data é desconhecida — não descartamos)."""
    age = _linkedin_posted_age_days(posted_at)
    if age is None:
        return True
    return age <= float(max_days)


def normalize_apify_items(items: list, *, label: str) -> list[dict]:
    """Normaliza itens Apify e descarta vagas já fechadas (filtro de coleta)."""
    from worth_reverify import apify_item_indicates_closed

    results: list[dict] = []
    source_name = f"Apify:{label}" if label else "Apify"
    is_linkedin = "linkedin" in (label or "").casefold()
    skipped_closed = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        if apify_item_indicates_closed(item):
            skipped_closed += 1
            continue
        url = _apify_item_url(item)
        title = str(item.get("title") or item.get("position") or item.get("jobTitle") or item.get("displayTitle") or "").strip()
        if not url or not title:
            continue
        description = str(
            item.get("descriptionText")
            or item.get("descriptionHtml")
            or item.get("description")
            or item.get("jobDescription")
            or item.get("jobDescriptionHTML")
            or ""
        )
        company = item.get("companyName") or item.get("company") or item.get("companyDetails") or item.get("jobSourceName") or {}
        if isinstance(company, dict):
            company = company.get("name") or company.get("display_name") or ""
        location = (
            item.get("formattedLocation")
            or item.get("location")
            or item.get("jobLocation")
            or ""
        )
        if isinstance(location, dict):
            formatted = location.get("formatted") or {}
            if isinstance(formatted, dict):
                location = (
                    formatted.get("long")
                    or formatted.get("short")
                    or location.get("fullAddress")
                    or location.get("display_name")
                    or location.get("name")
                    or ""
                )
            else:
                location = (
                    location.get("fullAddress")
                    or location.get("display_name")
                    or location.get("name")
                    or ""
                )
        if not location:
            city = str(item.get("jobLocationCity") or "").strip()
            state = str(item.get("jobLocationState") or "").strip()
            location = ", ".join(p for p in (city, state) if p)
        posted_at = item.get("postedAt") or item.get("publishedAt") or item.get("date") or item.get("pubDate")
        if isinstance(posted_at, (int, float)) and posted_at > 10_000_000_000:
            # Indeed pubDate em milissegundos.
            posted_at = datetime.fromtimestamp(posted_at / 1000, tz=timezone.utc).isoformat(timespec="seconds")
        if is_linkedin and not _linkedin_within_max_age(posted_at):
            continue  # teto duro: >7 dias fora
        results.append(
            {
                "source": source_name,
                "source_id": str(item.get("id") or item.get("jobId") or url),
                "title": title,
                "company": str(company or ""),
                "location": str(location or ""),
                "description": description,
                "url": url,
                "posted_at": posted_at,
            }
        )
    if skipped_closed:
        log_event(
            "info",
            "apify",
            f"{label or 'Apify'}: {skipped_closed} vaga(s) descartada(s) no filtro (fechadas / sem candidaturas).",
        )
    if is_linkedin and results:
        # Prioriza o mais recente (mesmo dia primeiro) na fila de inserção/triagem.
        def _sort_key(job: dict) -> tuple:
            age = _linkedin_posted_age_days(job.get("posted_at"))
            return (age if age is not None else 3.5, str(job.get("title") or ""))

        results.sort(key=_sort_key)
    return results


def run_apify_actor(
    token: str,
    actor: dict,
    cfg: dict[str, str],
    *,
    linkedin_filter: dict | None = None,
) -> list[dict]:
    actor_id = normalize_apify_actor_id(str(actor["id"]))
    label = str(actor.get("label") or actor_id)
    run_input = build_apify_run_input(actor, cfg, linkedin_filter=linkedin_filter)
    log_event("info", "apify", f"Iniciando actor {label} ({actor_id}).")
    started = fetch_json(
        f"{APIFY_API}/actors/{quote_plus(actor_id)}/runs",
        data=json.dumps(run_input).encode("utf-8"),
        headers=apify_headers(token),
        timeout=45,
    )
    run = started.get("data", started) if isinstance(started, dict) else {}
    run_id = str(run.get("id") or "")
    dataset_id = str(run.get("defaultDatasetId") or "")
    if not run_id:
        raise RuntimeError(f"Apify ({label}) não retornou o identificador da execução.")
    deadline = time.time() + 180
    status = str(run.get("status") or "READY")
    while status in {"READY", "RUNNING"} and time.time() < deadline:
        if collector.stop_event.is_set():
            raise RuntimeError(f"Coleta interrompida durante Apify ({label}).")
        time.sleep(5)
        progress = fetch_json(f"{APIFY_API}/actor-runs/{quote_plus(run_id)}", headers=apify_headers(token), timeout=30)
        run = progress.get("data", progress) if isinstance(progress, dict) else {}
        status = str(run.get("status") or status)
        dataset_id = str(run.get("defaultDatasetId") or dataset_id)
    if status != "SUCCEEDED":
        raise RuntimeError(f"Apify ({label}) encerrada com status {status}.")
    if not dataset_id:
        raise RuntimeError(f"Apify ({label}) não produziu dataset.")
    items = fetch_json(f"{APIFY_API}/datasets/{quote_plus(dataset_id)}/items?clean=1", headers=apify_headers(token), timeout=45)
    if not isinstance(items, list):
        items = []
    normalized = normalize_apify_items(items, label=label)
    log_event("success", "apify", f"Actor {label}: {len(normalized)} vaga(s) úteis.")
    return normalized


def iter_apify_jobs():
    """Run enabled actors; LinkedIn actors run once for each enabled filter."""
    token = secret_get("apify_token")
    if not token:
        raise ValueError("Configure o token da Apify no painel.")
    cfg = settings()
    try:
        limit = float(str(cfg.get("apify_monthly_credit_limit_usd", "5")).replace(",", "."))
    except ValueError:
        limit = 5.0
    if limit <= 0:
        raise ValueError("Limite mensal de créditos Apify está em zero; a fonte não será consultada.")
    used, cycle_label = apify_monthly_usage_usd(token)
    set_setting("apify_last_usage_usd", f"{used:.4f}")
    set_setting("apify_last_usage_cycle", cycle_label)
    if used >= limit:
        raise ValueError(f"Limite mensal de créditos Apify atingido ({used:.2f} USD de {limit:.2f} USD no ciclo {cycle_label}).")

    actors = [actor for actor in load_apify_actors(cfg) if actor.get("enabled", True)]
    if not actors:
        raise ValueError("Nenhum actor Apify habilitado em apify_actors_json.")
    linkedin_filters: list[dict] = []
    if any(actor.get("input_mode") == "linkedin_search" for actor in actors):
        linkedin_filters = [
            item for item in get_linkedin_search_filters(cfg) if item.get("enabled", True)
        ]
        if not linkedin_filters:
            log_event("info", "apify", "Todos os filtros LinkedIn estão desativados; nenhum run LinkedIn será iniciado.")

    results: list[dict] = []
    errors: list[str] = []
    budget_exhausted = False
    for actor in actors:
        if collector.stop_event.is_set():
            break
        actor_runs = (
            [(actor, item) for item in linkedin_filters]
            if actor.get("input_mode") == "linkedin_search"
            else [(actor, None)]
        )
        for run_actor, linkedin_filter in actor_runs:
            if collector.stop_event.is_set():
                break
            run_label = str(actor.get("label") or actor["id"])
            if linkedin_filter:
                run_actor = {**actor, "label": f"{run_label} — {linkedin_filter['name']}"}
                run_label = run_actor["label"]
            # Re-check the configured monthly credit ceiling before every run.
            try:
                used_now, _ = apify_monthly_usage_usd(token)
                if used_now >= limit:
                    errors.append(f"{run_label}: limite de créditos atingido antes da execução")
                    budget_exhausted = True
                    break
            except Exception:
                pass
            try:
                batch = run_apify_actor(token, run_actor, cfg, linkedin_filter=linkedin_filter)
                results.extend(batch)
            except Exception as exc:
                LOG.exception("Falha no actor Apify %s", run_label)
                errors.append(f"{run_label}: {exc}")
                log_event("error", "apify", f"Falha no actor {run_label}: {exc}")
            else:
                yield from batch
        if budget_exhausted:
            break
    try:
        used_after, cycle_after = apify_monthly_usage_usd(token)
        set_setting("apify_last_usage_usd", f"{used_after:.4f}")
        set_setting("apify_last_usage_cycle", cycle_after)
    except Exception:
        pass
    if errors and not results:
        raise RuntimeError("Todos os actors Apify falharam: " + "; ".join(errors))
    if errors:
        log_event("warning", "apify", "Alguns actors falharam: " + "; ".join(errors))


def fetch_apify() -> list[dict]:
    return list(iter_apify_jobs())


PROVIDERS = {"remotive": fetch_remotive, "remoteok": fetch_remoteok, "adzuna": fetch_adzuna, "apify": iter_apify_jobs}


class Collector:
    """Executa uma única consulta por clique; uma nova coleta exige novo clique."""
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.state = "stopped"
        self.message = "Coleta parada"
        self.last_run: str | None = None
        self.last_found = 0
        self.next_run_at: str | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "state": self.state,
                "message": self.message,
                "last_run": self.last_run,
                "last_found": self.last_found,
                "next_run_at": self.next_run_at,
            }

    def start(self) -> bool:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            self.stop_event.clear()
            self.state = "running"
            self.message = "Iniciando busca…"
            self.next_run_at = None
            self.thread = threading.Thread(target=self._loop, daemon=True, name="job-collector")
            self.thread.start()
            log_event("success", "collector", "Bot de coleta iniciado.")
            return True

    def _set_progress(self, message: str, found: int | None = None) -> None:
        with self.lock:
            self.message = message
            if found is not None:
                self.last_found = found

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            if self.state == "running":
                self.state = "stopping"
                self.message = "Parando após a busca atual…"
                log_event("info", "collector", "Parada solicitada; aguardando fim da busca atual.")

    def _loop(self) -> None:
        """Roda um único ciclo e finaliza; não agenda novas buscas automaticamente."""
        try:
            with self.lock:
                self.next_run_at = None
            self._run_once()
        except Exception as exc:
            LOG.exception("Falha no ciclo de coleta")
            self._set_progress(f"Coleta encerrada com erro: {exc.__class__.__name__}.")
            log_event("error", "collector", f"Falha no ciclo: {exc}")
        finally:
            with self.lock:
                self.state = "stopped"
                self.next_run_at = None
                if self.stop_event.is_set():
                    self.message = "Coleta interrompida. Clique em Iniciar para executar outra vez."
                elif "erro" not in self.message.casefold():
                    self.message += " Clique em Iniciar para executar outra vez."
            log_event("info", "collector", "Ciclo encerrado; nenhuma nova busca foi agendada.")

    def _run_once(self) -> None:
        started = now_iso()
        log_event("info", "collector", "Início do ciclo de busca.")
        with connect() as db:
            row = db.execute(
                "INSERT INTO runs(started_at,state) VALUES(?, 'running') RETURNING id",
                (started,),
            ).fetchone()
            run_id = row["id"]
        cfg = settings()
        keyword_terms = terms(cfg.get("keywords", ""))
        location_terms = terms(cfg.get("locations", ""))
        sources = [value.strip().casefold() for value in cfg.get("sources", "").split(",") if value.strip()]
        found = 0
        errors: list[str] = []
        self._set_progress("Buscando vagas…", 0)
        for source in sources:
            if self.stop_event.is_set():
                break
            provider = PROVIDERS.get(source)
            if not provider:
                errors.append(f"Fonte desconhecida: {source}")
                continue
            self._set_progress(f"Aguardando vagas de {source}… {found} vaga(s) nova(s) salva(s) nesta execução.", found)
            log_event("info", "collector", f"Consultando fonte {source}…")
            try:
                jobs = provider()
                received = 0
                for job in jobs:
                    received += 1
                    if self.stop_event.is_set():
                        break
                    searchable = " ".join((job["title"], job["company"], job["location"], re.sub("<[^>]+>", " ", job["description"]))).casefold()
                    if keyword_terms and not any(term in searchable for term in keyword_terms):
                        continue
                    if location_terms and not any(term in (job["location"] + " " + searchable).casefold() for term in location_terms):
                        continue
                    if not job["url"] or not job["title"]:
                        continue
                    language, language_confidence = detect_language(job["title"] + "\n" + re.sub(r"<[^>]+>", " ", job["description"]))
                    with connect() as db:
                        cursor = db.execute("""INSERT INTO jobs(source,source_id,title,company,location,description,url,posted_at,first_seen_at,fingerprint,language,language_confidence)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO NOTHING""", (job["source"], job["source_id"], job["title"], job["company"], job["location"], job["description"], job["url"], job["posted_at"],  now_iso(), fingerprint(job), language, language_confidence))
                        inserted = max(cursor.rowcount, 0)
                    found += inserted
                    if inserted:
                        self._set_progress(f"Consultando {source}… {found} vaga(s) nova(s) salva(s) nesta execução.", found)
                log_event("info", "collector", f"{source}: {received} anúncio(s) recebidos antes dos filtros.")
            except Exception as exc:  # keep other sources running if one is unavailable
                LOG.exception("Falha ao consultar %s", source)
                errors.append(f"{source}: {exc.__class__.__name__}")
                log_event("error", "collector", f"Falha ao consultar {source}: {exc}")
        apply_note = ""
        if not self.stop_event.is_set() and settings().get("auto_apply") == "1":
            self._set_progress(f"Triagem/candidatura automática… {found} vaga(s) nova(s).", found)
            log_event("info", "auto-apply", "Iniciando triagem/candidatura automática do ciclo.")
            apply_note = process_auto_apply_batch()
            log_event("info", "auto-apply", apply_note or "Batch concluído sem mensagens.")
        elif not self.stop_event.is_set():
            log_event("info", "auto-apply", "auto_apply desligado; triagem automática ignorada neste ciclo.")
        finished = now_iso()
        message = f"Busca concluída: {found} vaga(s) nova(s)." + (" Avisos: " + "; ".join(errors) if errors else "")
        if apply_note:
            message += f" Auto-apply: {apply_note}"
        log_event("success" if not errors else "warning", "collector", message)
        with connect() as db:
            db.execute("UPDATE runs SET finished_at=?,state=?,found_count=?,message=? WHERE id=?", (finished, "completed" if not self.stop_event.is_set() else "stopped", found, message, run_id))
        with self.lock:
            self.last_run, self.last_found, self.message = finished, found, message


collector = Collector()


def _boot_logging() -> None:
    ensure_log_table()
    attach_to_logger()
    log_event("info", "app", "Painel de logs ativo.")


def _boot_queue() -> None:
    queue.start()


def load_dashboard() -> tuple[dict, list, list]:
    with connect() as db:
        counts = {row["status"]: row["n"] for row in db.execute("SELECT status,COUNT(*) n FROM jobs GROUP BY status")}
        jobs = db.execute("SELECT jobs.*, cover_letters.body AS cover_letter FROM jobs LEFT JOIN cover_letters ON cover_letters.job_id=jobs.id ORDER BY first_seen_at DESC LIMIT 250").fetchall()
        runs = db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 8").fetchall()
    return counts, jobs, runs


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
        rows = db.execute(
            "SELECT jobs.id, jobs.title, jobs.company, jobs.location, jobs.source, jobs.url, "
            "jobs.posted_at, jobs.first_seen_at, jobs.status, jobs.description, jobs.notes, "
            "cover_letters.body AS cover_letter, " + _worth_match_expr() + " AS match_score "
            "FROM jobs LEFT JOIN cover_letters ON cover_letters.job_id=jobs.id "
            "ORDER BY jobs.first_seen_at DESC LIMIT 20"
        ).fetchall()
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
            "SELECT jobs.id, jobs.title, jobs.company, jobs.location, jobs.source, jobs.url, "
            "jobs.posted_at, jobs.first_seen_at, jobs.status, jobs.description, jobs.notes, "
            "cover_letters.body AS cover_letter, " + _worth_match_expr() + " AS match_score "
            "FROM jobs LEFT JOIN cover_letters ON cover_letters.job_id=jobs.id "
            f"WHERE {where} ORDER BY {worth_order_sql(sort)} LIMIT ? OFFSET ?",
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
    from usage_tracker import STORE_KEY, load_store, summarize_ai_usage

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


def send_smtp_test() -> tuple[bool, str]:
    cfg = settings()
    to_addr = (cfg.get("candidate_email") or cfg.get("smtp_from") or "").strip()
    if not to_addr:
        return False, "Configure o e-mail do candidato ou remetente para testar."
    try:
        send_smtp_email(
            cfg=cfg,
            password=secret_get("smtp_password"),
            to_addrs=[to_addr],
            subject="Odradek Scraper — teste SMTP",
            body="Este é um e-mail de teste do Odradek Scraper.",
            attachment_path=None,
        )
        log_event("success", "smtp", f"E-mail de teste enviado para {to_addr}.")
        return True, f"E-mail de teste enviado para {to_addr}."
    except Exception as exc:
        log_event("error", "smtp", f"Falha no teste SMTP: {exc}")
        return False, f"Falha no teste SMTP: {exc}"


SETTINGS_API_GROUPS = {
    "search": ("keywords", "locations", "sources", "adzuna_countries", "apify_monthly_credit_limit_usd",
               "apify_job_count", "apify_actors_json", "linkedin_filter_prompt", "apify_linkedin_filters_json"),
    "profile": ("candidate_name", "candidate_email", "candidate_phone", "candidate_linkedin",
                "candidate_city", "candidate_street", "candidate_postal_code", "candidate_state",
                "candidate_country", "candidate_current_company", "candidate_cpf", "salary_expectation_brl",
                "salary_expectation_usd", "salary_currency_preference", "salary_pj_multiplier",
                "candidate_contract_type", "candidate_gender", "candidate_race", "candidate_lgbtq",
                "candidate_pcd", "candidate_diversity_note"),
    "ai": ("ai_provider", "ai_model", "candidate_facts_pt", "candidate_facts_en",
           "job_selection_optimism", "job_selection_prompt"),
    "automation": ("auto_apply", "minimum_match_score", "queue_max_workers", "queue_max_attempts",
                   "queue_ttl_hours", "copilot_ask_sound", "copilot_ask_sound_volume"),
    "smtp": ("smtp_host", "smtp_port", "smtp_user", "smtp_from", "smtp_use_tls"),
}


def settings_api_payload(group: str) -> dict:
    cfg = settings()
    values = {key: cfg.get(key, "") for key in SETTINGS_API_GROUPS[group]}
    if group == "search":
        values["linkedin_filter_prompt"] = effective_linkedin_filter_prompt(cfg)
        if not values["apify_linkedin_filters_json"].strip():
            try:
                legacy = json.loads((cfg.get("apify_linkedin_filter_json") or "").strip() or "null")
            except json.JSONDecodeError:
                legacy = None
            if isinstance(legacy, dict):
                filters = _normalize_linkedin_filters([legacy], cfg, fallback_if_empty=False)
                values["apify_linkedin_filters_json"] = json.dumps(filters, ensure_ascii=False)
    return {"group": group, "values": values}


def save_settings_api_group(group: str, incoming: dict) -> str:
    if not isinstance(incoming, dict):
        raise ValueError("Configuração inválida")
    keys = SETTINGS_API_GROUPS[group]
    form = {}
    for key in keys:
        if key in incoming:
            value = incoming[key]
            if not isinstance(value, str) or len(value) > 30000:
                raise ValueError(f"Valor inválido para {key}")
            form[key] = value
    for key in ("api_key", "adzuna_app_id", "adzuna_app_key", "apify_token", "smtp_password"):
        if key in incoming and (not isinstance(incoming[key], str) or len(incoming[key]) > 8192):
            raise ValueError(f"Valor inválido para {key}")
    if group == "search":
        if "apify_job_count" in form and not (form["apify_job_count"].isdigit() and 1 <= int(form["apify_job_count"]) <= 100):
            raise ValueError("Máximo de vagas Apify deve ficar entre 1 e 100")
        if "apify_monthly_credit_limit_usd" in form:
            try:
                amount = float(form["apify_monthly_credit_limit_usd"])
                if not math.isfinite(amount) or amount < 0:
                    raise ValueError
            except ValueError as exc:
                raise ValueError("Limite Apify inválido") from exc
        if form.get("apify_actors_json", "").strip():
            load_apify_actors({**settings(), **form})
        if "apify_linkedin_filters_json" in form:
            form["apify_linkedin_filters_json"] = _validate_linkedin_filter_form(
                form["apify_linkedin_filters_json"], {**settings(), **form}
            )
        old_prompt = effective_linkedin_filter_prompt(settings())
        if "linkedin_filter_prompt" in form and form["linkedin_filter_prompt"].strip() == DEFAULT_LINKEDIN_FILTER_PROMPT.strip():
            form["linkedin_filter_prompt"] = ""
        save_settings(form)
        new_cfg = settings()
        if effective_linkedin_filter_prompt(new_cfg) != old_prompt:
            try:
                generated = get_linkedin_search_filters(new_cfg, force_refresh=True)
                return f"Preferências salvas e {len(generated)} filtros LinkedIn regenerados."
            except Exception as exc:
                log_event("warning", "settings", f"Regeneração do filtro LinkedIn falhou: {exc}")
                return "Preferências salvas. O filtro LinkedIn será regenerado na próxima coleta."
        if "apify_linkedin_filters_json" in form:
            set_setting("apify_linkedin_filter_hash", _resume_filter_fingerprint(new_cfg))
        return "Preferências de busca salvas."
    if group == "ai":
        if "ai_provider" in form and form["ai_provider"] not in {"gemini", "openai"}:
            raise ValueError("Provedor de IA inválido")
        if "job_selection_optimism" in form:
            form["job_selection_optimism"] = str(job_selection_optimism_instruction(form["job_selection_optimism"])[0])
        if "job_selection_prompt" in form and form["job_selection_prompt"].strip() == DEFAULT_JOB_SELECTION_PROMPT.strip():
            form["job_selection_prompt"] = ""
        save_settings(form)
        provider = form.get("ai_provider") or settings().get("ai_provider") or "gemini"
        if incoming.get("api_key"):
            save_ai_key(provider, str(incoming["api_key"]))
        for key in ("adzuna_app_id", "adzuna_app_key", "apify_token"):
            if incoming.get(key):
                secret_set(key, str(incoming[key]))
        return "IA e integrações salvas."
    if group == "automation":
        limits = {"minimum_match_score": (0, 100), "queue_max_workers": (1, 8),
                  "queue_max_attempts": (1, 200), "queue_ttl_hours": (1, 168)}
        for key, (lower, upper) in limits.items():
            if key in form and not (form[key].isdigit() and lower <= int(form[key]) <= upper):
                raise ValueError(f"Valor inválido para {key}: use {lower} a {upper}")
        for key in ("auto_apply", "copilot_ask_sound"):
            if key in form:
                form[key] = "1" if form[key] == "1" else "0"
        if "copilot_ask_sound_volume" in form:
            try:
                volume = float(form["copilot_ask_sound_volume"])
            except ValueError as exc:
                raise ValueError("Volume inválido") from exc
            form["copilot_ask_sound_volume"] = f"{max(0.0, min(1.0, volume)):.2f}"
    if group == "smtp":
        if "smtp_port" in form and not (form["smtp_port"].isdigit() and 1 <= int(form["smtp_port"]) <= 65535):
            raise ValueError("Porta SMTP inválida")
        save_settings(form)
        if incoming.get("smtp_password"):
            secret_set("smtp_password", str(incoming["smtp_password"]))
        return "SMTP salvo."
    save_settings(form)
    return "Perfil salvo." if group == "profile" else "Automação salva."


def generate_linkedin_filters(form: dict[str, str]) -> list[dict]:
    if form.get("apify_actors_json", "").strip():
        load_apify_actors({**settings(), "apify_actors_json": form["apify_actors_json"]})
    raw_prompt = (form.get("linkedin_filter_prompt") or "").strip()
    form["linkedin_filter_prompt"] = "" if raw_prompt == DEFAULT_LINKEDIN_FILTER_PROMPT.strip() else raw_prompt
    current_cfg = {**settings(), **form}
    if "apify_linkedin_filters_json" in form:
        form["apify_linkedin_filters_json"] = _validate_linkedin_filter_form(
            form["apify_linkedin_filters_json"], current_cfg
        )
    save_settings(form)
    cfg = settings()
    generated = ai_generate_linkedin_search_filters(cfg)
    previous = _normalize_linkedin_filters(
        json.loads(cfg.get("apify_linkedin_filters_json") or "[]"), cfg, fallback_if_empty=False,
    )
    previous_states = {item["name"].casefold(): item["enabled"] for item in previous}
    for item in generated:
        if item["name"].casefold() in previous_states:
            item["enabled"] = previous_states[item["name"].casefold()]
    set_setting("apify_linkedin_filters_json", json.dumps(generated, ensure_ascii=False))
    set_setting("apify_linkedin_filter_json", json.dumps(generated[0], ensure_ascii=False))
    set_setting("apify_linkedin_filter_hash", _resume_filter_fingerprint(cfg))
    log_event("info", "apify", f"IA gerou {len(generated)} filtros LinkedIn.")
    return generated


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api_asgi:api", host=HOST, port=PORT)
