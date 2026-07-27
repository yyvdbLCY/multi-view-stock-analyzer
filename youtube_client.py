"""YouTube caption / transcript fetcher.

Strategy (ordered, fall back on failure):
  1. ScraperAPI + YouTube oEmbed + timedtext   (preferred — bypasses YouTube's
                                                 datacenter IP block by routing
                                                 through ScraperAPI's residential pool)
  2. youtube-transcript-api                    (direct, may fail on Vercel IP)
  3. yt-dlp                                    (legacy fallback, needs cookies
                                                 on datacenter IPs)
  4. (Optional) Whisper                        (download audio + transcribe)

Returns plain text transcript + metadata.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import urllib.request
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class VideoInfo:
    video_id: str
    url: str
    title: str
    channel: str
    published: str  # ISO date string
    transcript: str
    caption_source: str  # 'scraperapi' | 'transcript-api' | 'yt-dlp' | 'whisper' | 'failed'


_YT_URL_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})"
)


def extract_video_id(url: str) -> str | None:
    m = _YT_URL_RE.search(url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Strategy 1: ScraperAPI proxy (preferred for cloud IPs)
# ---------------------------------------------------------------------------
def _scraperapi_url(target_url: str) -> str | None:
    """Wrap a target URL through ScraperAPI, or return None if not configured."""
    key = os.getenv("SCRAPER_API_KEY", "").strip()
    if not key:
        return None
    # ScraperAPI GET API: prepend their endpoint with the target as a query param
    return f"https://api.scraperapi.com/?api_key={key}&url={urllib.parse.quote(target_url, safe='')}"


def _http_get(url: str, timeout: int = 30) -> str | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        logger.info(f"http_get failed for {url[:80]}: {e}")
        return None


def _fetch_metadata_scraperapi(video_id: str) -> tuple[str, str, str]:
    """Fetch video metadata via ScraperAPI -> YouTube oEmbed.

    Returns (title, channel, published). published will be '' since oEmbed
    doesn't include upload date; we fall back to yt-dlp if we need it.
    """
    target = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    proxy = _scraperapi_url(target)
    if not proxy:
        return _fetch_metadata_oembed_direct(video_id)
    body = _http_get(proxy)
    if not body:
        return _fetch_metadata_oembed_direct(video_id)
    try:
        data = json.loads(body)
        return (
            data.get("title", ""),
            data.get("author_name", ""),
            "",  # oEmbed doesn't have upload date
        )
    except Exception as e:
        logger.info(f"scraperapi oembed parse failed: {e}")
        return _fetch_metadata_oembed_direct(video_id)


def _fetch_metadata_oembed_direct(video_id: str) -> tuple[str, str, str]:
    """Direct (no-proxy) oEmbed fallback. Works from cloud IPs."""
    target = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    body = _http_get(target, timeout=10)
    if body:
        try:
            data = json.loads(body)
            return (
                data.get("title", ""),
                data.get("author_name", ""),
                "",
            )
        except Exception:
            pass
    return ("", "", "")


def _fetch_transcript_scraperapi(video_id: str) -> str | None:
    """Fetch transcript via ScraperAPI.

    Approach: load the watch page, parse out the captionTracks JSON, then
    request the caption URL (also through ScraperAPI). Parses both JSON3
    and XML caption formats.
    """
    key = os.getenv("SCRAPER_API_KEY", "").strip()
    logger.info(
        f"_fetch_transcript_scraperapi({video_id}): "
        f"key_present={bool(key)}, key_len={len(key)}"
    )
    if not key:
        return None

    # 1) Load watch page to find captionTracks
    watch_target = f"https://www.youtube.com/watch?v={video_id}"
    watch_proxy = _scraperapi_url(watch_target)
    body = _http_get(watch_proxy, timeout=45)
    if not body:
        return None

    # Find captionTracks in the page JSON
    tracks = _extract_caption_tracks(body)
    if not tracks:
        logger.info(f"scraperapi: no captionTracks found for {video_id}")
        return None

    # Prefer an English track
    track = next(
        (t for t in tracks if (t.get("languageCode") or "").startswith("en")),
        tracks[0],
    )
    caption_url = track.get("baseUrl") or ""
    if not caption_url:
        return None

    # 2) Fetch the actual caption file (JSON3 if available)
    # Append fmt=json3 for easier parsing if not already in URL
    if "fmt=" not in caption_url:
        sep = "&" if "?" in caption_url else "?"
        caption_url_json = caption_url + sep + "fmt=json3"
    else:
        caption_url_json = caption_url

    cap_proxy = _scraperapi_url(caption_url_json)
    body = _http_get(cap_proxy, timeout=30)
    if not body:
        # Try the original (XML) URL
        cap_proxy_xml = _scraperapi_url(caption_url)
        body = _http_get(cap_proxy_xml, timeout=30)
    if not body:
        return None

    # 3) Parse as JSON3 or XML
    return _parse_caption_body(body)


def _extract_caption_tracks(html: str) -> list[dict]:
    """Pull captionTracks JSON out of a YouTube watch page HTML."""
    # captionTracks appears as "captionTracks":[{"baseUrl":"...","languageCode":"en",...},...]
    # The array is sometimes quite long; use a non-greedy match with a cap.
    m = re.search(r'"captionTracks"\s*:\s*(\[(?:[^\[\]]|\[[^\]]*\]){0,5000}\])', html)
    if not m:
        return []
    raw = m.group(1)
    try:
        tracks = json.loads(raw)
        return tracks if isinstance(tracks, list) else []
    except Exception as e:
        logger.info(f"captionTracks parse failed: {e}")
        return []


def _parse_caption_body(body: str) -> str | None:
    """Parse a caption response as JSON3 or XML into plain text."""
    # Try JSON3 first
    try:
        data = json.loads(body)
        if isinstance(data, dict) and "events" in data:
            texts = []
            for ev in data["events"]:
                for seg in ev.get("segs", []):
                    if "utf8" in seg and seg["utf8"]:
                        texts.append(seg["utf8"].replace("\n", " "))
            if texts:
                return " ".join(texts)
        if isinstance(data, list):
            # Older JSON3 format: list of {utf8, ...}
            texts = [s.get("utf8", s.get("text", "")).replace("\n", " ") for s in data if isinstance(s, dict)]
            if texts:
                return " ".join(texts)
    except Exception:
        pass

    # Try XML
    try:
        root = ET.fromstring(body)
        texts = []
        for t in root.iter("text"):
            if t.text:
                texts.append(t.text.replace("\n", " "))
        if texts:
            return " ".join(texts)
    except Exception as e:
        logger.info(f"caption XML parse failed: {e}")

    return None


# ---------------------------------------------------------------------------
# Strategy 2: youtube-transcript-api (direct, may fail on datacenter IPs)
# ---------------------------------------------------------------------------
def _fetch_via_transcript_api(video_id: str) -> str | None:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None
    try:
        api = YouTubeTranscriptApi()
        fetched = api.fetch(video_id)
        snippets = list(fetched)
        if not snippets:
            return None
        return " ".join(s.text.replace("\n", " ") for s in snippets)
    except Exception as e:
        logger.info(f"youtube-transcript-api failed for {video_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Strategy 3: yt-dlp (legacy, may need cookies)
# ---------------------------------------------------------------------------
def _clean_vtt(raw: str) -> str:
    lines = []
    seen = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if "-->" in line or re.match(r"^\d{2}:\d{2}", line):
            continue
        clean = re.sub(r"<[^>]+>", "", line)
        clean = clean.replace("&amp;", "&").replace("&#39;", "'").replace("&nbsp;", " ")
        if clean and clean not in seen:
            seen.add(clean)
            lines.append(clean)
    return " ".join(lines)


def _fetch_via_ytdlp(url: str) -> str | None:
    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        return None
    try:
        with YoutubeDL({"quiet": True, "skip_download": True, "writesubtitles": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        subs = info.get("subtitles", {}) or {}
        auto_subs = info.get("automatic_captions", {}) or {}
        lang_pref = ["en", "en-US", "en-GB"]

        def _pick(d: dict) -> str | None:
            for lang in lang_pref:
                if lang in d and d[lang]:
                    for fmt_pref in ("vtt", "srv3", "srt", "json3"):
                        for entry in d[lang]:
                            if entry.get("ext") == fmt_pref:
                                return entry.get("url")
            return None

        manual_url = _pick(subs)
        auto_url = _pick(auto_subs) if not manual_url else None
        chosen_url = manual_url or auto_url
        if not chosen_url:
            return None
        with urllib.request.urlopen(chosen_url, timeout=30) as r:
            raw = r.read().decode("utf-8", errors="ignore")
        return _clean_vtt(raw)
    except Exception as e:
        logger.info(f"yt-dlp fetch failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Strategy 4: Whisper fallback (optional)
# ---------------------------------------------------------------------------
def _whisper_fallback(url: str, video_id: str) -> str | None:
    if not settings.enable_whisper_fallback:
        return None
    try:
        import whisper  # type: ignore
    except ImportError:
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = Path(tmpdir) / f"{video_id}.m4a"
        from yt_dlp import YoutubeDL
        ydl_opts = {
            "quiet": True,
            "format": "bestaudio/best",
            "outtmpl": str(audio_path),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a"}],
        }
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        m4a = audio_path.with_suffix(".m4a")
        if not m4a.exists():
            return None
        model = whisper.load_model(settings.whisper_model)
        result = model.transcribe(str(m4a))
        return result.get("text", "")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def fetch_video(url: str) -> VideoInfo:
    """Main entry point. Fetches metadata + transcript. Raises on hard failure."""
    video_id = extract_video_id(url) or ""
    if not video_id:
        raise ValueError(f"Cannot parse YouTube video ID from URL: {url}")

    # 1) Metadata: try ScraperAPI -> oEmbed -> yt-dlp
    title, channel, published = _fetch_metadata_scraperapi(video_id)
    if not title:
        # Fallback: yt-dlp
        try:
            from yt_dlp import YoutubeDL
            with YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
                info = ydl.extract_info(url, download=False)
            title = info.get("title", "") or ""
            channel = info.get("uploader") or info.get("channel") or ""
            upload_date = info.get("upload_date") or ""
            if len(upload_date) == 8:
                published = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
        except Exception as e:
            logger.info(f"yt-dlp metadata fallback failed: {e}")

    if not title:
        raise RuntimeError(
            f"Could not fetch metadata for video {video_id}. "
            f"YouTube may be blocking this datacenter IP and no proxy is configured."
        )

    # 2) Transcript: try strategies in priority order
    transcript = _fetch_transcript_scraperapi(video_id)
    source = "scraperapi"
    if not transcript:
        transcript = _fetch_via_transcript_api(video_id)
        source = "transcript-api"
    if not transcript:
        transcript = _fetch_via_ytdlp(url)
        source = "yt-dlp"
    if not transcript:
        transcript = _whisper_fallback(url, video_id)
        source = "whisper" if transcript else "failed"

    if not transcript:
        raise RuntimeError(
            f"No captions available for video {video_id}. "
            f"Set ENABLE_WHISPER_FALLBACK=true to enable audio transcription."
        )

    return VideoInfo(
        video_id=video_id,
        url=url,
        title=title,
        channel=channel,
        published=published,
        transcript=transcript,
        caption_source=source,
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
