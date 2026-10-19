# Odradek Scraper (learning / educational purposes)

**Language / Idioma:** [Português (Brasil)](README.md) | English

A **personal, educational** project for exploring the following in a local environment:

- simple HTTP dashboards in Python (`http.server`)
- local storage with SQLite
- third-party API integrations (using API keys configured by the user)
- PDF parsing and web form workflows

**This is not a product, commercial service, or tool intended for mass use.**  
The code is intended for learning and experimentation **on your own computer**.

## Important notice

- Respect the **terms of use** of any website or API you access. Automation, scraping, or automated job applications may be **prohibited** by those platforms.
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
pip install -r requirements.txt
# Browser dependencies, if you plan to explore those modules:
# playwright install
python app.py
```

Open `http://127.0.0.1:8765`. Do not expose the port to the internet: the dashboard **has no authentication**.

Each click on **Iniciar bot** (Start bot) runs a single collection cycle and stops automatically. To search again, click **Iniciar bot** once more. There is no recurring schedule or automatic interval; **Parar bot** (Stop bot) interrupts the current cycle.

## Deployment with Neon/PostgreSQL

- The application uses local SQLite when `DATABASE_URL` is not set, and PostgreSQL when it points to Neon.
- Run the complete PostgreSQL schema (tables, indexes, and default values) in the Neon SQL Editor before starting the application.
- To copy local data to Neon, stop the hosted service and run `python migrate_sqlite_to_neon.py` on the computer containing `jobs.db`, with `DATABASE_URL` set to the Neon connection string. The script imports tables and data, preserves local settings, and replaces the initial rules and first-boot logs with the local versions.
- Configure the same `DATABASE_URL` as a secret environment variable in Render. Never save it in the repository.
- Render does not provide the operating system credential store used by local `keyring`. To save and update keys through the dashboard, configure `SECRET_ENCRYPTION_KEY` **once** under **Environment**. Generate a Fernet key locally with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`, copy the result to Render, and keep a secure backup: saved secrets cannot be decrypted without that same key. API keys are encrypted with Fernet before being stored as internal values in the Postgres `settings` table. Environment variables such as `GEMINI_API_KEY`, `OPENAI_API_KEY`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `APIFY_TOKEN`, and `SMTP_PASSWORD` are still accepted as fallbacks, but secrets saved to the database through the dashboard take priority.
- Résumés and uploaded files are stored in the filesystem, not in the database. Configure a persistent disk in Render and set `RESUMES_DIR` and `COPILOT_UPLOADS_DIR` to directories on that disk. Copy the local files there before using the migrated records.
- The dashboard has no authentication. Do not expose it publicly until access protection is in place.
- The server does not submit job applications through a browser. For jobs with an application form, use **Abrir vaga + extensão** (Open job + extension) and manually import your profile using the extension's button; review the fields and submit the application in Chrome. Server automation remains limited to sending email through SMTP. The `/api/autofill-rules` endpoint shares only field names and aliases with the extension.

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
