"""Shared Claude (Anthropic) client for every pipeline module.

Why this exists
---------------
Before 2026-09 each module built its own ``anthropic.Anthropic`` client with a
hard-coded model string and parsed the reply with ``json.loads`` on whatever
came back. Two production outages were caused by a deprecated model ID, and
truncated / fenced replies silently produced zero leads.

This module gives every call site:
  * one model name, overridable with the ``CLAUDE_MODEL`` env var (no deploy
    needed to move to a new model);
  * one client with sane retry / timeout settings (429 / 529 / 5xx are retried
    by the SDK with backoff);
  * ``ask_json`` — a tolerant JSON extractor that detects truncation and raises
    a clear ``LLMError`` instead of returning garbage.
"""
from __future__ import annotations

import json
import re
import threading
import time

import config

_client = None
_client_lock = threading.Lock()


class LLMError(RuntimeError):
    """Raised when Claude could not be called or its reply could not be parsed."""


def get_client():
    """Return a process-wide Anthropic client (created lazily, thread-safe)."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import anthropic
                if not config.ANTHROPIC_API_KEY:
                    raise LLMError("ANTHROPIC_API_KEY is not set")
                _client = anthropic.Anthropic(
                    api_key=config.ANTHROPIC_API_KEY,
                    timeout=config.CLAUDE_TIMEOUT_SECONDS,
                    max_retries=config.CLAUDE_MAX_RETRIES,
                )
    return _client


def ask_text(prompt: str, max_tokens: int, temperature: float | None = None,
             system: str | None = None, tag: str = "LLM") -> tuple[str, str]:
    """Send a single-turn prompt. Returns (text, stop_reason). Raises LLMError."""
    import anthropic

    kwargs = dict(
        model=config.CLAUDE_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    if temperature is not None:
        kwargs["temperature"] = temperature
    if system:
        kwargs["system"] = system

    started = time.time()
    try:
        message = get_client().messages.create(**kwargs)
    except anthropic.NotFoundError as e:
        # Almost always a retired model ID — make it impossible to miss.
        raise LLMError(
            f"Claude model '{config.CLAUDE_MODEL}' was rejected by the API "
            f"(set CLAUDE_MODEL to a current model): {e}"
        ) from e
    except anthropic.AuthenticationError as e:
        raise LLMError(f"Anthropic API key rejected: {e}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic API error {e.status_code} after retries: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"Could not reach the Anthropic API: {e}") from e

    text = "".join(getattr(block, "text", "") for block in message.content).strip()
    stop = getattr(message, "stop_reason", "") or ""
    print(f"[{tag}] claude {config.CLAUDE_MODEL} {time.time() - started:.1f}s "
          f"in={message.usage.input_tokens} out={message.usage.output_tokens} stop={stop}")
    return text, stop


def ask_json(prompt: str, max_tokens: int, expect: str = "object",
             temperature: float | None = None, tag: str = "LLM"):
    """Ask Claude for JSON and return the parsed value.

    ``expect`` is "object" or "array" and drives the tolerant extraction: we
    locate the outermost matching bracket pair so preambles, code fences and
    trailing commentary do not break parsing. A reply cut off by ``max_tokens``
    raises LLMError rather than being parsed as a partial result.
    """
    text, stop = ask_text(prompt, max_tokens=max_tokens, temperature=temperature, tag=tag)
    if stop == "max_tokens":
        raise LLMError(
            f"Claude reply was truncated at max_tokens={max_tokens}; "
            f"raise the budget or send less input"
        )
    try:
        return extract_json(text, expect=expect)
    except ValueError as e:
        raise LLMError(f"Could not parse Claude JSON reply: {e}. Reply began: {text[:200]!r}") from e


def extract_json(text: str, expect: str = "object"):
    """Pull a JSON object/array out of ``text`` even if wrapped in prose or fences."""
    if not text:
        raise ValueError("empty reply")

    # Strip markdown fences anywhere in the reply
    cleaned = re.sub(r"```(?:json)?", "", text).strip()

    # Fast path
    try:
        value = json.loads(cleaned)
        return _check_type(value, expect)
    except (ValueError, TypeError):
        pass

    open_ch, close_ch = ("[", "]") if expect == "array" else ("{", "}")
    start = cleaned.find(open_ch)
    end = cleaned.rfind(close_ch)
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"no JSON {expect} found")
    value = json.loads(cleaned[start:end + 1])
    return _check_type(value, expect)


def _check_type(value, expect: str):
    if expect == "array" and not isinstance(value, list):
        raise ValueError("expected a JSON array")
    if expect == "object" and not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value
