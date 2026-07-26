"""Real-time stock price lookup via yfinance."""
from __future__ import annotations

import logging
from functools import lru_cache

import yfinance as yf

logger = logging.getLogger(__name__)


@lru_cache(maxsize=256)
def get_latest_price(ticker: str) -> float | None:
    """Returns latest available price (regular market close during/after hours).
    Cached for process lifetime. Returns None on failure.
    """
    try:
        t = yf.Ticker(ticker.upper())
        info = t.fast_info  # type: ignore[attr-defined]
        price = getattr(info, "last_price", None) or getattr(info, "last_price", None)
        if price is None:
            # Fallback to history
            hist = t.history(period="1d", interval="1m")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
        return float(price) if price is not None else None
    except Exception as e:
        logger.warning(f"yfinance lookup failed for {ticker}: {e}")
        return None


def get_batch_prices(tickers: list[str]) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for t in tickers:
        out[t.upper()] = get_latest_price(t)
    return out


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python stock_client.py AAPL NVDA TSLA")
        sys.exit(1)
    for t in sys.argv[1:]:
        print(f"{t}: ${get_latest_price(t)}")
