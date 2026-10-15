import os
import re
import sys
import time
import json
import shutil
import asyncio
import logging
import secrets
import subprocess
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
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)

import modules.video_engine as ve
from modules.post_maker import (
    SENT_BY_BOT, esc, is_bot_template, extract_urls, media_of,
    is_video_media, remember_sent, safe, post_message, album_post, template_reply,
)
from modules.video_engine import (
    init_engine, spawn, rule_intent, status_text,
    download_message, chat_message, compress_message, handle_callback, janitor,
)

# --------------------------------------------------------------------- config
API_ID = int(os.environ.get("API_ID", 0) or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED = {int(x) for x in re.findall(r"\d+", os.environ.get("ALLOWED_IDS", ""))}
STALE_SEC = int(os.environ.get("STALE_SEC", 25))

# Ek saath aaye messages ko ek "batch" maana jaata hai (timing se, link se nahi)
BATCH_WINDOW = float(os.environ.get("BATCH_WINDOW", 4.0))
BATCH_TTL = int(os.environ.get("BATCH_TTL", 3600))
# auto  = single message ho to bot khud decide kare, 2+ ho to ek baar poochhe
# always= har baar poochhe
ASK_MODE = os.environ.get("ASK_MODE", "auto").lower()

START_TS = 0.0
SEEN = {}
BATCHES = {}
CHAT_BATCH = {}
BATCH_TASKS = {}

COMMANDS = ["start", "help", "queue", "download", "compress", "censor",
            "post", "cut", "template", "status", "ai"]

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
    "<b>2️⃣ Post Maker:</b> Diskwala/Terabox + Telegram links media ke saath bhejo — ek saaf post banega.\n"
    "<b>3️⃣ Auto Compress:</b> Video file bhejo — SSIM 0.985 lock ke saath size kam.\n"
    "<b>4️⃣ Censor (offline):</b> Video/image par nudity detect karke blur — sirf jab tum dabao.\n\n"
    "🗣 Baat AI se bhi kar sakte ho, ya <code>/</code> commands: /download /compress /censor /post /cut"
)

HELP_TEXT = (
    "<b>📖 Kaise use karein</b>\n\n"
    "• <b>Link bhejo</b> → quality button chuno.\n"
    "• <b>Video file bhejo</b> → compress ka option, ya seedha compress.\n"
    "• <b>Image/Video + link</b> → post maker.\n"
    "• <b>Censor</b>: video/image par <code>🔞 Censor</code> dabao (offline nudity blur).\n"
    "• <b>Commands:</b> /download /compress /censor /post /cut /template /status\n"
    "• <b>Ek saath kai message</b> bhejo → bot ek hi baar poochhega kya karna hai."
)

# ============================================================== AI command brain
# Gemini sirf TEXT/command samajhne ke liye. Adult media kabhi AI ko nahi jaata.
AI_ACTIONS = ("download", "compress", "censor", "cut", "audio", "post",
              "template", "status", "help", "start", "chat", "none")

AI_BRAIN = (
    "You are the control brain of a Telegram video-downloader bot. The owner writes in "
    "Hinglish, Hindi or English, often with spelling mistakes — understand the intent anyway. "
    "NEVER describe, mention or discuss adult/explicit content; just handle the command silently. "
    "Reply with ONLY a JSON object (no markdown):\n"
    '{"action": one of ' + str(list(AI_ACTIONS)) + ', "quality": integer video height or 0, '
    '"start": "HH:MM:SS" or null, "end": "HH:MM:SS" or null, '
    '"mode": "exact" or "fast" or null, "arg": string or null, '
    '"reply": "one short natural Hinglish line (vary it every time)"}'
)

FUZZY = {
    "download": ["download", "downlod", "dwnld", "dload", "bhej", "bhejo", "laao", "lelo", "get", "link"],
    "compress": ["compress", "compres", "cmprs", "shrink", "chhota", "small", "size"],
    "censor": ["censor", "censr", "sensor", "blur", "blurr", "hide", "dhak", "chhupa"],
    "cut": ["cut", "kat", "kaat", "kata", "trim", "clip"],
    "audio": ["audio", "mp3", "m4a", "gana", "gaana", "song", "music", "sound"],
    "post": ["post", "tamplet", "tempalte"],
    "status": ["status", "queue", "kaam"],
    "help": ["help", "madad", "kaise", "how"],
    "start": ["start", "shuru", "hi", "hello", "hey", "namaste"],
}


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
    it["reply"] = str(d.get("reply") or "").strip()[:300]
    return it


async def ai_intent(text, ctx):
    if not ve.ai_model or len((text or "").strip()) < 2:
        return None
    prompt = (AI_BRAIN + f"\nContext: has_links={ctx['has_links']}, media={ctx['media'] or 'none'}, "
              f"last_title={ctx['last_title'] or 'none'}\nOwner message: {text}")
    out = await ai_generate(prompt, temperature=0.4)
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
    """Pehle AI (spelling-tolerant, smart), warna local rule + fuzzy fallback."""
    d = await ai_intent(text, ctx)
    if d:
        return d
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
    prompt = (
        "You are a friendly Telegram bot assistant. The owner chats in Hinglish/Hindi/English. "
        "Reply in ONE short Hinglish line (max ~18 words). VARY the wording every time — never repeat "
        "the same line. NEVER mention or describe adult/explicit content. Be warm and brief. "
        "The bot can: download video from a link, compress video, censor (blur) nudity, cut clips, "
        "extract audio, and make posts.\n"
        f"Owner: {text}"
    )
    return await ai_generate(prompt, temperature=1.0)


# ============================================================== anti-loop filter
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


# ============================================================== batch helpers
def _new_batch_id():
    return ve.INSTANCE + secrets.token_hex(3)


def _fingerprint(message):
    text = re.sub(r"\s+", " ", (message.text or message.caption or "").strip().lower())
    kind, fid = media_of(message)
    return (kind or "", fid or "", text[:200])


def _dups_in(msgs):
    seen, dups = {}, []
    for m in msgs:
        fp = _fingerprint(m)
        if fp in seen:
            dups.append(m.id)
        else:
            seen[fp] = m.id
    return dups


def _find_msg(msgs, kinds):
    for m in msgs:
        k, _ = media_of(m)
        if k in kinds:
            return m
    return None


def _sweep(now):
    for k in [k for k, v in list(SEEN.items()) if now - v > 900]:
        SEEN.pop(k, None)
    for bid in [b for b, v in list(BATCHES.items()) if v.get("expires") and now > v["expires"]]:
        BATCHES.pop(bid, None)
        BATCH_TASKS.pop(bid, None)


async def _finalize_later(bid):
    try:
        await asyncio.sleep(BATCH_WINDOW + 0.3)
    except asyncio.CancelledError:
        return
    await _finalize_batch(bid)


async def intake(client, message):
    chat = message.chat.id
    now = time.time()
    bid = CHAT_BATCH.get(chat)
    if bid and bid in BATCHES and now - BATCHES[bid]["last"] <= BATCH_WINDOW:
        b = BATCHES[bid]
    else:
        bid = _new_batch_id()
        b = {"id": bid, "chat": chat, "msgs": [], "links": [], "media": set(),
             "created": now, "last": now, "done": False, "expires": 0.0}
        BATCHES[bid] = b
        CHAT_BATCH[chat] = bid
    b["last"] = now
    b["msgs"].append(message)
    for u in extract_urls(message):
        if u not in b["links"]:
            b["links"].append(u)
    k, _ = media_of(message)
    if k:
        b["media"].add(k)
    old = BATCH_TASKS.get(bid)
    if old and not old.done():
        old.cancel()
    BATCH_TASKS[bid] = asyncio.create_task(_finalize_later(bid))


async def _finalize_batch(bid):
    b = BATCHES.get(bid)
    if not b or b["done"]:
        return
    b["done"] = True
    b["expires"] = time.time() + BATCH_TTL
    msgs = b["msgs"]
    dups = _dups_in(msgs)
    if len(msgs) >= 2 or ASK_MODE == "always":
        await _ask_menu(b, dups)
    else:
        await _auto_decide(b, dups)


# ============================================================== ask menu
def _ask_kb(b: dict):
    bid = b["id"]
    rows = []
    if b["links"]:
        rows.append([InlineKeyboardButton("⬇️ Download link(s)", callback_data=f"a:d:{bid}")])
    if b["media"] & {"video", "document"}:
        rows.append([InlineKeyboardButton("🗜 Compress video", callback_data=f"a:c:{bid}")])
        rows.append([InlineKeyboardButton("🔞 Censor video", callback_data=f"a:z:{bid}")])
    if "photo" in b["media"]:
        rows.append([InlineKeyboardButton("🔞 Censor image", callback_data=f"a:zi:{bid}")])
    if b["links"] and b["media"]:
        rows.append([InlineKeyboardButton("📝 Make Post", callback_data=f"a:p:{bid}")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"a:x:{bid}")])
    return InlineKeyboardMarkup(rows)


async def _ask_menu(b, dups):
    first = b["msgs"][0]
    kinds = ", ".join(sorted(b["media"])) or "koi media nahi"
    lines = [f"🤔 <b>{len(b['msgs'])} message mile ek saath.</b>",
             f"🔗 Links: <code>{len(b['links'])}</code>  •  📎 Media: <code>{kinds}</code>"]
    if dups:
        lines.append(f"🔁 Same message dobara aaya: <code>{len(dups)}</code>")
    lines.append("\n<b>Kya karna hai?</b>")
    m = await safe(first.reply_text, "\n".join(lines), reply_markup=_ask_kb(b), quote=True)
    if getattr(m, "id", None):
        remember_sent(b["chat"], m.id)


async def _auto_decide(b, dups):
    message = b["msgs"][0]
    if dups:
        m = await safe(message.reply_text, "🔁 Ye message pehle bhi aaya tha — skip kar raha hoon.", quote=True)
        if getattr(m, "id", None):
            remember_sent(b["chat"], m.id)
        return
    text = message.text or message.caption or ""
    kind, _ = media_of(message)
    if kind and b["links"]:
        return await post_message(app, message, b["links"], kind, media_of(message)[1])
    if kind and is_video_media(message):
        return await compress_message(app, message, text)
    if kind == "photo":
        return await _ask_photo(message)
    if b["links"]:
        return await download_message(app, message, text, b["links"])
    if (text or "").strip():
        return await smart_chat(app, message, text)
    return None


async def _ask_photo(message):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔞 Censor image", callback_data=f"pz:img:{message.id}"),
    ]])
    m = await safe(message.reply_text, "🖼 Ye image hai. Kya karna hai?", reply_markup=kb, quote=True)
    if getattr(m, "id", None):
        remember_sent(message.chat.id, m.id)


# ============================================================== ask callbacks
async def handle_ask(client, cq: CallbackQuery, act, bid):
    b = BATCHES.get(bid)
    if not b:
        return await cq.answer("⌛ Ye request purani ho gayi — dobara bhejo.", show_alert=True)
    msgs = b["msgs"]
    first = msgs[0]
    text = first.text or first.caption or ""
    if act == "x":
        BATCHES.pop(bid, None)
        await cq.answer()
        try:
            await cq.message.delete()
        except Exception:
            pass
        return
    await cq.answer("✅ Theek hai…")
    BATCHES.pop(bid, None)
    if act == "d":
        if b["links"]:
            await download_message(client, first, text, b["links"])
    elif act == "c":
        vm = _find_msg(msgs, {"video", "document"})
        if vm:
            await compress_message(client, vm, vm.caption or "")
    elif act == "z":
        vm = _find_msg(msgs, {"video", "document"})
        if vm:
            await censor_uploaded_video(client, vm, "blur")
    elif act == "zi":
        pm = _find_msg(msgs, {"photo"})
        if pm:
            await ve.censor_photo_message(client, pm, "blur")
    elif act == "p":
        if b["links"]:
            pm = _find_msg(msgs, {"photo", "video", "animation", "document"}) or first
            k, fid = media_of(pm)
            await post_message(client, pm, b["links"], k, fid)


# ============================================================== censor an uploaded video
async def censor_uploaded_video(client, message: Message, mode="blur"):
    media = message.video or message.document
    if not media:
        return
    loop = asyncio.get_running_loop()
    st = await message.reply_text("🔞 Video censor ho rahi hai (offline)…", quote=True)
    if getattr(st, "id", None):
        remember_sent(message.chat.id, st.id)
    workdir = os.path.join("./downloads/", f"cv_{int(time.time())}")
    os.makedirs(workdir, exist_ok=True)
    try:
        path = await client.download_media(media, file_name=os.path.join(workdir, "in.mp4"))
        r = await loop.run_in_executor(None, ve.censor_any, path, None, mode)
        info = ve.ffprobe(r["path"]) or {}
        name = ve.sanitize_filename(os.path.splitext(getattr(media, "file_name", None) or "video.mp4")[0])
        cap = f"🔞 <b>Censored</b>\n🩹 Regions hidden: <code>{r.get('found', 0)}</code>"
        m = await client.send_video(
            message.chat.id, r["path"], caption=cap, file_name=f"{name}_censored.mp4",
            supports_streaming=True, duration=int(info.get("duration") or 0),
            reply_to_message_id=message.id
        )
        if getattr(m, "id", None):
            remember_sent(message.chat.id, m.id)
        try:
            await st.delete()
        except Exception:
            pass
    except Exception as e:
        try:
            await st.edit_text(f"❌ Censor fail: <code>{esc(str(e)[:200])}</code>")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ============================================================== smart chat
async def smart_chat(client, message, text):
    ctx = _ctx(message)
    it = await ai_or_rule(text, ctx)
    act = it["action"]
    if act in ("status", "help", "start", "template"):
        if act == "status":
            m = await safe(message.reply_text, status_text(), quote=True)
            if getattr(m, "id", None):
                remember_sent(message.chat.id, m.id)
            return
        if act == "help":
            m = await safe(message.reply_text, HELP_TEXT, quote=True)
            if getattr(m, "id", None):
                remember_sent(message.chat.id, m.id)
            return
        if act == "start":
            m = await safe(message.reply_text, START_TEXT, quote=True)
            if getattr(m, "id", None):
                remember_sent(message.chat.id, m.id)
            return
        return await template_reply(message, it["arg"])
    if act in ("ask", "chat", "none"):
        reply = it.get("reply") or await ai_free_reply(text, ctx)
        if not reply:
            reply = "Samajh nahi aaya — thoda aur batao. Ya /help dekho."
        m = await safe(message.reply_text, f"🤖 {esc(reply)}", quote=True)
        if getattr(m, "id", None):
            remember_sent(message.chat.id, m.id)
        return
    # baaki actions engine ke chat handler ko de do
    return await chat_message(client, message, text, START_TEXT, HELP_TEXT)


# ============================================================== router
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
            return await album_post(client, message)
        await intake(client, message)
    except Exception as e:
        logging.exception("router failed")
        try:
            m = await message.reply_text(f"❌ <code>{esc(str(e)[:200])}</code>", quote=True)
            if getattr(m, "id", None):
                remember_sent(message.chat.id, m.id)
        except Exception:
            pass


# ============================================================== commands
async def _cmd_reply(message, text):
    m = await safe(message.reply_text, text, quote=True)
    if getattr(m, "id", None):
        remember_sent(message.chat.id, m.id)
    return m


@app.on_message(filters.command("start") & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed)
async def start_cmd(client, message: Message):
    await _cmd_reply(message, START_TEXT)


@app.on_message(filters.command("help") & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed)
async def help_cmd(client, message: Message):
    await _cmd_reply(message, HELP_TEXT)


@app.on_message(filters.command("queue") & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed)
async def queue_cmd(client, message: Message):
    await _cmd_reply(message, status_text())


@app.on_message(filters.command("status") & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed)
async def status_cmd(client, message: Message):
    await _cmd_reply(message, status_text())


@app.on_message(filters.command("template") & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed)
async def template_cmd(client, message: Message):
    arg = (message.text or "").split(maxsplit=1)
    await template_reply(message, arg[1].strip() if len(arg) > 1 else "")


@app.on_message(filters.command("download") & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed)
async def download_cmd(client, message: Message):
    urls = extract_urls(message)
    if not urls and message.reply_to_message:
        urls = extract_urls(message.reply_to_message)
    if not urls:
        return await _cmd_reply(message, "🔗 Link bhejo ya kisi link-message par reply karke /download karo.")
    await download_message(client, message, message.text or "", urls[:10])


@app.on_message(filters.command("compress") & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed)
async def compress_cmd(client, message: Message):
    src = message if is_video_media(message) else message.reply_to_message
    if not (src and is_video_media(src)):
        return await _cmd_reply(message, "🎬 Video file bhejo (ya video par reply karke /compress).")
    await compress_message(client, src, src.caption or "")


@app.on_message(filters.command("censor") & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed)
async def censor_cmd(client, message: Message):
    src = message if media_of(message)[0] else message.reply_to_message
    if not src:
        return await _cmd_reply(message, "🔞 Image/Video bhejo (ya us par reply karke /censor).")
    kind, _ = media_of(src)
    if kind == "photo":
        await ve.censor_photo_message(client, src, "blur")
    elif kind in ("video", "document"):
        await censor_uploaded_video(client, src, "blur")
    else:
        await _cmd_reply(message, "🔞 Sirf image ya video censor kar sakta hoon.")


@app.on_message(filters.command("post") & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed)
async def post_cmd(client, message: Message):
    urls = extract_urls(message) or (extract_urls(message.reply_to_message) if message.reply_to_message else [])
    if not urls:
        return await _cmd_reply(message, "🔗 Post ke liye links bhejo (media ke saath ya bina).")
    k, fid = media_of(message)
    await post_message(client, message, urls, k, fid)


@app.on_message(filters.command("cut") & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed)
async def cut_cmd(client, message: Message):
    job = ve.JOBS.get(ve.LAST.get(message.chat.id))
    if not job:
        return await _cmd_reply(message, "✂️ Cut ke liye pehle koi video ka link bhejo, phir Cut dabao.")
    job.cs, job.ce, job.sel, job.fresh = 0.0, job.src.duration or 600.0, "s", True
    m = await safe(message.reply_text, ve.panel_text(job), reply_markup=ve.panel_kb(job), quote=True)
    if getattr(m, "id", None):
        remember_sent(message.chat.id, m.id)
    job.msg = m


@app.on_message(filters.command("ai") & filters.private & ~filters.me & ~filters.bot & ~filters.outgoing & allowed)
async def ai_cmd(client, message: Message):
    q = (message.text or "").split(maxsplit=1)
    text = q[1].strip() if len(q) > 1 else ""
    if not text:
        return await _cmd_reply(message, "🤖 Kuch likho: <code>/ai 720p mein download karo</code>")
    await smart_chat(client, message, text)


# ============================================================== callbacks
@app.on_callback_query(allowed)
async def on_callback(client, cq: CallbackQuery):
    parts = (cq.data or "").split(":")
    act = parts[0]
    # ask-menu callbacks (batch)
    if act == "a" and len(parts) >= 3:
        return await handle_ask(client, cq, parts[1], parts[2])
    # image censor from a single photo prompt
    if act == "pz" and len(parts) >= 3 and parts[1] == "img":
        await cq.answer("🔞 Censor shuru…")
        try:
            msg = await client.get_messages(cq.message.chat.id, int(parts[2]))
        except Exception:
            msg = None
        if msg and media_of(msg)[0] == "photo":
            return await ve.censor_photo_message(client, msg, "blur")
        return
    # everything else -> engine (job/cut/fail panels)
    return await handle_callback(client, cq)


# ============================================================== main
async def main():
    global START_TS
    init_engine()
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        logging.error("❌ ffmpeg/ffprobe install nahi hai! (apt install ffmpeg)")
    START_TS = time.time()
    await app.start()
    logging.info("🚀 Modular Bot Started (Batch + Ask Menu + AI Brain + Offline Censor)")
    spawn(janitor())
    try:
        await idle()
    finally:
        await app.stop()


if __name__ == "__main__":
    app.run(main())
