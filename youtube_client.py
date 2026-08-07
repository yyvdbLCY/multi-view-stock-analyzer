"""YouTube client.

Single path: youtube-transcript-api, optionally routed through
ScraperAPI's HTTP proxy when SCRAPER_API_KEY is set.

Why this approach:
  - youtube-transcript-api handles the YouTube timedtext API dance
    (signature, baseUrl, etc.) internally — we don't need to scrape
    watch pages ourselves
  - ScraperAPI HTTP proxy mode (proxy-server.scraperapi.com:8001) lets
    python `requests` go through ScraperAPI's network via the
    HTTPS_PROXY env var. No URL encoding issues, no REST endpoint
    weirdness
  - No API key, no OAuth, no cookies needed for this path

We deliberately skip metadata (title/channel) — it would require
either an extra call or scraping the watch page, both of which add
complexity. The bot only needs the transcript to do LLM extraction.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class VideoInfo:
    video_id: str
    url: str
    title: str  # empty — not fetched (we let the user see the URL)
    channel: str  # empty
    published: str  # empty
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
    """No-op now. We used to route youtube-transcript-api through
    ScraperAPI's HTTP CONNECT proxy to bypass YouTube's datacenter IP
    block. But the smoke-test diag (Aug 2026) showed:
      - direct curl to YouTube from GitHub Actions: HTTP 200 (works!)
      - curl via ScraperAPI CONNECT proxy: HTTP 401 (proxy rejects)
    ScraperAPI's free plan doesn't grant access to the proxy-server
    pool. So we just go direct.

    We DO set certifi CA bundle so the requests/urllib3 SSL handshake
    uses up-to-date roots.
    """
    try:
        import certifi
        cafile = certifi.where()
        os.environ["REQUESTS_CA_BUNDLE"] = cafile
        os.environ["SSL_CERT_FILE"] = cafile
    except Exception:
        pass
    print("[youtube_client] using direct YouTube access (no proxy)", flush=True)


# ---------------------------------------------------------------------------
# Session cookies — bypass YouTube's BOT_DETECTED on datacenter IPs
# ---------------------------------------------------------------------------
def _load_session_cookies() -> list[dict]:
    """Load YouTube account session cookies from the YOUTUBE_COOKIES env var.

    Accepts:
      - base64-encoded JSON list (our storage format)
      - plain JSON list

    Returns [] if env var is missing or malformed.
    """
    raw = os.getenv("YOUTUBE_COOKIES", "").strip()
    print(
        f"[youtube_client] cookies: YOUTUBE_COOKIES present={bool(raw)} len={len(raw)}",
        flush=True,
    )
    if not raw:
        return []
    # Try base64-decode first; if that fails, treat as plain JSON
    decoded = None
    if raw.startswith("base64:"):
        raw = raw[7:]
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
        print(
            f"[youtube_client] cookies: base64 decoded -> {len(decoded)} chars",
            flush=True,
        )
    except Exception:
        # Not base64 — try as plain JSON
        decoded = raw
    try:
        cookies = json.loads(decoded)
        if not isinstance(cookies, list):
            print(
                f"[youtube_client] cookies: expected JSON list, got {type(cookies)}",
                flush=True,
            )
            return []
        print(
            f"[youtube_client] cookies: loaded {len(cookies)} session cookies",
            flush=True,
        )
        return cookies
    except json.JSONDecodeError as e:
        print(
            f"[youtube_client] cookies: JSON decode failed: {e}, first 80 chars: {decoded[:80]!r}",
            flush=True,
        )
        return []


_setup_scraperapi_proxy()


# ---------------------------------------------------------------------------
# Transcript via youtube-transcript-api
# ---------------------------------------------------------------------------
def _fetch_transcript(video_id: str) -> tuple[str, str]:
    """Fetch transcript via youtube-transcript-api. Returns (text, language).

    YouTube's bot detection blocks requests with the default
    `python-requests/...` User-Agent. We wrap a session with a
    Chrome-style User-Agent and load the user's session cookies
    (if YOUTUBE_COOKIES env is set) so the request is treated as
    a logged-in user, not a datacenter bot.
    """
    import requests
    from youtube_transcript_api import YouTubeTranscriptApi

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9,zh-Hant;q=0.8",
    })
    # Load the user's YouTube session cookies (bypass BOT_DETECTED)
    for c in _load_session_cookies():
        name = c.get("name")
        value = c.get("value")
        domain = c.get("domain", ".youtube.com")
        if name and value:
            session.cookies.set(name, value, domain=domain)

    api = YouTubeTranscriptApi(http_client=session)
    fetched = api.fetch(
        video_id,
        languages=["en", "en-US", "zh-Hant", "zh-Hans", "zh-TW", "zh"],
    )
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
    """Fetch transcript via youtube-transcript-api. Fails fast on error."""
    video_id = extract_video_id(url) or ""
    if not video_id:
        raise ValueError(f"Cannot parse YouTube video ID from URL: {url}")

    transcript, lang = _fetch_transcript(video_id)
    print(
        f"[youtube_client] transcript: lang={lang} chars={len(transcript)} for {video_id}",
        flush=True,
    )

    return VideoInfo(
        video_id=video_id,
        url=url,
        title="",  # not fetched
        channel="",  # not fetched
        published="",  # not fetched
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
    print(f"video_id: {info.video_id}")
    print(f"caption_source: {info.caption_source}")
    print(f"transcript_chars: {len(info.transcript)}")
    print(f"First 500 chars: {info.transcript[:500]}")
