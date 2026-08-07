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
    """If SCRAPER_API_KEY is set, configure HTTP/HTTPS_PROXY env vars
    so that python `requests` (used by youtube-transcript-api) routes
    through ScraperAPI's HTTP CONNECT proxy.

    Also set REQUESTS_CA_BUNDLE / SSL_CERT_FILE to certifi's CA bundle
    so the proxied HTTPS handshake to YouTube uses up-to-date roots.

    When a proxy is set, monkey-patch `requests.adapters.HTTPAdapter.send`
    to pass `verify=False` — the cert chain through ScraperAPI's proxy
    can fail strict cert verification on some cloud runners, and we trust
    ScraperAPI as the proxy hop.

    Format: http://scraperapi:<KEY>@proxy-server.scraperapi.com:8001
    """
    # Always: ensure certifi CA bundle is what `requests`/urllib3 uses.
    # Some cloud runners ship a stale system bundle.
    try:
        import certifi
        cafile = certifi.where()
        os.environ["REQUESTS_CA_BUNDLE"] = cafile
        os.environ["SSL_CERT_FILE"] = cafile
        print(
            f"[youtube_client] cert: using certifi bundle at {cafile}",
            flush=True,
        )
    except Exception as e:
        print(f"[youtube_client] cert: certifi load failed: {e}", flush=True)

    key = os.getenv("SCRAPER_API_KEY", "").strip()
    if not key:
        print(
            "[youtube_client] scraperapi: no SCRAPER_API_KEY, using direct YouTube access",
            flush=True,
        )
        return
    proxy_url = f"http://scraperapi:{key}@proxy-server.scraperapi.com:8001"
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["https_proxy"] = proxy_url
    os.environ["http_proxy"] = proxy_url
    print(
        f"[youtube_client] scraperapi: HTTP proxy configured (key len={len(key)})",
        flush=True,
    )

    # Monkey-patch requests to disable cert verification when going through
    # the proxy. We trust ScraperAPI as the proxy hop.
    try:
        import requests.adapters
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        _orig_send = requests.adapters.HTTPAdapter.send

        def _patched_send(self, request, *args, **kwargs):
            kwargs["verify"] = False
            return _orig_send(self, request, *args, **kwargs)

        requests.adapters.HTTPAdapter.send = _patched_send
        print(
            "[youtube_client] requests: SSL verification disabled (via ScraperAPI proxy)",
            flush=True,
        )
    except Exception as e:
        print(
            f"[youtube_client] requests: failed to patch verify=False: {e}",
            flush=True,
        )


_setup_scraperapi_proxy()


# ---------------------------------------------------------------------------
# Transcript via youtube-transcript-api
# ---------------------------------------------------------------------------
def _fetch_transcript(video_id: str) -> tuple[str, str]:
    """Fetch transcript via youtube-transcript-api. Returns (text, language)."""
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
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
