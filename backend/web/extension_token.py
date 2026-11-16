"""Pairing token that lets the browser extension call a few panel endpoints.

Only a SHA-256 hash is stored, so the token is shown once, when generated. The
token is a bearer credential sent in a header (never an ambient cookie), so
requests that carry it do not need the same-origin or session checks.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from data.repository import connect, set_setting

HASH_KEY = "__extension_token_hash__"
CREATED_KEY = "__extension_token_created_at__"
# Paths the extension may call with the token. Anything else needs a panel session.
EXTENSION_PATHS = frozenset({
    "/api/assistant", "/api/autofill-profile", "/api/autofill-rules", "/api/autofill-resume",
})


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _read(key: str) -> str:
    with connect() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return (row["value"] if row else "") or ""


def token_status() -> dict:
    return {"configured": bool(_read(HASH_KEY)), "created_at": _read(CREATED_KEY) or None}


def issue_token() -> str:
    token = "odk_" + secrets.token_urlsafe(32)
    set_setting(HASH_KEY, _digest(token))
    set_setting(CREATED_KEY, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return token


def revoke_token() -> None:
    set_setting(HASH_KEY, "")
    set_setting(CREATED_KEY, "")


def verify_token(supplied: str) -> bool:
    stored = _read(HASH_KEY)
    if not stored or not supplied:
        return False
    return hmac.compare_digest(_digest(supplied).encode(), stored.encode())
