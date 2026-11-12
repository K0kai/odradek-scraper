"""Offline: fallback de modelo gratuito quando o modelo principal está sobrecarregado ou sem quota."""
import os
import unittest
from unittest.mock import patch

from candidate.resume_pipeline import AiUnavailableError
from integrations import ai_client

PRIMARY = "gemini-3.5-flash-lite"
F1, F2 = "gemini-3.8-flash", "gemini-test-fallback-2"  # os testes fixam a cadeia por AI_FALLBACK_MODELS
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
        os.environ["AI_FALLBACK_MODELS"] = f"{F1},{F2}"

    def call(self, plan, provider="gemini", **kwargs):
        fake = FakeRequest(plan)
        used = []
        with patch.object(ai_client, "_request", fake):
            result = ai_client.call_ai_json(
                prompt="x", schema=SCHEMA, provider=provider, model=PRIMARY, api_key="k", on_model=used.append, **kwargs)
        return result, fake.calls, used

    def test_overloaded_primary_503_falls_back_to_the_next_free_model(self):
        result, calls, used = self.call({PRIMARY: http_error(503), F1: '{"ok": true}'})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, [PRIMARY, F1])
        self.assertEqual(used, [F1])  # o modelo que respondeu é reportado

    def test_primary_is_tried_last_while_it_cools_down(self):
        plan = {PRIMARY: http_error(503), F1: '{"ok": true}'}
        self.call(plan)
        _, calls, used = self.call(plan)
        self.assertEqual(calls, [F1])  # não paga uma chamada falha por vaga
        self.assertEqual(used, [F1])

    def test_quota_429_also_falls_back_because_quota_is_per_model(self):
        _, calls, _ = self.call({PRIMARY: http_error(429), F1: '{"ok": true}'})
        self.assertEqual(calls, [PRIMARY, F1])

    def test_bad_key_or_permission_does_not_fall_back(self):
        for status in (401, 403):
            ai_client._cooldown_until.clear()
            with self.assertRaises(AiUnavailableError):
                self.call({PRIMARY: http_error(status)})

    def test_unusable_json_from_the_primary_falls_back(self):
        _, calls, used = self.call({PRIMARY: "isto não é json", F1: '{"ok": true}'})
        self.assertEqual(calls[-1], F1)
        self.assertEqual(used, [F1])

    def test_walks_the_whole_chain_and_raises_when_every_model_fails(self):
        plan = {PRIMARY: http_error(503), F1: http_error(503), F2: http_error(503)}
        fake = FakeRequest(plan)
        with patch.object(ai_client, "_request", fake), self.assertRaises(AiUnavailableError):
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="k")
        self.assertEqual(fake.calls, [PRIMARY, F1, F2])

    def test_other_providers_never_fall_back_to_gemini(self):
        fake = FakeRequest({"gpt-4o": http_error(503)})
        with patch.object(ai_client, "_request", fake), self.assertRaises(AiUnavailableError):
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="openai", model="gpt-4o", api_key="k")
        self.assertEqual(fake.calls, ["gpt-4o"])

    def test_fallback_list_comes_from_env_and_empty_disables_it(self):
        os.environ["AI_FALLBACK_MODELS"] = F2
        self.assertEqual(ai_client.model_chain("gemini", PRIMARY), [PRIMARY, F2])
        os.environ["AI_FALLBACK_MODELS"] = ""
        self.assertEqual(ai_client.model_chain("gemini", PRIMARY), [PRIMARY])

    def test_primary_listed_among_fallbacks_is_not_repeated(self):
        self.assertEqual(ai_client.model_chain("gemini", F1), [F1, F2])

    def test_text_calls_use_the_same_fallback(self):
        fake = FakeRequest({PRIMARY: http_error(503), F1: "olá"})
        with patch.object(ai_client, "_request", fake):
            text = ai_client.call_ai_text(prompt="x", provider="gemini", model=PRIMARY, api_key="k")
        self.assertEqual(text, "olá")


    def test_retired_model_404_skips_to_the_next_one_instead_of_crashing(self):
        gone = RuntimeError("HTTP 404 NOT_FOUND: no longer available")
        gone.http_status = 404
        result, calls, used = self.call({PRIMARY: http_error(503), F1: gone, F2: '{"ok": true}'})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(calls, [PRIMARY, F1, F2])
        self.assertEqual(used, [F2])

    def test_retired_model_is_remembered_and_not_called_again_for_hours(self):
        gone = RuntimeError("HTTP 404")
        gone.http_status = 404
        plan = {PRIMARY: http_error(503), F1: gone, F2: '{"ok": true}'}
        self.call(plan)
        _, calls, _ = self.call(plan)
        self.assertEqual(calls[0], F2)  # o aposentado vai para o fim da fila
        self.assertNotIn(F1, calls)  # e nem é tentado enquanto há quem responda

    def test_primary_that_is_gone_still_gets_an_answer_from_a_fallback(self):
        gone = RuntimeError("HTTP 404")
        gone.http_status = 404
        result, calls, used = self.call({PRIMARY: gone, F1: '{"ok": true}'})
        self.assertEqual((result, calls, used), ({"ok": True}, [PRIMARY, F1], [F1]))

    def test_404_on_the_last_model_still_raises(self):
        gone = RuntimeError("HTTP 404")
        gone.http_status = 404
        plan = {PRIMARY: http_error(503), F1: http_error(503), F2: gone}
        with patch.object(ai_client, "_request", FakeRequest(plan)), self.assertRaises(RuntimeError):
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="k")

    def test_default_fallbacks_are_current_models_not_the_retired_2_5_family(self):
        os.environ.pop("AI_FALLBACK_MODELS")
        self.assertEqual(ai_client.fallback_models(), ["gemini-3.8-flash"])
        self.assertFalse([m for m in ai_client.DEFAULT_FALLBACK_MODELS if m.startswith("gemini-2.5")])

    def test_when_every_model_fails_the_error_is_retryable_and_lists_each_model(self):
        gone = RuntimeError("HTTP 404 NOT_FOUND")
        gone.http_status = 404
        plan = {PRIMARY: http_error(503), F1: http_error(429), F2: gone}
        with patch.object(ai_client, "_request", FakeRequest(plan)), self.assertRaises(AiUnavailableError) as ctx:
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="k")
        message = str(ctx.exception)
        for model, status in ((PRIMARY, "503"), (F1, "429"), (F2, "404")):
            self.assertIn(f"{model}: HTTP {status}", message)
        # a fila só repete erros "indisponíveis": isso evita a falha definitiva da vaga
        from jobs.job_queue import is_retryable_error
        self.assertTrue(is_retryable_error(ctx.exception))

    def test_single_model_that_is_gone_is_retryable_instead_of_killing_the_job(self):
        os.environ["AI_FALLBACK_MODELS"] = ""
        gone = RuntimeError("HTTP 404 NOT_FOUND no longer available")
        gone.http_status = 404
        with patch.object(ai_client, "_request", FakeRequest({PRIMARY: gone})), self.assertRaises(AiUnavailableError):
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="k")


if __name__ == "__main__":
    unittest.main()
