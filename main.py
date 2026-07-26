"""Telegram Bot entry point.

Commands:
- /start          - greeting + usage
- /help           - usage details
- (any YouTube URL pasted) - runs the full pipeline on that single video:
    fetch captions -> extract stocks -> save -> aggregate (within window) -> synthesize -> reply
- /report <TICKER> - re-synthesize a single ticker from existing extractions
- /digest         - re-synthesize ALL tickers that have mentions in the window

Auth: only user IDs in TELEGRAM_ALLOWED_USER_IDS can use the bot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import traceback
import uuid
from pathlib import Path

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import settings
from extractor import extract_stocks_from_transcript
from youtube_client import fetch_video, extract_video_id
import storage
from aggregator import aggregate_for_ticker, aggregate_all_active_tickers
from evaluator import synthesize_ticker, synthesize_all

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
LOG_DIR = settings.project_root / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def _is_authorized(user_id: int) -> bool:
    if not settings.telegram_allowed_user_ids:
        return False
    return user_id in settings.telegram_allowed_user_ids


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
_SCORE_EMOJI = {1: "🔴", 2: "🔴", 3: "🟠", 4: "🟠", 5: "🟡", 6: "🟡", 7: "🟢", 8: "🟢", 9: "🟢", 10: "🟢"}
_CONF_EMOJI = {1: "❓", 2: "❓", 3: "⚠️", 4: "⚠️", 5: "➖", 6: "➖", 7: "✅", 8: "✅", 9: "✅", 10: "✅"}


def _fmt_report(report: dict) -> str:
    """Format a single report dict as Telegram markdown."""
    ticker = report.get("ticker", "?")
    score = report.get("overall_score")
    sentiment = report.get("overall_sentiment", "n/a")
    conf = report.get("confidence")
    summary = report.get("summary", "")
    price = report.get("latest_price")
    mention_count = report.get("mention_count", 0)

    score_str = f"{score}/10 {_SCORE_EMOJI.get(score, '')}" if score else "N/A"
    conf_str = f"{conf}/10 {_CONF_EMOJI.get(conf, '')}" if conf is not None else "N/A"
    price_str = f"${price:.2f}" if price else "N/A"

    lines = [
        f"*📊 {ticker}*  `{price_str}`",
        f"• 看好度: {score_str}",
        f"• 綜合情緒: {sentiment}",
        f"• 系統置信度: {conf_str}",
        f"• 樣本數: {mention_count} 個提及",
        "",
        f"{summary}",
    ]

    theses = report.get("key_thesis") or []
    if theses:
        lines.append("")
        lines.append("*關鍵論點:*")
        for t in theses[:5]:
            lines.append(f"  • {t}")

    risks = report.get("risks") or []
    if risks:
        lines.append("")
        lines.append("*風險:*")
        for r in risks[:3]:
            lines.append(f"  ⚠️ {r}")

    takeaway = report.get("actionable_takeaway")
    if takeaway:
        lines.append("")
        lines.append(f"*結論:* {takeaway}")

    lines.append("")
    lines.append("_⚠️ AI 生成，僅供參考，不構成投資建議_")
    return "\n".join(lines)


def _split_message(text: str, max_len: int = 3500) -> list[str]:
    """Split long messages into chunks respecting Telegram's 4096 limit."""
    if len(text) <= max_len:
        return [text]
    chunks = []
    while text:
        chunk = text[:max_len]
        # try to break at last double newline
        break_at = chunk.rfind("\n\n")
        if break_at > 1000:
            chunk = chunk[:break_at]
        chunks.append(chunk)
        text = text[len(chunk):].lstrip("\n")
    return chunks


# ---------------------------------------------------------------------------
# Pipeline (single video)
# ---------------------------------------------------------------------------
async def run_pipeline_for_video(url: str) -> dict:
    """Runs: fetch -> extract -> save. Returns summary dict.

    This function is CPU/IO-bound; should be called via asyncio.to_thread.
    """
    video_id = extract_video_id(url)
    if not video_id:
        return {"ok": False, "error": f"無法解析 YouTube URL: {url}"}

    info = fetch_video(url)
    storage.upsert_video(
        video_id=info.video_id,
        url=info.url,
        title=info.title,
        channel=info.channel,
        published=info.published,
    )
    storage.update_video_status(
        info.video_id,
        status="caption_ok",
        caption_source=info.caption_source,
        transcript_chars=len(info.transcript),
    )

    stocks = extract_stocks_from_transcript(
        info.title, info.channel, info.published, info.transcript
    )
    extraction_ids = [storage.save_extraction(info.video_id, s) for s in stocks]
    storage.update_video_status(info.video_id, status="extracted")

    return {
        "ok": True,
        "video_id": info.video_id,
        "title": info.title,
        "channel": info.channel,
        "caption_source": info.caption_source,
        "ticker_count": len(stocks),
        "tickers": sorted({s["ticker"] for s in stocks}),
        "extraction_count": len(extraction_ids),
    }


async def run_synthesis_for_tickers(tickers: list[str], run_id: str) -> list[dict]:
    """Aggregate + synthesize for given tickers. Returns list of reports."""
    reports = []
    for ticker in tickers:
        bundle = aggregate_for_ticker(ticker)
        if bundle["mention_count"] == 0:
            reports.append(
                {
                    "ticker": ticker,
                    "overall_sentiment": "no_data",
                    "confidence": 0,
                    "summary": f"聚合窗內 ({settings.aggregation_window_days} 天) 沒有 {ticker} 的提及紀錄。",
                    "mention_count": 0,
                }
            )
            continue
        report = synthesize_ticker(bundle)
        storage.save_report(ticker, run_id, report)
        reports.append(report)
    # Sort by confidence desc
    reports.sort(key=lambda r: -(r.get("confidence") or 0))
    return reports


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ 未授權使用。請聯繫管理員設定 TELEGRAM_ALLOWED_USER_IDS。")
        return
    await update.message.reply_text(
        "👋 歡迎使用 *美股 YouTube 觀點分析機器人*。\n\n"
        "*用法:*\n"
        "• 直接貼上 YouTube 影片連結，系統會抓字幕 → 提取股票論點 → 聚合近期同股票觀點 → 綜合評測\n"
        "• `/report TSLA` - 重新針對某 ticker 做綜合評測\n"
        "• `/digest` - 針對近七天所有提及的 ticker 全部做評測\n"
        "• `/help` - 完整說明\n\n"
        "_⚠️ AI 生成，僅供參考，不構成投資建議_",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "*指令說明*\n\n"
        "1. 直接貼 YouTube URL\n"
        "   → 抓字幕 → Gemini 提取 → 存 DB → 聚合該影片提及 ticker 在近 7 天的所有提及 → 綜合評測\n\n"
        "2. `/report TSLA`\n"
        "   → 只針對 TSLA 做綜合評測 (用既有 extractions)\n\n"
        "3. `/digest`\n"
        "   → 列出近 7 天所有有提及的 ticker，逐一做綜合評測\n\n"
        "_⚠️ AI 生成，僅供參考，不構成投資建議_",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("用法: `/report TSLA`", parse_mode=ParseMode.MARKDOWN)
        return
    ticker = context.args[0].upper()
    run_id = uuid.uuid4().hex[:8]
    await update.message.reply_text(f"🔍 重新評測 {ticker}...")

    try:
        reports = await asyncio.to_thread(
            lambda: asyncio.run(run_synthesis_for_tickers([ticker], run_id))
        )
    except Exception as e:
        logger.exception("cmd_report failed")
        await update.message.reply_text(f"❌ 失敗: {e}")
        return

    if not reports:
        await update.message.reply_text("沒有產出報告。")
        return
    for r in reports:
        for chunk in _split_message(_fmt_report(r)):
            try:
                await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                # retry without markdown if parse fails
                await update.message.reply_text(chunk)


async def cmd_digest(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update.effective_user.id):
        return
    run_id = uuid.uuid4().hex[:8]
    await update.message.reply_text("🧠 開始針對近 7 天所有提及的 ticker 做綜合評測，請稍候...")

    try:
        def _do():
            bundles = aggregate_all_active_tickers()
            if not bundles:
                return []
            reports = synthesize_all(bundles)
            for r in reports:
                storage.save_report(r.get("ticker", "?"), run_id, r)
            return reports

        reports = await asyncio.to_thread(_do)
    except Exception as e:
        logger.exception("cmd_digest failed")
        await update.message.reply_text(f"❌ 失敗: {e}")
        return

    if not reports:
        await update.message.reply_text("近 7 天沒有任何提及紀錄。先貼幾支影片進來吧！")
        return

    # Brief summary header
    header = f"📋 *本次評測共 {len(reports)} 檔 ticker*\n\n"
    table_lines = []
    for r in reports:
        ticker = r.get("ticker", "?")
        score = r.get("overall_score")
        conf = r.get("confidence")
        table_lines.append(
            f"  • {ticker}: 看好度 {score or '-'}/10, 置信度 {conf if conf is not None else '-'}/10"
        )
    await update.message.reply_text(
        header + "\n".join(table_lines) + "\n\n_依置信度高低排序_",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Send each full report
    for r in reports:
        for chunk in _split_message(_fmt_report(r)):
            try:
                await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await update.message.reply_text(chunk)
        await asyncio.sleep(0.2)  # avoid rate limit


_YT_URL_REGEX = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[A-Za-z0-9_\-\?=&]+"
)


async def handle_youtube_url(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update.effective_user.id):
        await update.message.reply_text("⛔ 未授權使用。")
        return

    text = update.message.text or ""
    match = _YT_URL_REGEX.search(text)
    if not match:
        await update.message.reply_text("⚠️ 看不到有效的 YouTube 連結。")
        return
    url = match.group(0)
    run_id = uuid.uuid4().hex[:8]

    status_msg = await update.message.reply_text(
        f"📥 收到連結，開始處理...\n`{url}`\n\n_步驟 1/3: 抓取字幕_",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Step 1: fetch + extract
    try:
        result = await asyncio.to_thread(lambda: asyncio.run(run_pipeline_for_video(url)))
    except Exception as e:
        logger.exception("pipeline failed")
        tb = traceback.format_exc()[-800:]
        await status_msg.edit_text(f"❌ 處理失敗:\n`{e}`\n\n```\n{tb}\n```", parse_mode=ParseMode.MARKDOWN)
        return

    if not result.get("ok"):
        await status_msg.edit_text(f"❌ {result.get('error', 'unknown error')}")
        return

    tickers = result.get("tickers", [])
    if not tickers:
        await status_msg.edit_text(
            f"✅ 字幕抓取成功 ({result['caption_source']})，但這支影片沒有提及任何明確的美股 ticker。\n\n"
            f"標題: {result['title']}"
        )
        return

    await status_msg.edit_text(
        f"✅ 抓取成功 ({result['caption_source']})\n"
        f"🎬 {result['title']}\n"
        f"📺 {result['channel']}\n"
        f"🏷️ 提及 {len(tickers)} 檔: {', '.join(tickers)}\n\n"
        f"_步驟 2/3: 聚合近 {settings.aggregation_window_days} 天觀點 + 抓股價_",
        parse_mode=ParseMode.MARKDOWN,
    )

    # Step 2 + 3: aggregate + synthesize for each ticker
    try:
        reports = await asyncio.to_thread(
            lambda: asyncio.run(run_synthesis_for_tickers(tickers, run_id))
        )
    except Exception as e:
        logger.exception("synthesis failed")
        await status_msg.edit_text(f"❌ 評測失敗: {e}")
        return

    # Step 3 done: send reports
    await status_msg.edit_text(
        f"✅ 評測完成 ({len(reports)} 檔報告產出，依置信度排序)"
    )

    for r in reports:
        for chunk in _split_message(_fmt_report(r)):
            try:
                await update.message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await update.message.reply_text(chunk)
        await asyncio.sleep(0.2)


async def handle_unknown(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "請貼上 YouTube 連結，或使用 /help 查看可用指令。"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    settings.ensure_dirs()

    missing = settings.validate()
    if missing:
        logger.error(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in values."
        )
        sys.exit(1)

    # Build app
    app = Application.builder().token(settings.telegram_bot_token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("report", cmd_report))
    app.add_handler(CommandHandler("digest", cmd_digest))

    # Catch YouTube URLs in plain messages
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex(_YT_URL_REGEX), handle_youtube_url))
    # Catch everything else text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown))

    logger.info("Bot starting up (POLLING mode — local)...")
    logger.info(f"Authorized user IDs: {settings.telegram_allowed_user_ids}")
    logger.info(f"DB path: {settings.db_path}")
    logger.info(f"Reports dir: {settings.reports_dir}")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
