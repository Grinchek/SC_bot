import asyncio
import os
import re
import tempfile
import shutil
import logging
import signal
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import Update, ChatMember
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut, NetworkError
from yt_dlp import YoutubeDL

# -------------------- Config & logging --------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@your_channel")  # можна @username або -100...
SOUNDCLOUD_RE = re.compile(r"https?://(?:www\.)?soundcloud\.com/[^\s]+", re.IGNORECASE)

MAX_CONCURRENCY = int(os.getenv("MAX_CONCURRENCY", "2"))
USER_COOLDOWN_SEC = float(os.getenv("USER_COOLDOWN_SEC", "20"))
DOWNLOAD_TIMEOUT_SEC = float(os.getenv("DOWNLOAD_TIMEOUT_SEC", "180"))
MAX_FILE_MB = float(os.getenv("MAX_FILE_MB", "45"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s"
)
log = logging.getLogger("sc-bot")

# Ліміт паралельних завантажень + антиспам по користувачу
sema = asyncio.Semaphore(MAX_CONCURRENCY)
last_request_ts: dict[int, float] = {}

# -------------------- Utils --------------------
async def safe_send(func, *args, **kwargs):
    """Надсилання з ретраями та обробкою лімітів Telegram."""
    delay = 1.0
    for attempt in range(4):
        try:
            return await func(*args, **kwargs)
        except RetryAfter as e:
            log.warning("RetryAfter: sleeping for %.2fs", e.retry_after)
            await asyncio.sleep(float(e.retry_after) + 0.5)
        except (TimedOut, NetworkError) as e:
            log.warning("Network/Timeout on send (attempt %d): %s", attempt + 1, e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8)
    raise RuntimeError("Send failed after retries")

def _valid_required_channel(value: str) -> bool:
    return value.startswith("@") or value.startswith("-100")

# -------------------- Subscription check --------------------
async def is_subscribed(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """
    True, якщо користувач підписаний на REQUIRED_CHANNEL.
    Для приватних каналів використовуйте numeric id -100xxxxxxxxxxxx.
    Бот має бути адміном каналу.
    """
    try:
        member = await context.bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        status = getattr(member, "status", None)
        return status not in ("left", "kicked")
    except BadRequest as e:
        log.warning("[is_subscribed] BadRequest: %s", e.message)
        return False
    except Forbidden as e:
        log.warning("[is_subscribed] Forbidden: %s", e.message)
        return False
    except Exception as e:
        log.exception("[is_subscribed] Unexpected error")
        return False

# -------------------- Handlers --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_send(
        update.message.reply_text,
        "Надішли посилання на трек SoundCloud.\n"
        f"Щоб отримати файл — підпишись на канал {REQUIRED_CHANNEL}."
    )

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ok = await is_subscribed(context, user.id)
    await safe_send(
        update.message.reply_text,
        f"Підписка на {REQUIRED_CHANNEL}: {'✅ так' if ok else '❌ ні'}\n"
        "Якщо ❌ — перевір, що бот адмін каналу та значення REQUIRED_CHANNEL коректне."
    )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    m = SOUNDCLOUD_RE.search(update.message.text)
    if not m:
        return

    user = update.effective_user
    if not user:
        return

    # Пер-користувацький cooldown (антиспам)
    now = asyncio.get_event_loop().time()
    prev = last_request_ts.get(user.id, 0.0)
    if prev + USER_COOLDOWN_SEC > now:
        await safe_send(update.message.reply_text, "Занадто часто. Спробуй трохи пізніше 🙏")
        return
    last_request_ts[user.id] = now

    # Перевірка підписки
    if not await is_subscribed(context, user.id):
        await safe_send(
            update.message.reply_text,
            f"Спершу підпишись на канал {REQUIRED_CHANNEL}, а потім повтори запит 🙌"
        )
        return

    url = m.group(0)
    await safe_send(update.message.reply_text, "⏳ Обробляю посилання…")

    tmpdir = Path(tempfile.mkdtemp(prefix="scdl_"))
    try:
        ydl_opts = {
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

        info = None
        audio_file: Optional[Path] = None

        def _download():
            nonlocal info, audio_file
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                for p in tmpdir.glob("*.mp3"):
                    audio_file = p
                    break

        # Обмежуємо паралельність важких задач
        async with sema:
            # Таймаут на завантаження/конвертацію
            loop = asyncio.get_event_loop()
            await asyncio.wait_for(loop.run_in_executor(None, _download), timeout=DOWNLOAD_TIMEOUT_SEC)

        if not info:
            await safe_send(update.message.reply_text, "Не вдалося отримати інформацію про трек.")
            return

        title = info.get("title") or "SoundCloud Track"
        uploader = info.get("uploader") or info.get("creator") or ""

        if audio_file and audio_file.exists():
            size_mb = audio_file.stat().st_size / (1024 * 1024)
            if size_mb > MAX_FILE_MB:
                await safe_send(
                    update.message.reply_text,
                    f"Файл завеликий для відправки (>{int(MAX_FILE_MB)} МБ). Ось посилання:\n{url}"
                )
                return

            with audio_file.open("rb") as f:
                await safe_send(
                    update.message.reply_audio,
                    audio=f,
                    title=title,
                    performer=uploader,
                    caption=f"Завантажено з: {url}"
                )
        else:
            await safe_send(
                update.message.reply_text,
                f"Не вдалось сформувати MP3 для цього треку.\n{url}"
            )

    except asyncio.TimeoutError:
        log.warning("Download timed out for %s", url)
        await safe_send(update.message.reply_text, "⏳ Перевищено час очікування завантаження. Спробуй пізніше.")
    except Exception:
        log.exception("Process error")
        await safe_send(update.message.reply_text, "Виникла помилка 😕 Спробуй інший лінк пізніше.")
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            log.warning("Failed to cleanup tmpdir %s", tmpdir)

# -------------------- App bootstrap --------------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не заданий у .env")
    if not _valid_required_channel(REQUIRED_CHANNEL):
        raise RuntimeError("REQUIRED_CHANNEL має бути '@username' або numeric '-100...'")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot is running. Press Ctrl+C to stop.")
    # run_polling вже коректно обробляє Ctrl+C; дод. хендлери сигналів не обов'язкові на Windows
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
