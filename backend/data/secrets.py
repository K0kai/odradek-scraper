"""Secret storage with encrypted database and local keyring fallbacks."""
import os
from typing import Callable


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


class SecretStore:
    def __init__(self, connect: Callable):
        self._connect = connect

    def _secret_from_environment(self, name: str) -> str:
        for env_name in SECRET_ENV_VARS.get(name.casefold(), ()):
            value = os.environ.get(env_name, "").strip()
            if value:
                return value
        return ""


    def _secret_from_keyring(self, name: str) -> str:
        try:
            import keyring
            return keyring.get_password("job-scraper", name) or ""
        except Exception:
            return ""


    def _secret_from_database(self, name: str) -> str | None:
        with self._connect() as db:
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
            from cryptography.fernet import Fernet

            return Fernet(master_key.encode("ascii")).decrypt(row["value"].encode("ascii")).decode("utf-8")
        except Exception as exc:
            raise RuntimeError(
                f"Não foi possível descriptografar o segredo '{name}'. Confira {SECRET_MASTER_KEY_ENV}; "
                "ela precisa ser a mesma usada quando o segredo foi salvo."
            ) from exc


    def set(self, name: str, value: str) -> None:
        value = value.strip()
        if not value:
            return
        master_key = os.environ.get(SECRET_MASTER_KEY_ENV, "").strip()
        if master_key:
            try:
                from cryptography.fernet import Fernet

                encrypted = Fernet(master_key.encode("ascii")).encrypt(value.encode("utf-8")).decode("ascii")
            except Exception as exc:
                raise RuntimeError(f"{SECRET_MASTER_KEY_ENV} inválida. Gere uma chave Fernet válida.") from exc
            db_key = SECRET_SETTING_PREFIX + name.casefold()
            with self._connect() as db:
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

            keyring.set_password("job-scraper", name, value)
        except Exception as exc:
            raise RuntimeError(
                f"Não foi possível acessar o cofre local. Configure {SECRET_MASTER_KEY_ENV} para usar o banco criptografado."
            ) from exc


    def get(self, name: str) -> str:
        stored = self._secret_from_database(name)
        if stored is not None:
            return stored
        return self._secret_from_environment(name) or self._secret_from_keyring(name)
