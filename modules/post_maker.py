import os
import re
import time
import html
import random
import asyncio
import logging
import requests
from urllib.parse import urlparse
from pyrogram import enums
from pyrogram.types import (
    Message, InputMediaPhoto, InputMediaVideo, InputMediaDocument
)
from pyrogram.errors import FloodWait

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
MAX_POST_LINKS = 60

# Anti-Loop & State Tracking
PREFS = {}
ALBUMS = {}
SENT_BY_BOT = set()

URL_REGEX = re.compile(r'https?://[^\s<>"\']+')

TG_HOSTS = ("t.me", "telegram.me", "telegram.dog", "telesco.pe")
FILEHOSTS = (
    "terabox", "diskwala", "mirrobox", "nephobox", "momerybox", "tibibox",
    "4funbox", "gibibox", "terafileshare", "terasharelink", "teraboxlink",
    "teraboxurl", "teraboxshare", "mega.nz", "mediafire.com", "gofile.io",
    "pixeldrain.com", "streamtape", "doodstream", "dood.", "mixdrop",
    "filemoon", "streamwish", "vidhide", "krakenfiles", "1fichier",
    "drive.google.com", "dropbox.com", "mdisk"
)

STYLES = [
    (0x1D400, 0x1D41A, 0x1D7CE),
    (0x1D5D4, 0x1D5EE, 0x1D7EC),
    (0x1D468, 0x1D482, 0x1D7CE),
    (0x1D670, 0x1D68A, 0x1D7F6),
    (0x1D63C, 0x1D656, 0x1D7EC),
]
FRAMES = [
    ("🍑💦", "💦🍑"), ("🍆🔥", "🔥🍆"), ("🔞😈", "😈🔞"), ("🥵💦", "💦🥵"), ("👅🫦", "🫦👅"),
    ("💋🍑", "🍑💋"), ("🍒🍆", "🍆🍒"), ("🌶️💦", "💦🌶️"), ("😈🍆", "🍆😈"), ("🍑🍆", "🍆🍑"),
    ("💦🔥", "🔥💦"), ("🥵🍑", "🍑🥵"), ("🫦💦", "💦🫦"), ("🍆💦", "💦🍆"), ("🔥🍑", "🍑🔥"),
    ("🔞🍆", "🍆🔞"), ("😈💦", "💦😈"), ("💋🍆", "🍆💋"), ("🥵🍆", "🍆🥵"), ("🍑🫦", "🫦🍑"),
    ("💦👅", "👅💦"), ("🔥😈", "😈🔥"), ("🍆🥵", "🥵🍆"), ("🍒💦", "💦🍒"), ("🔞🍑", "🍑🔞"),
    ("🍆🍑", "🍑🍆"), ("💦🍆", "🍆💦"), ("🥵🫦", "🫦🥵"), ("😈🍑", "🍑😈"), ("🔥👅", "👅🔥"),
    ("🍑🔥💦", "💦🔥🍑"), ("🍆💦🔥", "🔥💦🍆"), ("🫦🍆", "🍆🫦"), ("👅🍑", "🍑👅"), ("💋💦", "💦💋"),
    ("🍆🫦", "🫦🍆"), ("🍑👅", "👅🍑"), ("💦🥵", "🥵💦"), ("🔥💋", "💋🔥"), ("😈🫦", "🫦😈"),
    ("🍒🔥", "🔥🍒"), ("🌶️🍆", "🍆🌶️"), ("🔞💦", "💦🔞"), ("🥵👅", "👅🥵"), ("🍑💋💦", "💦💋🍑"),
]
TITLES = [
    "18+ EXCLUSIVE", "ADULT ANIMATION", "HARDCORE DROP", "AFTER DARK FUCK",
    "SPICY UPLOAD", "HOT & WET", "LATE NIGHT FUCK", "PRIVATE HOLE",
    "VIP ADULT UPLOAD", "NAUGHTY NIGHT", "UNCENSORED FUCK", "MIDNIGHT CUM",
    "FRESH & WET DROP", "ADULTS ONLY", "SECRET HOLE", "HOT NEW EPISODE",
    "STEAMY FUCK", "EXCLUSIVE PREMIERE", "FULL HARD VIDEO", "NEW CUM DROP",
    "SPECIAL FUCK SHOW", "TODAY'S HOLE", "TRENDING FUCK", "FRESH EPISODE",
    "FORBIDDEN HOLE", "SWEET & SPICY", "LATE NIGHT CUM", "VIP EXCLUSIVE",
    "ONLY HERE", "FULL HD FUCK", "RAW & DIRTY", "DEEP INSIDE",
    "CUM LOADED", "HARDCORE ONLY", "WET & READY", "DIRTY PREMIERE",
    "FULL LOAD DROP", "PRIVATE FUCK", "18+ HARDCORE", "MIDNIGHT HOLE",
]
SEPS = [
    "━" * 18, "─" * 16, "▬" * 12, "•┈┈┈┈•✦•┈┈┈┈•", "═" * 15, "✦━━━━━━━━━✦",
    "⋆⋅☆⋅⋆⋅☆⋅⋆⋅☆⋅⋆", "┄" * 14, "▰▱" * 8, "•" * 18, "◈━━━━━━━━━━◈", "⟡" * 10,
    "━━━━ ✦ ━━━━", "〰️" * 8, "═══ ◆ ═══", "✧･ﾟ: *✧･ﾟ:* *:･ﾟ✧",
    "🍑" * 7, "🍆" * 7, "💦" * 8, "🔞 ━━━━━━━━━ 🔞",
    "😈 • • • • • 😈", "🔥━━━━━━━━━━🔥", "💋 ─ ─ ─ ─ ─ 💋",
    "🍑💦🍑💦🍑💦🍑", "🍆🔥🍆🔥🍆🔥🍆", "💦🔞💦🔞💦🔞💦",
    "🫦━━━━━━━━━━🫦", "👅 • • • • • 👅", "🥵━━━━🥵━━━━🥵",
    "🍑🍆🍑🍆🍑🍆🍑", "🔥💦🔥💦🔥💦🔥", "😈🍆😈🍆😈🍆😈",
]
WATCH = [
    ("🍆", "WATCH FULL VIDEO", "👇💦"), ("🍑", "CLICK TO FUCK", "👇🔥"),
    ("💦", "WATCH NOW", "👇🍆"), ("🥵", "PLAY THIS HOLE", "👇🍑"),
    ("👅", "LICK & WATCH", "👇💦"), ("🫦", "FULL HARD VIDEO", "👇🔥"),
    ("🔥", "RAW & UNCUT", "👇🍆"), ("😈", "DIRTY VIDEO", "👇💦"),
    ("🔞", "ADULTS ONLY", "👇🍑"), ("🍆💦", "CUM WATCHING", "👇"),
    ("🍑🍆", "FUCK THIS VIDEO", "👇💦"), ("🥵🍆", "HARDCORE NOW", "👇🔥"),
    ("💦🍑", "WET & READY", "👇🍆"), ("🫦🔥", "DEEP INSIDE", "👇💦"),
    ("👅🍆", "SUCK & WATCH", "👇🍑"), ("🔥💦", "CUM HARD", "👇🍆"),
    ("😈🍑", "DIRTY HOLE", "👇💦"), ("💋🍆", "KISS THE TIP", "👇🔥"),
    ("🥵🫦", "MOAN LOUD", "👇🍑"), ("🍆🔥💦", "FULL LOAD", "👇"),
]
PASS = [
    ("🔐", "PASS LINK", "👇"), ("🗝️", "ACCESS LINK", "👇😈"),
    ("🔓", "UNLOCK PASS", "👇💦"), ("📢", "JOIN CHANNEL", "👇🔥"),
    ("🔞", "PRIVATE CHANNEL", "👇"), ("😈", "VIP CHANNEL", "👇"),
    ("🍑", "BACKUP CHANNEL", "👇"), ("💋", "MAIN CHANNEL", "👇"),
    ("🎟️", "ENTRY PASS", "👇🔥"), ("📌", "IMPORTANT LINK", "👇"),
]
FOOT = [
    ("🍑", "ENJOY THE HOLE", "💦"), ("😈", "FUCK HARD TONIGHT", ""),
    ("💦", "CUM & SHARE", ""), ("🔞", "ADULTS ONLY", ""),
    ("🥵", "STAY HORNY FOR MORE", ""), ("💋", "THANKS FOR WATCHING", "🍆"),
    ("🔥", "ENJOY THE FUCK", "🍑"), ("🤫", "KEEP IT PRIVATE", ""),
    ("🍆", "SEE YOU NEXT ROUND", "💦"), ("🫦", "LIKE & CUM", "💋"),
    ("👅", "LICK & COME BACK", "🍆"), ("🍑💦", "STAY WET", "🔥"),
    ("🍆🔥", "HARDER NEXT TIME", "💦"), ("🥵🍆", "MORE CUM SOON", ""),
    ("😈💦", "DIRTY AGAIN", "🍑"), ("💋🫦", "KISS & SHARE", ""),
    ("🔥🍑", "KEEP FUCKING", "💦"), ("👅🍆", "SUCK MORE", "🔥"),
]
BULLETS = ["🍑", "🍆", "💦", "🔥", "💋", "🔞", "🥵", "🫦", "👅", "😈", "🍒", "🌶️", "🍌", "🥒", "🍯"]
N_TPL = 999
SAMPLE_VIDEO = "https://example.com/video-link"
SAMPLE_TG = "https://t.me/+example"

MEDIA_METHOD = {
    "photo": "sendPhoto",
    "animation": "sendAnimation",
    "video": "sendVideo",
    "document": "sendDocument",
}


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def quote(x: str) -> str:
    return f"<blockquote>{x}</blockquote>"


def unquote_html(x: str) -> str:
    return x.replace("<blockquote>", "<code>").replace("</blockquote>", "</code>")


def plain_len(x: str) -> int:
    return len(html.unescape(re.sub(r"<[^>]+>", "", x)).encode("utf-16-le")) // 2


def fancy(text: str, style: tuple) -> str:
    A, a, d = style
    out = []
    for ch in text:
        if "A" <= ch <= "Z":
            out.append(chr(A + ord(ch) - 65))
        elif "a" <= ch <= "z":
            out.append(chr(a + ord(ch) - 97))
        elif "0" <= ch <= "9":
            out.append(chr(d + ord(ch) - 48))
        else:
            out.append(ch)
    return "".join(out)


def tpl(idx: int) -> dict:
    if idx == 1:
        return dict(
            st=STYLES[0], fl="🦢✨", fr="✨🦢", title="EXCLUSIVE ANIMATION", sep="━" * 18,
            w=("🎬", "WATCH NOW", "👇🔥"), p=("🔐", "PASS LINK", "👇"),
            f=("🍑", "ENJOY THE SHOW", "❤️‍🔥"), b="🔗", qb="🔗 ", sp=True
        )
    r = random.Random(idx * 7919 + 13)
    fl, fr = r.choice(FRAMES)
    return dict(
        st=r.choice(STYLES), fl=fl, fr=fr, title=r.choice(TITLES), sep=r.choice(SEPS),
        w=r.choice(WATCH), p=r.choice(PASS), f=r.choice(FOOT),
        b=r.choice(BULLETS), qb="", sp=False
    )


def _lab(t: dict, tup: tuple) -> str:
    pre, text, suf = tup
    return f"{pre} {esc(fancy(text, t['st']))}" + (f" {suf}" if suf else "")


def pick_tpl(chat_id: int) -> dict:
    return tpl(PREFS.get(chat_id) or random.randint(1, N_TPL))


def pack(t: dict, vids: list, tgs: list, first_limit: int) -> list:
    multi = len(vids) > 1
    head = f"{t['fl']} {esc(fancy(t['title'], t['st']))} {t['fr']}\n{t['sep']}\n{_lab(t, t['w'])}"
    items = [head]
    for i, u in enumerate(vids, 1):
        q = quote(f"{t['qb']}{esc(u)}")
        items.append(f"{t['b']} {fancy(f'Video {i}', t['st'])}\n{q}" if multi else q)
    tail = []
    if tgs:
        if t["sp"]:
            tail.append(t["sep"])
        tail += [_lab(t, t["p"]), quote("\n".join(f"{t['qb']}{esc(u)}" for u in tgs))]
    tail += [t["sep"], _lab(t, t["f"])]
    items.append("\n".join(tail))

    chunks, cur, lim = [], "", first_limit
    for it in items:
        cand = f"{cur}\n{it}" if cur else it
        if cur and plain_len(cand) > lim:
            chunks.append(cur)
            cur, lim = it, 4096
        else:
            cur = cand
    chunks.append(cur)
    return chunks


def is_bot_template(text: str) -> bool:
    """Agar message pehle se bot ke template format mein hai, toh use wapas mat chhedo (Anti-Loop)."""
    if not text:
        return False
    for s in SEPS:
        if s in text:
            for fl, fr in FRAMES:
                if fl in text and fr in text:
                    return True
    return False


def is_tg(u: str) -> bool:
    return urlparse(u).netloc.lower().split(":")[0] in TG_HOSTS


def is_filehost(u: str) -> bool:
    host = urlparse(u).netloc.lower()
    return any(k in host for k in FILEHOSTS)


def extract_urls(message: Message) -> list:
    text = message.text or message.caption or ""
    ents = message.entities or message.caption_entities or []
    found = [(m.start(), m.group(0).rstrip(").,;]")) for m in URL_REGEX.finditer(text)]
    for e in ents:
        u = getattr(e, "url", None)
        if u and u.startswith("http"):
            found.append((e.offset, u))
    out = []
    for _, u in sorted(found, key=lambda x: x[0]):
        if u not in out:
            out.append(u)
    return out


def media_of(m: Message):
    for kind in ("photo", "animation", "video", "document"):
        obj = getattr(m, kind, None)
        if obj:
            return kind, obj.file_id
    return None, None


def is_video_media(m: Message) -> bool:
    if getattr(m, "video", None):
        return True
    d = getattr(m, "document", None)
    return bool(d and str(getattr(d, "mime_type", "") or "").startswith("video/"))


def split_links(urls: list):
    return [u for u in urls if not is_tg(u)][:MAX_POST_LINKS], [u for u in urls if is_tg(u)][:6]


def remember_sent(chat_id: int, msg_id: int):
    if isinstance(msg_id, int):
        SENT_BY_BOT.add((chat_id, msg_id))
        if len(SENT_BY_BOT) > 5000:
            SENT_BY_BOT.clear()


class TgMaybeSent(Exception):
    pass


class TgFlood(Exception):
    def __init__(self, retry: int):
        super().__init__(f"flood {retry}")
        self.retry = retry


def _tg_call(method: str, data: dict):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            json=data, timeout=(15, 90)
        )
    except requests.exceptions.ReadTimeout:
        raise TgMaybeSent("read timeout")
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"bot api http {r.status_code}")
    if j.get("ok"):
        return j.get("result")
    if j.get("error_code") == 429:
        raise TgFlood(int((j.get("parameters") or {}).get("retry_after") or 5))
    raise RuntimeError(j.get("description") or f"bot api {r.status_code}")


async def tg(method: str, **params):
    loop = asyncio.get_running_loop()
    for _ in range(3):
        try:
            return await loop.run_in_executor(None, _tg_call, method, params)
        except TgFlood as e:
            await asyncio.sleep(e.retry + 1)
    return await loop.run_in_executor(None, _tg_call, method, params)


async def safe(fn, *a, **k):
    for _ in range(3):
        try:
            return await fn(*a, **k)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
    return await fn(*a, **k)


def reply_params(rid: int) -> dict:
    return {"message_id": rid, "allow_sending_without_reply": True}


async def out_text(client, chat: int, rid: int, text: str):
    try:
        res = await tg(
            "sendMessage", chat_id=chat, text=text, parse_mode="HTML",
            reply_parameters=reply_params(rid), link_preview_options={"is_disabled": True}
        )
        if isinstance(res, dict) and res.get("message_id"):
            remember_sent(chat, res["message_id"])
        return res
    except TgMaybeSent:
        return None
    except Exception as e:
        logging.warning(f"[botapi] text fallback: {str(e)[:120]}")
    m = await safe(
        client.send_message, chat, unquote_html(text),
        reply_to_message_id=rid, disable_web_page_preview=True
    )
    if getattr(m, "id", None):
        remember_sent(chat, m.id)
    return m


async def out_media(client, chat: int, rid: int, kind: str, fid: str, caption: str):
    try:
        res = await tg(
            MEDIA_METHOD[kind], chat_id=chat, **{kind: fid}, caption=caption,
            parse_mode="HTML", reply_parameters=reply_params(rid)
        )
        if isinstance(res, dict) and res.get("message_id"):
            remember_sent(chat, res["message_id"])
        return res
    except TgMaybeSent:
        return None
    except Exception as e:
        logging.warning(f"[botapi] {kind} fallback: {str(e)[:120]}")
    sender = {
        "photo": client.send_photo,
        "animation": client.send_animation,
        "video": client.send_video,
        "document": client.send_document,
    }[kind]
    m = await safe(sender, chat, fid, caption=unquote_html(caption), reply_to_message_id=rid)
    if getattr(m, "id", None):
        remember_sent(chat, m.id)
    return m


async def send_chunks(client, message: Message, kind: str, fid: str, chunks: list):
    chat, rid = message.chat.id, message.id
    first, rest = chunks[0], chunks[1:]
    if kind:
        fits = plain_len(first) <= 1024
        await out_media(client, chat, rid, kind, fid, first if fits else "")
        if not fits:
            rest = [first] + rest
    else:
        await out_text(client, chat, rid, first)
    for c in rest:
        await asyncio.sleep(0.8)
        await out_text(client, chat, rid, c)
    return True


async def post_message(client, message: Message, urls: list, kind: str, fid: str):
    """Jitne message user bhejega, exact utne hi template post banenge (1-to-1)."""
    vids, tgs = split_links(urls)
    if not vids and not tgs:
        m = await safe(message.reply_text, "🔗 Is message mein koi link nahi mila.", quote=True)
        if getattr(m, "id", None):
            remember_sent(message.chat.id, m.id)
        return
    t = pick_tpl(message.chat.id)
    limit = 1024 if kind else 4096
    await send_chunks(client, message, kind, fid, pack(t, vids, tgs, limit))


async def album_post(client, message: Message):
    chat, key, now = message.chat.id, (message.chat.id, message.media_group_id), time.time()
    for k in [k for k, v in list(ALBUMS.items()) if now - v > 120]:
        ALBUMS.pop(k, None)
    if key in ALBUMS:
        return
    ALBUMS[key] = now
    await asyncio.sleep(1.2)
    group = sorted(await client.get_media_group(chat, message.id), key=lambda m: m.id)
    anchor = group[0]
    urls = []
    for m in group:
        for u in extract_urls(m):
            if u not in urls:
                urls.append(u)
    vids, tgs = split_links(urls)
    if not vids and not tgs:
        m = await safe(anchor.reply_text, "🔗 Is album mein koi link nahi mila.", quote=True)
        if getattr(m, "id", None):
            remember_sent(chat, m.id)
        return
    chunks = pack(pick_tpl(chat), vids, tgs, 1024)
    cls = {"photo": InputMediaPhoto, "video": InputMediaVideo,
           "document": InputMediaDocument, "animation": InputMediaDocument}
    metas = [(k, f) for k, f in (media_of(m) for m in group) if k in cls]
    if not metas:
        return await send_chunks(client, anchor, None, None, chunks)
    fits = plain_len(chunks[0]) <= 1024
    items = [
        {"type": k, "media": f, **({"caption": chunks[0], "parse_mode": "HTML"} if (i == 0 and fits) else {})}
        for i, (k, f) in enumerate(metas)
    ]
    try:
        res = await tg("sendMediaGroup", chat_id=chat, media=items, reply_parameters=reply_params(anchor.id))
        for r in res if isinstance(res, list) else []:
            if isinstance(r, dict) and r.get("message_id"):
                remember_sent(chat, r["message_id"])
    except Exception:
        media = [
            cls[k](f, caption=unquote_html(chunks[0]) if (i == 0 and fits) else "", parse_mode=enums.ParseMode.HTML)
            for i, (k, f) in enumerate(metas)
        ]
        sent = await safe(client.send_media_group, chat, media, reply_to_message_id=anchor.id)
        for r in sent if isinstance(sent, list) else []:
            if getattr(r, "id", None):
                remember_sent(chat, r.id)
    for c in (chunks[1:] if fits else chunks):
        await asyncio.sleep(0.8)
        await out_text(client, chat, anchor.id, c)


async def template_reply(message: Message, arg: str):
    chat = message.chat.id
    if arg.isdigit() and arg != "0":
        n = int(arg)
        if not 1 <= n <= N_TPL:
            m = await safe(message.reply_text, f"🎨 Template number 1 se {N_TPL} ke beech likho.", quote=True)
            if getattr(m, "id", None):
                remember_sent(chat, m.id)
            return
        PREFS[chat] = n
        head = f"✅ Template <code>{n}</code> set. Ab har post isi style mein aayegi.\n\n"
    elif arg == "0":
        PREFS.pop(chat, None)
        head = "🎲 Random template mode on.\n\n"
    else:
        head = (
            f"🎨 <b>{N_TPL} templates</b> hain. <code>template 5</code> likho fix karne ke liye, "
            f"<code>template random</code> se wapas random. Sample:\n\n"
        )
    sample = "\n".join(pack(tpl(PREFS.get(chat) or random.randint(1, N_TPL)), [SAMPLE_VIDEO], [SAMPLE_TG], 4096))
    await out_text(message._client, chat, message.id, head + sample)
