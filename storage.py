"""SQLite persistence layer.

Tables:
- videos:        one row per YouTube video processed
- extractions:   one row per (video, ticker) extraction record
- reports:       one row per ticker synthesis report (per run)
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    channel TEXT,
    published TEXT,
    caption_source TEXT,           -- 'auto' | 'manual' | 'whisper' | 'failed'
    transcript_chars INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending', -- pending | caption_ok | extracted | failed
    error TEXT,
    created_at TEXT NOT NULL,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS extractions (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL,
    ticker TEXT NOT NULL,
    company TEXT,
    sentiment TEXT,
    speaker_confidence TEXT,
    key_points_json TEXT,          -- JSON array of strings
    price_target REAL,
    time_horizon TEXT,
    raw_json TEXT,                 -- full per-stock object from LLM
    created_at TEXT NOT NULL,
    FOREIGN KEY (video_id) REFERENCES videos(video_id)
);
CREATE INDEX IF NOT EXISTS idx_extractions_ticker ON extractions(ticker);
CREATE INDEX IF NOT EXISTS idx_extractions_created ON extractions(created_at);

CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    run_id TEXT NOT NULL,          -- groups reports from same TG message
    overall_score INTEGER,
    overall_sentiment TEXT,
    confidence INTEGER,
    summary TEXT,
    full_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_ticker ON reports(ticker);
CREATE INDEX IF NOT EXISTS idx_reports_run ON reports(run_id);
"""


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    settings.ensure_dirs()
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# Videos
# =============================================================================
def upsert_video(
    video_id: str,
    url: str,
    title: str | None = None,
    channel: str | None = None,
    published: str | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO videos(video_id, url, title, channel, published, created_at)
               VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(video_id) DO UPDATE SET
                 url=excluded.url,
                 title=COALESCE(excluded.title, videos.title),
                 channel=COALESCE(excluded.channel, videos.channel),
                 published=COALESCE(excluded.published, videos.published)""",
            (video_id, url, title, channel, published, _now()),
        )


def update_video_status(
    video_id: str,
    status: str,
    *,
    caption_source: str | None = None,
    transcript_chars: int | None = None,
    error: str | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE videos
               SET status = ?,
                   caption_source = COALESCE(?, caption_source),
                   transcript_chars = COALESCE(?, transcript_chars),
                   error = ?,
                   processed_at = ?
               WHERE video_id = ?""",
            (status, caption_source, transcript_chars, error, _now(), video_id),
        )


def get_video(video_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
        return dict(row) if row else None


# =============================================================================
# Extractions
# =============================================================================
def save_extraction(video_id: str, stock: dict[str, Any]) -> str:
    extraction_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO extractions
               (id, video_id, ticker, company, sentiment, speaker_confidence,
                key_points_json, price_target, time_horizon, raw_json, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                extraction_id,
                video_id,
                stock.get("ticker", "").upper(),
                stock.get("company"),
                stock.get("sentiment"),
                stock.get("speaker_confidence"),
                json.dumps(stock.get("key_points", []), ensure_ascii=False),
                stock.get("price_target"),
                stock.get("time_horizon"),
                json.dumps(stock, ensure_ascii=False),
                _now(),
            ),
        )
    return extraction_id


def get_mentions_for_ticker(ticker: str, days: int | None = None) -> list[dict]:
    """Return all extractions for a ticker, optionally within last `days`."""
    window_days = days if days is not None else settings.aggregation_window_days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT e.*, v.title as video_title, v.channel as channel, v.published as video_published,
                      v.url as video_url
               FROM extractions e
               JOIN videos v ON v.video_id = e.video_id
               WHERE e.ticker = ? AND e.created_at >= ?
               ORDER BY e.created_at DESC""",
            (ticker.upper(), cutoff),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["key_points"] = json.loads(d.pop("key_points_json") or "[]")
            out.append(d)
        return out


def list_recent_tickers(days: int | None = None) -> list[tuple[str, int]]:
    """Return (ticker, mention_count) for tickers mentioned in the window, sorted by count."""
    window_days = days if days is not None else settings.aggregation_window_days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT ticker, COUNT(*) as cnt
               FROM extractions
               WHERE created_at >= ?
               GROUP BY ticker
               ORDER BY cnt DESC""",
            (cutoff,),
        ).fetchall()
        return [(r["ticker"], r["cnt"]) for r in rows]


# =============================================================================
# Reports
# =============================================================================
def save_report(ticker: str, run_id: str, report: dict) -> Path:
    report_id = str(uuid.uuid4())
    full_json = json.dumps(report, ensure_ascii=False, indent=2)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO reports
               (id, ticker, run_id, overall_score, overall_sentiment, confidence,
                summary, full_json, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                report_id,
                ticker.upper(),
                run_id,
                report.get("overall_score"),
                report.get("overall_sentiment"),
                report.get("confidence"),
                report.get("summary"),
                full_json,
                _now(),
            ),
        )

    # Also persist as a JSON file for easy reading / sharing
    out_path = settings.reports_dir / f"{run_id}_{ticker}.json"
    out_path.write_text(full_json, encoding="utf-8")
    return out_path


def list_reports_by_run(run_id: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reports WHERE run_id = ? ORDER BY ticker", (run_id,)
        ).fetchall()
        return [dict(r) for r in rows]
