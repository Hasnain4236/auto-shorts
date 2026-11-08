"""Provider-agnostic LLM wrapper: free-tier providers, JSON mode, throttling, retries.

Providers are tried in config order. A provider is skipped if its API key env var
is unset. Rate-limit / transient errors (429, 5xx) are retried with exponential
backoff + jitter; if retries run out the next provider is tried.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from pipeline.config import Config, env

log = logging.getLogger(__name__)


class LLMError(Exception):
    """Non-retryable LLM failure (bad request, auth, unparseable output)."""


class RetryableLLMError(LLMError):
    """429 / 5xx / timeout: worth retrying after a backoff."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class QuotaExhaustedError(LLMError):
    """A daily (or otherwise long) quota is used up: skip this model until it resets."""


# Long waits mean a daily quota, not a per-minute one; don't sit in a retry loop for it.
MAX_RETRY_WAIT_S = 300


def _classify_429(provider: str, message: str, retry_after: float | None = None) -> LLMError:
    m = re.search(r"retry(?:Delay)?['\"]?\s*[:=]?\s*['\"]?(\d+(?:\.\d+)?)s", message)
    delay = retry_after if retry_after is not None else (float(m.group(1)) if m else None)
    if "PerDay" in message or "per day" in message.lower() or (delay is not None and delay > MAX_RETRY_WAIT_S):
        return QuotaExhaustedError(f"{provider}: daily quota exhausted")
    return RetryableLLMError(f"{provider} 429: {message[:300]}", retry_after=delay)


class Provider(Protocol):
    name: str
    model: str

    def complete(self, prompt: str, *, system: str | None, json_mode: bool, temperature: float,
                 images: list[bytes] | None = None) -> str: ...


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

class GeminiProvider:
    """Google Gemini via the official `google-genai` SDK (free tier from AI Studio)."""

    def __init__(self, model: str, api_key: str):
        from google import genai  # lazy: only needed when Gemini is actually used
        from google.genai import types

        self.name = "gemini"
        self.model = model
        # JSON mode (response_mime_type) is only enabled on v1beta for several models
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))

    def complete(self, prompt: str, *, system: str | None, json_mode: bool, temperature: float,
                 images: list[bytes] | None = None) -> str:
        from google.genai import errors, types

        cfg = types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
            response_mime_type="application/json" if json_mode else None,
        )
        contents: Any = prompt
        if images:
            contents = [prompt, *(types.Part.from_bytes(data=img, mime_type="image/jpeg") for img in images)]
        try:
            resp = self._client.models.generate_content(model=self.model, contents=contents, config=cfg)
        except errors.APIError as e:
            code = getattr(e, "code", None)
            if code == 429:
                raise _classify_429(f"gemini/{self.model}", str(e)) from e
            if isinstance(code, int) and code >= 500:
                raise RetryableLLMError(f"gemini {code}: {e}") from e
            raise LLMError(f"gemini {code}: {e}") from e
        except Exception as e:  # network-level failures (disconnects, timeouts) from the HTTP client
            raise RetryableLLMError(f"gemini/{self.model} network error: {type(e).__name__}: {e}") from e
        text = resp.text
        if not text:
            raise RetryableLLMError("gemini returned empty response (possibly safety-blocked)")
        return text


class OpenAICompatProvider:
    """Any OpenAI-compatible chat endpoint (Groq, OpenRouter free models) via plain HTTP."""

    def __init__(self, name: str, model: str, api_key: str, base_url: str, timeout: float = 180,
                 json_format: bool = True, vision: bool = False, extra: dict[str, Any] | None = None):
        self.json_format = json_format   # send response_format (not every model supports it)
        self.extra = extra or {}         # provider-specific body fields, e.g. reasoning effort, max_tokens
        self.vision = vision
        self.name = name
        self.model = model
        self._api_key = api_key
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._timeout = timeout

    def complete(self, prompt: str, *, system: str | None, json_mode: bool, temperature: float,
                 images: list[bytes] | None = None) -> str:
        import requests

        if images and not self.vision:
            raise LLMError(f"{self.name}/{self.model}: image input not configured")
        content: Any = prompt
        if images:
            import base64

            content = [{"type": "text", "text": prompt}] + [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(i).decode()}}
                for i in images
            ]
        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": content}
        ]
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature}
        if json_mode and self.json_format:
            body["response_format"] = {"type": "json_object"}
        body.update(self.extra)
        try:
            r = requests.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=body,
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            raise RetryableLLMError(f"{self.name} network error: {e}") from e
        retry_after = r.headers.get("retry-after")
        ra = float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else None
        if r.status_code == 429:
            raise _classify_429(f"{self.name}/{self.model}", r.text, ra)
        if r.status_code >= 500:
            raise RetryableLLMError(f"{self.name} {r.status_code}: {r.text[:300]}", retry_after=ra)
        if r.status_code >= 400:
            raise LLMError(f"{self.name} {r.status_code}: {r.text[:500]}")
        try:
            content = r.json()["choices"][0]["message"].get("content")
        except (KeyError, IndexError, ValueError) as e:
            raise LLMError(f"{self.name} unexpected response: {r.text[:500]}") from e
        if not content or not content.strip():
            # reasoning models sometimes spend the whole budget thinking and return no answer
            raise RetryableLLMError(f"{self.name}/{self.model} returned an empty answer")
        return content


class FakeProvider:
    """Deterministic provider for tests and dry runs. `responder(prompt) -> str`."""

    def __init__(self, responder: Callable[[str], str] | list[str], name: str = "fake"):
        self.name = name
        self.model = "fake"
        self._responder = responder
        self.calls: list[str] = []

    def complete(self, prompt: str, *, system: str | None, json_mode: bool, temperature: float,
                 images: list[bytes] | None = None) -> str:
        self.calls.append(prompt)
        if callable(self._responder):
            return self._responder(prompt)
        if not self._responder:
            raise LLMError("FakeProvider ran out of responses")
        item = self._responder.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


# ---------------------------------------------------------------------------
# Wrapper
# ---------------------------------------------------------------------------

@dataclass
class _Slot:
    provider: Provider
    min_interval_s: float
    last_call: float = 0.0


def extract_json(text: str) -> Any:
    """Parse JSON from a model reply, tolerating ```json fences and leading prose."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # fall back to the outermost {...} or [...]
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start, end = text.find(open_c), text.rfind(close_c)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"Could not parse JSON from model output: {text[:300]!r}")


class LLM:
    def __init__(
        self,
        providers: list[tuple[Provider, float]],
        *,
        max_retries: int = 6,
        base_backoff_s: float = 2.0,
        max_backoff_s: float = 64.0,
        temperature: float = 0.7,
        on_call: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        exhausted_file: Path | None = None,
    ):
        """`providers` is a list of (provider, requests_per_minute).

        `exhausted_file` remembers models whose daily quota ran out, so later stages and
        reruns on the same quota day skip them instead of wasting calls."""
        if not providers:
            raise LLMError("No LLM provider configured: set GEMINI_API_KEY (or GROQ/OPENROUTER) in .env")
        self._slots = [_Slot(p, 60.0 / rpm if rpm else 0.0) for p, rpm in providers]
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s
        self.max_backoff_s = max_backoff_s
        self.temperature = temperature
        self._on_call = on_call
        self._sleep = sleep
        self._exhausted_file = exhausted_file
        self._exhausted: set[str] = self._load_exhausted()

    @staticmethod
    def _quota_day() -> str:
        # Free-tier daily quotas (Gemini) reset at midnight US Pacific time
        return (datetime.now(timezone.utc) - timedelta(hours=8)).date().isoformat()

    def _load_exhausted(self) -> set[str]:
        if not self._exhausted_file or not self._exhausted_file.exists():
            return set()
        try:
            data = json.loads(self._exhausted_file.read_text(encoding="utf-8"))
            return set(data.get("models", [])) if data.get("day") == self._quota_day() else set()
        except (OSError, ValueError):
            return set()

    def _mark_exhausted(self, key: str) -> None:
        self._exhausted.add(key)
        if self._exhausted_file:
            self._exhausted_file.parent.mkdir(parents=True, exist_ok=True)
            self._exhausted_file.write_text(json.dumps({"day": self._quota_day(), "models": sorted(self._exhausted)}),
                                            encoding="utf-8")

    def _throttle(self, slot: _Slot) -> None:
        wait = slot.min_interval_s - (time.monotonic() - slot.last_call)
        if wait > 0:
            self._sleep(wait)
        slot.last_call = time.monotonic()

    def complete(
        self, prompt: str, *, system: str | None = None, json_mode: bool = False, temperature: float | None = None,
        images: list[bytes] | None = None,
    ) -> str:
        temp = self.temperature if temperature is None else temperature
        errors: list[str] = []
        for slot in self._slots:
            p = slot.provider
            key = f"{p.name}/{p.model}"
            if key in self._exhausted:
                continue
            for attempt in range(self.max_retries + 1):
                self._throttle(slot)
                try:
                    out = p.complete(prompt, system=system, json_mode=json_mode, temperature=temp, images=images)
                    if self._on_call:
                        self._on_call(f"llm.{key}")
                    return out
                except QuotaExhaustedError as e:
                    errors.append(str(e))
                    log.warning("%s daily quota used up; skipping it until the quota resets", key)
                    self._mark_exhausted(key)
                    break
                except RetryableLLMError as e:
                    if self._on_call:
                        self._on_call(f"llm.{key}")
                    if attempt == self.max_retries:
                        errors.append(f"{p.name}: {e}")
                        log.warning("%s exhausted retries; trying next provider", p.name)
                        break
                    delay = e.retry_after or min(self.max_backoff_s, self.base_backoff_s * 2**attempt)
                    delay += random.uniform(0, delay * 0.25)
                    log.warning("%s retryable error (%s); sleeping %.1fs", p.name, e, delay)
                    self._sleep(delay)
                except LLMError as e:
                    errors.append(f"{p.name}: {e}")
                    log.error("%s failed: %s; trying next provider", p.name, e)
                    break
        raise LLMError("All LLM providers failed: " + " | ".join(errors))

    def complete_json(
        self, prompt: str, *, system: str | None = None, temperature: float | None = None, parse_retries: int = 1,
        images: list[bytes] | None = None,
    ) -> Any:
        """Request JSON; if the reply doesn't parse, re-ask with a stricter instruction."""
        last_err: Exception | None = None
        current = prompt
        for _ in range(parse_retries + 1):
            text = self.complete(current, system=system, json_mode=True, temperature=temperature, images=images)
            try:
                return extract_json(text)
            except LLMError as e:
                last_err = e
                log.warning("LLM returned invalid JSON; re-asking")
                current = prompt + "\n\nIMPORTANT: Reply with ONLY valid JSON. No prose, no code fences."
        raise LLMError(f"LLM never returned valid JSON: {last_err}")


def build_llm(config: Config, on_call: Callable[[str], None] | None = None, role: str = "default") -> LLM:
    """Build the fallback chain for a role (llm.roles.<role>); unknown role -> 'default',
    no roles configured -> every provider in listed order."""
    specs = config.require("llm.providers")
    by_id = {s_.get("id", f"{s_['name']}:{s_['model']}"): s_ for s_ in specs}
    roles = config.get("llm.roles") or {}
    order = roles.get(role) or roles.get("default")
    chosen = [by_id[i] for i in order if i in by_id] if order else specs
    providers: list[tuple[Provider, float]] = []
    for spec in chosen:
        key = env(spec["api_key_env"], required=False)
        if not key:
            log.debug("LLM provider %s skipped (%s unset)", spec.get("id", spec["name"]), spec["api_key_env"])
            continue
        if spec["name"] == "gemini":
            prov: Provider = GeminiProvider(spec["model"], key)
        else:
            prov = OpenAICompatProvider(spec["name"], spec["model"], key, spec["base_url"],
                                        json_format=spec.get("json_format", True), vision=spec.get("vision", False),
                                        extra=spec.get("extra"), timeout=float(spec.get("timeout_s", 180)))
        providers.append((prov, float(spec.get("rpm", 0))))
    return LLM(
        providers,
        max_retries=int(config.get("llm.max_retries", 6)),
        base_backoff_s=float(config.get("llm.base_backoff_s", 2.0)),
        max_backoff_s=float(config.get("llm.max_backoff_s", 64.0)),
        temperature=float(config.get("llm.temperature", 0.7)),
        on_call=on_call,
        exhausted_file=config.runs_dir / "_cache" / "llm_exhausted.json",
    )
