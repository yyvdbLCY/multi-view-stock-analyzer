"""Vercel serverless entry point for multi-view-stock-analyzer.

Wraps python-telegram-bot's update processing inside a FastAPI app so it
can be hosted on Vercel's serverless platform (24/7 webhook, no local
machine needed).

Endpoints:
    GET  /              - health check
    POST /api/webhook   - Telegram update webhook

Env vars (set in Vercel dashboard or via API):
    TELEGRAM_BOT_TOKEN       (required)
    TELEGRAM_ALLOWED_USER_IDS (required, comma-separated)
    GEMINI_API_KEY           (required)
    GEMINI_MODEL_EXTRACT     (optional, default: gemini-2.0-flash)
    GEMINI_MODEL_EVAL        (optional, default: gemini-2.0-flash)
    WEBHOOK_SECRET           (optional, if set Telegram must send matching
                              X-Telegram-Bot-Api-Secret-Token header)
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

# Add project root to sys.path so `import config` works on Vercel
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from telegram import Update  # noqa: E402
from telegram.constants import ParseMode  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

import config  # noqa: E402
from main import (  # noqa: E402
    cmd_digest,
    cmd_help,
    cmd_report,
    cmd_start,
    handle_unknown,
    handle_youtube_url,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("multi-view-vercel")

# ---------------------------------------------------------------------------
# Validate config early so we fail loud on bad env
# ---------------------------------------------------------------------------
missing = config.settings.validate()
if missing:
    logger.error(f"Missing required env vars: {', '.join(missing)}")
    # Don't crash here — let the health endpoint show the error. Vercel will
    # log it and the user can fix env in the dashboard.

config.settings.ensure_dirs()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()

# ---------------------------------------------------------------------------
# Build the bot application ONCE at module load (Vercel reuses warm instances)
# ---------------------------------------------------------------------------
_YT_URL_REGEX = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[A-Za-z0-9_\-\?=&]+"
)

bot_app: Application = Application.builder().token(config.settings.telegram_bot_token).build()
bot_app.add_handler(CommandHandler("start", cmd_start))
bot_app.add_handler(CommandHandler("help", cmd_help))
bot_app.add_handler(CommandHandler("report", cmd_report))
bot_app.add_handler(CommandHandler("digest", cmd_digest))
bot_app.add_handler(MessageHandler(filters.TEXT & filters.Regex(_YT_URL_REGEX), handle_youtube_url))
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))


async def _init_bot() -> None:
    """Initialize the bot application once per cold start."""
    if not bot_app._initialized:  # type: ignore[attr-defined]
        await bot_app.initialize()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="multi-view-stock-analyzer", version="1.0.0")


@app.on_event("startup")
async def _startup() -> None:
    await _init_bot()
    logger.info(
        f"Bot ready. Authorized user IDs: {config.settings.telegram_allowed_user_ids}"
    )


@app.get("/")
async def root() -> dict:
    return {
        "status": "ok",
        "bot": "multi-view-stock-analyzer",
        "missing_env": missing,
        "authorized_users": config.settings.telegram_allowed_user_ids,
        "model_extract": config.settings.gemini_model_extract,
        "model_eval": config.settings.gemini_model_eval,
    }


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "ts": asyncio.get_event_loop().time()}


@app.get("/api/set_webhook")
async def set_webhook_endpoint(request: Request) -> dict:
    """One-shot helper to register this deployment as the bot's webhook.

    Hits Telegram's setWebhook API pointing at this deployment's
    /api/webhook URL. Vercel can reach api.telegram.org, so we use the
    bot token stored in env vars to register ourselves.

    Usage (one-time, after deploy):
        curl https://<your-deployment>.vercel.app/api/set_webhook

    For production: hit the aliased URL (multi-view-stock-analyzer.vercel.app).
    """
    if not config.settings.telegram_bot_token:
        raise HTTPException(status_code=500, detail="TELEGRAM_BOT_TOKEN not set")

    # Derive public URL: prefer x-forwarded-proto/host headers, fallback to env
    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("host") or request.headers.get("x-vercel-deployment-url", "")
    public_url = f"{proto}://{host}/api/webhook"

    # Call Telegram's setWebhook via raw HTTPS (avoid extra deps)
    import json as _json
    import urllib.request
    import urllib.parse

    telegram_url = f"https://api.telegram.org/bot{config.settings.telegram_bot_token}/setWebhook"
    data = urllib.parse.urlencode({"url": public_url}).encode()
    req = urllib.request.Request(telegram_url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read().decode("utf-8")
        telegram_resp = _json.loads(body)

    # Also fetch the webhook info for confirmation
    info_url = f"https://api.telegram.org/bot{config.settings.telegram_bot_token}/getWebhookInfo"
    with urllib.request.urlopen(info_url, timeout=15) as r:
        info_body = r.read().decode("utf-8")
        info = _json.loads(info_body)

    return {
        "ok": telegram_resp.get("ok", False),
        "webhook_url": public_url,
        "telegram_response": telegram_resp,
        "webhook_info": info.get("result", {}),
    }


@app.post("/api/webhook")
async def webhook(request: Request) -> dict:
    """Receive a Telegram update and dispatch it to the bot's handlers."""
    # Optional webhook secret
    if WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if secret != WEBHOOK_SECRET:
            logger.warning("Webhook secret mismatch (or missing).")
            raise HTTPException(status_code=403, detail="Forbidden")

    try:
        data = await request.json()
    except Exception as e:
        logger.error(f"Bad JSON from Telegram: {e}")
        raise HTTPException(status_code=400, detail="Bad JSON")

    update = Update.de_json(data, bot_app.bot)
    if update is None:
        raise HTTPException(status_code=400, detail="Invalid Update object")

    try:
        await bot_app.process_update(update)
    except Exception as e:
        logger.exception(f"process_update failed: {e}")
        # Return 200 so Telegram doesn't retry-loop on internal errors
        return {"ok": False, "error": str(e)}

    return {"ok": True}


# ---------------------------------------------------------------------------
# Vercel Python entrypoint shim
# ---------------------------------------------------------------------------
# Vercel looks for either `app` (ASGI) or `handler` (Mangum-style). FastAPI is
# ASGI; the @vercel/python builder auto-detects `app` if it is a FastAPI
# instance. We expose it under both names to be safe across build modes.
handler = app  # legacy alias
