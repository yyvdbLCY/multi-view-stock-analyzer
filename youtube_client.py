"""YouTube Data API v3 client.

Uses Google's official YouTube Data API v3 (no datacenter IP block, no
third-party proxies). Requires YOUTUBE_API_KEY in env.

Two API calls per video:
  1. videos.list  -> metadata (title, channel, publish date)
  2. captions.list -> list available caption tracks
  3. captions.download -> fetch the actual caption content (JSON3 format)

Note on captions access:
  - captions.list and captions.download normally require OAuth (the video
    owner must authorize the request). For *public* videos where the owner
    has enabled downloadable captions, an API key may work, but this is
    not guaranteed. If we hit 403, we surface a clear error so the user
    knows it's an API permission issue, not a code bug.

Returns plain text transcript + metadata, or raises on failure.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class VideoInfo:
    video_id: str
    url: str
    title: str
    channel: str
    published: str  # ISO date string (YYYY-MM-DD)
    transcript: str
    caption_source: str  # 'youtube-api' | 'failed'


_YT_URL_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})"
)


def extract_video_id(url: str) -> str | None:
    m = _YT_URL_RE.search(url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# YouTube Data API v3 helpers
# ---------------------------------------------------------------------------
def _api_key() -> str:
    key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "YOUTUBE_API_KEY is not set. Get one at "
            "https://console.cloud.google.com/apis/credentials"
        )
    return key


def _api_get(endpoint: str, params: dict | None = None, timeout: int = 30) -> dict:
    """Call a YouTube Data API v3 GET endpoint and return parsed JSON."""
    params = dict(params or {})
    params["key"] = _api_key()
    url = f"https://www.googleapis.com/youtube/v3/{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        # Try to extract a friendly error message
        try:
            err = json.loads(body)
            msg = err.get("error", {}).get("message", body[:200])
        except Exception:
            msg = body[:200]
        raise RuntimeError(
            f"YouTube API {e.code} on {endpoint}: {msg}"
        ) from e


def _fetch_metadata(video_id: str) -> tuple[str, str, str]:
    """videos.list -> (title, channel, published YYYY-MM-DD)."""
    data = _api_get("videos", {"part": "snippet", "id": video_id})
    items = data.get("items", [])
    if not items:
        raise RuntimeError(f"YouTube API returned no items for video {video_id}")
    snip = items[0].get("snippet", {})
    title = snip.get("title", "")
    channel = snip.get("channelTitle", "")
    published_at = snip.get("publishedAt", "")
    published = published_at[:10] if published_at else ""
    return title, channel, published


def _fetch_caption_id(video_id: str) -> tuple[str, str]:
    """captions.list -> (caption_id, language_code).

    Returns the id of the first available English caption track (or the
    first available track if no English one exists).

    Raises a clear error if 403 (most common case: this endpoint requires
    OAuth, not just an API key).
    """
    data = _api_get("captions", {"part": "snippet", "videoId": video_id})
    items = data.get("items", [])
    if not items:
        raise RuntimeError(
            f"YouTube API reports no captions for video {video_id}. "
            f"(Some videos disable captions; some require OAuth.)"
        )
    # Prefer English
    for item in items:
        lang = item.get("snippet", {}).get("language", "")
        if lang.lower().startswith("en"):
            return item["id"], lang
    # Fall back to the first available
    first = items[0]
    return first["id"], first.get("snippet", {}).get("language", "?")


def _download_caption(caption_id: str) -> str:
    """captions.download -> plain text (parse JSON3 events)."""
    data = _api_get(
        f"captions/{caption_id}",
        {"tfmt": "json3"},
        timeout=45,
    )
    # JSON3 format: {"events": [{"segs": [{"utf8": "..."}]}]}
    texts: list[str] = []
    for ev in data.get("events", []):
        for seg in ev.get("segs", []):
            utf8 = seg.get("utf8")
            if utf8:
                texts.append(utf8.replace("\n", " "))
    return " ".join(texts).strip()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def fetch_video(url: str) -> VideoInfo:
    """Fetch metadata + transcript via YouTube Data API v3.

    Raises RuntimeError on any failure. Callers should catch and report.
    """
    video_id = extract_video_id(url) or ""
    if not video_id:
        raise ValueError(f"Cannot parse YouTube video ID from URL: {url}")

    # 1) Metadata (always works with API key)
    title, channel, published = _fetch_metadata(video_id)
    if not title:
        raise RuntimeError(f"YouTube API returned empty title for video {video_id}")
    logger.info(f"youtube api: got metadata for {video_id}: '{title[:60]}'")

    # 2) Caption track id (may 403 if not public / requires OAuth)
    try:
        caption_id, lang = _fetch_caption_id(video_id)
        logger.info(f"youtube api: found caption track {caption_id} ({lang}) for {video_id}")
    except RuntimeError as e:
        # Re-raise with extra hint about OAuth limitation
        raise RuntimeError(
            f"{e}\n"
            f"  Hint: YouTube Data API v3 'captions' endpoints typically "
            f"require OAuth (video owner authorization), not just an API key. "
            f"If the video owner has made captions publicly downloadable, an "
            f"API key will work; otherwise you need OAuth credentials."
        ) from e

    # 3) Caption content
    transcript = _download_caption(caption_id)
    if not transcript:
        raise RuntimeError(f"Downloaded caption for {caption_id} was empty")

    return VideoInfo(
        video_id=video_id,
        url=url,
        title=title,
        channel=channel,
        published=published,
        transcript=transcript,
        caption_source="youtube-api",
    )


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python youtube_client.py <YOUTUBE_URL>")
        sys.exit(1)
    info = fetch_video(sys.argv[1])
    print(f"Title: {info.title}")
    print(f"Channel: {info.channel}")
    print(f"Published: {info.published}")
    print(f"Caption source: {info.caption_source}")
    print(f"Transcript chars: {len(info.transcript)}")
    print(f"First 500 chars: {info.transcript[:500]}")
