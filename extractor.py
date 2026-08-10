"""Layer 1: Gemini Flash extraction.

Takes a video transcript, asks Gemini to extract structured per-stock records.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import google.generativeai as genai

from config import settings
from prompts import build_extraction_prompt

logger = logging.getLogger(__name__)

# Initialize once
genai.configure(api_key=settings.gemini_api_key)
_extract_model = genai.GenerativeModel(
    settings.gemini_model_extract,
    generation_config={
        "temperature": 0.1,  # low temp for structured extraction
        "top_p": 0.9,
        "max_output_tokens": 8192,
        "response_mime_type": "application/json",
    },
)


def _extract_json(text: str) -> dict[str, Any]:
    """Robust JSON extraction. Handles markdown fences, stray text, or a
    token-truncated response (the model sometimes gets cut off mid-array)."""
    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strip markdown code fences
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Find first { ... last } then attempt self-healing if it looks truncated:
    # incomplete array/object tail is a common cut-off (ends without ] } or with
    # a dangling element such as "...waymo) i"). Try appending closers.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        for repaired in _heal_truncations(text[start:end + 1]):
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                continue

    raise ValueError(f"Could not parse JSON from model output:\n{text[:500]}")


def _heal_truncations(s: str) -> list[str]:
    """Yield progressively-repaired versions of a possibly cut-off JSON string."""
    # Close unclosed brackets by simple counting, then try parsing each candidate.
    candidates = [s]
    s2 = s + "]}"               # assume cut inside an array then object
    candidates.append(s2)
    if s.rstrip().endswith(","):
        candidates.append(s.rstrip()[:-1] + "]}")
    # balance arrays then objects
    n_open = s.count("[") - s.count("]")
    m_open = s.count("{") - s.count("}")
    if n_open > 0:
        candidates.append(s + "]" + "}" * max(m_open, 0))
    if m_open > 0:
        candidates.append(s + "}" * m_open)
    # dedupe, preserve order
    seen, result = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def extract_stocks_from_transcript(
    title: str,
    channel: str,
    published: str,
    transcript: str,
) -> list[dict[str, Any]]:
    """Returns list of per-stock extraction dicts. Empty list if no stocks found.

    Each dict shape:
        {
            "ticker": "AAPL",
            "company": "Apple Inc.",
            "sentiment": "bullish",
            "speaker_confidence": "high",
            "key_points": ["..."],
            "price_target": 200 | null,
            "time_horizon": "1-3 months"
        }
    """
    if not transcript or len(transcript) < 100:
        logger.info("Transcript too short, skipping extraction.")
        return []

    system, user = build_extraction_prompt(title, channel, published, transcript)

    try:
        response = _extract_model.generate_content([system, user])
        text = response.text or ""
    except Exception as e:
        logger.error(f"Gemini extraction call failed: {e}")
        return []

    try:
        parsed = _extract_json(text)
    except ValueError as e:
        logger.error(f"JSON parse failed: {e}")
        return []

    stocks = parsed.get("stocks", [])
    if not isinstance(stocks, list):
        return []

    # Normalize + filter invalid entries
    cleaned: list[dict[str, Any]] = []
    for s in stocks:
        if not isinstance(s, dict):
            continue
        ticker = (s.get("ticker") or "").strip().upper()
        if not ticker or len(ticker) > 10:
            continue
        # ensure required keys exist with safe defaults
        cleaned.append(
            {
                "ticker": ticker,
                "company": s.get("company") or "",
                "sentiment": (s.get("sentiment") or "neutral").lower(),
                "speaker_confidence": (s.get("speaker_confidence") or "medium").lower(),
                "key_points": s.get("key_points") or [],
                "price_target": s.get("price_target"),
                "time_horizon": s.get("time_horizon") or "unknown",
            }
        )
    return cleaned


if __name__ == "__main__":
    # CLI smoke test
    import sys

    from youtube_client import fetch_video

    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python extractor.py <YOUTUBE_URL>")
        sys.exit(1)

    info = fetch_video(sys.argv[1])
    print(f"Fetched: {info.title} ({info.caption_source})")
    stocks = extract_stocks_from_transcript(info.title, info.channel, info.published, info.transcript)
    print(json.dumps(stocks, ensure_ascii=False, indent=2))
