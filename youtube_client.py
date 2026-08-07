"""YouTube client.

Strategy (in order):
  1. youtube-transcript-api (direct or via ScraperAPI HTTP proxy)
     - The library handles the YouTube timedtext API dance
     - If SCRAPER_API_KEY is set, route through ScraperAPI's HTTP proxy
       to bypass datacenter IP blocks (Vercel, GitHub Actions, etc.)
  2. oEmbed (public, no auth) for metadata when transcript fails

Why youtube-transcript-api?
  - No API key, no OAuth needed
  - Pulls the same public caption tracks that the embedded YouTube player
    uses (manual + auto-generated)
  - Stable, maintained library
  - Works through ScraperAPI HTTP proxy (proxy-server.scraperapi.com:8001)
    when SCRAPER_API_KEY is provided
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
    published: str
    transcript: str
    caption_source: str  # 'youtube-transcript-api' | 'failed'


_YT_URL_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})"
)


def extract_video_id(url: str) -> str | None:
    m = _YT_URL_RE.search(url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# ScraperAPI HTTP proxy setup
# ---------------------------------------------------------------------------
def _setup_scraperapi_proxy() -> None:
    """If SCRAPER_API_KEY is set, configure youtube-transcript-api to route
    through ScraperAPI's HTTP proxy.

    ScraperAPI exposes a standard HTTP proxy at:
        http://scraperapi:<API_KEY>@proxy-server.scraperapi.com:8001

    Standard `requests`/urllib clients can use HTTPS_PROXY env var.
    """
    key = os.getenv("SCRAPER_API_KEY", "").strip()
    if not key:
        logger.info("scraperapi: no SCRAPER_API_KEY, using direct YouTube access")
        return
    proxy_url = f"http://scraperapi:{key}@proxy-server.scraperapi.com:8001"
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["https_proxy"] = proxy_url
    os.environ["http_proxy"] = proxy_url
    logger.info(f"scraperapi: HTTP proxy configured (key len={len(key)})")


# Initialize proxy on import
_setup_scraperapi_proxy()


# ---------------------------------------------------------------------------
# Metadata via oEmbed (always public, no auth needed)
# ---------------------------------------------------------------------------
def _http_get_json(url: str, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _fetch_metadata_oembed(video_id: str) -> tuple[str, str, str]:
    """oEmbed -> (title, author, published)."""
    target = (
        f"https://www.youtube.com/oembed?url="
        f"https://www.youtube.com/watch?v={video_id}&format=json"
    )
    data = _http_get_json(target)
    return data.get("title", ""), data.get("author_name", ""), ""


# ---------------------------------------------------------------------------
# Transcript via youtube-transcript-api
# ---------------------------------------------------------------------------
def _fetch_transcript(video_id: str) -> tuple[str, str]:
    """Fetch transcript via youtube-transcript-api. Returns (text, language)."""
    # Import inside function so we can configure the proxy first
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    fetched = api.fetch(video_id, languages=["en", "en-US", "zh-Hant", "zh-Hans", "zh-TW", "zh"])
    segments = list(fetched)
    if not segments:
        raise RuntimeError("youtube-transcript-api returned 0 segments")
    text = " ".join(seg.text.replace("\n", " ") for seg in segments).strip()
    if not text:
        raise RuntimeError("youtube-transcript-api segments all empty")
    return text, getattr(fetched, "language_code", "en") or "en"


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def fetch_video(url: str) -> VideoInfo:
    """Fetch metadata + transcript. Fails fast on any error."""
    video_id = extract_video_id(url) or ""
    if not video_id:
        raise ValueError(f"Cannot parse YouTube video ID from URL: {url}")

    # 1) Metadata (oEmbed is always public)
    title, channel, published = _fetch_metadata_oembed(video_id)
    if not title:
        raise RuntimeError(f"oEmbed returned empty title for video {video_id}")
    logger.info(f"metadata: '{title[:60]}' channel='{channel}'")

    # 2) Transcript
    transcript, lang = _fetch_transcript(video_id)
    logger.info(
        f"transcript: lang={lang} chars={len(transcript)} via youtube-transcript-api"
    )

    return VideoInfo(
        video_id=video_id,
        url=url,
        title=title,
        channel=channel,
        published=published,
        transcript=transcript,
        caption_source="youtube-transcript-api",
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
