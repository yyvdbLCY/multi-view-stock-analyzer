"""Aggregation layer: group extractions by ticker, attach live prices,
produce the mentions payload fed into the synthesis layer.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import storage
from stock_client import get_latest_price

logger = logging.getLogger(__name__)


def aggregate_for_ticker(ticker: str, days: int | None = None) -> dict[str, Any]:
    """Build the per-ticker aggregation bundle.

    Returns:
        {
            "ticker": "TSLA",
            "latest_price": 245.30 | None,
            "mention_count": 3,
            "mentions": [ {channel, video_published, sentiment, speaker_confidence,
                           key_points, price_target, video_url, video_title}, ... ]
        }
    """
    mentions_raw = storage.get_mentions_for_ticker(ticker, days=days)

    mentions = []
    for m in mentions_raw:
        mentions.append(
            {
                "channel": m.get("channel") or "(unknown)",
                "video_title": m.get("video_title") or "",
                "video_url": m.get("video_url") or "",
                "video_published": m.get("video_published") or "",
                "extraction_date": m.get("created_at") or "",
                "sentiment": m.get("sentiment") or "neutral",
                "speaker_confidence": m.get("speaker_confidence") or "medium",
                "key_points": m.get("key_points") or [],
                "price_target": m.get("price_target"),
                "time_horizon": m.get("time_horizon") or "unknown",
            }
        )

    latest_price = get_latest_price(ticker)

    return {
        "ticker": ticker.upper(),
        "latest_price": latest_price,
        "mention_count": len(mentions),
        "mentions": mentions,
    }


def aggregate_all_active_tickers(days: int | None = None) -> list[dict[str, Any]]:
    """Aggregate every ticker that has mentions within the window."""
    tickers = storage.list_recent_tickers(days=days)
    bundles = []
    for ticker, count in tickers:
        try:
            bundle = aggregate_for_ticker(ticker, days=days)
            bundles.append(bundle)
        except Exception as e:
            logger.error(f"Failed to aggregate {ticker}: {e}")
    return bundles


def mentions_to_json(mentions: list[dict]) -> str:
    """Compact JSON string for prompt injection."""
    return json.dumps(mentions, ensure_ascii=False, indent=2)
