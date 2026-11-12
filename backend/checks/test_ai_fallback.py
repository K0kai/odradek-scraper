"""Offline: fallback de modelo gratuito quando o modelo principal está sobrecarregado ou sem quota."""
import os
import unittest
from unittest.mock import patch

from candidate.resume_pipeline import AiUnavailableError
from integrations import ai_client

PRIMARY = "gemini-3.5-flash-lite"
SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}


def http_error(status):
    exc = AiUnavailableError(f"IA indisponível: HTTP {status}")
    exc.http_status = status
    return exc


class FakeRequest:
    """Substitui ai_client._request: `plan` mapeia modelo -> resposta (str JSON) ou exceção."""

    def __init__(self, plan):
        self.plan, self.calls = plan, []

    def __call__(self, **kwargs):
        self.calls.append(kwargs["model"])
        outcome = self.plan[kwargs["model"]]
        if isinstance(outcome, Exception):
            raise outcome
        return ai_client._Reply(outcome, False)


class FallbackTests(unittest.TestCase):
    def setUp(self):
        ai_client._cooldown_until.clear()
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AI_FALLBACK_MODELS", None)

    def call(self, plan, provider="gemini", **kwargs):
        fake = FakeRequest(plan)
        used = []
        with patch.object(ai_client, "_request", fake):
            result = ai_client.call_ai_json(
                prompt="x", schema=SCHEMA, provider=provider, model=PRIMARY, api_key="k", on_model=used.append, **kwargs)
        return result, fake.calls, used

    def test_overloaded_primary_503_falls_back_to_the_next_free_model(self):
        result, calls, used = self.call({PRIMARY: http_error(503), "gemini-2.5-flash": '{"ok": true}'})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, [PRIMARY, "gemini-2.5-flash"])
        self.assertEqual(used, ["gemini-2.5-flash"])  # o modelo que respondeu é reportado

    def test_primary_is_tried_last_while_it_cools_down(self):
        plan = {PRIMARY: http_error(503), "gemini-2.5-flash": '{"ok": true}'}
        self.call(plan)
        _, calls, used = self.call(plan)
        self.assertEqual(calls, ["gemini-2.5-flash"])  # não paga uma chamada falha por vaga
        self.assertEqual(used, ["gemini-2.5-flash"])

    def test_quota_429_also_falls_back_because_quota_is_per_model(self):
        _, calls, _ = self.call({PRIMARY: http_error(429), "gemini-2.5-flash": '{"ok": true}'})
        self.assertEqual(calls, [PRIMARY, "gemini-2.5-flash"])

    def test_bad_key_or_permission_does_not_fall_back(self):
        for status in (401, 403):
            ai_client._cooldown_until.clear()
            with self.assertRaises(AiUnavailableError):
                self.call({PRIMARY: http_error(status)})

    def test_unusable_json_from_the_primary_falls_back(self):
        _, calls, used = self.call({PRIMARY: "isto não é json", "gemini-2.5-flash": '{"ok": true}'})
        self.assertEqual(calls[-1], "gemini-2.5-flash")
        self.assertEqual(used, ["gemini-2.5-flash"])

    def test_walks_the_whole_chain_and_raises_when_every_model_fails(self):
        plan = {PRIMARY: http_error(503), "gemini-2.5-flash": http_error(503), "gemini-2.5-flash-lite": http_error(503)}
        fake = FakeRequest(plan)
        with patch.object(ai_client, "_request", fake), self.assertRaises(AiUnavailableError):
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="k")
        self.assertEqual(fake.calls, [PRIMARY, "gemini-2.5-flash", "gemini-2.5-flash-lite"])

    def test_other_providers_never_fall_back_to_gemini(self):
        fake = FakeRequest({"gpt-4o": http_error(503)})
        with patch.object(ai_client, "_request", fake), self.assertRaises(AiUnavailableError):
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="openai", model="gpt-4o", api_key="k")
        self.assertEqual(fake.calls, ["gpt-4o"])

    def test_fallback_list_comes_from_env_and_empty_disables_it(self):
        os.environ["AI_FALLBACK_MODELS"] = "gemini-2.5-flash-lite"
        self.assertEqual(ai_client.model_chain("gemini", PRIMARY), [PRIMARY, "gemini-2.5-flash-lite"])
        os.environ["AI_FALLBACK_MODELS"] = ""
        self.assertEqual(ai_client.model_chain("gemini", PRIMARY), [PRIMARY])

    def test_primary_listed_among_fallbacks_is_not_repeated(self):
        self.assertEqual(ai_client.model_chain("gemini", "gemini-2.5-flash"), ["gemini-2.5-flash", "gemini-2.5-flash-lite"])

    def test_text_calls_use_the_same_fallback(self):
        fake = FakeRequest({PRIMARY: http_error(503), "gemini-2.5-flash": "olá"})
        with patch.object(ai_client, "_request", fake):
            text = ai_client.call_ai_text(prompt="x", provider="gemini", model=PRIMARY, api_key="k")
        self.assertEqual(text, "olá")


if __name__ == "__main__":
    unittest.main()
