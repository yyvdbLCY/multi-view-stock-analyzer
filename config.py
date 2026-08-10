"""Centralized configuration. Reads from environment variables via python-dotenv."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(_PROJECT_ROOT / ".env")


def _get_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, "").strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "y", "on"}


def _get_int_list(key: str) -> list[int]:
    raw = os.getenv(key, "").strip()
    if not raw:
        return []
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.append(int(chunk))
    return out


class Settings:
    # Telegram
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_allowed_user_ids: list[int] = _get_int_list("TELEGRAM_ALLOWED_USER_IDS")

    # Gemini
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "").strip()
    gemini_model_extract: str = os.getenv("GEMINI_MODEL_EXTRACT", "gemini-2.0-flash").strip()
    gemini_model_eval: str = os.getenv("GEMINI_MODEL_EVAL", "gemini-2.0-flash").strip()

    # LLM provider switch: "gemini" (default) or "openai" (MiniMax / any OpenAI-compat)
    llm_provider: str = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    # OpenAI-compatible settings (used when LLM_PROVIDER=openai)
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.minimax.io/v1").strip()
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "").strip()
    openai_model: str = os.getenv("OPENAI_MODEL", "MiniMax-M3").strip()

    # Storage
    db_path: Path = _PROJECT_ROOT / os.getenv("DB_PATH", "db/analyzer.db")
    reports_dir: Path = _PROJECT_ROOT / os.getenv("REPORTS_DIR", "reports")

    # Aggregation
    aggregation_window_days: int = int(os.getenv("AGGREGATION_WINDOW_DAYS", "7"))

    # Whisper fallback
    whisper_model: str = os.getenv("WHISPER_MODEL", "base").strip()
    enable_whisper_fallback: bool = _get_bool("ENABLE_WHISPER_FALLBACK", False)

    project_root: Path = _PROJECT_ROOT

    @classmethod
    def validate(cls) -> list[str]:
        """Return list of missing required settings (empty list = OK)."""
        missing: list[str] = []
        if not cls.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not cls.telegram_allowed_user_ids:
            missing.append("TELEGRAM_ALLOWED_USER_IDS")
        if cls.llm_provider == "openai":
            if not cls.openai_api_key:
                missing.append("OPENAI_API_KEY")
            if not cls.openai_base_url:
                missing.append("OPENAI_BASE_URL")
            if not cls.openai_model:
                missing.append("OPENAI_MODEL")
        else:
            if not cls.gemini_api_key:
                missing.append("GEMINI_API_KEY")
        return missing

    @classmethod
    def ensure_dirs(cls) -> None:
        cls.db_path.parent.mkdir(parents=True, exist_ok=True)
        cls.reports_dir.mkdir(parents=True, exist_ok=True)
        (_PROJECT_ROOT / "logs").mkdir(parents=True, exist_ok=True)


settings = Settings()
