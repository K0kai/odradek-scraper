"""Seven-day signed browser sessions for the personal panel."""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie
from urllib.parse import parse_qs, urlparse

from web.panel_auth import configured


SESSION_COOKIE = "odradek_session"
SESSION_SECONDS = 7 * 24 * 60 * 60
_ATTEMPT_WINDOW = 15 * 60
_ATTEMPT_LIMIT = 10
_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()


def _session_key() -> bytes:
    # Including the password revokes all sessions when it changes.
    material = (os.environ.get("PANEL_SESSION_SECRET") or "") + "\0" + os.environ["PANEL_PASSWORD"]
    return hashlib.sha256(b"odradek-panel-session-v1\0" + material.encode("utf-8")).digest()


def issue_session(*, now: int | None = None) -> str:
    if not configured():
        raise RuntimeError("PANEL_PASSWORD não configurada")
    expires = (int(time.time()) if now is None else now) + SESSION_SECONDS
    payload = f"v1.{expires}.{secrets.token_urlsafe(18)}"
    signature = hmac.new(_session_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def session_expiry(cookie_header: str | None, *, now: int | None = None) -> int | None:
    if not configured() or not cookie_header:
        return None
    try:
        cookies = SimpleCookie()
        cookies.load(cookie_header)
        token = cookies[SESSION_COOKIE].value
        if len(token) > 256:
            return None
        version, expiry_text, nonce, signature = token.split(".")
        if version != "v1" or not expiry_text.isdecimal() or not nonce or len(signature) != 64:
            return None
        expiry = int(expiry_text)
        current = int(time.time()) if now is None else now
        if expiry <= current or expiry > current + SESSION_SECONDS:
            return None
        payload = f"{version}.{expiry_text}.{nonce}"
        expected = hmac.new(_session_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()
        return expiry if hmac.compare_digest(signature, expected) else None
    except (CookieError, KeyError, ValueError, UnicodeError):
        return None


def session_cookie(token: str, *, secure: bool) -> str:
    parts = [f"{SESSION_COOKIE}={token}", "Path=/", f"Max-Age={SESSION_SECONDS}",
             "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def clear_session_cookie(*, secure: bool) -> str:
    parts = [f"{SESSION_COOKIE}=", "Path=/", "Max-Age=0", "HttpOnly", "SameSite=Lax"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def secure_cookie(scheme: str = "http") -> bool:
    return os.environ.get("RENDER", "").casefold() == "true" or scheme == "https"


def verify_login(password: str, client_id: str, *, now: float | None = None) -> str:
    """Return ok, invalid, or blocked. Limit online guessing per client address."""
    current = time.time() if now is None else now
    with _attempts_lock:
        recent = [stamp for stamp in _attempts.get(client_id, []) if current - stamp < _ATTEMPT_WINDOW]
        if len(recent) >= _ATTEMPT_LIMIT:
            _attempts[client_id] = recent
            return "blocked"
        valid = configured() and hmac.compare_digest(
            password.encode("utf-8"), os.environ["PANEL_PASSWORD"].encode("utf-8")
        )
        if valid:
            _attempts.pop(client_id, None)
            return "ok"
        _attempts[client_id] = recent + [current]
        return "invalid"


def safe_next(raw: str | None) -> str:
    value = raw or "/new"
    if (not value.startswith("/") or value.startswith("//") or "\\" in value
            or any(ord(char) < 32 for char in value)):
        return "/new"
    path = urlparse(value).path
    if path in {"/login", "/logout"}:
        return "/new"
    return value


def login_form(body: bytes) -> tuple[str, str]:
    if len(body) > 8192:
        raise ValueError("Formulário muito grande")
    fields = parse_qs(body.decode("utf-8", errors="strict"), keep_blank_values=True)
    password = (fields.get("password") or [""])[0]
    target = safe_next((fields.get("next") or [""])[0])
    return password, target


def login_page(target: str = "/new", error: str = "") -> str:
    target_attr = html.escape(safe_next(target), quote=True)
    alert = f'<p class="error" role="alert">{html.escape(error)}</p>' if error else ""
    return f'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#111315"><title>Entrar | Odradek</title>
<link rel="icon" href="/odradek-icon.png" type="image/png">
<style>
:root{{font-family:Inter,ui-sans-serif,system-ui,Segoe UI,Arial,sans-serif;color-scheme:dark;background:#101213;color:#edeae5}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px}}
main{{width:min(100%,390px);padding:30px;border:1px solid #303837;border-radius:12px;background:#191d1e}}
.brand{{display:flex;align-items:center;gap:12px;margin-bottom:28px}}.brand img{{width:38px;height:38px}}
.brand strong{{display:block;font-size:19px}}.brand small{{display:block;color:#a2aba4;font-size:12px;margin-top:2px}}
h1{{font-size:24px;margin:0 0 8px}}p{{color:#aeb8af;font-size:13px;line-height:1.5;margin:0 0 24px}}
label{{display:block;font-size:12px;font-weight:700;margin-bottom:7px}}input{{width:100%;height:44px;padding:10px 12px;border:1px solid #526059;border-radius:7px;background:#111617;color:#fff;font:inherit}}
input:focus-visible,button:focus-visible{{outline:2px solid #ef7068;outline-offset:2px}}button{{width:100%;height:44px;margin-top:17px;border:0;border-radius:7px;background:#d5524c;color:#fff;font:inherit;font-weight:750;cursor:pointer}}
button:hover{{background:#eb635b}}.error{{padding:10px;border:1px solid #824440;border-radius:7px;background:#402827;color:#f7d2cf;margin:0 0 18px}}
.hint{{margin:18px 0 0;font-size:11px;color:#919b93}}
</style></head><body><main><div class="brand"><img src="/odradek-icon.png" alt=""><div><strong>Odradek</strong><small>coleta · triagem · candidaturas</small></div></div>
<h1>Acessar painel</h1><p>Digite a senha para continuar.</p>{alert}
<form method="post" action="/login"><input type="hidden" name="next" value="{target_attr}">
<label for="password">Senha</label><input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Entrar</button></form><p class="hint">A sessão fica ativa por 7 dias neste navegador.</p>
</main></body></html>'''
