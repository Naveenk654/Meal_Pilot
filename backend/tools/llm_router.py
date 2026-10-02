"""LLM router (§19). Single entrypoint for every LLM call.

- Primary + fallback providers, both env-configured (no model IDs hardcoded).
- Exponential-backoff retries on transient errors (429, 5xx, network).
- Permanent errors (4xx auth/schema) bail out of the current provider immediately.
- Both providers unavailable → raise DegradedModeSignal. The caller marks
  agent_runs.degraded_mode=true and uses the verified-data path.

Only two providers wired in M1: Gemini (native) and Groq (OpenAI-compatible).
Add a callable to _PROVIDERS to support another.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from backend.config import Settings, get_settings


class LLMConfigError(RuntimeError):
    """Router config is invalid — e.g. unknown provider name."""


class LLMTransientError(RuntimeError):
    """Retryable failure. Router will back off and retry, then fall through providers."""


class LLMPermanentError(RuntimeError):
    """Non-retryable (bad key, bad request). Router immediately tries the fallback."""


class DegradedModeSignal(RuntimeError):
    """§19 — primary AND fallback exhausted. Caller must set degraded_mode=true."""


@dataclass(frozen=True)
class LLMResult:
    text: str
    tokens_used: int
    latency_ms: int
    provider: str
    model: str


# --- Provider implementations -----------------------------------------------


async def _call_gemini(
    *,
    model: str,
    api_key: str,
    prompt: str,
    system: str | None,
    temperature: float,
    max_tokens: int,
    timeout_s: int,
    response_format: str | None = None,
) -> LLMResult:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    generation_config: dict = {
        "temperature": temperature,
        "maxOutputTokens": max_tokens,
    }
    if response_format == "json":
        # Without this, Gemini 2.0 Flash will often think-out-loud in prose
        # ("We need to generate 4 distinct candidate plans...") instead of
        # emitting the schema we asked for. Forcing the MIME type turns on
        # Gemini's constrained-JSON decoding.
        generation_config["responseMimeType"] = "application/json"
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    started = asyncio.get_event_loop().time()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(url, params={"key": api_key}, json=body)
    except httpx.HTTPError as exc:
        raise LLMTransientError(f"gemini network error: {exc}") from exc
    _raise_for_http_class(resp, provider="gemini")
    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise LLMPermanentError(f"gemini response missing text: {data}") from exc
    tokens = int(data.get("usageMetadata", {}).get("totalTokenCount", 0))
    latency_ms = int((asyncio.get_event_loop().time() - started) * 1000)
    return LLMResult(
        text=text, tokens_used=tokens, latency_ms=latency_ms, provider="gemini", model=model
    )


async def _call_groq(
    *,
    model: str,
    api_key: str,
    prompt: str,
    system: str | None,
    temperature: float,
    max_tokens: int,
    timeout_s: int,
    response_format: str | None = None,
) -> LLMResult:
    url = "https://api.groq.com/openai/v1/chat/completions"
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body: dict = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if response_format == "json":
        # Groq (OpenAI-compatible): require a well-formed JSON object. Prompt
        # must still include the word "json" per OpenAI spec.
        body["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {api_key}"}
    started = asyncio.get_event_loop().time()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise LLMTransientError(f"groq network error: {exc}") from exc
    _raise_for_http_class(resp, provider="groq")
    data = resp.json()
    try:
        message = data["choices"][0]["message"]
        text = message.get("content") or ""
        # Reasoning models (gpt-oss-*, qwen-thinking) emit tokens in `reasoning`
        # and often leave `content` empty when max_tokens truncates the thought
        # phase. Fall back to reasoning if content is missing.
        if not text.strip():
            text = message.get("reasoning") or ""
    except (KeyError, IndexError) as exc:
        raise LLMPermanentError(f"groq response missing content: {data}") from exc
    tokens = int(data.get("usage", {}).get("total_tokens", 0))
    latency_ms = int((asyncio.get_event_loop().time() - started) * 1000)
    return LLMResult(
        text=text, tokens_used=tokens, latency_ms=latency_ms, provider="groq", model=model
    )


def _raise_for_http_class(resp: httpx.Response, *, provider: str) -> None:
    if resp.status_code < 400:
        return
    body = resp.text[:500]
    if resp.status_code == 429 or resp.status_code >= 500:
        raise LLMTransientError(f"{provider} HTTP {resp.status_code}: {body}")
    raise LLMPermanentError(f"{provider} HTTP {resp.status_code}: {body}")


# Provider registry — model IDs never live here, only wire protocols.
_ProviderCall = Callable[..., Awaitable[LLMResult]]
_PROVIDERS: dict[str, _ProviderCall] = {
    "gemini": _call_gemini,
    "groq": _call_groq,
}


# --- Router -----------------------------------------------------------------


@dataclass(frozen=True)
class _ProviderConfig:
    provider: str
    model: str
    api_key: str


def _providers_from_settings(s: Settings) -> list[_ProviderConfig]:
    return [
        _ProviderConfig(s.llm_primary_provider, s.llm_primary_model, s.llm_primary_api_key),
        _ProviderConfig(s.llm_fallback_provider, s.llm_fallback_model, s.llm_fallback_api_key),
    ]


_CACHE: dict[str, tuple[float, LLMResult]] = {}
_CACHE_TTL_S = 45.0


def _cache_key(prompt: str, system: str | None, temperature: float, max_tokens: int) -> str:
    from hashlib import sha256
    payload = f"{system or ''}\x1e{prompt}\x1e{temperature}\x1e{max_tokens}"
    return sha256(payload.encode("utf-8")).hexdigest()


async def call_llm(
    prompt: str,
    *,
    system: str | None = None,
    temperature: float = 0.2,
    max_tokens: int = 1024,
    settings: Settings | None = None,
    response_format: str | None = None,
) -> LLMResult:
    """§19 — the sole LLM entrypoint. Every agent tool call routes through here.

    `response_format="json"` turns on provider-native JSON mode (Gemini
    `responseMimeType`, Groq `response_format={"type":"json_object"}`). Without
    it, Gemini 2.0 Flash in particular often returns prose "I need to..."
    reasoning instead of the schema. Callers whose output must parse as JSON
    (candidate_gen, menu_extract, pattern_detect) should pass "json".

    Deterministic (temperature=0) calls are cached in-process for `_CACHE_TTL_S`
    so repeated Generate clicks don't re-hit the provider."""
    import asyncio as _asyncio

    s = settings or get_settings()
    providers = _providers_from_settings(s)
    if not any(p.api_key for p in providers):
        raise LLMConfigError(
            "Neither LLM_PRIMARY_API_KEY nor LLM_FALLBACK_API_KEY is configured."
        )

    key = _cache_key(prompt, system, temperature, max_tokens) if temperature == 0.0 else None
    if key is not None:
        cached = _CACHE.get(key)
        if cached is not None:
            ts, result = cached
            if _asyncio.get_event_loop().time() - ts < _CACHE_TTL_S:
                return result

    last_error: Exception | None = None
    for cfg in providers:
        if not cfg.api_key:
            continue
        call = _PROVIDERS.get(cfg.provider)
        if call is None:
            raise LLMConfigError(f"Unknown LLM provider: {cfg.provider!r}")
        for attempt in range(s.llm_max_retries):
            try:
                result = await call(
                    model=cfg.model,
                    api_key=cfg.api_key,
                    prompt=prompt,
                    system=system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    timeout_s=s.llm_request_timeout_s,
                    response_format=response_format,
                )
                if key is not None:
                    _CACHE[key] = (_asyncio.get_event_loop().time(), result)
                return result
            except LLMTransientError as exc:
                last_error = exc
                if attempt + 1 < s.llm_max_retries:
                    await asyncio.sleep(2 ** attempt)
                    continue
                break  # exhausted retries on this provider → try fallback
            except LLMPermanentError as exc:
                last_error = exc
                break  # immediately try fallback
    raise DegradedModeSignal(
        f"primary and fallback LLMs unavailable; last_error={last_error!r}"
    )
