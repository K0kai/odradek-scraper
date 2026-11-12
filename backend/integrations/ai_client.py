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
DEFAULT_FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-2.5-flash-lite")
OVERLOAD_STATUSES = {429, 500, 502, 503, 504}  # 401/403 = chave/permissão: trocar de modelo não ajuda
PRIMARY_COOLDOWN_SECONDS = 90
_cooldown_until: dict[tuple[str, str], float] = {}
_cooldown_lock = threading.Lock()


def fallback_models() -> list[str]:
    raw = os.environ.get("AI_FALLBACK_MODELS")
    if raw is None:
        return list(DEFAULT_FALLBACK_MODELS)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _cooling_down(provider: str, model: str) -> bool:
    with _cooldown_lock:
        return _cooldown_until.get((provider, model), 0.0) > time.monotonic()


def _start_cooldown(provider: str, model: str) -> None:
    with _cooldown_lock:
        _cooldown_until[(provider, model)] = time.monotonic() + PRIMARY_COOLDOWN_SECONDS


def model_chain(provider: str, model: str) -> list[str]:
    """Modelo configurado primeiro; fallbacks gratuitos depois. Modelo em cooldown vai para o fim."""
    provider = provider.casefold().strip()
    if provider != "gemini":
        return [model]
    others = [m for m in fallback_models() if m.casefold() != model.casefold()]
    chain = [model, *dict.fromkeys(others)]
    if len(chain) > 1 and _cooling_down(provider, model):
        chain = [*chain[1:], model]  # primário sobrecarregado há pouco: tenta por último
    return chain


def _should_fall_back(exc: Exception) -> bool:
    """Sobrecarga/quota do modelo ou resposta inutilizável: outro modelo pode resolver."""
    if isinstance(exc, AiResponseError):
        return True
    return getattr(exc, "http_status", None) in OVERLOAD_STATUSES


def _with_fallback(provider: str, model: str, call, on_model=None):
    """Executa call(modelo) pela cadeia; o último erro sobe se todos falharem."""
    chain = model_chain(provider, model)
    for index, candidate in enumerate(chain):
        try:
            result = call(candidate)
        except AiUnavailableError as exc:
            if not _should_fall_back(exc) or index == len(chain) - 1:
                raise
            if candidate == model and not isinstance(exc, AiResponseError):
                _start_cooldown(provider.casefold().strip(), model)
            continue
        if on_model is not None:
            on_model(candidate)
        return result
    raise AiUnavailableError("IA indisponível: nenhum modelo respondeu.")  # cadeia vazia: não ocorre


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
        raise AiUnavailableError(f"IA indisponível: {exc}") from exc
    except AiUnavailableError:
        raise
    except Exception as exc:
        msg = str(exc).casefold()
        quota = "quota" in msg or "api key" in msg or "timed out" in msg or "unavailable" in msg
        _record(provider, model, None, ok=False, quota_error=quota)
        if quota:
            raise AiUnavailableError(f"IA indisponível: {exc}") from exc
        raise

    _record(provider, model, result if isinstance(result, dict) else None, ok=True, quota_error=False)
    result = result if isinstance(result, dict) else {}
    if provider == "openai":
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
    return _with_fallback(provider, model, lambda m: _request(
        prompt=prompt, provider=provider, model=m, api_key=api_key,
        max_output_tokens=max_output_tokens,
        temperature=temperature if provider.casefold().strip() == "gemini" else None,
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
    return _with_fallback(provider, model, lambda m: _call_ai_json_once(
        prompt=prompt, schema=schema, provider=provider, model=m, api_key=api_key,
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
