
import asyncio
import os
import tempfile
import shutil
import logging
from pathlib import Path
from typing import Optional, Tuple

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.error import RetryAfter, TimedOut, NetworkError
from yt_dlp import YoutubeDL

# ==================== Config & logging ====================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@your_channel")  # '@username' або '-100...'

MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "2"))
USER_COOLDOWN_SEC = float(os.getenv("USER_COOLDOWN_SEC", "20"))
DOWNLOAD_TIMEOUT_SEC = float(os.getenv("DOWNLOAD_TIMEOUT_SEC", "180"))
MAX_FILE_MB = float(os.getenv("MAX_FILE_MB", "45"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s"
)
log = logging.getLogger("music-bot")

sema = asyncio.Semaphore(MAX_CONCURRENCY)
last_request_ts: dict[int, float] = {}

# ==================== Helpers ====================
async def safe_send(func, *args, **kwargs):
    """Send with simple retries to handle Telegram timeouts / rate limits."""
    delay = 1.0
    for _ in range(4):
        try:
            return await func(*args, **kwargs)
        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 0.5)
        except (TimedOut, NetworkError):
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8)
    raise RuntimeError("Send failed after retries")

def _valid_required_channel(value: str) -> bool:
    return value.startswith("@") or value.startswith("-100")

def _common_ydl_opts(tmpdir: Path) -> dict:
    return {
        "outtmpl": str(tmpdir / "%(title)s.%(ext)s"),
        "restrictfilenames": True,
        "format": "bestaudio/best",
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
        ],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "writeinfojson": True,
        "source_address": "0.0.0.0",
    }

def _pick_first_mp3(tmpdir: Path) -> Optional[Path]:
    for p in tmpdir.glob("*.mp3"):
        return p
    return None

def _safe_title(info: dict, fallback: str = "Track") -> str:
    return info.get("title") or fallback

def _safe_artist(info: dict) -> str:
    return info.get("artist") or info.get("uploader") or info.get("creator") or ""

def _download_youtube_search(query: str, tmpdir: Path) -> Tuple[Optional[Path], Optional[dict]]:
    """
    Виконує пошук першого збігу на YouTube і завантажує аудіо як MP3.
    """
    info = None
    audio_file: Optional[Path] = None

    def _run():
        nonlocal info, audio_file
        ydl_opts = _common_ydl_opts(tmpdir)
        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query} audio", download=True)
            if info and "entries" in info and info["entries"]:
                info = info["entries"][0]
            audio_file = _pick_first_mp3(tmpdir)

    _run()
    return audio_file, info

# ==================== Handlers ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Привіт! 👋\n"
        "Надішли *назву пісні або виконавця* — я пришлю MP3 (пошук через YouTube).\n"
        f"Щоб отримувати файли, підпишись на канал {REQUIRED_CHANNEL}.\n\n"
        "Приклади:\n"
        "• Imagine Dragons Believer\n"
        "• Arctic Monkeys Do I Wanna Know"
    )
    await safe_send(update.message.reply_text, msg, disable_web_page_preview=True)

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user.id)
        status = getattr(member, "status", None)
        ok = status not in ("left", "kicked")
    except Exception:
        ok = False
    await safe_send(
        update.message.reply_text,
        f"Підписка на {REQUIRED_CHANNEL}: {'✅ так' if ok else '❌ ні'}"
    )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    user = update.effective_user
    if not user:
        return

    # Лише пошук по назві — блокуємо посилання
    if "http://" in text.lower() or "https://" in text.lower():
        await safe_send(update.message.reply_text, "Надішли *назву треку без посилань*, будь ласка 🙏")
        return

    # Пер-користувацький cooldown
    now = asyncio.get_event_loop().time()
    prev = last_request_ts.get(user.id, 0.0)
    if prev + USER_COOLDOWN_SEC > now:
        await safe_send(update.message.reply_text, "Занадто часто. Спробуй трохи пізніше 🙏")
        return
    last_request_ts[user.id] = now

    # Перевірка підписки
    try:
        member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user.id)
        status = getattr(member, "status", None)
        if status in ("left", "kicked"):
            await safe_send(
                update.message.reply_text,
                f"Спершу підпишись на канал {REQUIRED_CHANNEL}, а потім повтори запит 🙌"
            )
            return
    except Exception:
        await safe_send(
            update.message.reply_text,
            f"Перевір налаштування каналу {REQUIRED_CHANNEL} або дозволь мені бачити підписників."
        )
        return

    query = text
    await safe_send(update.message.reply_text, f"🔎 Шукаю: “{query}”…")
    # У PTB v21 немає ChatAction.UPLOAD_AUDIO → використовуємо UPLOAD_DOCUMENT або TYPING
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    tmpdir = Path(tempfile.mkdtemp(prefix="music_"))
    try:
        audio_file: Optional[Path] = None
        info: Optional[dict] = None

        async with sema:
            loop = asyncio.get_event_loop()
            audio_file, info = await asyncio.wait_for(
                loop.run_in_executor(None, _download_youtube_search, query, tmpdir),
                timeout=DOWNLOAD_TIMEOUT_SEC
            )

        if not audio_file or not audio_file.exists():
            await safe_send(
                update.message.reply_text,
                "Не вдалось знайти/завантажити трек. Спробуй точнішу назву."
            )
            return

        size_mb = audio_file.stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILE_MB:
            await safe_send(
                update.message.reply_text,
                f"Файл завеликий для відправки (>{int(MAX_FILE_MB)} МБ)."
            )
            return

        title = _safe_title(info, fallback=query)
        performer = _safe_artist(info)

        bot_name = (await context.bot.get_me()).username
        caption = f"Завантажено з: YouTube\nЗ допомогою @{bot_name}"

        with audio_file.open("rb") as f:
            await safe_send(
                update.message.reply_audio,
                audio=f,
                title=title,
                performer=performer,
                caption=caption
            )

    except asyncio.TimeoutError:
        await safe_send(update.message.reply_text, "⏳ Перевищено час очікування завантаження. Спробуй пізніше.")
    except Exception:
        log.exception("Process error")
        await safe_send(update.message.reply_text, "Виникла помилка 😕 Спробуй інший запит трохи згодом.")
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            log.warning("Failed to cleanup %s", tmpdir)

# ==================== Error handler ====================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    import traceback
    err = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    logging.error("Unhandled error:\n%s", err)

# ==================== App bootstrap ====================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не заданий у .env")
    if not _valid_required_channel(REQUIRED_CHANNEL):
        raise RuntimeError("REQUIRED_CHANNEL має бути '@username' або numeric '-100...'")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
