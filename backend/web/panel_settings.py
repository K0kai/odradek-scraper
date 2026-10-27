"""Panel settings, SMTP check and LinkedIn filter persistence."""
import json
import math

from app import (
    DEFAULT_JOB_SELECTION_PROMPT, DEFAULT_LINKEDIN_FILTER_PROMPT,
    _normalize_linkedin_filters, _resume_filter_fingerprint,
    _validate_linkedin_filter_form, ai_generate_linkedin_search_filters,
    effective_linkedin_filter_prompt, get_linkedin_search_filters,
    job_selection_optimism_instruction, load_apify_actors,
    save_settings, secret_get, secret_set, set_setting, settings,
)
from jobs.apply_channels import send_smtp_email
from web.panel_log import log_event


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
            secret_set(provider, str(incoming["api_key"]))
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
