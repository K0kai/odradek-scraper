"""Offline: fallback de modelo gratuito quando o modelo principal está sobrecarregado ou sem quota."""
import json
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
        ai_client.set_tokenharbor_source(None)  # o app pode ter registrado a fonte real ao ser importado
        self.addCleanup(ai_client.set_tokenharbor_source, None)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ["AI_FALLBACK_MODELS"] = f"{F1},{F2}"
        os.environ.pop("AI_TRANSIENT_RETRIES", None)
        self.pauses = []
        pause = patch.object(ai_client, "_pause", self.pauses.append)  # sem dormir de verdade
        pause.start()
        self.addCleanup(pause.stop)

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
        rounds = 1 + len(ai_client.TRANSIENT_RETRY_DELAYS)
        self.assertEqual(len(fake.calls), 3 * rounds)  # cada rodada percorre a cadeia toda
        self.assertEqual(len(self.pauses), rounds - 1)

    def test_other_providers_never_fall_back_to_gemini(self):
        fake = FakeRequest({"gpt-4o": http_error(503)})
        with patch.object(ai_client, "_request", fake), self.assertRaises(AiUnavailableError):
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="openai", model="gpt-4o", api_key="k")
        self.assertEqual(set(fake.calls), {"gpt-4o"})  # só repete o mesmo modelo, nunca troca de provedor

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

    # --- rodadas de retry para sobrecarga passageira (503 "high demand") ---

    def test_503_on_every_model_is_retried_with_backoff_until_one_recovers(self):
        outcomes = iter([http_error(503)] * 3 + ['{"ok": true}'])  # 1ª rodada inteira falha; 2ª rodada: o primário volta

        def request(**kwargs):
            calls.append(kwargs["model"])
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return ai_client._Reply(outcome, False)

        calls = []
        with patch.object(ai_client, "_request", request):
            result = ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="k")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(self.pauses), 1)
        self.assertTrue(1.0 <= self.pauses[0] <= 3.0)  # ~2s com jitter

    def test_connection_timeout_falls_back_and_is_retried_like_a_503(self):
        timeout = AiUnavailableError("IA indisponível: timed out")
        timeout.transient = True
        result, calls, used = self.call({PRIMARY: timeout, F1: '{"ok": true}'})
        self.assertEqual((result, calls, used), ({"ok": True}, [PRIMARY, F1], [F1]))

    def test_quota_429_is_not_retried_in_rounds_because_waiting_seconds_does_not_help(self):
        plan = {PRIMARY: http_error(429), F1: http_error(429), F2: http_error(429)}
        fake = FakeRequest(plan)
        with patch.object(ai_client, "_request", fake), self.assertRaises(AiUnavailableError):
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="k")
        self.assertEqual(fake.calls, [PRIMARY, F1, F2])
        self.assertEqual(self.pauses, [])

    def test_mixed_503_and_429_is_retried_in_rounds_like_the_production_log(self):
        # log real: "gemini-3.5-flash-lite: HTTP 503; gemini-3.8-flash: HTTP 429"
        plan = {PRIMARY: http_error(503), F1: http_error(429), F2: http_error(503)}
        fake = FakeRequest(plan)
        with patch.object(ai_client, "_request", fake), self.assertRaises(AiUnavailableError):
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="k")
        self.assertEqual(len(self.pauses), len(ai_client.TRANSIENT_RETRY_DELAYS))

    def test_429_then_recovery_on_the_second_round(self):
        outcomes = iter([http_error(503), http_error(429), http_error(503), '{"ok": true}'])
        calls = []

        def request(**kwargs):
            calls.append(kwargs["model"])
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return ai_client._Reply(outcome, False)

        with patch.object(ai_client, "_request", request):
            result = ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="k")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(self.pauses), 1)

    def test_retired_model_alongside_503_does_not_start_extra_rounds(self):
        gone = RuntimeError("HTTP 404")
        gone.http_status = 404
        fake = FakeRequest({PRIMARY: http_error(503), F1: gone, F2: http_error(503)})
        with patch.object(ai_client, "_request", fake), self.assertRaises(AiUnavailableError):
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="k")
        self.assertEqual(self.pauses, [])

    def test_missing_key_fails_immediately_without_retries(self):
        with self.assertRaises(AiUnavailableError):
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="")
        self.assertEqual(self.pauses, [])

    def test_transient_retries_can_be_limited_or_disabled_by_env(self):
        plan = {PRIMARY: http_error(503), F1: http_error(503), F2: http_error(503)}
        for value, rounds in (("0", 1), ("1", 2)):
            os.environ["AI_TRANSIENT_RETRIES"] = value
            ai_client._cooldown_until.clear()
            fake = FakeRequest(plan)
            with patch.object(ai_client, "_request", fake), self.assertRaises(AiUnavailableError):
                ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="k")
            self.assertEqual(len(fake.calls), 3 * rounds)

    def test_final_error_after_all_rounds_is_still_retryable_by_the_queue(self):
        plan = {PRIMARY: http_error(503), F1: http_error(503), F2: http_error(503)}
        with patch.object(ai_client, "_request", FakeRequest(plan)), self.assertRaises(AiUnavailableError) as ctx:
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="k")
        from jobs.job_queue import is_retryable_error
        self.assertTrue(is_retryable_error(ctx.exception))

    # --- TokenHarbor: último recurso gratuito, só depois de toda a cadeia do provedor principal ---

    TH1, TH2 = "deepseek-v4.1-flash:free", "mimo-v2.6-flash:free"

    def with_tokenharbor(self, key="thk_live_x", models=None):
        ai_client.set_tokenharbor_source(lambda: (key, [self.TH1, self.TH2] if models is None else models))
        self.addCleanup(ai_client.set_tokenharbor_source, None)

    def test_tokenharbor_is_only_tried_after_every_primary_model_failed(self):
        self.with_tokenharbor()
        plan = {PRIMARY: http_error(503), F1: http_error(503), F2: http_error(503), self.TH1: '{"ok": true}'}
        result, calls, used = self.call(plan)
        self.assertEqual((result, calls), ({"ok": True}, [PRIMARY, F1, F2, self.TH1]))
        self.assertEqual(used, [f"tokenharbor/{self.TH1}"])  # o rótulo deixa claro qual provedor respondeu

    def test_primary_success_never_touches_tokenharbor(self):
        self.with_tokenharbor()
        _, calls, used = self.call({PRIMARY: '{"ok": true}'})
        self.assertEqual((calls, used), ([PRIMARY], [PRIMARY]))

    def test_tokenharbor_is_off_without_key_or_without_models(self):
        for key, models in (("", None), ("thk_live_x", [])):
            ai_client._cooldown_until.clear()
            self.with_tokenharbor(key, models)
            plan = {PRIMARY: http_error(503), F1: http_error(503), F2: http_error(503)}
            fake = FakeRequest(plan)
            with patch.object(ai_client, "_request", fake), self.assertRaises(AiUnavailableError):
                ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="k")
            self.assertNotIn(self.TH1, fake.calls)

    def test_tokenharbor_gets_its_own_key_and_provider(self):
        self.with_tokenharbor("thk_live_secret")
        seen = []

        def request(**kwargs):
            seen.append((kwargs["provider"], kwargs["model"], kwargs["api_key"]))
            if kwargs["provider"] == "gemini":
                raise http_error(503)
            return ai_client._Reply('{"ok": true}', False)

        with patch.object(ai_client, "_request", request):
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="gemini-key")
        self.assertEqual(seen[-1], ("tokenharbor", self.TH1, "thk_live_secret"))
        self.assertTrue(all(key == "gemini-key" for prov, _, key in seen if prov == "gemini"))  # a chave Gemini não vaza

    def test_broken_tokenharbor_key_does_not_mask_the_primary_failure_or_skip_the_backoff_rounds(self):
        self.with_tokenharbor()
        plan = {PRIMARY: http_error(503), F1: http_error(503), F2: http_error(503),
                self.TH1: http_error(401), self.TH2: http_error(401)}
        fake = FakeRequest(plan)
        with patch.object(ai_client, "_request", fake), self.assertRaises(AiUnavailableError) as ctx:
            ai_client.call_ai_json(prompt="x", schema=SCHEMA, provider="gemini", model=PRIMARY, api_key="k")
        self.assertIn(f"tokenharbor/{self.TH1}: HTTP 401", str(ctx.exception))
        self.assertEqual(len(self.pauses), len(ai_client.TRANSIENT_RETRY_DELAYS))  # 401 do TokenHarbor não cancela o retry

    def test_tokenharbor_request_is_plain_chat_completions_with_the_schema_in_the_prompt(self):
        captured = {}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *exc): return False
            def read(self): return b'{"choices":[{"message":{"content":"```json\\n{\\"ok\\": true}\\n```"},"finish_reason":"stop"}]}'

        def fake_urlopen(request, timeout):
            captured["url"], captured["auth"] = request.full_url, request.get_header("Authorization")
            captured["body"] = json.loads(request.data)
            return Response()

        with patch.object(ai_client, "urlopen", fake_urlopen):
            result = ai_client.call_ai_json(prompt="oi", schema=SCHEMA, provider="tokenharbor", model=self.TH1, api_key="thk_live_x")
        self.assertEqual(result, {"ok": True})  # cerca ```json é tolerada
        self.assertEqual(captured["url"], "https://tokenharbor.ai/v1/chat/completions")
        self.assertEqual(captured["auth"], "Bearer thk_live_x")
        self.assertNotIn("response_format", captured["body"])  # structured output não é documentado para os :free
        self.assertIn('"ok"', captured["body"]["messages"][0]["content"])  # schema vai no prompt

    def test_model_list_parsing_trims_and_dedupes(self):
        self.assertEqual(ai_client.parse_model_list(" a:free, b ,a:free,\n c,, "), ["a:free", "b", "c"])


if __name__ == "__main__":
    unittest.main()
