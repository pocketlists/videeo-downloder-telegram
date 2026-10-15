import os
import re
import sys
import time
import shutil
import logging
import subprocess
from datetime import datetime

if os.environ.get("AUTO_UPDATE", "1") == "1":
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "-q", "yt-dlp[default,curl-cffi]"],
            capture_output=True, timeout=150
        )
    except Exception:
        pass

from pyrogram import Client, filters, enums, idle
from pyrogram.types import Message, CallbackQuery

from modules.post_maker import (
    SENT_BY_BOT, URL_REGEX, esc, is_bot_template, is_tg, is_filehost,
    extract_urls, media_of, is_video_media, remember_sent, safe,
    post_message, album_post
)
from modules.video_engine import (
    init_engine, spawn, rule_intent, status_text,
    download_message, chat_message, compress_message,
    handle_callback, janitor
)

API_ID = int(os.environ.get("API_ID", 0) or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED = {int(x) for x in re.findall(r"\d+", os.environ.get("ALLOWED_IDS", ""))}
STALE_SEC = int(os.environ.get("STALE_SEC", 25))

START_TS = 0.0
SEEN = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

try:
    app = Client(
        "video_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
        parse_mode=enums.ParseMode.HTML, max_concurrent_transmissions=4,
    )
except TypeError:
    app = Client(
        "video_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
        parse_mode=enums.ParseMode.HTML,
    )

START_TEXT = (
    "👋 <b>Seal Video Downloader + Post Maker + Smart Compressor</b>\n\n"
    "<b>1️⃣ Video Download (Seal Engine):</b> Kisi bhi website ka link bhejo. Quality buttons "
    "(1080p, 720p, 480p, 360p) + <b>✂️ Cut</b> aur <b>🎵 Audio</b> milenge.\n"
    "<b>2️⃣ Post Maker (1-to-1 No Spam):</b> Diskwala/Terabox ya Telegram links photo/video ke saath bhejo ya forward karo. "
    "Jitne messages bhejoge, exact utne hi template posts aayenge.\n"
    "<b>3️⃣ Auto Smart Compress:</b> Seedha koi bhi video bhejo — bot bina quality giraye (SSIM 0.985 Lock) "
    "size kam karke wapas bhej dega!"
)

HELP_TEXT = (
    "<b>📖 Kaise use karein</b>\n\n"
    "• <b>Video Download:</b> Link bhejo -> Quality button chuno (1080p/720p/480p/360p).\n"
    "• <b>Auto Video Compress:</b> Seedha video file bhejo -> Bot automatic Fixed Smart Compression se size kam kar dega.\n"
    "• <b>Post Maker:</b> Photo/Video ke saath Diskwala/Terabox link forward karo -> Exact utne hi template post banenge.\n"
    "• <b>Template Change:</b> <code>template 5</code> ya <code>template random</code> likho."
)


async def _allowed(_, __, update):
    # Layer 1: Ignore bot's own outgoing messages or any bot user
    if getattr(update, "outgoing", False):
        return False
    u = getattr(update, "from_user", None)
    if u and (getattr(u, "is_self", False) or getattr(u, "is_bot", False)):
        return False
    d = getattr(update, "date", None)
    if START_TS and isinstance(d, datetime) and d.timestamp() < START_TS - STALE_SEC:
        return False
    return not ALLOWED or bool(u and u.id in ALLOWED)

allowed = filters.create(_allowed)


def route_mode(text: str, urls: list, kind: str, isvid: bool = False) -> str:
    if kind:
        if urls:
            return "post"
        return "compress" if isvid else "nolink"
    if not urls:
        return "chat" if (text or "").strip() else "skip"
    rest = URL_REGEX.sub(" ", text or "").strip()
    if rule_intent(rest, True).action in ("cut", "audio", "download", "diag", "compress"):
        return "dl"
    if any(is_tg(u) or is_filehost(u) for u in urls):
        return "post"
    return "dl"


@app.on_message(filters.command("start") & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed)
async def start_cmd(client, message: Message):
    m = await safe(message.reply_text, START_TEXT, quote=True)
    if getattr(m, "id", None):
        remember_sent(message.chat.id, m.id)


@app.on_message(filters.command("help") & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed)
async def help_cmd(client, message: Message):
    m = await safe(message.reply_text, HELP_TEXT, quote=True)
    if getattr(m, "id", None):
        remember_sent(message.chat.id, m.id)


@app.on_message(filters.command("queue") & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed)
async def queue_cmd(client, message: Message):
    m = await safe(message.reply_text, status_text(), quote=True)
    if getattr(m, "id", None):
        remember_sent(message.chat.id, m.id)


@app.on_message(
    filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed
    & ~filters.command(["start", "help", "queue"])
)
async def router(client, message: Message):
    now = time.time()
    for k in [k for k, v in list(SEEN.items()) if now - v > 900]:
        SEEN.pop(k, None)

    key = (message.chat.id, message.id)
    # Layer 2 & 3: Skip already seen messages or messages sent by bot via HTTP API
    if key in SEEN or key in SENT_BY_BOT:
        return
    SEEN[key] = now

    text = message.text or message.caption or ""
    # Layer 4: Never re-process a message that already has the bot's template borders
    if is_bot_template(text):
        return

    try:
        if message.media_group_id:
            return await album_post(client, message)

        urls = extract_urls(message)
        kind, fid = media_of(message)
        mode = route_mode(text, urls, kind, is_video_media(message))
        logging.info(f"[router] chat={message.chat.id} msg={message.id} mode={mode} links={len(urls)} media={kind}")

        if mode == "post":
            await post_message(client, message, urls, kind, fid)
        elif mode == "dl":
            await download_message(client, message, text, urls)
        elif mode == "compress":
            await compress_message(client, message, text)
        elif mode == "nolink":
            m = await safe(message.reply_text, "🔗 Is message mein koi link nahi mila.", quote=True)
            if getattr(m, "id", None):
                remember_sent(message.chat.id, m.id)
        elif mode == "chat":
            await chat_message(client, message, text, START_TEXT, HELP_TEXT)
    except Exception as e:
        logging.exception("router failed")
        try:
            m = await message.reply_text(f"❌ <code>{esc(str(e)[:200])}</code>", quote=True)
            if getattr(m, "id", None):
                remember_sent(message.chat.id, m.id)
        except Exception:
            pass


@app.on_callback_query(allowed)
async def on_callback(client, cq: CallbackQuery):
    await handle_callback(client, cq)


async def main():
    global START_TS
    init_engine()
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        logging.error("❌ ffmpeg/ffprobe install nahi hai!")
    START_TS = time.time()
    await app.start()
    logging.info("🚀 Modular Bot Started (No-Loop Post Maker + Seal Engine + Auto Smart Compress)")
    spawn(janitor())
    try:
        await idle()
    finally:
        await app.stop()


if __name__ == "__main__":
    app.run(main())
