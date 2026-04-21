"""Unified LLM wrapper — Anthropic (default) or OpenAI.

All analysis modules call ``llm_complete(system=..., user=..., max_tokens=...)``
instead of talking to a specific SDK directly. This lets us flip providers
(or models) from the ``.env`` file without touching 9+ files.

Routing rules:

* ``settings.llm_provider == "anthropic"`` → Claude only. Requires
  ``ANTHROPIC_API_KEY``. No silent fallback to OpenAI.
* ``settings.llm_provider == "openai"`` → OpenAI when ``OPENAI_API_KEY`` is set;
  if that key is missing but ``ANTHROPIC_API_KEY`` is set, Claude is used once
  as a convenience fallback.

JSON mode:
    ``expect_json=True`` (default) enables OpenAI's ``response_format=json_object``
    so the model can't emit prose before/after the JSON payload. Anthropic
    doesn't have a dedicated JSON mode, so for Claude we just prepend a
    "JSON only" reminder to the system prompt.
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Literal, Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

# How many times to retry on 429 / transient server errors before giving up.
# OpenAI's free / starter tiers have a 30k TPM limit that gets blown by 8
# parallel workers — without retry every second call returns "" and the
# whole downstream pipeline (article analysis, governance, financials) is
# starved of data.
_MAX_RETRIES = 5
# Cap the total time we'll spend retrying a single call. Five retries at
# 2 → 4 → 8 → 16 → 30s worst case = ~60s, which is still cheaper than
# discarding the call entirely.
_MAX_RETRY_SECONDS = 60.0

_RETRY_AFTER_RE = re.compile(
    r"try again in ([0-9]*\.?[0-9]+)\s*(ms|s|sec|seconds?)", re.IGNORECASE
)


def _parse_retry_after(err_text: str) -> Optional[float]:
    """Extract the server-suggested wait from an OpenAI / Anthropic 429 body."""
    m = _RETRY_AFTER_RE.search(err_text or "")
    if not m:
        return None
    value = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    if unit == "ms":
        return value / 1000.0
    return value


def _is_retryable(err: Exception) -> tuple[bool, Optional[float]]:
    """Decide whether an OpenAI/Anthropic exception is worth retrying.

    Returns ``(retry?, suggested_wait_seconds_or_None)``.
    """
    status = getattr(err, "status_code", None)
    if status is None:
        resp = getattr(err, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None

    text = str(err)
    hint = _parse_retry_after(text)

    if status in (408, 409, 425, 429, 500, 502, 503, 504):
        return True, hint
    if "rate_limit" in text.lower() or "rate limit" in text.lower():
        return True, hint
    if "overloaded" in text.lower() or "temporarily" in text.lower():
        return True, hint
    if "timeout" in text.lower() or "connection" in text.lower():
        return True, hint
    return False, None

Provider = Literal["openai", "anthropic", ""]


def active_provider() -> Provider:
    """Return the provider we can actually talk to, given available keys."""
    s = get_settings()
    preferred = (s.llm_provider or "anthropic").lower()
    if preferred == "anthropic":
        return "anthropic" if s.anthropic_api_key else ""
    if preferred == "openai" and s.openai_api_key:
        return "openai"
    # openai preferred but no key — try the other provider once
    if preferred == "openai" and s.anthropic_api_key:
        return "anthropic"
    if s.openai_api_key:
        return "openai"
    if s.anthropic_api_key:
        return "anthropic"
    return ""


def active_model() -> str:
    s = get_settings()
    p = active_provider()
    if p == "openai":
        return s.openai_model
    if p == "anthropic":
        return s.anthropic_model
    return ""


def llm_available() -> bool:
    return active_provider() != ""


def llm_complete(
    *,
    system: str,
    user: str,
    max_tokens: Optional[int] = None,
    temperature: float = 0.0,
    expect_json: bool = True,
    purpose: str = "",
) -> str:
    """Send a (system, user) prompt and return the raw assistant text.

    Returns an empty string when no provider is configured OR when the API
    call fails. Callers are responsible for parsing the JSON and handling
    empty responses (identical to the old Anthropic flow).
    """
    s = get_settings()
    if max_tokens is None:
        max_tokens = s.llm_max_tokens or s.anthropic_max_tokens or 2200

    provider = active_provider()
    if not provider:
        return ""

    if provider == "openai":
        return _call_openai(
            system=system,
            user=user,
            model=s.openai_model,
            api_key=s.openai_api_key or "",
            max_tokens=max_tokens,
            temperature=temperature,
            expect_json=expect_json,
            purpose=purpose,
        )
    return _call_anthropic(
        system=system,
        user=user,
        model=s.anthropic_model,
        api_key=s.anthropic_api_key or "",
        max_tokens=max_tokens,
        temperature=temperature,
        expect_json=expect_json,
        purpose=purpose,
    )


# ─── OpenAI ──────────────────────────────────────────────────────────────

def _call_openai(
    *,
    system: str,
    user: str,
    model: str,
    api_key: str,
    max_tokens: int,
    temperature: float,
    expect_json: bool,
    purpose: str,
) -> str:
    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai package not installed — pip install openai>=1.40.")
        return ""

    # OpenAI's json_object mode requires the word "json" to appear in the
    # conversation. Every caller already spells out JSON in the prompt,
    # but we add a belt-and-suspenders reminder so the API accepts the
    # request even if a future prompt forgets.
    sys_prompt = system
    if expect_json and "json" not in system.lower() and "json" not in user.lower():
        sys_prompt = f"{system}\n\nAlways reply with a single valid JSON object."

    client = OpenAI(api_key=api_key, timeout=60.0)
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user},
        ],
    }
    if expect_json:
        kwargs["response_format"] = {"type": "json_object"}

    deadline = time.monotonic() + _MAX_RETRY_SECONDS
    attempt = 0
    last_error: Optional[Exception] = None
    while attempt < _MAX_RETRIES:
        attempt += 1
        try:
            resp = client.chat.completions.create(**kwargs)
            choices = getattr(resp, "choices", None) or []
            if not choices:
                return ""
            content = choices[0].message.content or ""
            if attempt > 1:
                logger.info(
                    "OpenAI retry succeeded on attempt %d (purpose=%s)",
                    attempt, purpose,
                )
            return content.strip()
        except Exception as e:  # noqa: BLE001
            last_error = e
            retry, hint = _is_retryable(e)
            if not retry:
                logger.info(
                    "OpenAI call failed (%s, purpose=%s, non-retryable): %s",
                    model, purpose, e,
                )
                return ""
            remaining = deadline - time.monotonic()
            if remaining <= 0 or attempt >= _MAX_RETRIES:
                logger.warning(
                    "OpenAI gave up after %d attempts (purpose=%s): %s",
                    attempt, purpose, e,
                )
                return ""
            # Pick a wait: server hint > exponential backoff with jitter.
            base = min(2 ** attempt, 30)
            wait = hint if hint is not None else base
            wait = min(wait + random.uniform(0, 0.75), remaining)
            logger.info(
                "OpenAI retry %d/%d in %.2fs (purpose=%s, hint=%s): %s",
                attempt, _MAX_RETRIES, wait, purpose, hint, str(e)[:160],
            )
            time.sleep(max(0.1, wait))

    if last_error is not None:
        logger.warning("OpenAI exhausted retries (purpose=%s): %s", purpose, last_error)
    return ""


# ─── OpenAI Responses API + web search ─────────────────────────────────
#
# For tasks whose answer depends on *current* public facts (who sits on the
# board today, which companies were recently sanctioned, …) the plain chat
# completion API is useless — gpt-4o's knowledge cutoff is October 2023 and
# we're running in April 2026. The Responses API exposes a built-in
# ``web_search`` tool: when we turn it on, the model issues HTTP search
# queries under the hood and cites real URLs before answering.


def llm_complete_with_web_search(
    *,
    system: str,
    user: str,
    max_tokens: Optional[int] = None,
    purpose: str = "",
) -> str:
    """Like :func:`llm_complete` but with OpenAI's built-in web search tool.

    Uses the Responses API with ``tools=[{"type": "web_search"}]``. Falls
    back to :func:`llm_complete` when OpenAI is not the active provider
    (Anthropic doesn't expose a comparable tool through its SDK yet).

    ``expect_json`` is not a parameter here because Responses + tools don't
    currently support ``response_format=json_object``. We rely on the caller
    to spell out the JSON schema in the prompt and then extract a JSON blob
    from the model's text output as usual.
    """
    s = get_settings()
    if max_tokens is None:
        max_tokens = s.llm_max_tokens or s.anthropic_max_tokens or 2200

    if active_provider() != "openai":
        # No web-search tool available — degrade to plain completion.
        return llm_complete(
            system=system, user=user, max_tokens=max_tokens,
            expect_json=True, purpose=purpose,
        )

    try:
        from openai import OpenAI
    except ImportError:
        logger.warning("openai package not installed — pip install openai>=1.40.")
        return ""

    client = OpenAI(api_key=s.openai_api_key or "", timeout=90.0)
    deadline = time.monotonic() + _MAX_RETRY_SECONDS
    attempt = 0
    last_error: Optional[Exception] = None
    while attempt < _MAX_RETRIES:
        attempt += 1
        try:
            resp = client.responses.create(
                model=s.openai_model,
                instructions=system,
                input=user,
                tools=[{"type": "web_search"}],
                tool_choice="auto",
                max_output_tokens=max_tokens,
            )
            text = (getattr(resp, "output_text", "") or "").strip()
            if attempt > 1:
                logger.info(
                    "OpenAI(web_search) retry succeeded on attempt %d (purpose=%s)",
                    attempt, purpose,
                )
            return text
        except Exception as e:  # noqa: BLE001
            last_error = e
            retry, hint = _is_retryable(e)
            if not retry:
                logger.info(
                    "OpenAI(web_search) call failed (purpose=%s, non-retryable): %s",
                    purpose, e,
                )
                return ""
            remaining = deadline - time.monotonic()
            if remaining <= 0 or attempt >= _MAX_RETRIES:
                logger.warning(
                    "OpenAI(web_search) gave up after %d attempts (purpose=%s): %s",
                    attempt, purpose, e,
                )
                return ""
            base = min(2 ** attempt, 30)
            wait = hint if hint is not None else base
            wait = min(wait + random.uniform(0, 0.75), remaining)
            logger.info(
                "OpenAI(web_search) retry %d/%d in %.2fs (purpose=%s): %s",
                attempt, _MAX_RETRIES, wait, purpose, str(e)[:160],
            )
            time.sleep(max(0.1, wait))

    if last_error is not None:
        logger.warning(
            "OpenAI(web_search) exhausted retries (purpose=%s): %s",
            purpose, last_error,
        )
    return ""


# ─── Anthropic ───────────────────────────────────────────────────────────

def _call_anthropic(
    *,
    system: str,
    user: str,
    model: str,
    api_key: str,
    max_tokens: int,
    temperature: float,
    expect_json: bool,
    purpose: str,
) -> str:
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed.")
        return ""

    sys_prompt = system
    if expect_json and "json" not in system.lower():
        sys_prompt = system + "\n\nReply with a single valid JSON object and no other text."

    client = anthropic.Anthropic(api_key=api_key)
    deadline = time.monotonic() + _MAX_RETRY_SECONDS
    attempt = 0
    last_error: Optional[Exception] = None
    while attempt < _MAX_RETRIES:
        attempt += 1
        try:
            msg = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                system=sys_prompt,
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(getattr(b, "text", "") or "" for b in msg.content).strip()
            if attempt > 1:
                logger.info("Anthropic retry succeeded on attempt %d (purpose=%s)", attempt, purpose)
            return text
        except Exception as e:  # noqa: BLE001
            last_error = e
            retry, hint = _is_retryable(e)
            if not retry:
                logger.info(
                    "Anthropic call failed (%s, purpose=%s, non-retryable): %s",
                    model, purpose, e,
                )
                return ""
            remaining = deadline - time.monotonic()
            if remaining <= 0 or attempt >= _MAX_RETRIES:
                logger.warning(
                    "Anthropic gave up after %d attempts (purpose=%s): %s",
                    attempt, purpose, e,
                )
                return ""
            base = min(2 ** attempt, 30)
            wait = hint if hint is not None else base
            wait = min(wait + random.uniform(0, 0.75), remaining)
            logger.info(
                "Anthropic retry %d/%d in %.2fs (purpose=%s): %s",
                attempt, _MAX_RETRIES, wait, purpose, str(e)[:160],
            )
            time.sleep(max(0.1, wait))

    if last_error is not None:
        logger.warning("Anthropic exhausted retries (purpose=%s): %s", purpose, last_error)
    return ""
