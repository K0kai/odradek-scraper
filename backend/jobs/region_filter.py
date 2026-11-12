"""Vagas de regiões excluídas (Índia): coleta, triagem e limpeza do histórico usam a mesma regra."""
import re


# Vagas da Índia: a esmagadora maioria é golpe ou vaga fantasma. Nunca entram na coleta nem na triagem.
_EXCLUDED_REGION_LOCATION = re.compile(
    r"\b(?:india|bengaluru|bangalore|mumbai|navi mumbai|new delhi|delhi|hyderabad|chennai|pune|kolkata|noida|gurgaon|gurugram|"
    r"ahmedabad|jaipur|kochi|coimbatore|indore|chandigarh|lucknow|thiruvananthapuram|karnataka|maharashtra|telangana|"
    r"tamil nadu|uttar pradesh|haryana|gujarat|kerala)\b"
)
_EXCLUDED_REGION_TEXT = re.compile(r"\b(?:anywhere|across|within|all over|pan)[ -](?:in )?india\b|\bindian (?:citizens?|nationals?|residents?)\b|₹")


def excluded_region_reason(job: dict) -> str:
    """Motivo (não vazio) quando a vaga é da Índia: pelo local, ou por pistas explícitas no anúncio."""
    from jobs.job_assessment import clean_job_html
    from candidate.resume_profile import normalize_text

    location = str(job.get("location") or "")
    # Lista longa de países ("USA, UK, India, Australia, …") é vaga multirregião, não vaga da Índia.
    if location.count(",") < 4 and _EXCLUDED_REGION_LOCATION.search(normalize_text(location)):
        return f"local da vaga na Índia ({location[:60]})"
    text = f"{job.get('title') or ''} {clean_job_html(str(job.get('description') or ''))}"
    if _EXCLUDED_REGION_TEXT.search(text.casefold()):
        return "anúncio restrito à Índia"
    return ""


def drop_excluded_region_jobs(batch: list[dict]) -> tuple[list[dict], int]:
    kept = [job for job in batch if not excluded_region_reason(job)]
    return kept, len(batch) - len(kept)
