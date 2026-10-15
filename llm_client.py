"""One thin Groq client for the whole app (replaces CrewAI).

Why: every "agent" in the old pipeline was just one prompt, so CrewAI added
119 packages, slow cold starts and hidden retries for no benefit. This module
is ~100 lines, has one default model, and does the three things that matter:
timeouts, retries on rate limits / server errors, and optional JSON mode.
"""
import json
import logging
import os
import re
import time

import requests

log = logging.getLogger("zerotohire.llm")

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# THE default model. Override with GROQ_MODEL (a leading "groq/" is ignored).
DEFAULT_MODEL = "llama-3.3-70b-versatile"


class LLMError(RuntimeError):
    """Raised with a message that is safe to show to the user."""


def model_name() -> str:
    raw = (os.getenv("GROQ_MODEL") or DEFAULT_MODEL).strip()
    return raw[len("groq/"):] if raw.startswith("groq/") else raw


def chat(messages, *, temperature=0.35, max_tokens=2000, json_mode=False,
         timeout=60, retries=3, model=None) -> str:
    """Send a chat completion and return the reply text.

    Retries on 429 (honouring Retry-After) and 5xx / network errors with
    exponential backoff. Never puts the API key or raw provider errors in the
    message that reaches the user.
    """
    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        raise LLMError("The AI service is not configured (GROQ_API_KEY is missing).")

    body = {
        "model": model or model_name(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    last = "unknown error"
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            last = f"network error ({type(exc).__name__})"
            resp = None

        if resp is not None:
            if resp.status_code == 200:
                try:
                    content = resp.json()["choices"][0]["message"]["content"]
                except (KeyError, IndexError, ValueError):
                    last = "unexpected response format"
                else:
                    if content and content.strip():
                        return content.strip()
                    last = "empty response"
            elif resp.status_code == 429 or resp.status_code >= 500:
                last = f"provider busy ({resp.status_code})"
                wait = _retry_after(resp)
                if attempt < retries:
                    time.sleep(wait if wait is not None else min(2 ** attempt * 2, 20))
                    continue
            elif resp.status_code in (400, 401, 403, 404):
                log.error("Groq rejected request: %s %s", resp.status_code, resp.text[:300])
                if resp.status_code == 400 and json_mode and "json" in resp.text.lower():
                    # model/endpoint refused JSON mode: caller can retry without it
                    raise LLMError("json_mode_unsupported")
                raise LLMError("The AI service rejected the request. Please try again later.")
            else:
                last = f"status {resp.status_code}"

        if attempt < retries:
            time.sleep(min(2 ** attempt * 2, 20))

    log.error("Groq failed after retries: %s", last)
    raise LLMError("The AI service is busy right now. Please try again in a minute.")


def _retry_after(resp):
    try:
        value = float(resp.headers.get("retry-after", ""))
        return max(1.0, min(value, 30.0))
    except (TypeError, ValueError):
        return None


def chat_json(messages, **kwargs) -> dict:
    """Chat that must return a JSON object. Tries JSON mode, then plain mode;
    extracts the first {...} block if the model wrapped it in prose."""
    last_err = None
    for use_json_mode in (True, False):
        try:
            text = chat(messages, json_mode=use_json_mode, **kwargs)
        except LLMError as exc:
            if str(exc) == "json_mode_unsupported":
                continue
            raise
        data = parse_json_object(text)
        if data is not None:
            return data
        last_err = "model did not return valid JSON"
    raise LLMError("The AI returned an unreadable answer. Please try again.")


def parse_json_object(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(text[start:end + 1])
            return value if isinstance(value, dict) else None
        except ValueError:
            return None
    return None


class SimpleLLM:
    """Tiny shim so older call sites that did `llm.call(prompt)` keep working."""

    def __init__(self, temperature=0.35, max_tokens=1500):
        self.temperature = temperature
        self.max_tokens = max_tokens

    def call(self, prompt) -> str:
        return chat([{"role": "user", "content": str(prompt)}],
                    temperature=self.temperature, max_tokens=self.max_tokens)
