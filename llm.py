"""Unified LLM client. Supports two backends, switched via LLM_PROVIDER:

- "gemini"  (default): Google Gemini via google.generativeai
- "openai"  : any OpenAI-compatible chat/completions endpoint,
              including MiniMax official API (platform.minimax.io / api.minimax.io)

Both expose `complete(system, user, *, temperature, max_output_tokens) -> str`,
returning the raw text (expected to be JSON). Parsing/robustness stays in the
callers. No third-party dependency for the openai path (uses urllib).
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from config import settings

logger = logging.getLogger(__name__)

PROVIDER = (settings.llm_provider or "gemini").strip().lower()

# ---------------------------------------------------------------------------
# Gemini backend
# ---------------------------------------------------------------------------
def _gemini_complete(system: str, user: str, *, temperature: float,
                     max_output_tokens: int) -> str:
    import google.generativeai as genai
    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(
        settings.gemini_model_extract,
        generation_config={
            "temperature": temperature,
            "top_p": 0.9,
            "max_output_tokens": max_output_tokens,
            "response_mime_type": "application/json",
        },
    )
    resp = model.generate_content([system, user])
    return resp.text or ""


# ---------------------------------------------------------------------------
# OpenAI-compatible backend (MiniMax etc.)
# ---------------------------------------------------------------------------
def _openai_complete(system: str, user: str, *, temperature: float,
                     max_output_tokens: int) -> str:
    base = (settings.openai_base_url or "").rstrip("/")
    if not base:
        raise RuntimeError("LLM_PROVIDER=openai 但未設定 OPENAI_BASE_URL")
    url = f"{base}/chat/completions"
    key = (settings.openai_api_key or "").strip()
    if not key:
        raise RuntimeError("LLM_PROVIDER=openai 但未設定 OPENAI_API_KEY")

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    body: dict[str, Any] = {
        "model": settings.openai_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_output_tokens,
    }
    # Ask for JSON object where supported. MiniMax OpenAI-compat honours this.
    body["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:500]
        logger.error("OpenAI-compat LLM HTTP %s: %s", e.code, detail)
        raise RuntimeError(f"LLM HTTP {e.code}: {detail}") from e

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        logger.error("Unexpected OpenAI-compat response shape: %s", data)
        raise RuntimeError(f"Unexpected LLM response: {data}") from e


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------
def complete(system: str, user: str, *, temperature: float = 0.1,
             max_output_tokens: int = 8192) -> str:
    """Return raw model text for the (system, user) prompt pair."""
    if PROVIDER == "openai":
        return _openai_complete(system, user, temperature=temperature,
                                max_output_tokens=max_output_tokens)
    # default / gemini
    return _gemini_complete(system, user, temperature=temperature,
                            max_output_tokens=max_output_tokens)


def provider() -> str:
    return PROVIDER


if __name__ == "__main__":
    # CLI smoke test:  python llm.py  (uses current .env provider)
    import sys
    logging.basicConfig(level=logging.INFO)
    sys_it = "你要用繁體中文回答: 請回傳一行 JSON, 欄位 {\"status\": \"ok\", \"provider\": \"minimax\"}"
    print(f"[provider] {provider()}")
    try:
        out = complete("你是測試助手。", sys_it, temperature=0.1, max_output_tokens=256)
        print("[response]", out)
    except Exception as e:
        print("[ERROR]", e)
        sys.exit(1)
