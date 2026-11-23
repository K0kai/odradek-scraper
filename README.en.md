# Odradek Scraper (learning / educational purposes)

**Language / Idioma:** [Português (Brasil)](README.md) | English

A **personal, educational** project for exploring the following in a local environment:

- a responsive React/TypeScript dashboard served by FastAPI
- local storage with SQLite
- third-party API integrations (using API keys configured by the user)
- PDF parsing and manual form assistance

The interface code lives in `frontend/`. The API, collection, and business modules live in `backend/`. The SQLite database and persistent files remain in the project root to preserve existing local data.

Database access uses PostgreSQL (Supabase/Neon) when `DATABASE_URL` is set. Without it, the app uses the local `jobs.db` SQLite fallback, including isolated local tests.

Within `backend/`, `web/` contains routes, authentication, and panel responses; `data/` contains database and secrets; `jobs/` contains queues, filters, and rules; `candidate/` contains resume processing and candidate context; `integrations/` contains external clients. `app.py` coordinates collection and workflows.

LinkedIn/Apify collection has exactly two filters: the **Main** one (IT and programming, the area of most experience) and an optional **Secondary** one (a backup area less affected by the downturn). Each is a single job title with at most two phrases: the title and one equivalent variant of the same title (for example its Portuguese/English version). The actor's base count (`count`, or `apify_job_count` when the actor has none) goes entirely to the Main filter when it is the only one active; with the Secondary active, Main gets the `linkedin_main_share` slice (default 70%, 50% to 95%) and Secondary the rest, with at least 1 job each (base 25: 18 and 7). Each phrase x scope combination produces 24-hour and up-to-7-day searches in batches of up to 8 URLs, with the 24-hour batches of both filters first. The reduced count is sent in every batch; it is not a global per-cycle cap nor a fixed cost guarantee. The monthly budget is checked before every run. With no Main filter generated, no LinkedIn search runs (no fallback to the general keywords and no automatic generation). Filters are only replaced by an explicit regeneration, and generating one never changes the other. `custom` actors keep their own count and use their template and the general preferences.

**Location and work model are not chosen by the AI.** With geographic eligibility on (`geo_eligibility_enabled`, default on), searches are derived from your Profile: remote in "Worldwide", remote in your country, and any work model in your city. Triage applies the same rule using quoted, verified statements from the posting: a job abroad passes only if it is remote **and** explicitly says candidates from your location can apply; hybrid or on-site jobs pass only in your home city; remote jobs in your own country always pass. Turned off, country and work model never block a job and searches do not filter by work model. An explicit work-authorization requirement you do not meet blocks in both cases.

The AI proposes each filter from the candidate's **verified resume profile** (roles with dates, skills with usage months computed in code). The Main filter is the best-supported role (most dated months and/or current role); every phrase must cite `evidence_ids` that exist in the profile and at least one must be one of your roles, otherwise the phrase is dropped. Filters are by role, never by stack: phrases naming a language, framework or tool from your profile (such as "Python" or "C#") are dropped, because what defines the career is logical reasoning and architecture knowledge, not the language. A role whose title is not a developer title but whose duties are development and automation can anchor the filter. The anchor roles and career length shown on the filter are computed by code, never reported by the AI. Each collected job stores the filter that found it (`search_filter_id`), and the screen shows per-filter yield (7/30 days), AI approvals and approvals you later ignored, with a warning (never an automatic deactivation) when precision is low after 20 triaged jobs.

**Secondary filter (optional).** Generated only by the "Generate secondary filter with AI" button, and it turns itself on when generated (there is an on/off switch). The AI picks a function and sector historically less exposed to downturns where IT plus administrative experience applies, citing transferable capabilities or roles from the resume, with `sector` and `resilience_reason`. An optional hint field ("administrative with systems") steers the choice, and the AI will not repeat the Main filter's role family. Its jobs use `transferable_minimum_score` and require coverage of the core responsibilities.

### Resume and triage (v2)

1. **Verified profile.** The PDF keeps its lines and sections. The AI extracts roles, skills, education, languages and transferable capabilities, each with a verbatim quote; code verifies every quote against the PDF text and discards what it cannot find. Skill months, `last_used` and total experience are computed by code.
2. **Requirements x evidence.** For each job the AI extracts requirements (quoting the posting) and marks each met, transferable, partial or unmet, quoting the resume. Code verifies both quotes: a requirement without a valid posting quote is dropped, and "met" without a valid resume quote becomes "unmet".
3. **Inflated requirements.** The AI judges each requirement against the posting itself (for example, an internship asking for 5 years) and may only mark it inflated by quoting the contradicting part of the posting. Inflated requirements are excluded from scoring and blocks, and the panel shows which were disregarded and why. Location, work authorization, required language and credentials are never disregarded.
4. **Deterministic score, no fixed lists.** The score combines must-have coverage (70%), nice-to-haves (15%) and role-family fit (15%); a verified transferable skill counts 0.75. Blocks compare what the posting demands with what the resume proves. Weights live in `SCORING_V2` (`backend/jobs/job_assessment.py`). The selection tolerance setting maps to the minimum must-have coverage (0 -> 100%, 50 -> 80%, 75 -> 70%, 100 -> 55%); `minimum_match_score` (default 70) and `transferable_minimum_score` (default 60) are the per-track score cut-offs.
5. **Skeptical review.** Every approved job gets a second call that looks for concrete, quoted reasons not to apply; a verified blocking objection rejects it.
6. **Letters and Qpid.** Cover letters receive the verified transferable bridges. Qpid receives resume excerpts by section or role, never AI-written text, with the excerpts backing the job's triage first.

The full audit lives in `ai_decisions.assessment_json` and is shown on the job card under "Por que esta nota". To calibrate weights and cut-offs with your own data, run `python backend/checks/eval_triage.py` manually (it calls the real AI; `--dry-run` only lists the labelled jobs).

**This is not a product, commercial service, or tool intended for mass use.**  
The code is intended for learning and experimentation **on your own computer**.

## Important notice

- Respect the **terms of use** of any website or API you access. Automation and scraping may be **prohibited** by those platforms.
- The author **does not encourage** violating terms of service, bypassing CAPTCHAs or login requirements, or abusing accounts.
- Any use beyond local learning is **at your own risk**.
- Sensitive data (résumés, Brazilian taxpayer IDs/CPF, API keys, browser sessions) should remain **only on your computer** — never in commits.

This repository is provided **“as is”**, without warranties. It does not constitute legal advice.

## What runs locally

1. A web interface at `http://127.0.0.1:8765` (localhost only by default).
2. A local SQLite database (`jobs.db` — ignored by Git).
3. Secrets (AI API keys, tokens, SMTP password) stored through the operating system's **keyring**, not in the repository.

## Setup (reference)

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
# Build the React frontend:
cd frontend
npm ci
npm run build
cd ..
# Browser dependencies, if you plan to explore those modules:
# playwright install
python backend/app.py
```

Open `http://127.0.0.1:8765`. Local access is open by default. Set `PANEL_PASSWORD` to enable the password screen locally. On Render, this variable is required; the signed browser session lasts seven days.

Each click on **Iniciar bot** (Start bot) runs a single collection cycle and stops automatically. To search again, click **Iniciar bot** once more. There is no recurring schedule or automatic interval; **Parar bot** (Stop bot) interrupts the current cycle.

## Deployment with PostgreSQL (Supabase or Neon) and a standby

- The application uses local SQLite when `DATABASE_URL` is not set, and PostgreSQL (Supabase, Neon or any other) when it points to one. On an empty Postgres, startup creates the whole versioned schema from `backend/data/schema_postgres.sql` by itself; no manual SQL is needed.
- **Recommended: Supabase as primary, Neon as standby.** Neon Free has 100 CU-hours of compute per month and, once exceeded, refuses every connection until the next cycle (the dashboard answers 503 with the cause). Supabase Free runs on a fixed instance with no compute-hour quota (500 MB database, 5 GB egress; paused after 1 week of inactivity).
  1. In Supabase, create the project and copy the **pooler** URL (Connect › Session pooler or Transaction pooler, port 5432/6543). Use the pooler, not the direct connection: the direct one is IPv6-only and Render cannot reach it. The app already disables prepared statements, which transaction mode requires.
  2. Copy the current data: `cd backend && python db_copy.py --from "$NEON_URL" --to "$SUPABASE_URL"` (SQLite or Postgres on either side; `--dry-run` only counts rows). The target is replaced in a single transaction and id sequences are realigned. The script refuses to copy onto the active `DATABASE_URL` without `--force`.
  3. On Render, set `DATABASE_URL` (Supabase) and `DATABASE_URL_STANDBY` (Neon). Every day after 04:00 (Brasília time) the app copies the primary to the standby; the result shows on **Uso** (Usage), which also has a **Sincronizar standby agora** (sync standby now) button.
  4. If the primary goes down, swap `DATABASE_URL` for the standby URL on Render and restart. When the primary is back, run `db_copy.py --from standby --to primary` and revert the variable.
- With the database down the server stays up: `/health` answers 200 without touching the database (use it as Render's Health Check Path), `/health/db` tests the connection and reports the reason, the screens show the last known state with a read-only notice, and startup finishes on its own when the database returns.
- To spend little Neon compute: in its console, cap compute at **0.25 CU** (with autoscaling up to 2 CU the quota runs out 8x faster) and make sure no pinger or Health Check hits a route that queries the database. The app batches writes (logs, AI usage), reuses connections (`psycopg-pool`; `DB_POOL_DISABLED=1` turns it off, `DB_POOL_MAX` tunes the limit), and the UI stops polling the server after ~10 minutes without interaction.
- To copy local data (`jobs.db`) to Postgres, stop the hosted service and run `python backend/migrate_sqlite_to_neon.py` (a shortcut for `db_copy.py` from `jobs.db` to `DATABASE_URL`).
- Configure the same `DATABASE_URL` as a secret environment variable in Render. Never save it in the repository.
- Render does not provide the operating system credential store used by local `keyring`. To save and update keys through the dashboard, configure `SECRET_ENCRYPTION_KEY` **once** under **Environment**. Generate a Fernet key locally with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`, copy the result to Render, and keep a secure backup: saved secrets cannot be decrypted without that same key. API keys are encrypted with Fernet before being stored as internal values in the Postgres `settings` table. Environment variables such as `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `APIFY_TOKEN`, and `SMTP_PASSWORD` are still accepted as fallbacks, but secrets saved to the database through the dashboard take priority.
- Résumé PDFs live in the database (`resume_files`); other uploaded files in `COPILOT_UPLOADS_DIR` still need a persistent disk. On Render, `PANEL_PASSWORD` is required and protects the dashboard and API with a 7-day cookie session.
- The server does not submit job applications through a browser. For jobs with an application form, use **Abrir vaga + extensão** (Open job + extension) and manually import your profile using the extension's button; review the fields and submit the application in Chrome. The AI never sends email on its own: for approved jobs with a contact email in the posting, the card lists the detected addresses and a confirmed **Enviar por e-mail** (send by email) button sends the cover letter and resume through SMTP. The `/api/autofill-rules` endpoint shares only field names and aliases with the extension.

## Privacy and what to keep out of version control

Already ignored by Git (among others):

- `jobs.db` and its variants
- PDFs in `resumes/`
- `linkedin_browser_profile/`, `copilot_uploads/`
- `docs/superpowers/` (internal planning notes)
- `tests/` (local test suite)

Before every push, run `git status` and confirm that no personal data, résumés, or keys are included.

## License

Educational / learning use. No license for commercial redistribution has been defined.  
If you fork the code, retain the notices in this README.

Resume PDFs are now stored transactionally in `resume_files` (PostgreSQL `BYTEA` / SQLite `BLOB`). The Qpid download reads from the database, so uploaded PDFs survive deployments. Startup creates the table and imports accessible legacy PDFs after verifying their hash. Previously lost files must be uploaded once again. Other uploads still require persistent filesystem storage.
