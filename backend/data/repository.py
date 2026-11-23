"""Database schema, migrations and saved settings."""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

from data.dbutil import open_connection, using_postgres
from jobs.form_rules import ensure_default_rules
from jobs.copilot_asks import ensure_table as ensure_copilot_asks
from paths import DB_PATH, RESUMES_DIR
from candidate.resume_storage import ensure_table as ensure_resume_files, backfill as backfill_resume_files
from web.panel_log import ensure_log_table


# As vagas são deduplicadas pela impressão digital; cartas e execuções ficam
# separadas para registrar versões sem alterar os dados da vaga.
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
    "ai_model": "gemini-3.5-flash-lite",
    # Último recurso quando todos os modelos do provedor falham; só vale com a chave do TokenHarbor salva.
    "tokenharbor_fallback_models": "deepseek-v4.1-flash:free,mimo-v2.6-flash:free",
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
    "minimum_match_score": "70",
    "job_selection_optimism": "75",
    # Dois filtros: Principal (TI/programação) e Secundário (área de reserva resiliente).
    # Cada slot guarda UM objeto JSON (ou vazio). Sem listas de filtros.
    "linkedin_main_filter_json": "",
    "linkedin_secondary_filter_json": "",
    "linkedin_main_share": "70",
    "linkedin_max_age_days": "7",
    "linkedin_preferred_countries": "",
    "secondary_area_hint": "",
    "geo_eligibility_enabled": "1",
    # Portfólio público (schema 2 de /ai/profile.json) como segunda fonte do perfil; vazio = desligado.
    "portfolio_profile_url": "",
    "portfolio_version": "",
    "portfolio_profile_json": "",
    "portfolio_text": "",
    "portfolio_synced_at": "",
    "portfolio_error": "",
    "transferable_minimum_score": "60",
    "transferable_prompt": "",
    "job_selection_prompt": "",
    "maximum_applications_per_run": "5",
    "adzuna_countries": "br,us,gb,ca",
    "apify_monthly_credit_limit_usd": "5",
    "apify_job_count": "25",
    "apify_actors_json": "[{\"id\": \"curious_coder~linkedin-jobs-scraper\", \"label\": \"LinkedIn Jobs\", \"enabled\": true, \"input_mode\": \"linkedin_search\", \"count\": 25}]",
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
    "standby_sync_last_daily": "",
    "standby_last_sync_json": "",
    # Perfil de diversidade (identidade NUNCA vem de IA; 'not_informed' = não informar)
    "candidate_gender": "not_informed",
    "candidate_race": "not_informed",
    "candidate_lgbtq": "not_informed",
    "candidate_pcd": "no",
    "candidate_diversity_note": "",
}


# (tabela, coluna, definição) — aplicadas de forma idempotente em SQLite e Postgres.
NEW_COLUMNS = (
    ("resumes", "profile_json", "TEXT NOT NULL DEFAULT ''"),
    ("resumes", "profile_version", "TEXT NOT NULL DEFAULT ''"),
    ("ai_decisions", "assessment_json", "TEXT NOT NULL DEFAULT ''"),
    ("ai_decisions", "scoring_version", "TEXT NOT NULL DEFAULT ''"),
    ("ai_decisions", "provider", "TEXT NOT NULL DEFAULT ''"),
    ("ai_decisions", "model", "TEXT NOT NULL DEFAULT ''"),
    ("jobs", "search_filter_id", "TEXT NOT NULL DEFAULT ''"),
    # '1' = a vaga veio de uma busca com o filtro "remoto" do LinkedIn (f_WT=2); vazio = origem não remota/desconhecida.
    ("jobs", "search_remote", "TEXT NOT NULL DEFAULT ''"),
    ("jobs", "reviewed_at", "TEXT NOT NULL DEFAULT ''"),  # quando o usuário conferiu uma vaga descartada pela IA
)


def ensure_new_columns(db, *, postgres: bool) -> None:
    for table, column, definition in NEW_COLUMNS:
        if postgres:
            db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}")
        else:
            existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def migrate_scoring_v2_defaults(db) -> None:
    """Uma vez: o mínimo padrão antigo (65) sobe para 70 na triagem v2 (verificada)."""
    marker = db.execute("SELECT value FROM settings WHERE key='scoring_v2_migrated'").fetchone()
    if marker and marker["value"] == "1":
        return
    db.execute("UPDATE settings SET value='70' WHERE key='minimum_match_score' AND value IN ('65','80')")
    db.execute(
        "INSERT INTO settings(key,value) VALUES('scoring_v2_migrated','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def migrate_discard_excluded_region_jobs(db) -> None:
    """Uma vez: vagas antigas da Índia (golpe/vaga fantasma) saem de todas as listas.

    Candidaturas feitas e vagas salvas pelo usuário ficam como estão. Vagas já ignoradas só ganham a marca de
    conferida; as demais (novas, em triagem, preparadas, bloqueadas, para revisar) viram ignoradas com o motivo.
    O marcador evita desfazer uma vaga que o usuário mover para revisar depois.
    """
    marker = db.execute("SELECT value FROM settings WHERE key='india_jobs_discarded_v1'").fetchone()
    if marker and marker["value"] == "1":
        return
    from datetime import datetime, timezone

    from jobs.region_filter import excluded_region_reason

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = db.execute(
        "SELECT id, status, location, title, description FROM jobs WHERE status NOT IN ('applied','saved')"
    ).fetchall()
    for row in rows:
        job = dict(row)
        reason = excluded_region_reason(job)
        if not reason:
            continue
        if job["status"] == "ignored":
            db.execute("UPDATE jobs SET reviewed_at=? WHERE id=?", (now, job["id"]))
        else:
            db.execute(
                "UPDATE jobs SET status='ignored', reviewed_at=?, notes=? WHERE id=?",
                (now, f"Região excluída: {reason}.", job["id"]),
            )
    db.execute(
        "INSERT INTO settings(key,value) VALUES('india_jobs_discarded_v1','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


_LEGACY_FILTER_PROMPT_MARKERS = ("MULTIPLE FILTERS (mandatory)", "KEYWORD BREADTH (mandatory)", "Produce 4-8 separate filters")


def migrate_filter_prompts(db) -> None:
    """Uma vez: prompts salvos do modelo antigo (vários filtros, uma skill basta) viram o padrão novo.

    O painel salvava o texto antigo como 'personalizado' e ele passava por cima do prompt novo.
    """
    marker = db.execute("SELECT value FROM settings WHERE key='filter_prompts_v3_migrated'").fetchone()
    if marker and marker["value"] == "1":
        return
    for key in ("linkedin_filter_prompt", "transferable_prompt"):
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row and any(m in (row["value"] or "") for m in _LEGACY_FILTER_PROMPT_MARKERS):
            db.execute("UPDATE settings SET value='' WHERE key=?", (key,))
    db.execute(
        "INSERT INTO settings(key,value) VALUES('filter_prompts_v3_migrated','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


# Frase do prompt antigo que manda basear o score numa "calibração de otimismo" que a triagem v2 não envia.
_LEGACY_SELECTION_PROMPT_MARKER = "optimism calibration supplied below"


def migrate_selection_prompt(db) -> None:
    """Uma vez: o prompt de seleção antigo salvo como 'personalizado' volta ao padrão atual.

    Ele passava por cima do padrão novo e pedia à IA para decidir/rejeitar, o que na v2 é do código.
    """
    marker = db.execute("SELECT value FROM settings WHERE key='selection_prompt_v2_migrated'").fetchone()
    if marker and marker["value"] == "1":
        return
    row = db.execute("SELECT value FROM settings WHERE key='job_selection_prompt'").fetchone()
    if row and _LEGACY_SELECTION_PROMPT_MARKER in (row["value"] or ""):
        db.execute("UPDATE settings SET value='' WHERE key='job_selection_prompt'")
    db.execute(
        "INSERT INTO settings(key,value) VALUES('selection_prompt_v2_migrated','1') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
    )


def connect() -> sqlite3.Connection:
    return open_connection(str(DB_PATH), timeout=60.0)


POSTGRES_SCHEMA_PATH = Path(__file__).with_name("schema_postgres.sql")
POSTGRES_REQUIRED_TABLES = (
    "settings", "jobs", "runs", "ai_decisions", "cover_letters", "resumes", "resume_files",
    "form_field_rules", "applications", "form_answers", "ai_answer_cache",
    "copilot_asks", "queue_jobs", "event_logs", "qpid_application_attempts", "qpid_application_results",
)


def missing_postgres_tables(db) -> list[str]:
    rows = db.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
    ).fetchall()
    available = {row["table_name"] if isinstance(row, dict) else row[0] for row in rows}
    return sorted(set(POSTGRES_REQUIRED_TABLES) - available)


def postgres_schema_statements() -> list[str]:
    """Instruções do schema_postgres.sql, uma a uma (comentários de linha inteira são ignorados)."""
    lines = [line for line in POSTGRES_SCHEMA_PATH.read_text(encoding="utf-8").splitlines()
             if not line.lstrip().startswith("--")]
    return [statement.strip() for statement in "\n".join(lines).split(";\n") + [""] if statement.strip().rstrip(";")]


def apply_postgres_schema(db) -> None:
    """Idempotente (CREATE ... IF NOT EXISTS): cria só o que falta e não altera tabelas existentes."""
    for statement in postgres_schema_statements():
        db.execute(statement.rstrip(";"))


def initialize() -> None:
    """Cria o banco e aplica pequenas migrações compatíveis com versões anteriores."""
    os.makedirs(RESUMES_DIR, exist_ok=True)
    if using_postgres():
        with connect() as db:
            missing = missing_postgres_tables(db)
            if missing:
                # Banco vazio (ou incompleto): cria o que falta a partir do schema versionado, sem passo manual.
                apply_postgres_schema(db)
                missing = missing_postgres_tables(db)
            if missing:
                raise RuntimeError("PostgreSQL schema is incomplete; missing tables: " + ", ".join(missing))
            ensure_resume_files(db)
            backfill_resume_files(db, RESUMES_DIR)
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
            # Consultas de cada poll do painel e de cada claim da fila; o schema do Neon foi criado à mão, sem eles.
            db.execute("CREATE INDEX IF NOT EXISTS jobs_seen_idx ON jobs(first_seen_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS queue_jobs_status_idx ON queue_jobs(status, next_run_at)")
            db.execute("CREATE INDEX IF NOT EXISTS copilot_asks_status_idx ON copilot_asks(status)")
            ensure_new_columns(db, postgres=True)
            migrate_scoring_v2_defaults(db)
            migrate_filter_prompts(db)
            migrate_selection_prompt(db)
            migrate_discard_excluded_region_jobs(db)
            ensure_log_table(db)
        return

    with connect() as db:
        initialize_sqlite(db)


def initialize_sqlite(db) -> None:
    """Schema e migrações do SQLite numa conexão aberta (também usado para preparar um SQLite vazio)."""
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
    ensure_resume_files(db)
    backfill_resume_files(db, RESUMES_DIR)
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
    ensure_new_columns(db, postgres=False)
    migrate_scoring_v2_defaults(db)
    migrate_filter_prompts(db)
    migrate_selection_prompt(db)
    migrate_discard_excluded_region_jobs(db)
    ensure_log_table(db)


# A tabela inteira (prompts, perfil, JSON de uso, segredos cifrados) era relida a cada chamada. Com banco em rede
# isso é uma conexão + dezenas de KB por leitura; guardamos em memória por alguns segundos. SQLite local não
# precisa (conexão é barata) e continua lendo direto.
SETTINGS_CACHE_SECONDS = 30.0
_settings_cache: dict[str, object] = {"at": float("-inf"), "data": None}
_settings_cache_lock = threading.Lock()


def invalidate_settings_cache() -> None:
    with _settings_cache_lock:
        _settings_cache["at"] = float("-inf")
        _settings_cache["data"] = None


def settings() -> dict[str, str]:
    """Lê as preferências salvas (cópia; em banco de rede vem do cache de 30 s)."""
    if not using_postgres():
        with connect() as db:
            return {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM settings")}
    with _settings_cache_lock:
        data = _settings_cache["data"]
        if data is not None and time.monotonic() - float(_settings_cache["at"]) < SETTINGS_CACHE_SECONDS:
            return dict(data)  # type: ignore[arg-type]
    try:
        with connect() as db:
            fresh = {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM settings")}
    except Exception:
        with _settings_cache_lock:
            stale = _settings_cache["data"]
        if stale is not None:  # banco fora do ar: o último valor conhecido é melhor que derrubar o painel
            return dict(stale)  # type: ignore[arg-type]
        raise
    with _settings_cache_lock:
        _settings_cache["data"] = fresh
        _settings_cache["at"] = time.monotonic()
    return dict(fresh)


def get_setting(key: str, default: str = "") -> str:
    """Uma chave só: usa o cache quente, senão lê 1 linha (em vez da tabela inteira)."""
    if using_postgres():
        with _settings_cache_lock:
            data = _settings_cache["data"]
            if data is not None and time.monotonic() - float(_settings_cache["at"]) < SETTINGS_CACHE_SECONDS:
                return str(data.get(key, default))  # type: ignore[union-attr]
    with connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row else default


def _remember_settings(values: dict[str, str]) -> None:
    """Depois de gravar: o cache já reflete o novo valor (sem reler a tabela inteira na próxima leitura)."""
    with _settings_cache_lock:
        data = _settings_cache["data"]
        if isinstance(data, dict):
            data.update(values)


_UPSERT_SETTING = "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value"


def save_settings(data: dict[str, str]) -> None:
    """Atualiza somente os campos enviados pelo formulário atual."""
    values = {key: data.get(key, "") for key in DEFAULT_SETTINGS if key in data}
    set_settings_many(values)


def set_settings_many(values: dict[str, str]) -> None:
    """Várias chaves numa conexão só."""
    if not values:
        return
    with connect() as db:
        for key, value in values.items():
            db.execute(_UPSERT_SETTING, (key, value))
    _remember_settings(values)


def set_setting(key: str, value: str) -> None:
    set_settings_many({key: value})
