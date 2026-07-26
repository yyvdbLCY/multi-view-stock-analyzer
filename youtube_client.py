"""YouTube caption / transcript fetcher.

Strategy (ordered, fall back on failure):
  1. youtube-transcript-api   (lightweight, just hits the captions endpoint;
                                bypasses yt-dlp's browser-impersonation
                                which trips YouTube's anti-bot on datacenter IPs
                                like Vercel's).
  2. yt-dlp                    (legacy fallback if transcripts endpoint blocked).
  3. (Optional) Whisper        (download audio + transcribe, requires ffmpeg).

Returns plain text transcript + metadata.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

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
    caption_source: str  # 'transcript-api' | 'yt-dlp' | 'whisper' | 'failed'


_YT_URL_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})"
)


def extract_video_id(url: str) -> str | None:
    m = _YT_URL_RE.search(url)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Strategy 1: youtube-transcript-api (preferred for cloud/datacenter IPs)
# ---------------------------------------------------------------------------
def _fetch_via_transcript_api(video_id: str) -> str | None:
    """Fetch the transcript using the lightweight youtube-transcript-api.

    Bypasses yt-dlp's anti-bot fingerprinting. Returns plain text or None.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        logger.info("youtube-transcript-api not installed, skipping.")
        return None

    try:
        api = YouTubeTranscriptApi()
        # Newer API (>=0.6): api.fetch(video_id) returns FetchedTranscript
        fetched = api.fetch(video_id)
        snippets = list(fetched)  # FetchedTranscriptSnippet objects
        if not snippets:
            return None
        return " ".join(s.text.replace("\n", " ") for s in snippets)
    except Exception as e:
        logger.info(f"youtube-transcript-api failed for {video_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# Strategy 2: yt-dlp (legacy fallback — may fail on datacenter IPs)
# ---------------------------------------------------------------------------
def _clean_vtt(raw: str) -> str:
    """Strip VTT/SRT timestamps and tags, return plain text joined by spaces."""
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
        source = "manual" if manual_url else ("auto" if auto_url else None)
        if not chosen_url:
            return None

        import urllib.request
        with urllib.request.urlopen(chosen_url, timeout=30) as r:
            raw = r.read().decode("utf-8", errors="ignore")
        return _clean_vtt(raw), source
    except Exception as e:
        logger.info(f"yt-dlp fetch failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Strategy 3: Whisper fallback (optional)
# ---------------------------------------------------------------------------
def _whisper_fallback(url: str, video_id: str) -> str | None:
    if not settings.enable_whisper_fallback:
        return None
    try:
        import whisper  # type: ignore
    except ImportError:
        logger.error("Whisper fallback enabled but `whisper` package not installed.")
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

    # Metadata: try yt-dlp first (it gives us title/channel/date reliably)
    # If yt-dlp fails (e.g. anti-bot), try the YouTube oEmbed endpoint.
    title, channel, published = _fetch_metadata(url, video_id)
    if not title:
        raise RuntimeError(
            f"Could not fetch metadata for video {video_id}. "
            f"YouTube may be blocking this datacenter IP."
        )

    # Transcript: try strategies in order
    transcript = _fetch_via_transcript_api(video_id)
    source = "transcript-api"
    if not transcript:
        result = _fetch_via_ytdlp(url)
        if result:
            transcript, yt_source = result
            source = f"yt-dlp:{yt_source}"
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


def _fetch_metadata(url: str, video_id: str) -> tuple[str, str, str]:
    """Try yt-dlp first, then oEmbed as fallback. Returns (title, channel, published)."""
    # Try yt-dlp
    try:
        from yt_dlp import YoutubeDL
        with YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        title = info.get("title") or ""
        channel = info.get("uploader") or info.get("channel") or ""
        upload_date = info.get("upload_date") or ""
        published = ""
        if len(upload_date) == 8:
            published = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
        if title:
            return title, channel, published
    except Exception as e:
        logger.info(f"yt-dlp metadata fetch failed: {e}")

    # Fallback: YouTube oEmbed (no anti-bot)
    try:
        import urllib.request
        oembed_url = f"https://www.youtube.com/oembed?url={url}&format=json"
        with urllib.request.urlopen(oembed_url, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        return (
            data.get("title", "(unknown title)"),
            data.get("author_name", "(unknown channel)"),
            "",  # oEmbed doesn't return upload date
        )
    except Exception as e:
        logger.info(f"oEmbed fallback failed: {e}")
        return ("", "", "")


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
