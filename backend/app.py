# Odradek Scraper: dados, coleta, busca e preparação de candidaturas.
# O host e a porta podem ser sobrescritos pelo ambiente de deploy (ex.: Render).
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen

from data.repository import connect, initialize, settings, save_settings, set_setting
from data.secrets import SecretStore
from jobs.apply_channels import apply_via_email, record_blocked
from dates_br import BRASILIA, format_cycle_range, parse_utc

from jobs.job_queue import (
    KIND_APPLY,
    KIND_RESUME,
    KIND_WORTH_REVERIFY,
    JobQueue,
)
from candidate.resume_pipeline import (
    AiUnavailableError,
    get_resume,
    mark_resume_analysis_error,
    resume_summaries,
    run_resume_analysis,
)
from web.panel_log import attach_to_logger, ensure_log_table, log_event
from paths import DB_PATH as DEFAULT_DB_PATH, FRONTEND_DIST as DEFAULT_FRONTEND_DIST, ICON_PATH, RESUMES_DIR, COPILOT_UPLOADS_DIR


DB_PATH = str(DEFAULT_DB_PATH)
FAVICON_PATH = str(ICON_PATH)
FRONTEND_DIST = str(DEFAULT_FRONTEND_DIST)
HOST = os.environ.get("HOST", "0.0.0.0" if os.environ.get("RENDER", "").casefold() == "true" else "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
LOG = logging.getLogger("odradek-scraper")

STATUSES = {"new": "Nova", "review": "Na fila", "worth": "Vale a pena olhar", "saved": "Salva", "prepared": "Carta preparada", "applied": "Aplicada", "ignored": "Ignorada", "blocked": "Envio indisponível"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _boot_usage_tracker() -> None:
    from integrations.usage_tracker import STORE_KEY, configure_persistence

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


def ai_generate_letter(provider: str, model: str, language: str, job: dict, cfg: dict[str, str], bridges: list[str] | None = None) -> str:
    """Pede somente a carta; o modelo não recebe, cria ou altera o arquivo de currículo."""
    from candidate.experience_priority import EXPERIENCE_PRIORITY_RULE

    provider = provider.casefold().strip()
    language_name = "Portuguese" if language == "pt" else "English"
    facts_key = "candidate_facts_pt" if language == "pt" else "candidate_facts_en"
    facts = cfg.get(facts_key, "").strip()
    name = cfg.get("candidate_name", "").strip()
    bridge_block = ""
    if bridges:
        bridge_block = (
            "Verified links between the candidate's real experience and this job's requirements "
            "(use them to connect transferable experience explicitly; do not go beyond them):\n"
            + "\n".join(f"- {item}" for item in bridges[:8]) + "\n"
        )
    prompt = f"""Write a concise, specific cover letter in {language_name} for this job application.
Use only the candidate facts provided below. Never invent experience, qualifications, results, employers, dates, or skills. If facts are sparse, keep the letter brief and make no unsupported claims. Do not claim the candidate already applied. Return only the letter, with no subject line or commentary.

{EXPERIENCE_PRIORITY_RULE}

Candidate name: {name or '[candidate name]'}
Candidate-provided facts:
{facts or '[No candidate facts provided.]'}
{bridge_block}Resume analysis summary (authoritative; do not invent beyond this):
{cfg.get("resume_summary_pt" if language == "pt" else "resume_summary_en", "") or "[No resume analysis provided.]"}

Job title: {job['title']}
Company: {job['company']}
Location / eligibility: {job['location']}
Job description:
{re.sub(r'<[^>]+>', ' ', job['description'])[:10000]}
"""
    if provider not in ("openai", "gemini"):
        raise ValueError("Provedor de IA deve ser Gemini ou OpenAI.")
    from integrations.ai_client import call_ai_text

    letter = call_ai_text(
        prompt=prompt, provider=provider, model=model, api_key=get_ai_key(provider),
        max_output_tokens=1400, temperature=0.4,
    )
    if not letter:
        raise RuntimeError("A API não retornou uma carta de apresentação.")
    return letter


_secrets = SecretStore(connect)
secret_get = _secrets.get
secret_set = _secrets.set
get_ai_key = _secrets.get


def create_cover_letter(job_id: int, resume_language: str | None = None, bridges: list[str] | None = None) -> int:
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
    letter = ai_generate_letter(provider, model, language, dict(job), cfg, bridges)
    stamp = now_iso()
    with connect() as db:
        db.execute("""INSERT INTO cover_letters(job_id,language,provider,model,body,created_at,updated_at) VALUES(?,?,?,?,?,?,?)
          ON CONFLICT(job_id) DO UPDATE SET language=excluded.language,provider=excluded.provider,model=excluded.model,body=excluded.body,updated_at=excluded.updated_at""", (job_id, language, provider, model, letter, stamp, stamp))
        db.execute("UPDATE jobs SET status='prepared' WHERE id=? AND status IN ('new','review')", (job_id,))
        row = db.execute("SELECT id FROM cover_letters WHERE job_id=?", (job_id,)).fetchone()
    return int(row["id"])


DEFAULT_JOB_SELECTION_PROMPT = """You are a fair, practical job-fit evaluator. Judge whether this candidate has a credible, evidence-backed path to succeed in this role.

- Judge the role by its real day-to-day responsibilities, not by an inflated wish-list of requirements.
- Give credit to transferable skills and adjacent experience only when the resume proves the underlying capability.
- Treat missing secondary or preferred tools as a modest gap, not an automatic rejection.
- Requirements that contradict the posting itself (e.g. an internship asking for many years of experience) should be disregarded.
- Do not invent employers, degrees, skills, or experience absent from the candidate information.
"""


def effective_job_selection_prompt(cfg: dict[str, str] | None = None) -> str:
    cfg = cfg or {}
    custom = str(cfg.get("job_selection_prompt") or "").strip()
    return custom if custom else DEFAULT_JOB_SELECTION_PROMPT


def job_selection_optimism_instruction(value: object) -> tuple[int, str]:
    """Normaliza o slider (0-100). Ele define a cobertura mínima dos obrigatórios core na triagem v2."""
    from jobs.job_assessment import optimism_threshold

    try:
        optimism = max(0, min(100, int(float(str(value or "75")))))
    except (TypeError, ValueError):
        optimism = 75
    return optimism, f"Exige {optimism_threshold(optimism):.0%} dos requisitos obrigatórios essenciais atendidos."


def job_track(job: dict, cfg: dict[str, str]) -> str:
    """'transferable' quando a vaga veio do filtro Secundário (ids 'sec-', e 'tr-' do formato antigo)."""
    filter_id = str(job.get("search_filter_id") or "").strip()
    return "transferable" if filter_id.startswith(("sec-", "tr-")) else "core"


def _assessment_caller(provider: str, model: str, api_key: str):
    from integrations.ai_client import call_ai_json

    def call(prompt: str, schema: dict, *, max_output_tokens: int, thinking_budget: int | None = None) -> dict:
        return call_ai_json(
            prompt=prompt, schema=schema, provider=provider, model=model, api_key=api_key,
            max_output_tokens=max_output_tokens, thinking_budget=thinking_budget,
        )

    return call


def ai_assess_job(job: dict, cfg: dict[str, str], *, track: str = "core", call_json=None) -> dict:
    """Triagem v2: requisitos × evidência verificada (ver jobs/job_assessment.py)."""
    from candidate.resume_profile import merged_profile
    from jobs.job_assessment import SCORING_VERSION, assess_job

    language = job.get("language", "unknown")
    if language not in ("pt", "en"):
        raise ValueError("Idioma incerto; a IA não vai escolher um currículo por suposição.")
    with connect() as db:
        summaries = resume_summaries(db)
        bundle = merged_profile(db, prefer=language)
    analyzed = {lang for lang, summary in summaries.items() if (summary or "").strip()}
    if not analyzed:
        raise ValueError("Faça upload e análise de pelo menos um currículo antes da triagem automática.")
    if bundle is None:
        raise AiUnavailableError("O perfil verificado do currículo ainda não foi gerado; a triagem tentará de novo após a reanálise.")
    profile, resume_norm = bundle

    provider = cfg.get("ai_provider", "gemini").casefold()
    model = cfg.get("ai_model", "gemini-2.5-flash").strip()
    if call_json is None:
        api_key = get_ai_key(provider)
        if not api_key:
            raise AiUnavailableError("Configure a chave da API de IA para ativar o julgamento automático.")
        call_json = _assessment_caller(provider, model, api_key)
    result = assess_job(
        job, cfg, profile=profile, resume_norm=resume_norm, call_json=call_json, language=language,
        preferences=effective_job_selection_prompt(cfg), track=track,
    )
    # O currículo é escolhido pelo idioma da vaga (não pelo LLM); cai para o outro se ausente.
    other = "en" if language == "pt" else "pt"
    resume_language = language if language in analyzed else other
    return {
        **result, "provider": provider, "model": model, "resume_language": resume_language,
        "scoring_version": SCORING_VERSION,
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
        decision = ai_assess_job(job, cfg, track=job_track(job, cfg))
    except AiUnavailableError:
        # Propagates to the job queue for retry/backoff.
        raise
    except ValueError as exc:
        with connect() as db:
            db.execute("UPDATE jobs SET status='blocked', notes=? WHERE id=?", (str(exc), job_id))
        log_event("warning", "auto-apply", f"Vaga #{job_id}: {exc}")
        return f"vaga {job_id}: {exc}"

    # A aprovação já aplica o mínimo da trilha (core/transferível), bloqueios e revisão cética.
    qualifies = bool(decision["should_apply"])
    resume_language = decision["resume_language"]
    with connect() as db:
        db.execute(
            """INSERT INTO ai_decisions(job_id,decided_at,match_score,should_apply,letter_required,reason,resume_language,apply_channel,apply_result,
                                        assessment_json,scoring_version,provider,model)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, now_iso(), decision["match_score"], int(qualifies), int(decision["letter_required"]), decision["reason"], resume_language, "", "",
             json.dumps(decision["assessment"], ensure_ascii=False), decision["scoring_version"], decision["provider"], decision["model"]),
        )
    log_event(
        "info",
        "auto-apply",
        f"Vaga #{job_id}: score {decision['match_score']}/100, aprovada={qualifies}, trilha={decision['track']}, CV={resume_language}. {decision['reason']}",
    )
    if not qualifies:
        with connect() as db:
            db.execute("UPDATE jobs SET status='ignored', notes=? WHERE id=?", (f"IA: score {decision['match_score']}/100. {decision['reason']}", job_id))
        log_event("info", "auto-apply", f"Vaga #{job_id} ignorada (bloqueio, cobertura ou score abaixo do mínimo).")
        return f"IA descartou vaga {job_id} ({decision['match_score']}/100)"

    with connect() as db:
        resume = get_resume(db, resume_language)
    if not resume or not (resume["analysis_summary"] or "").strip():
        with connect() as db:
            db.execute("UPDATE jobs SET status='blocked', notes=? WHERE id=?", (f"Sem currículo analisado em {resume_language}.", job_id))
        return f"vaga {job_id}: currículo {resume_language} ausente"

    try:
        cover_id = create_cover_letter(job_id, resume_language=resume_language, bridges=decision.get("bridges"))
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

    from candidate.resume_storage import materialize_pdf
    with connect() as db:
        resume_path = materialize_pdf(db, resume, RESUMES_DIR)
    ok, detail = apply_via_email(
        connect,
        job,
        cfg,
        cover_letter=cover_body,
        resume_path=resume_path,
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


def enqueue_new_jobs_for_triage(limit: int | None = None) -> str:
    """Enfileira vagas com status 'new' para triagem IA. Não exige auto_apply.

    `limit` restringe quantas vagas são enfileiradas; as demais continuam 'new'."""
    cfg = settings()
    with connect() as db:
        rows = db.execute("SELECT id FROM jobs WHERE status='new' ORDER BY id ASC").fetchall()
    if not rows:
        return "nenhuma vaga nova para enfileirar"
    remaining = max(len(rows) - limit, 0) if limit is not None else 0
    if limit is not None:
        rows = rows[:limit]
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
    note = f"{queued} vaga(s) enfileirada(s) para triagem (até {workers} worker(s) em paralelo)"
    if remaining:
        note += f"; limite atingido, {remaining} vaga(s) nova(s) ficaram para a próxima execução"
    return note


def process_auto_apply_batch(limit: int | None = None) -> str:
    """Usado pelo ciclo de coleta quando auto_apply=1; delega ao enqueue manual."""
    return enqueue_new_jobs_for_triage(limit)


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
    from jobs.worth_reverify import format_summary, reverify_worth_jobs

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


def sync_portfolio_now(*, force: bool = False) -> dict:
    """Sincroniza o portfólio público (se houver URL). Nunca levanta; registra no log do painel."""
    from candidate.portfolio_source import sync_portfolio

    url = (settings().get("portfolio_profile_url") or "").strip()
    with connect() as db:
        result = sync_portfolio(db, url, force=force, now_iso=now_iso())
    if result["status"] == "updated":
        log_event("success", "portfolio", result["message"])
    elif result["status"] == "error":
        log_event("error", "portfolio", f"Falha ao sincronizar o portfólio: {result['message']}")
    return result


def _daily_worth_check_loop() -> None:
    """Queue one browser-free liveness pass per Brasília calendar day, after 03:00."""
    portfolio_day = ""
    while True:
        try:
            today = datetime.now(BRASILIA).date().isoformat()
            if today != portfolio_day:  # no boot e uma vez por dia; só reprocessa se a version do portfólio mudou
                portfolio_day = today
                sync_portfolio_now()
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
    from integrations.usage_tracker import APIFY_SNAPSHOT_KEY

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
    from integrations.usage_tracker import APIFY_SNAPSHOT_KEY, parse_apify_limits

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
#: Past 24 hours — máxima prioridade (vagas do mesmo dia).
LINKEDIN_TPR_DAY = "r86400"
#: Past week — teto duro de 7 dias (nunca buscar mais antigo que isso).
LINKEDIN_TPR_WEEK = "r604800"
LINKEDIN_MAX_AGE_DAYS = 7
#: Cada filtro tem o cargo e, no máximo, UMA variante equivalente dele (ex.: versão PT/EN).
LINKEDIN_FILTER_MAX_KEYWORDS = 2
LINKEDIN_SEARCH_MAX_URLS = 8

# Placeholders do texto editável: {pref_keywords} {facts_pt} {facts_en} {profile} {area_hint} {main_filter}
# (e {resume_pt}/{resume_en}, legado)
DEFAULT_LINKEDIN_FILTER_PROMPT = """You design the ONE main LinkedIn Jobs search filter for this candidate (Apify, curious_coder linkedin-jobs-scraper).
The main area is IT and programming: the area where the candidate has the most verified experience, the one that defines their career. Return EXACTLY ONE filter, as sharp as possible: the single job title that best captures what is most career-defining for this candidate, grounded ONLY in the verified profile below.

LinkedIn experience codes (f_E): 1 Internship, 2 Entry, 3 Associate, 4 Mid-Senior, 5 Director, 6 Executive.

KEYWORD RULES (mandatory):
- `keywords` holds 1 or 2 phrases that all name the SAME role: the title plus at most ONE equivalent variant of that same title (for example its Portuguese/English version or its most common synonym). Never a second, different role and never an intersection of unrelated skills.
- NO PROGRAMMING LANGUAGE, FRAMEWORK OR TOOL in any phrase (no "C#", "Python", "Java", "ASP.NET Core", "React", "SQL", "AWS"...). With AI-assisted development, the specific language matters little for this candidate; what defines the career is logical reasoning, problem solving and knowledge of software architecture (system design, APIs, data modeling, integrations, automation). Search by stack-agnostic role title ("Software Engineer", "Software Developer", "Desenvolvedor de Software", "Backend Engineer", "Full Stack Developer"...). Phrases that name a language, framework or tool found in the profile are discarded by the program.
- Choose the role family from what the dated roles show the candidate DOES (building, designing and automating software and integrations), not from the language used. A role whose title is not a developer title but whose responsibilities are software development or automation can anchor the filter; explain it in `reason`.
- EVERY phrase must list `evidence_ids`, and at least one must be a ROLE id ([R1]) or one of its responsibilities ([R1.2]). Phrases that do not are discarded by the program. Prefer responsibilities that show reasoning, architecture, integrations or automation.
- `anchor_role_ids`: the role ids that prove this career line.
- Do NOT invent employers, titles or skills. No fixed seniority-to-code mapping and no universal years cutoff: choose `experience_levels` from the computed months of the anchor roles and explain the level and the evidence in `reason` (Portuguese).
- Location and work model are NOT yours to choose (the program derives them); do not mention them.

Panel keyword preferences (roles/skills context only): {pref_keywords}
Candidate facts PT: {facts_pt}
Candidate facts EN: {facts_en}

VERIFIED CANDIDATE PROFILE (PT and EN resumes merged; evidence ids in brackets):
{profile}
"""

DEFAULT_TRANSFERABLE_PROMPT = """You design the ONE secondary (backup) LinkedIn Jobs search filter for this candidate.
Goal: ONE role, in a sector and function that are historically LESS exposed to economic downturns, where this candidate's VERIFIED capabilities transfer, especially the combination of IT/programming skills with administrative/operational experience.

LinkedIn experience codes (f_E): 1 Internship, 2 Entry, 3 Associate, 4 Mid-Senior, 5 Director, 6 Executive.

How to choose (use the profile's TRANSFERABLE CAPABILITIES, roles and skills as evidence):
- Think function x sector. Functions where IT plus an administrative background is a differentiator: IT support/helpdesk, systems/ERP-oriented administrative analyst, process/automation analyst, data/BI assistant, back-office with systems, implementation or support of business software. Sectors that tend to hold up in downturns: healthcare, public sector and outsourced public services, education, utilities/energy, logistics/supply chain, insurance, accounting/tax, back-office of financial services. These lists are only illustrative: choose what THIS profile evidences and justify the choice.
- The candidate's hint for the backup area (a direction, not an order; ignore it if the profile gives no evidence for it): {area_hint}
- The MAIN filter already covers: {main_filter}. Choose a DIFFERENT role family; do not repeat its titles.
- `keywords` holds 1 or 2 phrases that all name the SAME role (the title plus at most one equivalent variant), optionally with a sector qualifier. Never a bare skill, and never a programming language, framework or tool name (phrases that name one found in the profile are discarded).
- EVERY phrase must list `evidence_ids`, and at least one must be a TRANSFERABLE capability ([T1]) or a role/responsibility ([R1], [R1.2]). Phrases without valid ids are discarded.
- `anchor_role_ids`: the roles whose experience transfers.
- Fill `sector` (the target sector) and `resilience_reason` (why this sector/function is comparatively resilient AND which profile evidence transfers). Stay qualitative; no statistics you cannot support.
- Choose experience codes realistic for moving into a new function (often Entry/Associate), justified by the profile. Explain in `reason` (Portuguese).
- Location and work model are NOT yours to choose; do not mention them.

Panel keyword preferences (context only): {pref_keywords}
Candidate facts PT: {facts_pt}
Candidate facts EN: {facts_en}

VERIFIED CANDIDATE PROFILE (PT and EN resumes merged; evidence ids in brackets):
{profile}
"""

FILTER_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "filter": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "keywords": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"phrase": {"type": "string"}, "evidence_ids": {"type": "array", "items": {"type": "string"}}},
                    },
                },
                "anchor_role_ids": {"type": "array", "items": {"type": "string"}},
                "experience_levels": {"type": "array", "items": {"type": "integer"}},
                "reason": {"type": "string"},
                "sector": {"type": "string"},
                "resilience_reason": {"type": "string"},
            },
        },
    },
}

_TECH_SKILL_CATEGORIES = {"language", "framework", "tool", "cloud", "db"}


def effective_linkedin_filter_prompt(cfg: dict[str, str] | None = None) -> str:
    """Prompt efetivo do filtro principal: custom do painel, senão o default."""
    cfg = cfg or {}
    custom = str(cfg.get("linkedin_filter_prompt") or "").strip()
    return custom if custom else DEFAULT_LINKEDIN_FILTER_PROMPT


def effective_transferable_prompt(cfg: dict[str, str] | None = None) -> str:
    """Prompt efetivo do filtro secundário."""
    cfg = cfg or {}
    custom = str(cfg.get("transferable_prompt") or "").strip()
    return custom if custom else DEFAULT_TRANSFERABLE_PROMPT


def render_linkedin_filter_prompt(
    template: str,
    *,
    pref_keywords: str,
    facts_pt: str,
    facts_en: str,
    resume_pt: str = "",
    resume_en: str = "",
    profile: str = "",
    area_hint: str = "",
    main_filter: str = "",
) -> str:
    """Substitui placeholders sem .format() (o template pode ter chaves JSON).

    Prompts personalizados antigos ({resume_pt}/{resume_en}) continuam válidos: ambos recebem o
    perfil verificado fundido. Sem nenhum desses placeholders, o perfil é anexado ao final.
    """
    pairs = {
        "pref_keywords": pref_keywords or "[none]",
        "facts_pt": facts_pt or "[none]",
        "facts_en": facts_en or "[none]",
        "resume_pt": resume_pt or profile or "[none]",
        "resume_en": resume_en or ("(PT and EN resumes are merged in the PT block above)" if (resume_pt or profile) else "[none]"),
        "profile": profile or "[none]",
        "area_hint": area_hint or "[none: choose from the profile]",
        "main_filter": main_filter or "[not generated yet]",
    }
    out = template or DEFAULT_LINKEDIN_FILTER_PROMPT
    carries_profile = any("{" + key + "}" in out for key in ("profile", "resume_pt", "resume_en"))
    for key, value in pairs.items():
        out = out.replace("{" + key + "}", value)
    if not carries_profile and profile:
        out += f"\n\nVERIFIED CANDIDATE PROFILE (evidence ids in brackets):\n{profile}\n"
    return out


# ---------------------------------------------------------------------------
# Dois filtros: Principal (TI/programação) e Secundário (área de reserva)
# ---------------------------------------------------------------------------
FILTER_SLOT_PREFIX = {"main": "main-", "secondary": "sec-"}
FILTER_SLOT_KEY = {"main": "linkedin_main_filter_json", "secondary": "linkedin_secondary_filter_json"}


def _filter_levels(raw: object) -> list[int]:
    levels: list[int] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            code = int(item)
        except (TypeError, ValueError):
            continue
        if code in LINKEDIN_EXPERIENCE_CODES and code not in levels:
            levels.append(code)
    return levels


def _normalize_filter(raw: object, slot: str) -> dict | None:
    """Um filtro: 1-2 frases do MESMO cargo, níveis de experiência e metadados verificados."""
    if not isinstance(raw, dict):
        return None
    keywords: list[str] = []
    for entry in raw.get("keywords") if isinstance(raw.get("keywords"), list) else []:
        phrase = entry.get("phrase") if isinstance(entry, dict) else entry
        phrase = str(phrase or "").strip()[:120]
        if phrase and phrase.casefold() not in {k.casefold() for k in keywords}:
            keywords.append(phrase)
    keywords = keywords[:LINKEDIN_FILTER_MAX_KEYWORDS]
    if not keywords:
        return None
    levels = _filter_levels(raw.get("experience_levels"))
    signature = json.dumps({"keywords": keywords, "levels": levels}, ensure_ascii=False, sort_keys=True)
    out: dict = {
        "id": FILTER_SLOT_PREFIX[slot] + hashlib.sha256(signature.encode("utf-8")).hexdigest()[:10],
        "name": str(raw.get("name") or "").strip()[:80] or keywords[0],
        "slot": slot,
        "enabled": True if slot == "main" else bool(raw.get("enabled", True)),
        "keywords": keywords,
        "experience_levels": levels,
        "reason": str(raw.get("reason") or "").strip()[:400],
        "source": str(raw.get("source") or "manual")[:20],
    }
    if slot == "secondary":
        out["track"] = "transferable"
        for key in ("sector", "resilience_reason"):
            if str(raw.get(key) or "").strip():
                out[key] = str(raw[key]).strip()[:400]
    evidence = raw.get("evidence")
    if isinstance(evidence, dict):
        out["evidence"] = {
            phrase: [str(i)[:12] for i in evidence[phrase]][:4]
            for phrase in keywords if isinstance(evidence.get(phrase), list)
        }
    anchors = []
    for item in raw.get("anchor_roles") if isinstance(raw.get("anchor_roles"), list) else []:
        if isinstance(item, dict) and str(item.get("id") or "").strip():
            try:
                months = int(item.get("months") or 0)
            except (TypeError, ValueError):
                months = 0
            anchors.append({"id": str(item["id"])[:12], "title": str(item.get("title") or "")[:160],
                            "employer": str(item.get("employer") or "")[:160], "months": months})
    if anchors:
        out["anchor_roles"] = anchors[:6]
    try:
        out["career_months"] = max(0, int(raw.get("career_months") or 0))
    except (TypeError, ValueError):
        pass
    if raw.get("generated_at"):
        out["generated_at"] = str(raw["generated_at"])[:40]
    return out


def _validate_filter_form(raw: str, slot: str) -> str:
    """Valida o JSON salvo pelo painel: vazio (sem filtro) ou UM objeto com 1-2 frases."""
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON do filtro inválido: {exc}") from exc
    if isinstance(data, list):
        raise ValueError("Use um único filtro (objeto), não uma lista.")
    if not isinstance(data, dict):
        raise ValueError("Filtro inválido.")
    if not str(data.get("name") or "").strip():
        raise ValueError("Dê um nome ao filtro.")
    keywords = data.get("keywords")
    distinct = {
        str(entry.get("phrase") if isinstance(entry, dict) else entry or "").strip().casefold()
        for entry in (keywords if isinstance(keywords, list) else [])
    } - {""}
    if not distinct:
        raise ValueError("Adicione ao menos uma keyword ao filtro.")
    if len(distinct) > LINKEDIN_FILTER_MAX_KEYWORDS:
        raise ValueError(
            f"Use no máximo {LINKEDIN_FILTER_MAX_KEYWORDS} frases por filtro: o cargo e uma variante equivalente dele."
        )
    return json.dumps(_normalize_filter(data, slot), ensure_ascii=False)


def _load_filter(cfg: dict[str, str], slot: str) -> dict | None:
    raw = (cfg.get(FILTER_SLOT_KEY[slot]) or "").strip()
    if not raw:
        return None
    try:
        item = _normalize_filter(json.loads(raw), slot)
    except (ValueError, TypeError):
        item = None
    if item is None:
        log_event("warning", "apify", f"Filtro {'principal' if slot == 'main' else 'secundário'} salvo é inválido; ignorado.")
    return item


def get_main_filter(cfg: dict[str, str]) -> dict | None:
    """Filtro Principal salvo (nunca gerado automaticamente: só por ação explícita)."""
    return _load_filter(cfg, "main")


def get_secondary_filter(cfg: dict[str, str]) -> dict | None:
    """Filtro Secundário salvo (nunca gerado automaticamente)."""
    return _load_filter(cfg, "secondary")


def _verify_filter(raw: dict, profile: dict, slot: str, avoid: tuple[str, ...] = ()) -> dict:
    """Confere o filtro proposto pela IA contra o perfil verificado; levanta ValueError se nada sobrar."""
    from candidate.resume_profile import evidence_index, months_for_roles, normalize_text

    index = evidence_index(profile)
    derived = profile.get("derived", {})
    roles = {role["id"]: role for role in profile.get("roles", [])}
    avoid_keys = {item.casefold() for item in avoid}
    wanted_kinds = {"role", "responsibility"} if slot == "main" else {"transferable", "role", "responsibility"}

    def named_technology(phrase: str) -> str:
        """Linguagem, framework ou ferramenta do perfil citada na frase: o filtro é por cargo, não por stack."""
        norm = f" {normalize_text(phrase)} "
        for skill in profile.get("skills", []):
            if skill.get("category") not in _TECH_SKILL_CATEGORIES:
                continue
            name = normalize_text(skill.get("name", ""))
            if name and f" {name} " in norm:
                return skill["name"]
        return ""

    kept: list[str] = []
    evidence: dict[str, list[str]] = {}
    anchor_ids: list[str] = []
    for entry in raw.get("keywords") or []:
        phrase = str((entry.get("phrase") if isinstance(entry, dict) else entry) or "").strip()
        ids = [i for i in ((entry.get("evidence_ids") if isinstance(entry, dict) else None) or []) if isinstance(i, str) and i in index]
        reason = ""
        if not phrase:
            continue
        if not ids:
            reason = "sem evidência válida no perfil"
        elif phrase.casefold() in avoid_keys:
            reason = "repete o filtro principal"
        elif not {index[i]["kind"] for i in ids} & wanted_kinds:
            reason = "não cita um cargo do currículo" if slot == "main" else "não cita capacidade transferível nem cargo"
        elif (skill := named_technology(phrase)):
            reason = f"cita a tecnologia '{skill}'; o filtro é pelo cargo, não pela stack"
        if reason:
            log_event("info", "apify", f"Keyword descartada ({reason}): {phrase}")
            continue
        if phrase.casefold() in {k.casefold() for k in kept}:
            continue
        kept.append(phrase)
        evidence[phrase] = ids[:4]
        for i in ids:
            if index[i]["kind"] in ("role", "responsibility"):
                anchor_ids.append(i.split(".")[0])
        if len(kept) == LINKEDIN_FILTER_MAX_KEYWORDS:
            break
    if not kept:
        raise ValueError(
            "A IA não retornou uma frase com evidência válida no currículo "
            + ("(o filtro principal precisa citar um cargo seu)." if slot == "main" else "(capacidade transferível ou cargo).")
        )
    for i in raw.get("anchor_role_ids") or []:
        if i in roles:
            anchor_ids.append(i)
    anchor_ids = [i for n, i in enumerate(anchor_ids) if i in roles and i not in anchor_ids[:n]]
    role_months = derived.get("role_months", {})
    out = {
        "name": raw.get("name"), "keywords": kept, "experience_levels": raw.get("experience_levels"),
        "reason": raw.get("reason"), "evidence": evidence, "source": "ai", "generated_at": now_iso(),
        "anchor_roles": [
            {"id": i, "title": roles[i].get("title", ""), "employer": roles[i].get("employer", ""), "months": role_months.get(i, 0)}
            for i in anchor_ids
        ],
        "career_months": months_for_roles(profile, anchor_ids),
        "enabled": True,
    }
    if slot == "secondary":
        out["sector"], out["resilience_reason"] = raw.get("sector"), raw.get("resilience_reason")
    return out


def _generate_filter(cfg: dict[str, str], slot: str) -> dict:
    """Gera UM filtro (principal ou secundário) a partir do perfil verificado do currículo."""
    from candidate.resume_profile import compact_profile_text, merged_profile
    from integrations.ai_client import call_ai_json

    with connect() as db:
        bundle = merged_profile(db, prefer="pt")
    if bundle is None:
        raise ValueError(
            "Analise um currículo antes de gerar o filtro LinkedIn/Apify "
            "(se já analisou, aguarde a reanálise do perfil verificado na fila)."
        )
    profile, _ = bundle
    provider = cfg.get("ai_provider", "gemini").casefold()
    model = cfg.get("ai_model", "gemini-2.5-flash").strip()
    api_key = get_ai_key(provider)
    if not api_key:
        raise AiUnavailableError("Configure a chave da API de IA para gerar o filtro LinkedIn.")

    secondary = slot == "secondary"
    main = get_main_filter(cfg) if secondary else None
    prompt = render_linkedin_filter_prompt(
        effective_transferable_prompt(cfg) if secondary else effective_linkedin_filter_prompt(cfg),
        pref_keywords=cfg.get("keywords", ""),
        facts_pt=(cfg.get("candidate_facts_pt") or "").strip(),
        facts_en=(cfg.get("candidate_facts_en") or "").strip(),
        profile=compact_profile_text(profile, max_chars=9500),
        area_hint=(cfg.get("secondary_area_hint") or "").strip(),
        main_filter=" / ".join(main["keywords"]) if main else "",
    )
    prompt += (
        "\n\nOUTPUT: JSON {\"filter\": {...}} with EXACTLY ONE filter: name (short, Portuguese), "
        "keywords (1-2 objects {\"phrase\", \"evidence_ids\"} naming the SAME role), anchor_role_ids, "
        "experience_levels, reason (Portuguese), sector and resilience_reason (empty strings for the main filter)."
    )
    decision = call_ai_json(
        prompt=prompt, schema=FILTER_SCHEMA, provider=provider, model=model, api_key=api_key,
        max_output_tokens=3000, thinking_budget=1024,
    )
    raw = decision.get("filter") if isinstance(decision.get("filter"), dict) else {}
    verified = _verify_filter(raw, profile, slot, avoid=tuple(main["keywords"]) if main else ())
    result = _normalize_filter(verified, slot)
    if result is None:
        raise ValueError("A IA não retornou um filtro utilizável.")
    return result


def ai_generate_main_filter(cfg: dict[str, str]) -> dict:
    """Filtro Principal (TI/programação) a partir do perfil verificado."""
    return _generate_filter(cfg, "main")


def ai_generate_secondary_filter(cfg: dict[str, str]) -> dict:
    """Filtro Secundário (área de reserva resiliente), com a dica opcional do painel."""
    return _generate_filter(cfg, "secondary")


def linkedin_search_scope(cfg: dict[str, str]) -> list[dict]:
    """Pares (local, modelo) das buscas, derivados do Perfil; a IA não escolhe local nem modelo.

    Com a elegibilidade geográfica ligada: remoto mundial, remoto no seu país e qualquer modelo
    na sua cidade. Desligada: só mundial e país, sem filtro de modelo.
    """
    from jobs.form_rules import infer_candidate_country
    from jobs.job_assessment import geo_enabled

    enabled = geo_enabled(cfg)
    remote = "2" if enabled else ""
    country = infer_candidate_country(cfg).strip()
    city = (cfg.get("candidate_city") or "").strip()
    pairs: list[tuple[str, str]] = [("Worldwide", remote)]
    if country and country.casefold() != "worldwide":
        pairs.append((country, remote))
    if enabled and city:
        pairs.append((city, ""))
    scope: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for location, f_wt in pairs:
        key = (location.casefold(), f_wt)
        if key in seen:
            continue
        seen.add(key)
        scope.append({"location": location, "f_wt": f_wt,
                      "label": ("Remoto — " if f_wt == "2" else "Qualquer modelo — ") + location})
    return scope


def build_linkedin_search_urls(filter_data: dict, scope: list[dict]) -> list[str]:
    """Cobre todas as combinações keyword × escopo, primeiro 24 horas e depois até 7 dias.

    O limite por execução é aplicado em lotes, nunca descartando URLs aqui.
    """
    keywords = [str(k) for k in (filter_data.get("keywords") or []) if str(k).strip()][:LINKEDIN_FILTER_MAX_KEYWORDS]
    if not keywords or not scope:
        return []
    experience = [int(x) for x in (filter_data.get("experience_levels") or []) if int(x) in LINKEDIN_EXPERIENCE_CODES]
    f_e = "%2C".join(str(x) for x in experience) if experience else ""

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
        for item in scope:
            day_urls.append(_url(keyword, item["location"], item["f_wt"], LINKEDIN_TPR_DAY))
            week_urls.append(_url(keyword, item["location"], item["f_wt"], LINKEDIN_TPR_WEEK))
    return list(dict.fromkeys(day_urls + week_urls))


def split_apify_count(base: int, filters: list[dict], main_share: int) -> list[int]:
    """Principal sozinho: tudo. Principal + Secundário: fatia configurável (mínimo de 1 vaga cada)."""
    base = max(1, int(base))
    if len(filters) < 2:
        return [base] * len(filters)
    share = max(50, min(95, int(main_share)))
    main = max(1, int(base * share / 100 + 0.5))
    return [main, max(1, base - main)]


def plan_apify_runs(
    actors: list[dict],
    main: dict | None,
    secondary: dict | None = None,
    *,
    scope: list[dict],
    main_share: int = 70,
) -> list[tuple[dict, dict | None, list[str] | None]]:
    """Divide a quantidade entre Principal e Secundário e prioriza os lotes do dia."""
    planned: list[tuple[dict, dict | None, list[str] | None]] = []
    active = [item for item in (main, secondary) if item and item.get("enabled", True)]
    for actor in actors:
        if not actor.get("enabled", True):
            continue
        if actor.get("input_mode") != "linkedin_search":
            planned.append((actor, None, None))
            continue
        if not active:
            continue
        counts = split_apify_count(int(actor.get("count") or 25), active, main_share)
        searches = [(item, build_linkedin_search_urls(item, scope), count) for item, count in zip(active, counts)]
        for tpr in (LINKEDIN_TPR_DAY, LINKEDIN_TPR_WEEK):
            for item, urls, count in searches:
                phase_urls = [url for url in urls if f"f_TPR={tpr}" in url]
                for offset in range(0, len(phase_urls), LINKEDIN_SEARCH_MAX_URLS):
                    batch = phase_urls[offset:offset + LINKEDIN_SEARCH_MAX_URLS]
                    label = str(actor.get("label") or actor["id"])
                    phase = "24h" if tpr == LINKEDIN_TPR_DAY else "7d"
                    run_actor = {**actor, "count": count,
                                 "label": f"{label}: {item['name']} ({phase}, lote {offset // LINKEDIN_SEARCH_MAX_URLS + 1})"}
                    planned.append((run_actor, item, batch))
    return planned


def build_apify_run_input(
    actor: dict,
    cfg: dict[str, str],
    *,
    linkedin_filter: dict | None = None,
    linkedin_urls: list[str] | None = None,
) -> dict:
    keywords = terms(cfg.get("keywords", ""))[:3] or ["software engineer"]
    locations = terms(cfg.get("locations", ""))[:3] or ["remote"]
    count = int(actor.get("count") or 25)
    mode = str(actor.get("input_mode") or "linkedin_search")
    if mode == "linkedin_search":
        filter_data = linkedin_filter or get_main_filter(cfg)
        if filter_data is None:
            raise ValueError("Gere o filtro Principal no painel antes de coletar no LinkedIn.")
        urls = linkedin_urls if linkedin_urls is not None else build_linkedin_search_urls(filter_data, linkedin_search_scope(cfg))
        return {"urls": urls, "count": count, "scrapeCompany": False}

    template = actor.get("input_template")
    if not isinstance(template, (dict, list)):
        raise ValueError(
            f"Actor {actor.get('label')} em modo custom precisa de input_template (objeto JSON)."
        )
    # Custom actors use their own template and the general panel preferences.
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
    from jobs.worth_reverify import apify_item_indicates_closed

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
    linkedin_urls: list[str] | None = None,
) -> list[dict]:
    actor_id = normalize_apify_actor_id(str(actor["id"]))
    label = str(actor.get("label") or actor_id)
    run_input = build_apify_run_input(actor, cfg, linkedin_filter=linkedin_filter, linkedin_urls=linkedin_urls)
    log_event("info", "apify", f"Iniciando actor {label} ({actor_id}); quantidade solicitada: {run_input.get('count', 'definida pelo template')}.")
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
    """Executa todos os lotes dos filtros ativos, respeitando parada e orçamento."""
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
    main_filter: dict | None = None
    secondary_filter: dict | None = None
    scope: list[dict] = []
    try:
        main_share = int(cfg.get("linkedin_main_share") or 70)
    except ValueError:
        main_share = 70
    if any(actor.get("input_mode") == "linkedin_search" for actor in actors):
        main_filter = get_main_filter(cfg)
        if main_filter is None:
            log_event("warning", "apify", "Sem filtro Principal: nenhum run LinkedIn será iniciado. Gere o filtro Principal no painel.")
        else:
            secondary = get_secondary_filter(cfg)
            secondary_filter = secondary if secondary and secondary.get("enabled", True) else None
            scope = linkedin_search_scope(cfg)
            if not any(item["location"] == (cfg.get("candidate_city") or "").strip() for item in scope) and str(cfg.get("geo_eligibility_enabled", "1")) != "0":
                log_event("warning", "apify", "Cidade de residência não configurada no Perfil: a busca na sua cidade (híbrido/presencial) foi pulada.")
            log_event("info", "apify", "Escopo das buscas: " + "; ".join(item["label"] for item in scope) + ".")

    results: list[dict] = []
    errors: list[str] = []
    planned = plan_apify_runs(actors, main_filter, secondary_filter, scope=scope, main_share=main_share)
    active_count = sum(1 for item in (main_filter, secondary_filter) if item)
    log_event("info", "apify", f"Coleta planejada: Principal{' + Secundário' if secondary_filter else ''} ({active_count} filtro(s)), {len(planned)} execução(ões) Apify.")
    attempted = 0
    for run_actor, linkedin_filter, linkedin_urls in planned:
        if collector.stop_event.is_set():
            break
        run_label = str(run_actor.get("label") or run_actor["id"])
        # A failed usage check must not start additional paid batches unchecked.
        try:
            used_now, _ = apify_monthly_usage_usd(token)
        except Exception as exc:
            errors.append(f"{run_label}: não foi possível verificar créditos ({exc})")
            break
        if used_now >= limit:
            errors.append(f"{run_label}: limite de créditos atingido antes da execução")
            break
        attempted += 1
        try:
            batch = run_apify_actor(token, run_actor, cfg, linkedin_filter=linkedin_filter, linkedin_urls=linkedin_urls)
            if linkedin_filter:
                batch = [{**job, "_linkedin_filter_id": linkedin_filter["id"]} for job in batch]
            results.extend(batch)
        except Exception as exc:
            LOG.exception("Falha no actor Apify %s", run_label)
            errors.append(f"{run_label}: {exc}")
            log_event("error", "apify", f"Falha no actor {run_label}: {exc}")
        else:
            yield from batch
    if attempted < len(planned):
        log_event("warning", "apify", f"Cobertura incompleta: {attempted} de {len(planned)} execução(ões) iniciadas; {len(planned) - attempted} pendente(s).")
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


def matches_collection_preferences(job: dict, keyword_terms: list[str]) -> bool:
    """Filtros LinkedIn específicos substituem as preferências gerais da coleta."""
    if job.get("_linkedin_filter_id"):
        return True
    from jobs.job_assessment import clean_job_html

    searchable = " ".join((job["title"], job["company"], job["location"], clean_job_html(job["description"]))).casefold()

    def has_term(term: str) -> bool:
        # Termo inteiro: "qa" não casa com "aqua", "java" não casa com "javascript".
        return bool(term) and re.search(r"(?<![^\W_])" + re.escape(term) + r"(?![^\W_])", searchable) is not None

    if keyword_terms and not any(has_term(term) for term in keyword_terms):
        return False
    return True


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
        self.max_apply: int | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "state": self.state,
                "message": self.message,
                "last_run": self.last_run,
                "last_found": self.last_found,
                "next_run_at": self.next_run_at,
                "max_apply": self.max_apply,
            }

    def start(self, max_apply: int | None = None) -> bool:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            self.max_apply = max_apply
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
        try:
            sync_portfolio_now()  # antes de ler settings: o snapshot novo vale já neste ciclo
        except Exception as exc:
            LOG.warning("Sync do portfólio falhou: %s", exc)
        cfg = settings()
        keyword_terms = terms(cfg.get("keywords", ""))
        sources = [value.strip().casefold() for value in cfg.get("sources", "").split(",") if value.strip()]
        found = 0
        limit_reached = False
        errors: list[str] = []
        self._set_progress("Buscando vagas…", 0)
        for source in sources:
            if self.stop_event.is_set() or limit_reached:
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
                    if self.max_apply and found >= self.max_apply:
                        limit_reached = True
                        break
                    if not matches_collection_preferences(job, keyword_terms):
                        continue
                    if not job["url"] or not job["title"]:
                        continue
                    language, language_confidence = detect_language(job["title"] + "\n" + re.sub(r"<[^>]+>", " ", job["description"]))
                    with connect() as db:
                        cursor = db.execute("""INSERT INTO jobs(source,source_id,title,company,location,description,url,posted_at,first_seen_at,fingerprint,language,language_confidence,search_filter_id)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO NOTHING""", (job["source"], job["source_id"], job["title"], job["company"], job["location"], job["description"], job["url"], job["posted_at"],  now_iso(), fingerprint(job), language, language_confidence, str(job.get("_linkedin_filter_id") or "")))
                        inserted = max(cursor.rowcount, 0)
                    found += inserted
                    if inserted:
                        self._set_progress(f"Consultando {source}… {found} vaga(s) nova(s) salva(s) nesta execução.", found)
                        if self.max_apply and found >= self.max_apply:
                            limit_reached = True
                            break
                if limit_reached and hasattr(jobs, "close"):
                    jobs.close()  # impede que o gerador inicie novos lotes pagos
                log_event("info", "collector", f"{source}: {received} anúncio(s) recebidos antes dos filtros.")
            except Exception as exc:  # keep other sources running if one is unavailable
                LOG.exception("Falha ao consultar %s", source)
                errors.append(f"{source}: {exc.__class__.__name__}")
                log_event("error", "collector", f"Falha ao consultar {source}: {exc}")
        apply_note = ""
        if not self.stop_event.is_set() and settings().get("auto_apply") == "1":
            self._set_progress(f"Triagem/candidatura automática… {found} vaga(s) nova(s).", found)
            log_event("info", "auto-apply", "Iniciando triagem/candidatura automática do ciclo.")
            apply_note = process_auto_apply_batch(self.max_apply)
            log_event("info", "auto-apply", apply_note or "Batch concluído sem mensagens.")
        elif not self.stop_event.is_set():
            log_event("info", "auto-apply", "auto_apply desligado; triagem automática ignorada neste ciclo.")
        finished = now_iso()
        message = f"Busca concluída: {found} vaga(s) nova(s)." + (f" Limite de {self.max_apply} atingido; coleta encerrada." if limit_reached else "") + (" Avisos: " + "; ".join(errors) if errors else "")
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


def enqueue_missing_resume_profiles() -> list[str]:
    """Currículos analisados antes do perfil verificado (v2) são reanalisados em segundo plano."""
    from candidate.resume_profile import PROFILE_VERSION

    with connect() as db:
        rows = db.execute(
            "SELECT language, profile_version FROM resumes WHERE analysis_status IN ('ok','reused') "
            "AND analysis_summary != ''"
        ).fetchall()
    pending = [row["language"] for row in rows if (row["profile_version"] or "") != PROFILE_VERSION]
    for language in pending:
        queue.enqueue(KIND_RESUME, {"language": language, "force": True, "requested_at": now_iso()}, dedupe_key=f"resume:{language}")
    if pending:
        log_event("info", "resume", f"Perfil verificado ausente em {', '.join(x.upper() for x in pending)}; reanálise enfileirada.")
    return pending


def _boot_queue() -> None:
    queue.start()
    try:
        enqueue_missing_resume_profiles()
    except Exception:
        LOG.exception("Falha ao enfileirar reanálise de perfil do currículo")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web.api_asgi:api", host=HOST, port=PORT)
