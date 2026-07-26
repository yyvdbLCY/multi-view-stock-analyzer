"""YouTube caption / transcript fetcher using yt-dlp.

Strategy:
1. Try manual English captions first (highest quality).
2. Fallback to auto-generated English captions.
3. (Optional, gated by ENABLE_WHISPER_FALLBACK) Download audio + Whisper.

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
from yt_dlp import YoutubeDL

logger = logging.getLogger(__name__)


@dataclass
class VideoInfo:
    video_id: str
    url: str
    title: str
    channel: str
    published: str  # ISO date string
    transcript: str
    caption_source: str  # 'manual' | 'auto' | 'whisper' | 'failed'


_YT_URL_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})"
)


def extract_video_id(url: str) -> str | None:
    m = _YT_URL_RE.search(url)
    return m.group(1) if m else None


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
        # timestamp lines like "00:00:01.234 --> 00:00:03.456"
        if "-->" in line or re.match(r"^\d{2}:\d{2}", line):
            continue
        # strip <c> tags etc.
        clean = re.sub(r"<[^>]+>", "", line)
        clean = clean.replace("&amp;", "&").replace("&#39;", "'").replace("&nbsp;", " ")
        if clean and clean not in seen:
            seen.add(clean)
            lines.append(clean)
    return " ".join(lines)


def _fetch_subtitle_text(url: str) -> tuple[str | None, str]:
    """Returns (text, source). source is 'manual' | 'auto' | None."""
    # First probe: list available subtitles
    with YoutubeDL({"quiet": True, "skip_download": True, "writesubtitles": True}) as ydl:
        info = ydl.extract_info(url, download=False)

    subs = info.get("subtitles", {}) or {}
    auto_subs = info.get("automatic_captions", {}) or {}

    # Preferred language order
    lang_pref = ["en", "en-US", "en-GB"]

    def _pick(d: dict) -> str | None:
        for lang in lang_pref:
            if lang in d and d[lang]:
                # prefer vtt/srv3 (most reliable)
                for fmt_pref in ("vtt", "srv3", "srt", "json3"):
                    for entry in d[lang]:
                        if entry.get("ext") == fmt_pref:
                            return entry.get("url")
        return None

    manual_url = _pick(subs)
    auto_url = _pick(auto_subs) if not manual_url else None

    import urllib.request

    if manual_url:
        try:
            with urllib.request.urlopen(manual_url, timeout=30) as r:
                raw = r.read().decode("utf-8", errors="ignore")
            return _clean_vtt(raw), "manual"
        except Exception as e:
            logger.warning(f"manual caption fetch failed: {e}")

    if auto_url:
        try:
            with urllib.request.urlopen(auto_url, timeout=30) as r:
                raw = r.read().decode("utf-8", errors="ignore")
            return _clean_vtt(raw), "auto"
        except Exception as e:
            logger.warning(f"auto caption fetch failed: {e}")

    return None, "none"


def _whisper_fallback(url: str, video_id: str) -> str | None:
    """Download audio and transcribe with Whisper. Requires ffmpeg + whisper installed."""
    if not settings.enable_whisper_fallback:
        return None
    try:
        import whisper  # type: ignore
    except ImportError:
        logger.error("Whisper fallback enabled but `whisper` package not installed.")
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        audio_path = Path(tmpdir) / f"{video_id}.m4a"
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


def fetch_video(url: str) -> VideoInfo:
    """Main entry point. Fetches metadata + transcript. Raises on hard failure."""
    video_id = extract_video_id(url) or ""
    if not video_id:
        raise ValueError(f"Cannot parse YouTube video ID from URL: {url}")

    # Probe metadata
    with YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)

    title = info.get("title") or "(unknown title)"
    channel = info.get("uploader") or info.get("channel") or "(unknown channel)"
    # upload_date is YYYYMMDD
    upload_date = info.get("upload_date") or ""
    if len(upload_date) == 8:
        published = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    else:
        published = ""

    transcript, source = _fetch_subtitle_text(url)
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
    # Quick CLI test: python youtube_client.py <URL>
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
