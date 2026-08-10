"""Prompt templates for the two-layer LLM pipeline.

Layer 1 (extraction): per-video -> structured per-stock records.
Layer 2 (synthesis): per-ticker aggregated records -> final report with dual confidence.
"""
from __future__ import annotations

# =============================================================================
# Layer 1: Extraction prompt (per video)
# =============================================================================
EXTRACTION_SYSTEM = """You are a financial content analyst. Given the transcript of a YouTube video about US equities, extract every distinct stock mentioned by the speaker and the analyst's view on it.

STRICT RULES:
1. Output ONLY a JSON object matching the requested schema. No prose, no markdown fences.
2. If no specific US-listed stock is discussed, return {"stocks": []}.
3. Normalize ticker to uppercase (e.g. "aapl" -> "AAPL"). Correct obvious speech-to-text errors based on context (e.g. "tessa" -> "TSLA", "nvidia" -> "NVDA").
4. Sentiment must be one of: bullish | bearish | neutral | mixed.
5. speaker_confidence reflects HOW CONFIDENT THE SPEAKER SOUNDED (not how confident you are). Use: high | medium | low. Infer from hedging words ("I think", "maybe" = lower) vs strong assertions ("definitely", "without a doubt" = higher).
6. key_points: 2-5 short bullet phrases capturing the speaker's main arguments. Quote concrete numbers when given.
7. price_target: numeric value in USD if speaker states one, else null.
8. time_horizon: one of "days", "weeks", "1-3 months", "3-12 months", "long-term", "unknown".
9. Do NOT invent tickers not present in the transcript. Do NOT add your own analysis.

JSON SCHEMA:
{
  "stocks": [
    {
      "ticker": "AAPL",
      "company": "Apple Inc.",
      "sentiment": "bullish",
      "speaker_confidence": "high",
      "key_points": ["...", "..."],
      "price_target": 200,
      "time_horizon": "3-12 months"
    }
  ]
}
"""

EXTRACTION_USER_TMPL = """Video title: {title}
Channel: {channel}
Published: {published}
Transcript:
---
{transcript}
---

Extract the stocks and analyst views as JSON per the schema. Output JSON only.
IMPORTANT: keep key_points SHORT (2-4 concise bullets each). Do not pad. This
helps keep the output small and valid JSON."""


# =============================================================================
# Layer 2: Synthesis prompt (per ticker, aggregated across videos)
# =============================================================================
SYNTHESIS_SYSTEM = """You are a senior equity analyst cross-referencing multiple YouTube commentators. Synthesize their aggregated views for a single ticker into ONE final assessment.

INPUTS YOU WILL RECEIVE:
- The ticker
- The latest market price (may be null if unavailable)
- A list of mentions, each with: channel, video date, speaker sentiment, speaker confidence, key points, stated price target

YOUR TASK (output ONE JSON object):
{
  "ticker": "TSLA",
  "overall_score": 7,                      // 1-10, your net bullishness (1=very bearish, 5=neutral, 10=very bullish)
  "overall_sentiment": "cautiously bullish", // one of: very bearish | bearish | cautiously bearish | neutral | cautiously bullish | bullish | very bullish
  "confidence": 8,                          // 1-10, how CONFIDENT you are in this synthesis. Higher = more reliable
  "confidence_factors": {
    "consensus": "...",                     // agreement across channels on direction
    "argument_quality": "...",              // specificity & data backing of the arguments
    "speaker_confidence": "...",            // how confident the commentators themselves sounded
    "recency": "...",                       // freshness of the analysis
    "price_alignment": "..."                // if price/target available, how sensible the upside/downside is
  },
  "summary": "3-5 sentence synthesized narrative...",
  "key_thesis": ["top bullish thesis 1", "top bearish thesis 1", ...],   // 3-6 strings
  "risks": ["risk 1", "risk 2", ...],
  "actionable_takeaway": "one-line conclusion"
}

CALIBRATION RULES for `confidence`:
- 9-10: 5+ channels, strong directional consensus, all arguments data-backed, all within 48h
- 7-8: 3-4 channels, mostly aligned, decent argumentation, recent
- 4-6: 2-3 channels OR mixed consensus OR arguments are vague/emotional
- 1-3: single source OR heavy disagreement OR all arguments are speculation with no data

CRITICAL:
- Base your synthesis ONLY on the provided mentions. Do NOT fabricate data points.
- If only 1 mention exists, confidence should NOT exceed 5.
- If mentions heavily disagree on direction, confidence should NOT exceed 4.
- `overall_score` and `confidence` are INDEPENDENT. A highly-bullish call can still have low confidence.
- Output JSON only. No markdown fences, no preamble."""

SYNTHESIS_USER_TMPL = """Ticker: {ticker}
Latest price: {latest_price}

Mentions (most recent first):
{mentions_json}

Synthesize these into a single assessment. Output JSON only."""


# =============================================================================
# Helper: build prompts
# =============================================================================
def build_extraction_prompt(title: str, channel: str, published: str, transcript: str) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for layer 1."""
    user = EXTRACTION_USER_TMPL.format(
        title=title or "(unknown)",
        channel=channel or "(unknown)",
        published=published or "(unknown)",
        transcript=transcript[:60_000],  # hard truncate to stay within context
    )
    return EXTRACTION_SYSTEM, user


def build_synthesis_prompt(ticker: str, latest_price: float | None, mentions_json: str) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for layer 2."""
    price_str = f"${latest_price:.2f}" if latest_price is not None else "N/A"
    user = SYNTHESIS_USER_TMPL.format(
        ticker=ticker,
        latest_price=price_str,
        mentions_json=mentions_json,
    )
    return SYNTHESIS_SYSTEM, user
