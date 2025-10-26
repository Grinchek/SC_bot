
import asyncio
import os
import tempfile
import shutil
import logging
import re
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import urllib.request

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

# ==================== URL helpers ====================
SOUNDCLOUD_RE = re.compile(
    r"https?://(?:www\.)?(?:soundcloud\.com|on\.soundcloud\.com|snd\.sc)/[^\s]+",
    re.IGNORECASE
)

def _clean_sc_url(url: str) -> str:
    """Прибрати UTM з soundcloud.com-посилань."""
    scheme, netloc, path, query, frag = urlsplit(url)
    if "soundcloud.com" in netloc:
        q = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if not k.lower().startswith("utm_")]
        query = urlencode(q)
    return urlunsplit((scheme, netloc, path, query, frag))

def _resolve_short_sync(url: str) -> str:
    """Розгорнути короткі on.soundcloud.com/snd.sc редиректи."""
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.geturl()
    except Exception:
        return url

# ==================== Telegram helpers ====================
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

# ==================== yt-dlp helpers ====================
def _common_ydl_opts(tmpdir: Path) -> dict:
    # Загальні опції; формат вкажемо при завантаженні
    return {
        "outtmpl": str(tmpdir / "%(title)s.%(ext)s"),
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "prefer_ffmpeg": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
        ],
        "writeinfojson": True,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
    }

def _pick_first_mp3(tmpdir: Path) -> Optional[Path]:
    for p in tmpdir.glob("*.mp3"):
        return p
    return None

def _safe_title(info: dict, fallback: str = "Track") -> str:
    return (info or {}).get("title") or fallback

def _safe_artist(info: dict) -> str:
    return (info or {}).get("artist") or (info or {}).get("uploader") or (info or {}).get("creator") or ""

def _download_soundcloud_search(query: str, tmpdir: Path) -> tuple[Optional[Path], Optional[dict]]:
    """
    Пошук першого релевантного треку на SoundCloud:
    1) робимо scsearch1 без завантаження,
    2) беремо нормальний webpage_url/permalink_url,
    3) качаємо вже за цією URL.
    """
    info = None
    audio_file: Optional[Path] = None

    def _run():
        nonlocal info, audio_file
        # Крок 1: пошук без завантаження
        probe_opts = _common_ydl_opts(tmpdir)
        with YoutubeDL(probe_opts) as ydl:
            res = ydl.extract_info(f"scsearch1:{query}", download=False)
            if not res or "entries" not in res or not res["entries"]:
                return
            info = res["entries"][0] or {}
            # Крок 2: дістаємо нормальну сторінкову URL
            page_url = (
                info.get("webpage_url")
                or info.get("permalink_url")
                or info.get("url")  # інколи вже правильна
            )
        if not page_url:
            return

        # Розгорнемо короткі on.soundcloud.com, якщо трапиться
        if "on.soundcloud.com" in page_url or "snd.sc" in page_url:
            page_url = _resolve_short_sync(page_url)
        page_url = _clean_sc_url(page_url)

        # Крок 3: реальне завантаження по нормальній URL
        dl_opts = _common_ydl_opts(tmpdir)
        dl_opts["format"] = "bestaudio/best"
        with YoutubeDL(dl_opts) as ydl2:
            info2 = ydl2.extract_info(page_url, download=True)
            if info2:
                info.update(info2 if isinstance(info2, dict) else {})
        audio_file = _pick_first_mp3(tmpdir)

    _run()
    return audio_file, info

# ==================== Handlers ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Привіт! 👋\n"
        "• Надішли *посилання на SoundCloud* АБО *назву пісні/виконавця* — я пришлю MP3.\n"
        f"• Щоб отримувати файли, підпишись на канал {REQUIRED_CHANNEL}.\n\n"
        "Приклади:\n"
        "1) https://soundcloud.com/artist/track\n"
        "2) Monolink Return to Oz"
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

    # Пер-користувацький ###### cooldown
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

    url_match = SOUNDCLOUD_RE.search(text)
    if url_match:
        url = url_match.group(0)
        # Розгортання коротких лінків та чистка UTM
        if "on.soundcloud.com" in url or "snd.sc" in url:
            loop = asyncio.get_event_loop()
            url = await loop.run_in_executor(None, _resolve_short_sync, url)
        url = _clean_sc_url(url)
        source_label = url
        action_message = "⏳ Обробляю посилання SoundCloud…"
        downloader = lambda tmp: _download_soundcloud_url(url, tmp)
    else:
        query = text
        source_label = "SoundCloud (пошук)"
        action_message = f"🔎 Шукаю на SoundCloud: “{query}”…"
        downloader = lambda tmp: _download_soundcloud_search(query, tmp)

    await safe_send(update.message.reply_text, action_message)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_DOCUMENT)

    tmpdir = Path(tempfile.mkdtemp(prefix="sc_"))
    try:
        audio_file: Optional[Path] = None
        info: Optional[dict] = None

        async with sema:
            loop = asyncio.get_event_loop()
            audio_file, info = await asyncio.wait_for(
                loop.run_in_executor(None, downloader, tmpdir),
                timeout=DOWNLOAD_TIMEOUT_SEC
            )

        if not audio_file or not audio_file.exists():
            await safe_send(
                update.message.reply_text,
                "Не вдалось знайти/завантажити трек на SoundCloud. Спробуй іншу назву або лінк."
            )
            return

        size_mb = audio_file.stat().st_size / (1024 * 1024)
        if size_mb > MAX_FILE_MB:
            await safe_send(
                update.message.reply_text,
                f"Файл завеликий для відправки (>{int(MAX_FILE_MB)} МБ). Джерело: {source_label}"
            )
            return

        title = _safe_title(info, fallback=text if not url_match else "Track")
        performer = _safe_artist(info)

        bot_name = (await context.bot.get_me()).username
        caption = f"Завантажено \nЗ допомогою @{bot_name}"

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
