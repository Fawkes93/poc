"""Gemini Flash twice, then gemini-4-hydrogen twice, then Grok once.

A 429 moves to the next model. A timeout or a 503 waits 20 seconds and retries.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import httpx

from poc.ai.prompt import system_prompt
from poc.config import Settings
from poc.models import Decision

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROK_URL = "https://api.x.ai/v1/chat/completions"
_RETRY_WAIT_SEC = 20.0
_URL = re.compile(r"https?://[^\s'\"]+")
_SECRET = re.compile(r"(?i)\b((?:api[_-]?)?key|token)=([^\s&]+)")

DECISION_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "action": {"type": "STRING", "enum": ["none", "market", "pending"]},
        "side": {"type": "STRING", "enum": ["buy", "sell"], "nullable": True},
        "entry_price": {"type": "NUMBER", "nullable": True},
        "stop_loss": {"type": "NUMBER", "nullable": True},
        "take_profit": {"type": "NUMBER", "nullable": True},
        "volume": {"type": "NUMBER", "nullable": True},
        "confidence": {"type": "NUMBER"},
        "reasoning": {"type": "STRING"},
        "alert_levels": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "price": {"type": "NUMBER"},
                    "type": {"type": "STRING", "enum": ["support", "resistance", "custom"]},
                },
                "required": ["price", "type"],
            },
        },
        "manage_existing": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "position_id": {"type": "STRING"},
                    "action": {
                        "type": "STRING",
                        "enum": ["leave", "close", "move_sl_to_breakeven", "cancel"],
                    },
                    "new_sl": {"type": "NUMBER", "nullable": True},
                    "new_tp": {"type": "NUMBER", "nullable": True},
                },
                "required": ["position_id", "action"],
            },
        },
    },
    "required": [
        "action",
        "side",
        "entry_price",
        "stop_loss",
        "take_profit",
        "volume",
        "confidence",
        "reasoning",
        "alert_levels",
        "manage_existing",
    ],
}

GROK_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["none", "market", "pending"]},
        "side": {"anyOf": [{"type": "string", "enum": ["buy", "sell"]}, {"type": "null"}]},
        "entry_price": {"type": ["number", "null"]},
        "stop_loss": {"type": ["number", "null"]},
        "take_profit": {"type": ["number", "null"]},
        "volume": {"type": ["number", "null"]},
        "confidence": {"type": "number"},
        "reasoning": {"type": "string"},
        "alert_levels": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "price": {"type": "number"},
                    "type": {"type": "string", "enum": ["support", "resistance", "custom"]},
                },
                "required": ["price", "type"],
            },
        },
        "manage_existing": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "position_id": {"type": "string"},
                    "action": {
                        "type": "string",
                        "enum": ["leave", "close", "move_sl_to_breakeven", "cancel"],
                    },
                    "new_sl": {"type": ["number", "null"]},
                    "new_tp": {"type": ["number", "null"]},
                },
                "required": ["position_id", "action", "new_sl", "new_tp"],
            },
        },
    },
    "required": [
        "action",
        "side",
        "entry_price",
        "stop_loss",
        "take_profit",
        "volume",
        "confidence",
        "reasoning",
        "alert_levels",
        "manage_existing",
    ],
}


def extract_json_object(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object")
    parsed = json.loads(raw[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("json was not an object")
    return parsed


def _status(exc: BaseException) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None


def _rate_limited(exc: BaseException) -> bool:
    if _status(exc) == 429:
        return True
    text = str(exc).lower()
    return "429" in text or "too many requests" in text or "resource_exhausted" in text


def _retryable(exc: BaseException) -> bool:
    """A 429 moves on. A timeout or a 503 gets the other try. Any other 4xx stops."""
    if _rate_limited(exc):
        return False
    status = _status(exc)
    if status is not None and 400 <= status < 500:
        return False
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    if status is not None and status >= 500:
        return True
    text = str(exc).lower()
    return any(
        phrase in text
        for phrase in ("timed out", "timeout", "503", "unavailable", "connection reset", "eof")
    )


def _times(count: int) -> str:
    if count == 1:
        return "once"
    if count == 2:
        return "twice"
    return f"{count} times"


def _decision_text(decision: Decision) -> str:
    lines: list[str] = []
    if decision.action == "none":
        lines.append("leave the book")
    elif decision.action == "market":
        lines.append(f"market {decision.side}")
    else:
        lines.append(f"pending {decision.side} {decision.entry_price}".strip())
    if decision.action != "none":
        lines.append(f"SL {decision.stop_loss} TP {decision.take_profit}")
    changes = [
        f"{item.action} {item.position_id}"
        for item in decision.manage_existing
        if item.action != "leave"
    ]
    if changes:
        lines.append("manage " + ", ".join(changes))
    reason = (decision.reasoning or "").strip()
    if reason:
        lines.append(reason[:400])
    return "\n".join(lines)


def short_reason(exc: BaseException) -> str:
    """One line for Telegram. URLs and query keys stay out of the chat."""
    if isinstance(exc, httpx.TimeoutException):
        return "read timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        phrase = exc.response.reason_phrase or ""
        return f"{exc.response.status_code} {phrase}".strip()
    text = str(exc).strip()
    lowered = text.lower()
    if "read operation timed out" in lowered or "timed out" in lowered:
        return "read timeout"
    text = _URL.sub("", text)
    text = _SECRET.sub(lambda match: f"{match.group(1)}=redacted", text)
    text = " ".join(text.split())
    return (text or exc.__class__.__name__)[:160]


def gemini_text(body: dict[str, Any]) -> str:
    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("gemini returned no candidates")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    chunks = [part.get("text") for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
    text = "\n".join(chunks).strip()
    if not text:
        raise ValueError("gemini returned no text")
    return text


def grok_text(body: dict[str, Any]) -> str:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("grok returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        chunks = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                chunks.append(part["text"])
        content = "\n".join(chunks)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("grok returned no text")
    return content


def _decision_from_text(text: str) -> Decision:
    return Decision.model_validate(extract_json_object(text))


def _gemini_payload(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "systemInstruction": {"parts": [{"text": system_prompt()}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": json.dumps(context, default=str)}],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": DECISION_SCHEMA,
        },
    }


def _grok_payload(settings: Settings, context: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": settings.grok_model,
        "messages": [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": json.dumps(context, default=str)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "decision",
                "strict": True,
                "schema": GROK_DECISION_SCHEMA,
            },
        },
    }


def _call_gemini_once(
    settings: Settings,
    context: dict[str, Any],
    model: str,
    *,
    post: Callable[..., Any] | None,
) -> Decision:
    payload = _gemini_payload(context)
    if post is not None:
        body = post({**payload, "model": model})
    else:
        response = httpx.post(
            GEMINI_URL.format(model=model),
            json=payload,
            headers={
                "x-goog-api-key": settings.gemini_api_key,
                "Content-Type": "application/json",
            },
            timeout=settings.ai_timeout_sec,
        )
        response.raise_for_status()
        body = response.json()
    if not isinstance(body, dict):
        raise ValueError("gemini returned a non-object")
    return _decision_from_text(gemini_text(body))


def _try_model(
    settings: Settings,
    context: dict[str, Any],
    model: str,
    *,
    post: Callable[..., Any] | None,
    wait: Callable[[float], None],
    tries: int,
) -> tuple[Decision | None, int, Exception | None]:
    """Up to `tries` calls. A 429, or any other non-retryable error, stops this model."""
    attempts = 0
    last: Exception | None = None
    while attempts < tries:
        attempts += 1
        try:
            return _call_gemini_once(settings, context, model, post=post), attempts, None
        except Exception as exc:
            last = exc
            if attempts < tries and _retryable(exc):
                wait(_RETRY_WAIT_SEC)
                continue
            return None, attempts, exc
    assert last is not None
    return None, attempts, last


def _fallback_note(failed: list[str], model: str, decision: Decision) -> str:
    return "\n".join(failed) + f"\n{model}:\n{_decision_text(decision)}"


def _call_grok(
    settings: Settings,
    context: dict[str, Any],
    *,
    post: Callable[..., Any] | None,
) -> Decision:
    if post is None and not settings.grok_api_key:
        raise RuntimeError("missing XAI_API_KEY")
    payload = _grok_payload(settings, context)
    if post is not None:
        body = post(payload)
    else:
        response = httpx.post(
            GROK_URL,
            json=payload,
            headers={
                "Authorization": f"Bearer {settings.grok_api_key}",
                "Content-Type": "application/json",
            },
            timeout=settings.grok_timeout_sec,
        )
        response.raise_for_status()
        body = response.json()
    if not isinstance(body, dict):
        raise ValueError("grok returned a non-object")
    return _decision_from_text(grok_text(body))


def decide(
    settings: Settings,
    context: dict[str, Any],
    *,
    pause: Callable[[float], None] | None = None,
    post: Callable[..., Any] | None = None,
    grok_post: Callable[..., Any] | None = None,
    on_fallback: Callable[[str], None] | None = None,
) -> Decision:
    """Flash twice, then gemini-4-hydrogen twice, then Grok once.

    A 429 skips the rest of that model. A timeout or a 503 waits 20 seconds and uses the second try.
    """
    if not settings.gemini_api_key:
        raise RuntimeError("missing GEMINI_API_KEY")
    wait = pause or time.sleep
    failed: list[str] = []
    models = [settings.gemini_model]
    fallback = settings.gemini_fallback_model
    if fallback and fallback != settings.gemini_model:
        models.append(fallback)
    for model in models:
        decision, attempts, exc = _try_model(
            settings, context, model, post=post, wait=wait, tries=2
        )
        if exc is None:
            assert decision is not None
            if failed and on_fallback is not None:
                on_fallback(_fallback_note(failed, model, decision))
            return decision
        failed.append(f"{model} failed {_times(attempts)}. Last error: {short_reason(exc)}.")
    note = "\n".join(failed)
    try:
        decision = _call_grok(settings, context, post=grok_post)
    except Exception as grok_exc:
        raise RuntimeError(
            f"{note}\n"
            f"{settings.grok_model} failed. Last error: {short_reason(grok_exc)}.\n"
            "No decision this check."
        ) from grok_exc
    if on_fallback is not None:
        on_fallback(_fallback_note(failed, settings.grok_model, decision))
    return decision
