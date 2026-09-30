"""LLM router unit tests. Uses httpx MockTransport so no network calls are made."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from backend.config import Settings
import backend.tools.llm_router as router_mod
from backend.tools.llm_router import (
    DegradedModeSignal,
    LLMConfigError,
    LLMPermanentError,
    LLMTransientError,
    _raise_for_http_class,
    call_llm,
)


def _settings(**overrides) -> Settings:
    base = dict(
        supabase_url="",
        supabase_anon_key="",
        supabase_service_role_key="",
        supabase_jwt_secret="",
        llm_primary_provider="gemini",
        llm_primary_model="gemini-2.0-flash",
        llm_primary_api_key="p-key",
        llm_fallback_provider="groq",
        llm_fallback_model="llama-3.1-8b-instant",
        llm_fallback_api_key="f-key",
        llm_request_timeout_s=5,
        llm_max_retries=2,
    )
    base.update(overrides)
    return Settings(**base)


def test_http_class_transient_on_429():
    resp = httpx.Response(429, text="rate limited")
    with pytest.raises(LLMTransientError):
        _raise_for_http_class(resp, provider="gemini")


def test_http_class_transient_on_5xx():
    resp = httpx.Response(503, text="unavailable")
    with pytest.raises(LLMTransientError):
        _raise_for_http_class(resp, provider="gemini")


def test_http_class_permanent_on_4xx_non_429():
    resp = httpx.Response(401, text="bad key")
    with pytest.raises(LLMPermanentError):
        _raise_for_http_class(resp, provider="gemini")


def test_router_raises_config_error_when_no_keys_set():
    s = _settings(llm_primary_api_key="", llm_fallback_api_key="")
    with pytest.raises(LLMConfigError):
        asyncio.run(call_llm("hello", settings=s))


def test_router_raises_config_error_on_unknown_provider():
    s = _settings(llm_primary_provider="unknown-provider")
    with pytest.raises(LLMConfigError):
        asyncio.run(call_llm("hello", settings=s))


def test_router_falls_back_when_primary_permanent_failure(monkeypatch):
    """Primary returns 401, router should try fallback and succeed."""
    calls: list[tuple[str, str]] = []

    async def fake_gemini(**kw):
        calls.append(("gemini", kw["model"]))
        raise LLMPermanentError("bad key")

    async def fake_groq(**kw):
        calls.append(("groq", kw["model"]))
        from backend.tools.llm_router import LLMResult
        return LLMResult(text="ok", tokens_used=5, latency_ms=1, provider="groq", model=kw["model"])

    monkeypatch.setitem(router_mod._PROVIDERS, "gemini", fake_gemini)
    monkeypatch.setitem(router_mod._PROVIDERS, "groq", fake_groq)
    s = _settings()
    result = asyncio.run(call_llm("hi", settings=s))
    assert result.provider == "groq"
    assert [c[0] for c in calls] == ["gemini", "groq"]


def test_router_retries_transient_then_falls_back(monkeypatch):
    """Primary always transient, router should retry N times then try fallback."""
    primary_attempts = 0

    async def fake_gemini(**kw):
        nonlocal primary_attempts
        primary_attempts += 1
        raise LLMTransientError("timeout")

    async def fake_groq(**kw):
        from backend.tools.llm_router import LLMResult
        return LLMResult(text="ok", tokens_used=1, latency_ms=1, provider="groq", model=kw["model"])

    monkeypatch.setitem(router_mod._PROVIDERS, "gemini", fake_gemini)
    monkeypatch.setitem(router_mod._PROVIDERS, "groq", fake_groq)
    # zero-out backoff sleep so the test is fast (bind original to avoid self-recursion)
    _original_sleep = asyncio.sleep

    async def _instant_sleep(_delay):
        await _original_sleep(0)

    monkeypatch.setattr(router_mod.asyncio, "sleep", _instant_sleep)
    s = _settings(llm_max_retries=3)
    result = asyncio.run(call_llm("hi", settings=s))
    assert result.provider == "groq"
    assert primary_attempts == 3


def test_router_raises_degraded_mode_when_both_fail(monkeypatch):
    async def failing(**kw):
        raise LLMPermanentError("nope")

    monkeypatch.setitem(router_mod._PROVIDERS, "gemini", failing)
    monkeypatch.setitem(router_mod._PROVIDERS, "groq", failing)
    with pytest.raises(DegradedModeSignal):
        asyncio.run(call_llm("hi", settings=_settings()))
