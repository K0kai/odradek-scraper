"""Transporte de IA síncrono compartilhado (Gemini/OpenAI).

Único ponto de chamada HTTP para os provedores:
- call_ai_text: 1 prompt → 1 texto.
- call_ai_json: 1 prompt + JSON Schema estrito → 1 objeto (com 1 retry em JSON
  inválido/truncado; falha final vira AiResponseError, que a fila trata como
  transitória em vez de bloquear a vaga).

Levanta AiUnavailableError para chave/quota/indisponibilidade, RuntimeError para
HTTP genérico.
"""
from __future__ import annotations

import copy
import json
import os
import random
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from candidate.resume_pipeline import AiResponseError, AiUnavailableError

QUOTA_STATUSES = {401, 403, 429, 502, 503, 504}

# Fallback de modelo, só Gemini e só modelos com camada gratuita (mesma chave, sem custo extra).
# O 503 "high demand" e a quota (429) são POR MODELO: outro modelo costuma responder na hora.
# AI_FALLBACK_MODELS (separado por vírgula) substitui a lista; vazio desliga o fallback.
# Modelos saem do ar: um 404 ("no longer available") só faz a cadeia pular para o próximo, nunca derruba a chamada.
# gemini-2.5-flash e gemini-2.5-flash-lite foram aposentados (404 "no longer available"). Ajustável por AI_FALLBACK_MODELS.
DEFAULT_FALLBACK_MODELS = ("gemini-3.8-flash",)
OVERLOAD_STATUSES = {429, 500, 502, 503, 504}  # 401/403 = chave/permissão: trocar de modelo não ajuda
GONE_STATUS = 404  # modelo aposentado/inexistente para esta conta
FALLBACK_STATUSES = OVERLOAD_STATUSES | {GONE_STATUS}
# 503 "high demand" costuma durar segundos: antes de desistir, repete a cadeia inteira algumas vezes com backoff.
TRANSIENT_STATUSES = {500, 502, 503, 504}  # falha do lado do provedor
RATE_LIMIT_STATUS = 429  # quota por minuto (RPM) passa em segundos; só entra nas rodadas junto de uma falha transitória
TRANSIENT_RETRY_DELAYS = (2.0, 6.0, 15.0)  # pausa antes de cada nova rodada; AI_TRANSIENT_RETRIES limita/desliga
COOLDOWN_SECONDS = 90  # sobrecarga é passageira
GONE_COOLDOWN_SECONDS = 6 * 3600  # modelo aposentado não volta tão cedo: não paga uma chamada falha por vaga
_cooldown_until: dict[tuple[str, str], float] = {}
_cooldown_lock = threading.Lock()


def fallback_models() -> list[str]:
    raw = os.environ.get("AI_FALLBACK_MODELS")
    if raw is None:
        return list(DEFAULT_FALLBACK_MODELS)
    return [item.strip() for item in raw.split(",") if item.strip()]


# Último recurso: modelos gratuitos do TokenHarbor (OpenAI-compatível), tentados só depois que todos os do provedor
# principal falharam. Só ativo com chave salva. Prompts de rota gratuita podem ser retidos e usados pelos provedores
# dos modelos (política de privacidade do TokenHarbor), por isso o uso é opt-in pela chave.
TOKENHARBOR = "tokenharbor"
TOKENHARBOR_ENDPOINT = "https://tokenharbor.ai/v1/chat/completions"
DEFAULT_TOKENHARBOR_MODELS = ("deepseek-v4.1-flash:free", "mimo-v2.6-flash:free")
_TH_CACHE_SECONDS = 30.0
_th_source = None  # callable -> (api_key, [modelos]); definido pelo app para ler painel/cofre
_th_cache: tuple[float, tuple[str, list[str]]] | None = None
_th_lock = threading.Lock()


def set_tokenharbor_source(source) -> None:
    """Registra de onde vêm a chave e a lista de modelos (o app liga ao cofre/configurações)."""
    global _th_source, _th_cache
    with _th_lock:
        _th_source, _th_cache = source, None


def parse_model_list(raw: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in (raw or "").replace("\n", ",").split(",") if item.strip()))


def tokenharbor_config() -> tuple[str, list[str]]:
    """(chave, modelos); chave vazia ou lista vazia = fallback desligado. Nunca levanta."""
    global _th_cache
    with _th_lock:
        source, cached = _th_source, _th_cache
    if source is None:
        return "", []
    if cached and cached[0] > time.monotonic():
        return cached[1]
    try:
        key, models = source()
        value = ((key or "").strip(), list(models or []))
    except Exception:
        value = ("", [])
    with _th_lock:
        _th_cache = (time.monotonic() + _TH_CACHE_SECONDS, value)
    return value


def _cooling_down(provider: str, model: str) -> bool:
    with _cooldown_lock:
        return _cooldown_until.get((provider, model), 0.0) > time.monotonic()


def _start_cooldown(provider: str, model: str, seconds: float = COOLDOWN_SECONDS) -> None:
    with _cooldown_lock:
        _cooldown_until[(provider, model)] = time.monotonic() + seconds


def model_chain(provider: str, model: str) -> list[str]:
    """Modelo configurado primeiro; fallbacks gratuitos depois. Modelo em cooldown vai para o fim."""
    provider = provider.casefold().strip()
    if provider != "gemini":
        return [model]
    others = [m for m in fallback_models() if m.casefold() != model.casefold()]
    chain = [model, *dict.fromkeys(others)]
    # Quem falhou há pouco (sobrecarga ou modelo aposentado) vai para o fim, mantendo a ordem relativa.
    return [m for m in chain if not _cooling_down(provider, m)] + [m for m in chain if _cooling_down(provider, m)]


def _should_fall_back(exc: Exception) -> bool:
    """Sobrecarga/quota do modelo, queda de rede/timeout ou resposta inutilizável: outro modelo pode resolver."""
    if isinstance(exc, AiResponseError) or getattr(exc, "transient", False):
        return True
    return getattr(exc, "http_status", None) in FALLBACK_STATUSES


def _is_transient(exc: BaseException) -> bool:
    """Falha que costuma passar sozinha em segundos (5xx, timeout, conexão), ao contrário de quota, chave ou modelo aposentado."""
    return bool(getattr(exc, "transient", False)) or getattr(exc, "http_status", None) in TRANSIENT_STATUSES


def _worth_another_round(errors: list[BaseException]) -> bool:
    """Vale repetir a cadeia depois de um backoff?

    Sim quando toda falha é passageira (5xx/timeout) ou limite por minuto (429) e há ao menos uma passageira,
    como "503 num modelo + 429 no outro". 429 puro (cota esgotada), 404, 401/403 e JSON inutilizável não melhoram esperando segundos.
    """
    if not errors or not any(_is_transient(err) for err in errors):
        return False
    return all(_is_transient(err) or getattr(err, "http_status", None) == RATE_LIMIT_STATUS for err in errors)


def transient_retry_delays() -> tuple[float, ...]:
    raw = os.environ.get("AI_TRANSIENT_RETRIES")
    if raw is None or not raw.strip():
        return TRANSIENT_RETRY_DELAYS
    try:
        rounds = max(0, int(raw))
    except ValueError:
        return TRANSIENT_RETRY_DELAYS
    return TRANSIENT_RETRY_DELAYS[:rounds]


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _failure_label(exc: BaseException) -> str:
    if isinstance(exc, AiResponseError):
        return "resposta inutilizável"
    status = getattr(exc, "http_status", None)
    return f"HTTP {status}" if status else exc.__class__.__name__


def _walk_chain(provider: str, model: str, call, on_model=None):
    """Uma passada pela cadeia de modelos; se todos falharem, levanta o erro final com `.chain_transient`.

    `call(provedor, modelo)` executa uma tentativa. A cadeia do provedor principal vem primeiro; os modelos
    gratuitos do TokenHarbor (se configurados) só entram depois que todos eles falharam.
    """
    primary = provider.casefold().strip()
    chain = [(primary, name) for name in model_chain(provider, model)]
    extras_loaded = primary == TOKENHARBOR
    failures: list[tuple[str, BaseException]] = []
    weighed: list[BaseException] = []
    index = 0
    while index < len(chain):
        prov, candidate = chain[index]
        label = candidate if prov == primary else f"{prov}/{candidate}"
        try:
            result = call(prov, candidate)
        except RuntimeError as exc:  # inclui AiUnavailableError/AiResponseError e o 404 de modelo aposentado
            # Falha do último recurso (chave/permissão do TokenHarbor) não mascara o erro do provedor principal.
            if prov == primary and not _should_fall_back(exc):
                raise
            failures.append((label, exc))
            # Uma chave/modelo do TokenHarbor quebrados não podem impedir as rodadas de backoff do principal.
            if prov == primary or _is_transient(exc) or getattr(exc, "http_status", None) == RATE_LIMIT_STATUS:
                weighed.append(exc)
            if not isinstance(exc, AiResponseError):
                gone = getattr(exc, "http_status", None) == GONE_STATUS
                _start_cooldown(prov, candidate, GONE_COOLDOWN_SECONDS if gone else COOLDOWN_SECONDS)
            index += 1
            if index == len(chain) and not extras_loaded:
                extras_loaded = True
                key, models = tokenharbor_config()
                if key:
                    ordered = [m for m in models if not _cooling_down(TOKENHARBOR, m)] + [m for m in models if _cooling_down(TOKENHARBOR, m)]
                    chain.extend((TOKENHARBOR, name) for name in ordered)
            if index < len(chain):
                continue
            summary = "; ".join(f"{name}: {_failure_label(err)}" for name, err in failures)
            final = AiUnavailableError(f"IA indisponível: nenhum modelo respondeu ({summary}).")
            final.http_status = getattr(exc, "http_status", None)  # type: ignore[attr-defined]
            if isinstance(exc, AiResponseError):
                final = AiResponseError(str(final))
            final.chain_transient = _worth_another_round(weighed)  # type: ignore[attr-defined]
            raise final from exc
        if on_model is not None:
            on_model(label)
        return result
    raise AiUnavailableError("IA indisponível: nenhum modelo respondeu.")  # cadeia vazia: não ocorre


def _credentials(call_provider: str, primary_provider: str, api_key: str) -> str:
    """Chave da tentativa: a do provedor principal, ou a do TokenHarbor nos modelos de último recurso."""
    if call_provider == primary_provider.casefold().strip():
        return api_key
    return tokenharbor_config()[0]


def _with_fallback(provider: str, model: str, call, on_model=None):
    """Executa call(modelo) pela cadeia, com novas rodadas quando a falha é só sobrecarga passageira.

    Se todos os modelos falharem por motivo de modelo (sobrecarga, quota, aposentado, JSON inutilizável), o erro
    final é AiUnavailableError, que a fila repete depois, e lista o resultado de cada modelo.
    Quando TODAS as falhas da passada são transitórias (503/5xx, timeout), a cadeia é repetida após um backoff
    com jitter (TRANSIENT_RETRY_DELAYS) antes de desistir: o pico de "high demand" costuma passar em segundos.
    Erros que trocar de modelo não resolve (chave inválida, permissão) sobem como vieram.
    """
    delays = transient_retry_delays()
    for attempt in range(len(delays) + 1):
        try:
            return _walk_chain(provider, model, call, on_model)
        except RuntimeError as exc:
            if attempt >= len(delays) or not getattr(exc, "chain_transient", False):
                raise
            _pause(delays[attempt] * random.uniform(0.75, 1.25))  # jitter: workers paralelos não voltam em sincronia
    raise AiUnavailableError("IA indisponível: nenhum modelo respondeu.")  # inalcançável


def _detail_from_body(body: str, *, code: int, provider: str, model: str, operation: str, api_key: str) -> str:
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    parts = []
    if isinstance(error, dict):
        for field in ("status", "code", "param", "message"):
            value = error.get(field)
            if isinstance(value, (str, int)) and value:
                parts.append(str(value))
    elif isinstance(error, str):
        parts.append(error)
    # Do not copy arbitrary HTML/proxy responses, which may echo the request.
    detail = " | ".join(parts) or "O provedor não retornou uma explicação JSON."
    if api_key:
        detail = detail.replace(api_key, "[chave omitida]").replace(quote_plus(api_key), "[chave omitida]")
    detail = " ".join(detail.split())[:700]
    return f"{operation}: {provider}/{model} (HTTP {code}): {detail}"


def ai_http_error_detail(exc: HTTPError, *, provider: str, model: str, operation: str, api_key: str) -> str:
    """Keep the provider's explanation without exposing credentials or request URLs."""
    body = exc.read(65536).decode("utf-8", errors="replace")
    return _detail_from_body(body, code=exc.code, provider=provider, model=model, operation=operation, api_key=api_key)


def strictify_schema(schema: dict) -> dict:
    """Cópia do schema em modo estrito: todo objeto fecha propriedades e as exige todas.

    Exigido pelo structured output da OpenAI (strict) e inofensivo para o Gemini.
    """
    def walk(node):
        if isinstance(node, dict):
            out = {key: walk(value) for key, value in node.items()}
            if out.get("type") == "object" and isinstance(out.get("properties"), dict):
                out["additionalProperties"] = False
                out["required"] = list(out["properties"].keys())
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(copy.deepcopy(schema))


class _Reply:
    __slots__ = ("text", "truncated")

    def __init__(self, text: str, truncated: bool) -> None:
        self.text = text
        self.truncated = truncated


def _request(
    *,
    prompt: str,
    provider: str,
    model: str,
    api_key: str,
    max_output_tokens: int,
    temperature: float | None,
    timeout: int,
    schema: dict | None = None,
    thinking_budget: int | None = None,
    use_schema: bool = True,
) -> _Reply:
    if not api_key:
        raise AiUnavailableError("IA indisponivel: chave nao configurada.")
    provider = provider.casefold().strip()
    result: dict | None = None
    try:
        if provider == "openai":
            body: dict = {"model": model, "input": prompt, "store": False, "max_output_tokens": max_output_tokens}
            if schema is not None:
                body["text"] = {"format": {"type": "json_schema", "name": "result", "strict": True, "schema": strictify_schema(schema)}}
            if temperature is not None and model.casefold().startswith("gpt-4"):
                body["temperature"] = temperature
            endpoint = "https://api.openai.com/v1/responses"
            payload = json.dumps(body).encode("utf-8")
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        elif provider == TOKENHARBOR:
            # Chat Completions puro: structured output não é documentado para os modelos gratuitos, então o schema
            # vai no prompt e a resposta passa pelo parser tolerante (cercas ```json, texto extra).
            content = prompt
            if schema is not None:
                content += (
                    "\n\nRespond ONLY with one valid JSON object (no markdown, no commentary) that follows this JSON Schema:\n"
                    + json.dumps(schema, ensure_ascii=False)
                )
            body = {"model": model, "messages": [{"role": "user", "content": content}], "max_tokens": max_output_tokens}
            if temperature is not None:
                body["temperature"] = temperature
            endpoint = TOKENHARBOR_ENDPOINT
            payload = json.dumps(body).encode("utf-8")
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        elif provider == "gemini":
            config: dict = {"maxOutputTokens": max_output_tokens}
            if temperature is not None:
                config["temperature"] = temperature
            if schema is not None:
                config["responseMimeType"] = "application/json"
                if use_schema:
                    config["responseJsonSchema"] = strictify_schema(schema)
            if thinking_budget is not None and model.casefold().startswith("gemini-2.5"):
                config["thinkingConfig"] = {"thinkingBudget": thinking_budget}
            endpoint = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{quote_plus(model)}:generateContent?key={quote_plus(api_key)}"
            )
            payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config}).encode("utf-8")
            headers = {"Content-Type": "application/json"}
        else:
            raise ValueError("Provedor de IA invalido.")
        request = Request(endpoint, data=payload, headers=headers, method="POST")
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body_text = exc.read(65536).decode("utf-8", errors="replace")
        quota = exc.code in QUOTA_STATUSES or "quota" in body_text[:300].casefold()
        _record(provider, model, None, ok=False, quota_error=quota)
        detail = _detail_from_body(body_text, code=exc.code, provider=provider, model=model, operation="Chamada de IA", api_key=api_key)
        if quota:
            unavailable = AiUnavailableError(f"IA indisponível: {detail}")
            unavailable.http_status = exc.code  # type: ignore[attr-defined]
            raise unavailable from exc
        err = RuntimeError(detail)
        err.http_status = exc.code  # type: ignore[attr-defined]
        raise err from exc
    except (URLError, TimeoutError, ConnectionError) as exc:
        _record(provider, model, None, ok=False, quota_error=True)
        unavailable = AiUnavailableError(f"IA indisponível: {exc}")
        unavailable.transient = True  # type: ignore[attr-defined]  # timeout/queda de conexão: tenta outro modelo e repete
        raise unavailable from exc
    except AiUnavailableError:
        raise
    except Exception as exc:
        msg = str(exc).casefold()
        quota = "quota" in msg or "api key" in msg or "timed out" in msg or "unavailable" in msg
        _record(provider, model, None, ok=False, quota_error=quota)
        if quota:
            raise AiUnavailableError(f"IA indisponível: {exc}") from exc
        raise

    # O contador de uso entende o formato Gemini/OpenAI; as respostas do TokenHarbor só contam como chamada.
    _record(provider, model, result if isinstance(result, dict) and provider != TOKENHARBOR else None, ok=True, quota_error=False)
    result = result if isinstance(result, dict) else {}
    if provider == TOKENHARBOR:
        choices = result.get("choices") or [{}]
        message = (choices[0].get("message") or {}) if isinstance(choices[0], dict) else {}
        content = message.get("content")
        text = (content if isinstance(content, str) else "").strip()
        truncated = isinstance(choices[0], dict) and choices[0].get("finish_reason") == "length"
    elif provider == "openai":
        text = "\n".join(
            part.get("text", "")
            for item in result.get("output", [])
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ).strip()
        details = result.get("incomplete_details") or {}
        truncated = result.get("status") == "incomplete" and details.get("reason") == "max_output_tokens"
    else:
        candidates = result.get("candidates", [])
        text = "\n".join(
            part.get("text", "")
            for item in candidates
            for part in item.get("content", {}).get("parts", [])
            if not part.get("thought")
        ).strip()
        truncated = any(item.get("finishReason") == "MAX_TOKENS" for item in candidates)
    return _Reply(text, truncated)


def call_ai_text(
    *,
    prompt: str,
    provider: str,
    model: str,
    api_key: str,
    max_output_tokens: int = 450,
    temperature: float = 0.2,
    timeout: int = 60,
    on_model=None,
) -> str:
    """Chama o provedor configurado e devolve o texto puro da resposta (com fallback de modelo)."""
    return _with_fallback(provider, model, lambda prov, m: _request(
        prompt=prompt, provider=prov, model=m, api_key=_credentials(prov, provider, api_key),
        max_output_tokens=max_output_tokens,
        temperature=temperature if prov in ("gemini", TOKENHARBOR) else None,
        timeout=timeout,
    ).text, on_model)


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def _parse_json_object(text: str) -> dict:
    cleaned = _FENCE.sub("", text.strip()).strip()
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("a resposta não é um objeto JSON")
    return data


def call_ai_json(
    *,
    prompt: str,
    schema: dict,
    provider: str,
    model: str,
    api_key: str,
    max_output_tokens: int = 4096,
    temperature: float = 0.0,
    thinking_budget: int | None = None,
    timeout: int = 90,
    on_model=None,
) -> dict:
    """call_ai_json com fallback de modelo gratuito; `on_model(nome)` recebe o modelo que respondeu."""
    return _with_fallback(provider, model, lambda prov, m: _call_ai_json_once(
        prompt=prompt, schema=schema, provider=prov, model=m, api_key=_credentials(prov, provider, api_key),
        max_output_tokens=max_output_tokens, temperature=temperature, thinking_budget=thinking_budget, timeout=timeout,
    ), on_model)


def _call_ai_json_once(
    *,
    prompt: str,
    schema: dict,
    provider: str,
    model: str,
    api_key: str,
    max_output_tokens: int = 4096,
    temperature: float = 0.0,
    thinking_budget: int | None = None,
    timeout: int = 90,
) -> dict:
    """Chama a IA exigindo JSON conforme `schema`.

    Um retry cobre JSON inválido e saída truncada (dobrando o orçamento de tokens);
    se ainda falhar, levanta AiResponseError (transitória para a fila).
    """
    tokens = max_output_tokens
    use_schema = True
    last_problem = "resposta vazia"
    for _ in range(3):
        try:
            reply = _request(
                prompt=prompt, provider=provider, model=model, api_key=api_key,
                max_output_tokens=tokens, temperature=temperature, timeout=timeout,
                schema=schema, thinking_budget=thinking_budget, use_schema=use_schema,
            )
        except RuntimeError as exc:
            # Alguns modelos Gemini rejeitam responseJsonSchema: repete uma vez sem schema nativo.
            if getattr(exc, "http_status", None) == 400 and use_schema and provider.casefold().strip() == "gemini":
                use_schema = False
                continue
            raise
        if reply.truncated:
            last_problem = f"saída truncada em {tokens} tokens"
            tokens = min(tokens * 2, 32768)
            continue
        try:
            return _parse_json_object(reply.text)
        except (ValueError, TypeError) as exc:
            last_problem = f"JSON inválido ({exc})"
    raise AiResponseError(f"IA indisponível: resposta inutilizável do modelo ({last_problem}).")


def _record(provider: str, model: str, result: dict | None, *, ok: bool, quota_error: bool) -> None:
    try:
        from integrations.usage_tracker import try_record_ai_result

        try_record_ai_result(
            provider=provider,
            model=model,
            result=result,
            ok=ok,
            quota_error=quota_error,
        )
    except Exception:
        pass
