import os
import re
import sys
import time
import json
import random
import shutil
import asyncio
import logging
import subprocess
from collections import deque
from datetime import datetime
from difflib import get_close_matches

if os.environ.get("AUTO_UPDATE", "1") == "1":
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "-q", "yt-dlp[default,curl-cffi]"],
            capture_output=True, timeout=150
        )
    except Exception:
        pass

from pyrogram import Client, filters, enums, idle
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
)

import modules.video_engine as ve
import modules.censor_engine as cen
from modules.post_maker import (
    SENT_BY_BOT, POST_GAP, esc, is_bot_template, is_post_only_links, extract_urls, media_of,
    is_video_media, remember_sent, safe, post_message, album_post, template_reply,
)
from modules.video_engine import (
    init_engine, spawn, rule_intent, status_text,
    download_message, chat_message, compress_message, handle_callback, janitor,
)

API_ID = int(os.environ.get("API_ID", 0) or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED = {int(x) for x in re.findall(r"\d+", os.environ.get("ALLOWED_IDS", ""))}
STALE_SEC = int(os.environ.get("STALE_SEC", 25))
TEXT_WINDOW = float(os.environ.get("TEXT_WINDOW", 1.8))

START_TS = 0.0
SEEN = {}
CHAT_Q = {}
CHAT_WORKERS = {}
TEXT_BUF = {}
TEXT_TASKS = {}
HISTORY = {}
LAST_LOCAL = {}

COMMANDS = ["start", "help", "queue", "download", "compress", "censor",
            "post", "cut", "template", "status", "ai", "cancel", "stop", "highlight", "preview"]

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
    "👋 <b>Seal Video Downloader + Post Maker + Smart Compressor + Censor</b>\n\n"
    "<b>1️⃣ Video Download:</b> Kisi bhi website ka link bhejo. Quality buttons + ✂️ Cut + 🎵 Audio.\n"
    "<b>2️⃣ Post Maker:</b> Media + links bhejo (ya sirf Diskwala/Terabox/Telegram links) — har message ka apna alag post.\n"
    "<b>3️⃣ Auto Compress:</b> Video file bhejo — SSIM 0.985 lock ke saath size kam.\n"
    "<b>4️⃣ Censor:</b> Anime 2D/3D + real video/image par tight meme-sticker/emoji censor (bada blur box nahi) — sirf jab tum dabao ya <code>/censor</code> likho.\n"
    "<b>5️⃣ Smart:</b> 🔥 Highlight = sabse zyada dekha jaane wala hissa, 🎬 Preview = hot part se 10s sticker-censored clip, 🖼 HD Thumbnail alag se download.\n\n"
    "🛑 Kuch galat chal raha ho to <code>/cancel</code> (ya sirf <code>cancel</code> likho) — sab kuch ruk jayega.\n"
    "🗣 Baat AI se bhi kar sakte ho, ya <code>/</code> commands: /download /compress /censor /post /cut /cancel"
)

HELP_TEXT = (
    "<b>📖 Kaise use karein</b>\n\n"
    "• <b>Link bhejo</b> → quality button chuno.\n"
    "• <b>Video file bhejo</b> → compress. Caption mein <code>censor</code> likho to censor.\n"
    "• <b>Image/Video + link</b> → post maker (har message ka alag post).\n"
    "• <b>Sirf Diskwala/Terabox links</b> → har link ka alag post.\n"
    "• <b>Censor</b>: video/image par <code>/censor</code> reply karo. Default style <code>sticker</code> (meme emoji, tight box). "
    "Baaki: <code>/censor mosaic</code>, <code>blur</code>, <code>black</code>, <code>blend</code>. Video max "
    f"<code>{int(cen.CENSOR_MAX_SEC)}s</code>.\n"
    "• <b>Roko:</b> <code>/cancel</code> ya <code>/stop</code> — chal rahe kaam + queue dono band.\n"
    "• <b>Commands:</b> /download /compress /censor /post /cut /template /status /cancel"
)

AI_ACTIONS = ("download", "compress", "censor", "cut", "audio", "post",
              "template", "status", "help", "start", "cancel", "chat", "none")

AI_BRAIN = (
    "You are the control brain and friendly chat partner of a Telegram video bot. The owner writes in "
    "Hinglish, Hindi or English, often with spelling mistakes — understand the intent anyway. "
    "NEVER describe, mention or discuss adult/explicit content; just handle the command silently. "
    "If the owner wants everything stopped (ruk, bas, stop, cancel, band karo) use action cancel. "
    "For normal conversation use action chat and put a helpful, natural answer in reply. "
    "Reply with ONLY a JSON object (no markdown):\n"
    '{"action": one of ' + str(list(AI_ACTIONS)) + ', "quality": integer video height or 0, '
    '"start": "HH:MM:SS" or null, "end": "HH:MM:SS" or null, '
    '"mode": "exact" or "fast" or null, "arg": string or null, '
    '"reply": "one or two short natural Hinglish sentences (vary wording every time)"}'
)

FUZZY = {
    "download": ["download", "downlod", "dwnld", "dload", "bhej", "bhejo", "laao", "lelo", "get", "link"],
    "compress": ["compress", "compres", "cmprs", "shrink", "chhota", "small", "size"],
    "censor": ["censor", "censr", "sensor", "blur", "blurr", "hide", "dhak", "chhupa", "mosaic"],
    "cut": ["cut", "kat", "kaat", "kata", "trim", "clip"],
    "audio": ["audio", "mp3", "m4a", "gana", "gaana", "song", "music", "sound"],
    "post": ["post", "tamplet", "tempalte"],
    "status": ["status", "queue", "kaam"],
    "help": ["help", "madad", "kaise", "how"],
    "start": ["start", "shuru", "hi", "hello", "hey", "namaste"],
}

CANCEL_RE = re.compile(
    r"^\s*/?(?:cancel|stop|ruk+o*|ruk\s*ja+o|rok+o*|rok\s*do|band|band\s*kar(?:o|\s*do)|bas|bas\s*kar(?:o|\s*do)|"
    r"cancel\s*kar(?:o|\s*do)|sab\s*(?:cancel|band|stop)(?:\s*kar(?:o|\s*do))?)\s*[.!]*\s*$",
    re.I,
)

GREET = [
    "Hey! 👋 Link, video ya image bhejo — baaki main sambhal lunga.",
    "Namaste! Kya download ya process karna hai aaj?",
    "Hello ji 😊 Link bhejo ya video file do, kaam shuru karte hain.",
    "Hii! Bolo, kis cheez mein madad chahiye?",
]
THANKS = ["Koi baat nahi 😊", "Anytime! Aur kuch chahiye to bolo.", "Welcome ji 🙌", "Mazaa aaya madad karke 👍"]
HOWRU = [
    "Bilkul mast! Tum batao, aaj kya karna hai?",
    "Ekdum fit 😄 Link ya video bhejo, shuru karte hain.",
    "Badhiya hoon! Kaam batao.",
]
ABOUT = [
    "Main link se video download, compress, cut, audio, censor aur post bana sakta hoon. /help dekho.",
    "Download, compress, cut, censor aur post — sab kar leta hoon. Bas link ya file bhejo.",
]
OKAY = ["👍", "Theek hai 👌", "Done ✅", "Okk 🙂"]
FALLBACK = [
    "Samajh nahi aaya — link/video bhejo ya /help dekho.",
    "Thoda aur saaf batao? Link bhejo, ya likho jaise <code>720p mein download</code>.",
    "Mujhe iska matlab nahi mila. /help se commands dekh sakte ho.",
]


def _tsec(v):
    try:
        if v is None or v == "" or str(v).lower() == "null":
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        return ve.to_sec(s) if ":" in s else float(s)
    except Exception:
        return None


def hist(chat):
    return HISTORY.setdefault(chat, deque(maxlen=10))


CMD_WORDS = re.compile(
    r"\d{3,4}\s*p\b|\b(download|dl|cut|compress|audio|mp3|censor|template|status|queue|trim|link)\b|\d+:\d+", re.I
)


def conv_pool(text):
    t = (text or "").lower().strip()
    if len(re.findall(r"\w+", t)) > 6 or CMD_WORDS.search(t):
        return None
    if re.search(r"\b(thanks|thank you|thx|tnx|shukriya|dhanyavad|thanku)\b", t):
        return THANKS
    if re.search(r"\b(kaise ho|kaisa hai|kaisi ho|kya haal|how are you|wassup|sup)\b", t):
        return HOWRU
    if re.search(r"\b(kaun ho|who are you|kya kar sakte|kya kya|what can you do|tum kya)\b", t):
        return ABOUT
    if re.fullmatch(r"\W*(ok|okay|k|hmm+|accha|acha|theek|thik|haan|ha|hn|done)\W*", t):
        return OKAY
    if re.match(r"\W*(hi+|hello|hey|heyy+|namaste|yo)\b", t):
        return GREET
    return None


def pick_reply(chat, pool):
    last = LAST_LOCAL.get(chat)
    choices = [x for x in pool if x != last] or pool
    r = random.choice(choices)
    LAST_LOCAL[chat] = r
    return r


def local_chat(chat, text):
    return pick_reply(chat, conv_pool(text) or FALLBACK)


async def ai_generate(prompt, temperature=0.9, timeout=25):
    if not ve.ai_model:
        return None
    loop = asyncio.get_running_loop()

    def _run():
        try:
            return ve.ai_model.generate_content(
                prompt, generation_config={"temperature": temperature}
            ).text
        except Exception as e:
            logging.warning(f"[ai] {str(e)[:140]}")
            return None

    try:
        return await asyncio.wait_for(loop.run_in_executor(None, _run), timeout=timeout)
    except Exception:
        return None


def _ctx(message):
    job = ve.JOBS.get(ve.LAST.get(message.chat.id))
    return {
        "chat": message.chat.id,
        "has_links": bool(extract_urls(message)),
        "media": (media_of(message)[0] or ""),
        "last_title": job.src.title if job else "",
    }


def normalize_ai(d):
    act = str(d.get("action") or "").lower()
    it = {"action": act if act in AI_ACTIONS else "none", "q": 0, "start": None,
          "end": None, "mode": "", "arg": "", "reply": ""}
    m = re.search(r"\d+", str(d.get("quality") or ""))
    if m:
        q = int(m.group(0))
        if 0 < q < 144:
            q = 2160 if q == 4 else 1440 if q == 2 else 0
        it["q"] = q
    it["start"], it["end"] = _tsec(d.get("start")), _tsec(d.get("end"))
    md = str(d.get("mode") or "").lower()
    if md in ("exact", "fast"):
        it["mode"] = md
    it["arg"] = re.sub(r"[^\w]", "", str(d.get("arg") or ""))[:12].lower()
    if it["arg"] == "random":
        it["arg"] = "0"
    it["reply"] = str(d.get("reply") or "").strip()[:400]
    return it


async def ai_intent(text, ctx):
    if not ve.ai_model or len((text or "").strip()) < 2:
        return None
    recent = "\n".join(hist(ctx["chat"]))
    prompt = (
        AI_BRAIN + f"\nContext: has_links={ctx['has_links']}, media={ctx['media'] or 'none'}, "
        f"last_title={ctx['last_title'] or 'none'}\nRecent chat:\n{recent or '(none)'}\nOwner message: {text}"
    )
    out = await ai_generate(prompt, temperature=0.5)
    if not out:
        return None
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    try:
        return normalize_ai(json.loads(m.group(0)))
    except Exception:
        return None


def fuzzy_action(text):
    vocab = {}
    for act, keys in FUZZY.items():
        for k in keys:
            vocab[k] = act
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        w = re.sub(r"(.)\1+", r"\1", w)
        if w in vocab:
            return vocab[w]
        close = get_close_matches(w, list(vocab), n=1, cutoff=0.82)
        if close:
            return vocab[close[0]]
    return None


async def ai_or_rule(text, ctx):
    d = await ai_intent(text, ctx)
    if d:
        return d
    pool = conv_pool(text)
    if pool and not ctx["has_links"]:
        return {"action": "chat", "q": 0, "start": None, "end": None, "mode": "", "arg": "",
                "reply": pick_reply(ctx["chat"], pool)}
    base = rule_intent(text, ctx["has_links"])
    it = {"action": base.action, "q": base.q, "start": base.start, "end": base.end,
          "mode": base.mode, "arg": base.arg, "reply": base.reply}
    if it["action"] == "none":
        f = fuzzy_action(text)
        if f:
            it["action"] = f
    return it


async def ai_free_reply(text, ctx):
    if not ve.ai_model:
        return None
    recent = "\n".join(hist(ctx["chat"]))
    prompt = (
        "You are a friendly, witty Telegram bot assistant chatting with its owner in Hinglish/Hindi/English. "
        "Answer the message naturally and helpfully in 1-3 short sentences (max ~40 words). "
        "Match the owner's language and mood. VARY the wording — never repeat earlier replies. "
        "NEVER mention or describe adult/explicit content. "
        "The bot can: download video from a link, compress video, censor (mosaic) nudity in anime/real videos, "
        "cut clips, extract audio, make posts, and /cancel everything.\n"
        f"Recent chat:\n{recent or '(none)'}\nOwner: {text}"
    )
    return await ai_generate(prompt, temperature=1.0)


async def _allowed(_, __, update):
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


def _sweep(now):
    for k in [k for k, v in list(SEEN.items()) if now - v > 900]:
        SEEN.pop(k, None)


async def _reply(message, text, **kw):
    m = await safe(message.reply_text, text, quote=True, **kw)
    if getattr(m, "id", None):
        remember_sent(message.chat.id, m.id)
    return m


def cancel_pending(chat):
    dropped = 0
    q = CHAT_Q.get(chat)
    while q is not None and not q.empty():
        try:
            q.get_nowait()
            dropped += 1
        except asyncio.QueueEmpty:
            break
    dropped += len(TEXT_BUF.pop(chat, []))
    t = TEXT_TASKS.pop(chat, None)
    if t and not t.done():
        t.cancel()
    w = CHAT_WORKERS.get(chat)
    if w and not w.done() and w is not asyncio.current_task():
        w.cancel()
        CHAT_WORKERS.pop(chat, None)
    return dropped


async def do_cancel(client, message):
    chat = message.chat.id
    dropped = cancel_pending(chat)
    stopped = ve.cancel_chat(chat)
    await _reply(
        message,
        f"🛑 <b>Sab ruk gaya.</b>\n• Queue se hataye: <code>{dropped}</code>\n"
        f"• Chal rahe kaam band: <code>{stopped}</code>\n\nNaya link/media bhejo jab chaho."
    )


def _is_post_links(links):
    return is_post_only_links(links)


async def process_message(message):
    text = message.text or message.caption or ""
    links = extract_urls(message)
    kind, fid = media_of(message)
    wants_censor = bool(kind) and not links and fuzzy_action(text) == "censor"
    if kind and links:
        return await post_message(app, message, links, kind, fid)
    if wants_censor and kind == "photo":
        return await ve.censor_photo_message(app, message, None)
    if wants_censor and is_video_media(message):
        return await censor_uploaded_video(app, message, None)
    if kind and is_video_media(message):
        return await compress_message(app, message, text)
    if kind == "photo":
        return await _ask_photo(message)
    if links:
        if _is_post_links(links):
            return await post_message(app, message, links, None, None)
        return await download_message(app, message, text, links)
    if text.strip():
        return await smart_chat(app, message, text)
    return None


async def _ask_photo(message):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔞 Censor image", callback_data=f"pz:img:{message.id}"),
    ]])
    await _reply(message, "🖼 Ye image hai. Kya karna hai?", reply_markup=kb)


async def _handle(item):
    kind, payload = item
    if kind == "album":
        return await album_post(app, payload)
    if kind == "chat":
        texts = [(m.text or "").strip() for m in payload if (m.text or "").strip()]
        if texts:
            return await smart_chat(app, payload[-1], "\n".join(texts))
        return None
    return await process_message(payload)


async def _worker(chat):
    q = CHAT_Q[chat]
    while True:
        try:
            item = await asyncio.wait_for(q.get(), timeout=90)
        except asyncio.TimeoutError:
            return
        try:
            await _handle(item)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.exception("handler failed")
            target = item[1][-1] if item[0] == "chat" else item[1]
            try:
                await _reply(target, f"❌ <code>{esc(str(e)[:200])}</code>")
            except Exception:
                pass
        await asyncio.sleep(POST_GAP)


def _enqueue(chat, item):
    q = CHAT_Q.get(chat)
    if q is None:
        q = CHAT_Q[chat] = asyncio.Queue()
    q.put_nowait(item)
    w = CHAT_WORKERS.get(chat)
    if w is None or w.done():
        CHAT_WORKERS[chat] = asyncio.create_task(_worker(chat))


async def _flush_text_later(chat):
    try:
        await asyncio.sleep(TEXT_WINDOW)
    except asyncio.CancelledError:
        return
    msgs = TEXT_BUF.pop(chat, [])
    TEXT_TASKS.pop(chat, None)
    if msgs:
        _enqueue(chat, ("chat", msgs))


async def intake(message):
    chat = message.chat.id
    kind, _ = media_of(message)
    if kind or extract_urls(message):
        return _enqueue(chat, ("msg", message))
    TEXT_BUF.setdefault(chat, []).append(message)
    old = TEXT_TASKS.get(chat)
    if old and not old.done():
        old.cancel()
    TEXT_TASKS[chat] = asyncio.create_task(_flush_text_later(chat))


async def censor_uploaded_video(client, message: Message, style=None):
    media = message.video or message.animation or message.document
    if not media:
        return
    chat = message.chat.id
    dur = getattr(media, "duration", 0) or 0
    if dur and dur > cen.CENSOR_MAX_SEC:
        return await _reply(
            message,
            f"✂️ Video <code>{int(dur)}s</code> ki hai. Censor ke liye max "
            f"<code>{int(cen.CENSOR_MAX_SEC)}s</code> — pehle chhoti karo."
        )
    loop = asyncio.get_running_loop()
    st = await _reply(message, "🔞 Censor shuru ho raha hai…")
    prog = ve.Progress(loop, st, "🔞 <b>Censor</b>", ve.stop_event(chat))
    workdir = os.path.join("./downloads/", f"cv_{int(time.time())}_{message.id}")
    os.makedirs(workdir, exist_ok=True)
    try:
        if ve.ENC_SEM.locked():
            await prog.stage("Queue mein hai… (pehla censor khatam hone do)")
        async with ve.ENC_SEM:
            await prog.stage("⬇️ Video download ho rahi hai…")
            path = await client.download_media(
                media, file_name=os.path.join(workdir, "in.mp4"),
                progress=ve._up_progress, progress_args=(prog, "⬇️ Downloading Video")
            )
            if not path:
                raise ve.Cancelled()
            secs = await loop.run_in_executor(None, cen.video_seconds, path)
            if secs and secs > cen.CENSOR_MAX_SEC:
                return await st.edit_text(
                    f"✂️ Video <code>{int(secs)}s</code> ki nikli. Censor ke liye max "
                    f"<code>{int(cen.CENSOR_MAX_SEC)}s</code> — pehle chhoti karo."
                )
            await prog.stage("🔎 Anime/3D nudity scan + censor…")
            r = await loop.run_in_executor(None, cen.censor_any, path, prog, style)
            info = ve.ffprobe(r["path"]) or {}
            name = ve.sanitize_filename(os.path.splitext(getattr(media, "file_name", None) or "video.mp4")[0])
            cap = (
                f"🔞 <b>Censored</b>\n🩹 Regions hidden: <code>{r.get('found', 0)}</code>\n"
                f"🧠 Detector: <code>{'+'.join(r.get('backends') or [])}</code>"
            )
            await ve.send_one(
                client, chat, r["path"], f"{name}_censored.mp4", cap, info, None,
                prog, "cz", workdir, message.id
            )
        try:
            await st.delete()
        except Exception:
            pass
    except ve.Cancelled:
        await ve.cancel_note(st)
    except asyncio.CancelledError:
        await ve.cancel_note(st)
        raise
    except Exception as e:
        logging.exception("censor failed")
        try:
            await st.edit_text(f"❌ Censor fail: <code>{esc(str(e)[:250])}</code>")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def smart_chat(client, message, text):
    chat = message.chat.id
    ctx = _ctx(message)
    it = await ai_or_rule(text, ctx)
    act = it["action"]
    hist(chat).append(f"Owner: {text[:300]}")
    if act == "cancel":
        return await do_cancel(client, message)
    if act in ("status", "help", "start", "template"):
        if act == "status":
            return await _reply(message, status_text())
        if act == "help":
            return await _reply(message, HELP_TEXT)
        if act == "start":
            return await _reply(message, START_TEXT)
        return await template_reply(message, it["arg"])
    if act in ("chat", "none"):
        reply = it.get("reply") or await ai_free_reply(text, ctx) or local_chat(chat, text)
        hist(chat).append(f"Bot: {reply[:300]}")
        return await _reply(message, f"🤖 {esc(reply)}")
    return await chat_message(client, message, text, START_TEXT, HELP_TEXT)


@app.on_message(filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed
                & ~filters.command(COMMANDS))
async def router(client, message: Message):
    now = time.time()
    _sweep(now)
    key = (message.chat.id, message.id)
    if key in SEEN or key in SENT_BY_BOT:
        return
    SEEN[key] = now
    text = message.text or message.caption or ""
    if is_bot_template(text):
        return
    try:
        if message.media_group_id:
            return _enqueue(message.chat.id, ("album", message))
        if CANCEL_RE.match(text) and not media_of(message)[0] and not extract_urls(message):
            return await do_cancel(client, message)
        await intake(message)
    except Exception as e:
        logging.exception("router failed")
        try:
            await _reply(message, f"❌ <code>{esc(str(e)[:200])}</code>")
        except Exception:
            pass


def _cmd(name):
    return filters.command(name) & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed


@app.on_message(_cmd("start"))
async def start_cmd(client, message: Message):
    await _reply(message, START_TEXT)


@app.on_message(_cmd("help"))
async def help_cmd(client, message: Message):
    await _reply(message, HELP_TEXT)


@app.on_message(_cmd(["queue", "status"]))
async def queue_cmd(client, message: Message):
    chat = message.chat.id
    q = CHAT_Q.get(chat)
    await _reply(
        message,
        status_text() + f"\n• Tumhare chal rahe kaam: <code>{ve.active_count(chat)}</code>"
        f"\n• Queue mein: <code>{q.qsize() if q else 0}</code>"
    )


@app.on_message(_cmd(["cancel", "stop"]))
async def cancel_cmd(client, message: Message):
    await do_cancel(client, message)


@app.on_message(_cmd("template"))
async def template_cmd(client, message: Message):
    arg = (message.text or "").split(maxsplit=1)
    await template_reply(message, arg[1].strip() if len(arg) > 1 else "")


@app.on_message(_cmd("download"))
async def download_cmd(client, message: Message):
    urls = extract_urls(message)
    if not urls and message.reply_to_message:
        urls = extract_urls(message.reply_to_message)
    if not urls:
        return await _reply(message, "🔗 Link bhejo ya kisi link-message par reply karke /download karo.")
    await download_message(client, message, message.text or "", urls[:10])


@app.on_message(_cmd("compress"))
async def compress_cmd(client, message: Message):
    src = message if is_video_media(message) else message.reply_to_message
    if not (src and is_video_media(src)):
        return await _reply(message, "🎬 Video file bhejo (ya video par reply karke /compress).")
    await compress_message(client, src, src.caption or "")


@app.on_message(_cmd("censor"))
async def censor_cmd(client, message: Message):
    parts = (message.text or message.caption or "").split()
    style = next((p.lower() for p in parts[1:] if p.lower() in cen.STYLES), None)
    src = message if media_of(message)[0] else message.reply_to_message
    if not src:
        return await _reply(message, "🔞 Image/Video bhejo (ya us par reply karke /censor).")
    kind, _ = media_of(src)
    if kind == "photo":
        await ve.censor_photo_message(client, src, style)
    elif kind in ("video", "document", "animation"):
        await censor_uploaded_video(client, src, style)
    else:
        await _reply(message, "🔞 Sirf image ya video censor kar sakta hoon.")


@app.on_message(_cmd("post"))
async def post_cmd(client, message: Message):
    urls = extract_urls(message) or (extract_urls(message.reply_to_message) if message.reply_to_message else [])
    if not urls:
        return await _reply(message, "🔗 Post ke liye links bhejo (media ke saath ya bina).")
    k, fid = media_of(message)
    await post_message(client, message, urls, k, fid)


@app.on_message(_cmd("cut"))
async def cut_cmd(client, message: Message):
    job = ve.JOBS.get(ve.LAST.get(message.chat.id))
    if not job:
        return await _reply(message, "✂️ Cut ke liye pehle koi video ka link bhejo, phir Cut dabao.")
    job.cs, job.ce, job.sel, job.fresh = 0.0, job.src.duration or 600.0, "s", True
    m = await safe(message.reply_text, ve.panel_text(job), reply_markup=ve.panel_kb(job), quote=True)
    if getattr(m, "id", None):
        remember_sent(message.chat.id, m.id)
    job.msg = m


@app.on_message(_cmd("highlight"))
async def highlight_cmd(client, message: Message):
    job = ve.JOBS.get(ve.LAST.get(message.chat.id))
    if not job:
        return await _reply(message, "🔥 Pehle koi video ka link bhejo, phir /highlight karo.")
    if job.busy:
        return await _reply(message, "⏳ Pichla kaam abhi chal raha hai.")
    st = await _reply(message, "🔥 Smart Highlight bana raha hoon…")
    job.msg = st
    ve.spawn(ve.run_smart_clip(client, job, "highlight"), message.chat.id)


@app.on_message(_cmd("preview"))
async def preview_cmd(client, message: Message):
    job = ve.JOBS.get(ve.LAST.get(message.chat.id))
    if not job:
        return await _reply(message, "🎬 Pehle koi video ka link bhejo, phir /preview karo.")
    if job.busy:
        return await _reply(message, "⏳ Pichla kaam abhi chal raha hai.")
    st = await _reply(message, "🎬 10s Preview bana raha hoon…")
    job.msg = st
    ve.spawn(ve.run_smart_clip(client, job, "preview"), message.chat.id)


@app.on_message(_cmd("ai"))
async def ai_cmd(client, message: Message):
    q = (message.text or "").split(maxsplit=1)
    text = q[1].strip() if len(q) > 1 else ""
    if not text:
        return await _reply(message, "🤖 Kuch likho: <code>/ai 720p mein download karo</code>")
    await smart_chat(client, message, text)


@app.on_callback_query(allowed)
async def on_callback(client, cq: CallbackQuery):
    parts = (cq.data or "").split(":")
    act = parts[0]
    if act == "pz" and len(parts) >= 3 and parts[1] == "img":
        await cq.answer("🔞 Censor shuru…")
        try:
            msg = await client.get_messages(cq.message.chat.id, int(parts[2]))
        except Exception:
            msg = None
        if msg and media_of(msg)[0] == "photo":
            return await ve.censor_photo_message(client, msg, None)
        return
    return await handle_callback(client, cq)


async def main():
    global START_TS
    init_engine()
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        logging.error("❌ ffmpeg/ffprobe install nahi hai! (apt install ffmpeg)")
    START_TS = time.time()
    await app.start()
    try:
        await app.set_bot_commands([
            BotCommand("cancel", "Sab chal raha kaam roko"),
            BotCommand("help", "Kaise use karein"),
            BotCommand("status", "Queue / status"),
            BotCommand("censor", "Video/image censor"),
            BotCommand("compress", "Video compress"),
            BotCommand("cut", "Video cut"),
            BotCommand("post", "Post banao"),
            BotCommand("template", "Template chuno"),
        ])
    except Exception:
        pass
    asyncio.get_running_loop().run_in_executor(None, cen._load)
    logging.info("🚀 Modular Bot Started (Per-message queue + AI Brain + Anime/3D Censor + /cancel)")
    spawn(janitor())
    try:
        await idle()
    finally:
        await app.stop()


if __name__ == "__main__":
    app.run(main())
