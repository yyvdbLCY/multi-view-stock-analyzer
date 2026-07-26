"""Layer 2: Gemini Flash synthesis.

Takes an aggregated per-ticker bundle, asks Gemini to produce a final assessment
with overall_score, overall_sentiment, confidence, and confidence_factors.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import google.generativeai as genai

from config import settings
from prompts import build_synthesis_prompt

logger = logging.getLogger(__name__)

genai.configure(api_key=settings.gemini_api_key)
_eval_model = genai.GenerativeModel(
    settings.gemini_model_eval,
    generation_config={
        "temperature": 0.3,
        "top_p": 0.95,
        "max_output_tokens": 4096,
        "response_mime_type": "application/json",
    },
)


def _extract_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"Could not parse JSON from synthesis output:\n{text[:500]}")


def synthesize_ticker(bundle: dict[str, Any]) -> dict[str, Any]:
    """Run the synthesis prompt for one ticker bundle.

    `bundle` is the output of aggregator.aggregate_for_ticker().
    Returns the LLM's structured report dict, augmented with ticker + risk warning.
    """
    ticker = bundle["ticker"]
    mentions = bundle["mentions"]
    latest_price = bundle.get("latest_price")

    if not mentions:
        return {
            "ticker": ticker,
            "overall_score": None,
            "overall_sentiment": "no_data",
            "confidence": 0,
            "summary": "No mentions found in the aggregation window.",
            "key_thesis": [],
            "risks": [],
            "actionable_takeaway": "N/A",
        }

    mentions_json = json.dumps(mentions, ensure_ascii=False, indent=2)
    system, user = build_synthesis_prompt(ticker, latest_price, mentions_json)

    try:
        response = _eval_model.generate_content([system, user])
        text = response.text or ""
        report = _extract_json(text)
    except Exception as e:
        logger.error(f"Synthesis failed for {ticker}: {e}")
        return {
            "ticker": ticker,
            "overall_score": None,
            "overall_sentiment": "error",
            "confidence": 0,
            "summary": f"Synthesis failed: {e}",
            "key_thesis": [],
            "risks": [],
            "actionable_takeaway": "N/A",
        }

    # Ensure required keys + add metadata
    report.setdefault("ticker", ticker)
    report.setdefault("latest_price", latest_price)
    report.setdefault("mention_count", bundle.get("mention_count", len(mentions)))
    report["risk_warning"] = (
        "本報告由 AI 生成，僅供參考，不構成投資建議。"
        "投資有風險，請自行審慎判斷。"
    )

    # Clamp scores to valid ranges
    for k in ("overall_score", "confidence"):
        v = report.get(k)
        if v is not None:
            try:
                v_int = int(v)
                report[k] = max(1, min(10, v_int))
            except (TypeError, ValueError):
                report[k] = None

    return report


def synthesize_all(bundles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run synthesis across all ticker bundles. Returns list of report dicts."""
    reports = []
    for bundle in bundles:
        try:
            r = synthesize_ticker(bundle)
            reports.append(r)
        except Exception as e:
            logger.error(f"synthesize_all failed for {bundle.get('ticker')}: {e}")
            reports.append(
                {
                    "ticker": bundle.get("ticker"),
                    "overall_sentiment": "error",
                    "confidence": 0,
                    "summary": f"Synthesis crashed: {e}",
                }
            )
    # Sort by confidence desc, then overall_score desc
    reports.sort(
        key=lambda r: (
            -(r.get("confidence") or 0),
            -(r.get("overall_score") or 0),
        )
    )
    return reports
