"""Offline: o token de pareamento libera só as rotas da extensão, sem cookie nem origem do painel."""
import asyncio
import json
import os
import unittest
from unittest.mock import patch

from checks.test_linkedin_filters import AppDbCase
from web import api_asgi, extension_token

EXT_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def call(method, path, headers=None, body=b""):
    """Chama o app ASGI direto (sem httpx) e devolve (status, json)."""
    sent = []
    raw_headers = [(b"host", b"panel.test")] + [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "path": path,
             "raw_path": path.encode(), "query_string": b"", "headers": raw_headers, "scheme": "http",
             "server": ("panel.test", 80), "client": ("127.0.0.1", 1), "root_path": ""}
    pending = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(api_asgi.api(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    try:
        return status, json.loads(data or b"{}")
    except ValueError:
        return status, {}


PANEL = {"origin": "http://panel.test", "sec-fetch-site": "same-origin"}
EXTENSION = {"origin": EXT_ORIGIN, "sec-fetch-site": "cross-site"}


class ExtensionTokenTests(AppDbCase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"PANEL_PASSWORD": "", "RENDER": ""})
        env.start(); self.addCleanup(env.stop)

    def issue(self):
        status, body = call("POST", "/api/extension-token", PANEL)
        self.assertEqual(status, 200)
        return body["token"]

    def test_status_starts_unconfigured_and_token_is_not_stored_in_clear(self):
        self.assertFalse(extension_token.token_status()["configured"])
        token = self.issue()
        self.assertTrue(extension_token.token_status()["configured"])
        self.assertTrue(token.startswith("odk_"))
        self.assertNotIn(token, json.dumps(call("GET", "/api/extension-token")[1]))
        from data import repository
        self.assertNotIn(token, json.dumps(repository.settings()))

    def test_extension_without_token_is_still_blocked_by_origin_check(self):
        self.assertEqual(call("POST", "/api/assistant", EXTENSION)[0], 403)

    def test_valid_token_allows_extension_routes_from_extension_origin(self):
        token = self.issue()
        auth = {**EXTENSION, "authorization": f"Bearer {token}"}
        with patch.object(api_asgi, "_answer_assistant", return_value="resposta"):
            status, body = call("POST", "/api/assistant", {**auth, "content-type": "application/x-www-form-urlencoded"},
                                b"message=oi&history=%5B%5D")
        self.assertEqual((status, body.get("answer")), (200, "resposta"))
        self.assertEqual(call("GET", "/api/autofill-profile", auth)[0], 200)

    def test_wrong_token_is_rejected_with_a_readable_error(self):
        self.issue()
        status, body = call("POST", "/api/assistant", {**EXTENSION, "authorization": "Bearer odk_wrong"})
        self.assertEqual(status, 401)
        self.assertIn("Token da extensão", body["error"])

    def test_no_token_configured_rejects_any_bearer(self):
        self.assertEqual(call("GET", "/api/autofill-profile", {"authorization": "Bearer odk_x"})[0], 401)

    def test_revoked_token_stops_working(self):
        token = self.issue()
        self.assertEqual(call("POST", "/api/extension-token/revoke", PANEL)[0], 200)
        self.assertEqual(call("GET", "/api/autofill-profile", {"authorization": f"Bearer {token}"})[0], 401)

    def test_regenerating_invalidates_the_previous_token(self):
        old = self.issue()
        new = self.issue()
        self.assertNotEqual(old, new)
        self.assertEqual(call("GET", "/api/autofill-profile", {"authorization": f"Bearer {old}"})[0], 401)
        self.assertEqual(call("GET", "/api/autofill-profile", {"authorization": f"Bearer {new}"})[0], 200)

    def test_token_does_not_open_other_routes(self):
        token = self.issue()
        auth = {**EXTENSION, "authorization": f"Bearer {token}"}
        self.assertEqual(call("POST", "/api/extension-token", auth)[0], 403)
        self.assertEqual(call("POST", "/api/settings/ai", {**auth, "content-type": "application/json"}, b"{}")[0], 403)

    def test_token_replaces_the_panel_session_when_a_password_is_required(self):
        token = self.issue()
        with patch.dict(os.environ, {"PANEL_PASSWORD": "senha"}):
            self.assertEqual(call("GET", "/api/autofill-profile")[0], 401)
            self.assertEqual(call("GET", "/api/autofill-profile", {"authorization": f"Bearer {token}"})[0], 200)
            self.assertEqual(call("GET", "/api/extension-token")[0], 401)


if __name__ == "__main__":
    unittest.main()
