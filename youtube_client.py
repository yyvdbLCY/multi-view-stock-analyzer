"""YouTube client via ScraperAPI proxy.

Uses ScraperAPI to bypass YouTube's datacenter IP block.
Requires SCRAPER_API_KEY in env.

Strategy (single path, fail-fast):
  1. oEmbed via ScraperAPI -> title, channel
  2. Watch page via ScraperAPI -> find captionTracks
  3. caption track baseUrl + fmt=json3 via ScraperAPI -> transcript text

If any step fails, raises immediately. No silent fallback.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
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
    published: str  # YYYY-MM-DD (may be empty if oEmbed doesn't return it)
    transcript: str
    caption_source: str  # 'scraperapi'


_YT_URL_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})"
)


def extract_video_id(url: str) -> str | None:
    m = _YT_URL_RE.search(url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# ScraperAPI helpers
# ---------------------------------------------------------------------------
def _api_key() -> str:
    key = os.getenv("SCRAPER_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "SCRAPER_API_KEY is not set. Get one at https://www.scraperapi.com/"
        )
    return key


def _scraperapi_get(target_url: str, timeout: int = 60) -> str:
    """Fetch `target_url` through ScraperAPI and return the raw body string."""
    proxy = (
        f"https://api.scraperapi.com/?api_key={_api_key()}"
        f"&url={urllib.parse.quote(target_url, safe='')}"
    )
    logger.info(f"scraperapi: GET {target_url[:80]}...")
    try:
        with urllib.request.urlopen(proxy, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="ignore")
            logger.info(f"scraperapi: status={r.status}, len={len(body)}")
            return body
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="ignore")[:200]
        raise RuntimeError(
            f"ScraperAPI HTTP {e.code} on {target_url[:80]}: {err_body}"
        ) from e
    except Exception as e:
        raise RuntimeError(
            f"ScraperAPI request failed for {target_url[:80]}: "
            f"{type(e).__name__}: {e}"
        ) from e


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------
def _fetch_metadata(video_id: str) -> tuple[str, str, str]:
    """oEmbed via ScraperAPI -> (title, author, published)."""
    target = (
        f"https://www.youtube.com/oembed?url="
        f"https://www.youtube.com/watch?v={video_id}&format=json"
    )
    body = _scraperapi_get(target)
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"oEmbed returned non-JSON ({len(body)} bytes): {body[:200]}"
        ) from e
    title = data.get("title", "")
    author = data.get("author_name", "")
    return title, author, ""  # oEmbed doesn't expose published date


# ---------------------------------------------------------------------------
# Transcript
# ---------------------------------------------------------------------------
def _find_caption_tracks(watch_body: str) -> list[dict]:
    """Extract the captionTracks JSON array from a YouTube watch page.

    captionTracks is a JSON array embedded in the page. We find its
    bracket span and parse the slice as JSON.
    """
    m = re.search(r'"captionTracks"\s*:\s*(\[)', watch_body)
    if not m:
        raise RuntimeError(
            f"No captionTracks found in watch page (body len={len(watch_body)})"
        )
    arr_start = m.end() - 1
    depth = 0
    arr_end = arr_start
    for i in range(arr_start, min(arr_start + 50_000, len(watch_body))):
        ch = watch_body[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                arr_end = i
                break
    if depth != 0:
        raise RuntimeError("Could not find end of captionTracks array")
    try:
        return json.loads(watch_body[arr_start : arr_end + 1])
    except json.JSONDecodeError as e:
        raise RuntimeError(f"captionTracks parse failed: {e}") from e


def _fetch_transcript(video_id: str) -> str:
    """Watch page -> captionTracks -> timedtext (JSON3) -> text."""
    target = f"https://www.youtube.com/watch?v={video_id}"
    body = _scraperapi_get(target, timeout=60)

    tracks = _find_caption_tracks(body)
    if not tracks:
        raise RuntimeError("captionTracks array is empty")

    # Prefer English, else first available
    chosen = next(
        (
            t for t in tracks
            if (t.get("languageCode") or "").lower().startswith("en")
        ),
        tracks[0],
    )
    base_url = chosen.get("baseUrl", "")
    if not base_url:
        raise RuntimeError("caption track has no baseUrl")
    lang = chosen.get("languageCode", "?")
    logger.info(
        f"scraperapi: chose caption track lang={lang} for {video_id}"
    )

    # Fetch caption content as JSON3 (structured events)
    json3_url = base_url + "&fmt=json3"
    caption_body = _scraperapi_get(json3_url, timeout=60)

    try:
        events = json.loads(caption_body).get("events", [])
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"timedtext not JSON3 ({len(caption_body)} bytes): "
            f"{caption_body[:200]}"
        ) from e

    texts: list[str] = []
    for ev in events:
        for seg in ev.get("segs", []):
            utf8 = seg.get("utf8")
            if utf8:
                texts.append(utf8.replace("\n", " "))
    transcript = " ".join(texts).strip()

    if not transcript:
        raise RuntimeError("timedtext returned empty transcript")
    return transcript


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------
def fetch_video(url: str) -> VideoInfo:
    """Fetch metadata + transcript via ScraperAPI. Fails fast on any error."""
    video_id = extract_video_id(url) or ""
    if not video_id:
        raise ValueError(f"Cannot parse YouTube video ID from URL: {url}")

    title, channel, published = _fetch_metadata(video_id)
    if not title:
        raise RuntimeError(
            f"ScraperAPI oEmbed returned empty title for video {video_id}"
        )
    logger.info(f"scraperapi: title='{title[:60]}' channel='{channel}'")

    transcript = _fetch_transcript(video_id)
    logger.info(f"scraperapi: transcript len={len(transcript)} for {video_id}")

    return VideoInfo(
        video_id=video_id,
        url=url,
        title=title,
        channel=channel,
        published=published,
        transcript=transcript,
        caption_source="scraperapi",
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
