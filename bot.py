import os
import re
import sys
import math
import random
import json
import time
import html
import shutil
import asyncio
import logging
import secrets
import threading
import subprocess
import http.cookiejar
from datetime import datetime
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from urllib.parse import urlparse, urljoin, unquote

if os.environ.get("AUTO_UPDATE", "1") == "1":
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "-q", "yt-dlp[default,curl-cffi]"],
            capture_output=True, timeout=150
        )
    except Exception:
        pass

import yt_dlp
import requests
import cloudscraper
from pyrogram import Client, filters, enums, idle
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    InputMediaPhoto, InputMediaVideo, InputMediaDocument,
)
from pyrogram.errors import FloodWait
from playwright.async_api import async_playwright

try:
    import google.generativeai as genai
except Exception:
    genai = None

API_ID = int(os.environ.get("API_ID", 0) or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", 2))
MiB = 1024 * 1024
UPLOAD_LIMIT = int(os.environ.get("UPLOAD_LIMIT_MB", 1990)) * MiB
COMPRESS_TARGET = int(os.environ.get("COMPRESS_TARGET_MB", 1900)) * MiB
MAX_HEIGHT = 4320
FALLBACK_HEIGHT = 720
TIERS = (MAX_HEIGHT, FALLBACK_HEIGHT)
STANDARD_QUALITIES = (2160, 1440, 1080, 720, 480, 360)
COOKIES_FILE = "cookies.txt"
ALLOWED = {int(x) for x in re.findall(r"\d+", os.environ.get("ALLOWED_IDS", ""))}
PROXY_URL = os.environ.get("PROXY_URL", "")
FORCE_IPV4 = os.environ.get("FORCE_IPV4", "0") == "1"
HEADLESS = os.environ.get("HEADLESS", "1") == "1"

DOWNLOAD_DIR = "./downloads/"
THUMB_DIR = "./thumbs/"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

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

ai_model = None
if GEMINI_API_KEY and genai:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        ai_model = genai.GenerativeModel(GEMINI_MODEL)
        logging.info(f"✅ Gemini ready ({GEMINI_MODEL})")
    except Exception as e:
        logging.error(f"Gemini init failed: {e}")
else:
    logging.warning("⚠️ Gemini disabled (key/lib missing) — baaki engines chalenge")

JOBS = {}
ANALYZE_SEM = None
DL_SEM = None
ENC_SEM = None
active_downloads = 0
analyzing = 0
NO_IMP = False
LAST = {}
FAILS = {}
PREFS = {}
ALBUMS = {}
SEEN = {}
TRACK = {}
CJOBS = {}
CONFIRMED = set()
LOST = set()
MSG_LOCKS = {}
INSTANCE = secrets.token_hex(2)
CLAIM_WAIT = float(os.environ.get("CLAIM_WAIT", 1.0))
START_TS = 0.0
STALE_SEC = int(os.environ.get("STALE_SEC", 25))
MAX_POST_LINKS = 60
BG_TASKS = set()
URL_REGEX = re.compile(r'https?://[^\s<>"\']+')
TIME_PAT = r"\d{1,2}(?::\d{1,2}){1,2}"
RANGE_RE = re.compile(rf"(?<![\d:])({TIME_PAT})\s*(?:-|–|—|to|se)\s*({TIME_PAT})(?![\d:])", re.I)


def spawn(coro):
    t = asyncio.create_task(coro)
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)
    return t


SITE_SELECTORS = {
    "rule34video.com": {
        "download_selectors": [
            'a[href*="/get_file/"]', 'a[href*="download"]', 'a[href*=".mp4"]',
            'a:has-text("1080p")', 'a:has-text("720p")', 'a:has-text("480p")',
            'a:has-text("Download")', 'a[download]', '.download-button',
            '.btn-download', '#download', '#btn-download',
        ],
        "title_selector": "h1, .video-title, .title",
    },
    "default": {
        "download_selectors": [
            'a[href*="download"]', 'a[href*=".mp4"]', 'a[href*=".m3u8"]',
            'a:has-text("Download")', 'a:has-text("Download Video")',
            'a[download]', '.download-button', '.btn-download', '#download',
        ],
        "title_selector": "h1, title",
    },
}
UNIVERSAL_SELECTORS = [
    'a[href*="2160"]', 'a[href*="1440"]', 'a[href*="1080"]', 'a[href*="720"]',
    'a[href*="480"]', 'a[href*="360"]', 'a[href*="get_file"]', 'a[href*=".mp4"]',
    'a[href*="download"]', 'a:has-text("1080p")', 'a:has-text("720p")',
    'a:has-text("480p")', 'a:has-text("MP4")', 'a:has-text("Download")',
    'a[download]', '.download-button', '.btn-download', '#download', '#btn-download',
]
PLAY_SELECTORS = [
    '.vjs-big-play-button', '.jw-icon-display', '.plyr__control--overlaid',
    'button[aria-label*="play" i]', '.play-button', '.fp-play', '[class*="play-btn"]',
    '[class*="PlayButton"]', '.ytp-large-play-button', 'video',
]
CONSENT_SELECTORS = [
    'button:has-text("I am 18")', 'button:has-text("I\'m 18")', 'a:has-text("I am 18")',
    'button:has-text("Enter")', 'a:has-text("Enter")', 'button:has-text("I Agree")',
    'button:has-text("Accept")', 'button:has-text("Continue")', '#age-verify-yes',
]
PLAY_JS = """() => { document.querySelectorAll('video').forEach(v => {
    try { v.muted = true; const p = v.play(); if (p && p.catch) p.catch(()=>{}); } catch(e){} }); }"""

STEALTH_JS = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});
window.chrome=window.chrome||{runtime:{}};
const _q=navigator.permissions&&navigator.permissions.query;
if(_q){navigator.permissions.query=p=>p&&p.name==='notifications'?Promise.resolve({state:Notification.permission}):_q.call(navigator.permissions,p)}
"""
AGE_JS = r"""() => {
  const bad = /\b(exit|leave|no|under|not|decline|cancel|close)\b/i;
  const good = /(\b(i\s*am|i'm|im|yes)\b[^.]{0,30}\b(18|21)\b)|\b18\s*\+|\b(over|above)\s*(18|21)\b|\b(enter|agree|accept|continue|confirm|proceed)\b/i;
  const txt = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim();
  const els = [...document.querySelectorAll('button,a,input[type=button],input[type=submit],[role=button],.btn,[class*=enter],[class*=agree],[id*=age],[class*=age]')];
  const cand = els.filter(e => {
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    const t = txt(e);
    return t && t.length <= 60 && good.test(t) && !bad.test(t);
  });
  cand.sort((a, b) => (/18|21/.test(txt(b)) ? 1 : 0) - (/18|21/.test(txt(a)) ? 1 : 0));
  if (cand[0]) { const t = txt(cand[0]); cand[0].click(); return t; }
  return '';
}"""
AGE_COOKIES = (
    "age_verified", "ageverified", "age_check", "agecheck", "over18", "is_adult", "adult",
    "agreed", "age_gate", "age-verified", "isAdult", "consent", "splash_i"
)
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif', '.svg', '.ico', '.bmp', '.avif')
VIDEO_FILE_EXTS = ('.mp4', '.webm', '.mkv', '.mov', '.m4v', '.avi', '.flv')
SEGMENT_EXTS = ('.ts', '.m4s', '.aac', '.m4a', '.vtt', '.srt', '.key', '.mp3')
BAD_KEYWORDS = ('preview', 'thumb', 'poster', 'sprite', 'trailer', 'placeholder', 'banner')
SEG_RE = re.compile(r'(?:^|[/_\-.])(?:seg(?:ment)?|chunk|frag(?:ment)?)[-_]?\d+', re.I)


def get_site_config(url: str) -> dict:
    domain = urlparse(url).netloc.lower().replace("www.", "")
    for site, cfg in SITE_SELECTORS.items():
        if site != "default" and site in domain:
            return cfg
    return SITE_SELECTORS["default"]


def esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def humanbytes(size) -> str:
    size = float(size or 0)
    if size <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


def fmt_dur(sec) -> str:
    sec = int(round(max(0, sec or 0)))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_hms(sec) -> str:
    sec = int(round(max(0, sec or 0)))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def to_sec(t: str) -> float:
    n = 0
    for x in t.split(":"):
        n = n * 60 + int(x)
    return float(n)


def parse_range(text):
    m = RANGE_RE.search(text or "")
    if not m:
        return None
    a, b = to_sec(m.group(1)), to_sec(m.group(2))
    return (a, b) if b > a else None


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name or "")
    name = re.sub(r'\s+', ' ', name).strip(" .")
    return name[:120] or "video"


def url_path(u: str) -> str:
    return urlparse(u).path.lower()


def is_image_url(u: str) -> bool:
    return url_path(u).endswith(IMAGE_EXTS)


def is_bad_url(u: str) -> bool:
    low = u.lower()
    return any(b in low for b in BAD_KEYWORDS)


def classify_media(url: str, ctype: str = "", total: int = 0):
    p = url_path(url)
    ctype = (ctype or "").lower()
    if "mp2t" in ctype or "iso.segment" in ctype:
        return None
    if p.endswith(SEGMENT_EXTS) or SEG_RE.search(p):
        return None
    if p.endswith(".m3u8") or "mpegurl" in ctype:
        return "hls"
    if p.endswith(".mpd") or "dash+xml" in ctype:
        return "dash"
    if ctype.startswith("video/") or p.endswith(VIDEO_FILE_EXTS):
        return "mp4"
    if any(k in p for k in ("/get_file/", "/get_video/", "videoplayback", "/mp4/", "/videos/")):
        return "mp4"
    if ctype == "application/octet-stream" and total > 2 * MiB:
        return "mp4"
    return None


def name_from_url(u: str) -> str:
    try:
        base = os.path.basename(unquote(urlparse(u).path.rstrip("/")))
        base = os.path.splitext(base)[0]
        return sanitize_filename(base) if base else ""
    except Exception:
        return ""


def origin_of(u: str) -> str:
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}"


def make_headers(page_url: str, origin: bool = True) -> dict:
    h = {
        "User-Agent": UA, "Referer": page_url, "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if origin:
        h["Origin"] = origin_of(page_url)
    return h


def _hdr_str(headers: dict) -> str:
    return "".join(f"{k}: {v}\r\n" for k, v in headers.items())


def cookies_ok(path: str = COOKIES_FILE) -> bool:
    if not os.path.exists(path):
        return False
    try:
        try:
            from yt_dlp.cookies import YoutubeDLCookieJar
            jar = YoutubeDLCookieJar(path)
        except ImportError:
            jar = http.cookiejar.MozillaCookieJar(path)
        jar.load(ignore_discard=True, ignore_expires=True)
        return True
    except Exception as e:
        logging.warning(f"cookies.txt ignore kiya (format galat): {str(e)[:120]}")
        return False


COOKIES_USABLE = cookies_ok()


def load_cookie_list(path: str = COOKIES_FILE) -> list:
    out = []
    if not os.path.exists(path):
        return out
    try:
        cj = http.cookiejar.MozillaCookieJar(path)
        cj.load(ignore_discard=True, ignore_expires=True)
        for c in cj:
            out.append({
                "name": c.name, "value": c.value, "domain": c.domain,
                "path": c.path or "/", "secure": bool(c.secure),
                "expires": c.expires if c.expires else -1,
            })
    except Exception as e:
        logging.warning(f"cookies.txt load error: {e}")
    return out


def cookie_header(url: str, cookies: list) -> str:
    host = urlparse(url).netloc.split(":")[0].lower()
    pairs = []
    for c in cookies or []:
        d = (c.get("domain") or "").lstrip(".").lower()
        if d and (host == d or host.endswith("." + d)):
            pairs.append(f"{c['name']}={c['value']}")
    return "; ".join(pairs)


def make_session(headers: dict, cookies: list):
    s = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "desktop": True})
    s.headers.update(headers or {})
    if PROXY_URL:
        s.proxies = {"http": PROXY_URL, "https": PROXY_URL}
    for c in cookies or []:
        try:
            s.cookies.set(c["name"], c["value"], domain=c.get("domain") or None, path=c.get("path") or "/")
        except Exception:
            pass
    return s


@lru_cache(maxsize=8)
def impersonate_target(name="chrome"):
    try:
        import curl_cffi  # noqa: F401  (availability probe: yt-dlp impersonation needs it)
        from yt_dlp.networking.impersonate import ImpersonateTarget
        return ImpersonateTarget.from_str(name)
    except Exception:
        return None


def age_cookies_for(url):
    host = urlparse(url).netloc.split(":")[0].lower()
    parts = host.split(".")
    dom = "." + ".".join(parts[-2:]) if len(parts) >= 2 else host
    return [
        http.cookiejar.Cookie(0, n, "1", None, False, dom, True, True, "/", True, False, None, False, None, None, {})
        for n in AGE_COOKIES
    ]


IMP_LADDER = ("chrome", "safari", "edge", "")
BLOCK_HINTS = (
    "403", "forbidden", "cloudflare", "429", "captcha", "challenge", "timed out", "timeout",
    "unable to download", "connection", "http error 5", "ssl", "sign in", "bot"
)


def why(err):
    m = (err or "").lower()
    if "unsupported url" in m:
        return "yt-dlp is page ko nahi pehchanta aur browser se bhi video link nahi mila."
    if "geo" in m or "your country" in m or "not available in" in m:
        return "Country/region block hai. Isme PROXY_URL lagana padega."
    if "sign in" in m or "log in" in m or "login" in m or "private" in m or "members" in m or "premium" in m:
        return "Is video ke liye login/cookies chahiye. Browser se cookies.txt export karke COOKIES secret mein daalo."
    if "403" in m or "forbidden" in m or "cloudflare" in m or "captcha" in m or "challenge" in m:
        return "Site ne bot/server IP ko block kiya hai. PROXY_URL ya cookies.txt try karo."
    if "429" in m:
        return "Site ne zyada requests ki wajah se rok diya, thodi der baad try karo."
    if "timed out" in m or "timeout" in m:
        return "Site bahut slow hai ya server tak pahunch nahi paa raha (timeout)."
    if "404" in m or "not found" in m or "removed" in m or "deleted" in m:
        return "Video hata di gayi hai ya link galat hai."
    return ""


@dataclass
class Source:
    page_url: str
    dl_url: str
    mode: str
    headers: dict
    cookies: list
    title: str = "video"
    duration: float = 0.0
    height: int = 0
    size: int = 0
    formats: list = field(default_factory=list)
    est: dict = field(default_factory=dict)
    quals: list = field(default_factory=list)
    thumbs: list = field(default_factory=list)
    use_url_name: bool = False
    imp: str = "chrome"
    alt_urls: dict = field(default_factory=dict)  # {height: direct_url}


@dataclass
class Job:
    id: str
    chat_id: int
    src: Source
    msg: Message = None
    created: float = field(default_factory=time.time)
    cs: float = 0.0
    ce: float = 0.0
    sel: str = "s"
    mode: str = "exact"
    fresh: bool = True
    busy: bool = False
    thumb_path: str = ""
    thumb_sent: bool = False
    reply_to: int = None
    q: int = 0


@dataclass
class Intent:
    action: str = "card"
    q: int = 0
    start: float = None
    end: float = None
    mode: str = ""
    reply: str = ""
    arg: str = ""


def ffprobe(target, headers=None, timeout=40):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams"]
    if str(target).startswith("http"):
        cmd += ["-rw_timeout", "20000000"]
        if PROXY_URL:
            cmd += ["-http_proxy", PROXY_URL]
        if headers:
            cmd += ["-headers", _hdr_str(headers)]
    cmd.append(str(target))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        data = json.loads(r.stdout or "{}")
    except Exception:
        return None
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if not v and not a:
        return None

    def num(x, d=0.0):
        try:
            return float(x)
        except Exception:
            return d

    fps = 0.0
    try:
        n, d = (v or {}).get("avg_frame_rate", "0/1").split("/")
        fps = float(n) / float(d) if float(d) else 0.0
    except Exception:
        pass
    w, h = int((v or {}).get("width") or 0), int((v or {}).get("height") or 0)
    dur = num(fmt.get("duration")) or num((v or {}).get("duration"))
    size = int(num(fmt.get("size")))
    a_bps = int(num((a or {}).get("bit_rate")))
    v_bps = int(num((v or {}).get("bit_rate")))
    if v and not v_bps and dur and size:
        v_bps = max(int(size * 8 / dur) - a_bps, 0)
    return {
        "duration": dur, "size": size,
        "width": w, "height": h, "short": min(w, h) if w and h else 0,
        "vcodec": (v or {}).get("codec_name"), "pix_fmt": (v or {}).get("pix_fmt"),
        "fps": fps, "v_bps": v_bps, "ctrans": (v or {}).get("color_transfer"),
        "acodec": (a or {}).get("codec_name"), "a_bps": a_bps,
        "ar": int(num((a or {}).get("sample_rate"))), "ch": int((a or {}).get("channels") or 0),
    }


def run_ffmpeg(args, duration=0.0, on_progress=None):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats"] + [str(a) for a in args]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    err = []
    t = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
    t.start()
    try:
        for line in p.stdout:
            if on_progress and duration and line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    cur = int(line.split("=", 1)[1]) / 1_000_000
                    on_progress(min(max(cur, 0), duration), duration)
                except Exception:
                    pass
        p.wait()
    finally:
        t.join(timeout=5)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg fail: {(err[0] if err else '')[-400:]}")


def keyframe_times(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "packet=pts_time,flags", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=900,
    )
    out = []
    for line in r.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) >= 2 and "K" in parts[1]:
            try:
                out.append(float(parts[0]))
            except ValueError:
                pass
    return sorted(out)


def grab_frame(src, out_jpg, at=5.0, headers=None, width=None, quality=2):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-nostdin"]
    if str(src).startswith("http") and headers:
        cmd += ["-headers", _hdr_str(headers)]
    cmd += ["-ss", f"{max(at, 0):.2f}", "-i", str(src), "-frames:v", "1", "-q:v", str(quality)]
    if width:
        cmd += ["-vf", f"scale={width}:-2"]
    cmd.append(out_jpg)
    subprocess.run(cmd, capture_output=True, timeout=90)
    return os.path.exists(out_jpg) and os.path.getsize(out_jpg) > 1000


def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass


def ensure_compatible(path, target_h=0, on_progress=None):
    info = ffprobe(path)
    if not info:
        raise RuntimeError("Downloaded file invalid hai (ffprobe fail)")
    ok_v = info["vcodec"] == "h264" and info["pix_fmt"] in ("yuv420p", "yuvj420p")
    ok_a = info["acodec"] in (None, "aac", "mp3")
    need_scale = bool(target_h and 0 < target_h < info["short"] - 20)

    if ok_v and ok_a and path.lower().endswith(".mp4") and not need_scale:
        return path

    out = os.path.splitext(path)[0] + "_tg.mp4"
    maps = ["-map", "0:v:0", "-map", "0:a:0?"]
    if ok_v and ok_a and not need_scale:
        args = ["-i", path] + maps + ["-c", "copy"]
    elif ok_v and not need_scale:
        args = ["-i", path] + maps + ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k"]
    else:
        W, H = info["width"], info["height"]
        vf = []
        if need_scale:
            scale_expr = f"scale=-2:{target_h}" if W >= H else f"scale={target_h}:-2"
            vf = ["-vf", scale_expr]
        enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p"] + vf
        if info["v_bps"] and not need_scale:
            cap = int(max(info["v_bps"] * 1.5, 1_500_000))
            enc += ["-maxrate", str(cap), "-bufsize", str(cap * 2)]
        args = ["-i", path] + maps + enc + ["-c:a", "aac", "-b:a", "128k"]
    run_ffmpeg(args + ["-movflags", "+faststart", out], info["duration"], on_progress)
    _rm(path)
    return out


def _venc(info, crf):
    o = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"]
    vb = info["v_bps"]
    if vb:
        vb = max(vb, 600_000)
        o += ["-maxrate", str(vb), "-bufsize", str(vb * 2)]
    return o


def _assemble(parts, src, dst, a_ss, a_dur, has_a, dur):
    lst = dst + ".list.txt"
    with open(lst, "w") as f:
        for p in parts:
            f.write("file '" + os.path.abspath(p).replace("'", "'\\''") + "'\n")
    args = ["-f", "concat", "-safe", "0", "-i", lst]
    if has_a:
        args += ["-ss", f"{a_ss:.3f}", "-t", f"{a_dur:.3f}", "-i", src]
    args += ["-map", "0:v:0"] + (["-map", "1:a:0"] if has_a else [])
    args += ["-c", "copy", "-t", f"{dur:.3f}", "-movflags", "+faststart", dst]
    try:
        run_ffmpeg(args, dur)
    finally:
        for p in parts + [lst]:
            _rm(p)


def smart_cut(src, dst, start, end, info, cb=None):
    dur = end - start
    has_a = bool(info["acodec"])
    enc = _venc(info, 18)
    prog = (lambda c, t: cb.update("✂️ Cutting", c, t, "time")) if cb else None
    kfs = keyframe_times(src) if info["vcodec"] == "h264" and dur > 40 else []
    k1 = next((k for k in kfs if k >= start), None)
    k2 = max((k for k in kfs if k <= end), default=None)
    if k1 is None or k2 is None or k2 - k1 < 6:
        run_ffmpeg(["-ss", f"{start:.3f}", "-i", src, "-t", f"{dur:.3f}", "-map", "0:v:0", "-map", "0:a:0?"] + enc +
                   (["-c:a", "copy"] if has_a else ["-an"]) + ["-movflags", "+faststart", dst], dur, prog)
        return
    work = os.path.dirname(dst) or "."
    a, b, c = (os.path.join(work, n) for n in ("cut_a.ts", "cut_b.ts", "cut_c.ts"))
    vmap = ["-map", "0:v:0", "-an"]
    parts = []
    if k1 - start > 0.05:
        run_ffmpeg(["-ss", f"{start:.3f}", "-i", src, "-t", f"{k1 - start:.3f}"] + vmap + enc + ["-f", "mpegts", a])
        parts.append(a)
    run_ffmpeg(["-ss", f"{k1 + 0.005:.3f}", "-i", src, "-t", f"{k2 - k1:.3f}"] + vmap +
               ["-c:v", "copy", "-f", "mpegts", b], k2 - k1, prog)
    parts.append(b)
    if end - k2 > 0.05:
        run_ffmpeg(["-ss", f"{k2:.3f}", "-i", src, "-t", f"{end - k2:.3f}"] + vmap + enc + ["-f", "mpegts", c])
        parts.append(c)
    _assemble(parts, src, dst, start, dur, has_a, dur)


def cut_video(src, dst, start, end, mode, info, cb=None):
    dur = max(0.5, end - start)
    if mode == "fast":
        run_ffmpeg(["-ss", f"{start:.3f}", "-i", src, "-t", f"{dur:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
                    "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", dst],
                   dur, (lambda c, t: cb.update("✂️ Cutting", c, t, "time")) if cb else None)
        out = ffprobe(dst)
        if out and out["duration"] >= dur * 0.5:
            return "fast"
    smart_cut(src, dst, start, end, info, cb)
    return "exact"


def compress_to_fit(src, dst, target, on_progress=None):
    info = ffprobe(src)
    if not info or info["duration"] <= 0:
        raise RuntimeError("Compress: duration nahi mili")
    if info["vcodec"] == "h264" and info["pix_fmt"] in ("yuv420p", "yuvj420p"):
        try:
            if _compress_partial(src, dst, info, target, on_progress):
                return "partial"
        except Exception as e:
            logging.warning(f"[compress] partial fail ({e}) -> full")
    _compress_full(src, dst, info, target, on_progress)
    return "full"


def _compress_partial(src, dst, info, target, cb):
    D = info["duration"]
    S = os.path.getsize(src)
    kfs = keyframe_times(src)
    if len(kfs) < 6:
        return False
    has_a = bool(info["acodec"])
    a_old = info["a_bps"] or (128_000 if has_a else 0)
    a_new = a_old
    B = S * 8 / D
    v_old = max(B - a_old, 300_000)
    delta = S * 8 - target * 8
    MAXF = 0.90
    vm = 0.60 * v_old
    vmin = max(700_000, 0.30 * v_old)

    def need(v):
        save = B - (v + a_new)
        return delta * 1.06 / save if save > 0 else float("inf")

    Dm = need(vm)
    if Dm > MAXF * D:
        save = delta * 1.06 / (MAXF * D)
        vm = B - a_new - save
        if vm < vmin:
            return False
        Dm = MAXF * D

    work = os.path.dirname(dst) or "."
    for _ in range(3):
        mid_start = (D - Dm) / 2
        k1 = next((k for k in kfs if k >= mid_start), None)
        k2 = next((k for k in kfs if k >= mid_start + Dm), None)
        if k1 is None or k2 is None or k2 - k1 < 2 or k2 > D - 0.5:
            return False
        _build_spliced(src, dst, info, k1, k2, vm, work, cb)
        out = ffprobe(dst)
        size = os.path.getsize(dst)
        if not out or abs(out["duration"] - D) > max(3.0, D * 0.02):
            return False
        if size <= target:
            return True
        excess = (size - target * 0.98) * 8
        Dr = k2 - k1
        vm_new = vm - excess / Dr
        if vm_new >= vmin:
            vm = vm_new
        else:
            remaining = excess - (vm - vmin) * Dr
            vm = vmin
            Dm = Dr + remaining / max(B - vmin - a_new, 1)
            if Dm > MAXF * D:
                return False
    return False


def _build_spliced(src, dst, info, k1, k2, vm, work, cb):
    head, mid, tail = (os.path.join(work, n) for n in ("seg_head.ts", "seg_mid.ts", "seg_tail.ts"))
    vmap = ["-map", "0:v:0", "-an"]
    D = info["duration"]
    run_ffmpeg(["-i", src, "-t", f"{k1:.3f}"] + vmap + ["-c:v", "copy", "-f", "mpegts", head])
    run_ffmpeg(["-ss", f"{k2 + 0.005:.3f}", "-i", src] + vmap + ["-c:v", "copy", "-f", "mpegts", tail])
    vb = int(vm)
    run_ffmpeg(["-ss", f"{k1:.3f}", "-i", src, "-t", f"{k2 - k1:.3f}"] + vmap +
               ["-c:v", "libx264", "-preset", "veryfast", "-b:v", str(vb), "-maxrate", str(int(vb * 1.3)),
                "-bufsize", str(vb * 2), "-pix_fmt", "yuv420p", "-f", "mpegts", mid],
               k2 - k1, (lambda c, t: cb.update("🗜 Compressing (beech ka hissa)", c, t, "time")) if cb else None)
    _assemble([head, mid, tail], src, dst, 0.0, D, bool(info["acodec"]), D)


def _compress_full(src, dst, info, target, cb):
    D = info["duration"]
    has_a = bool(info["acodec"])
    a = 128_000 if has_a else 0
    v = max(int(target * 8 * 0.96 / D - a), 200_000)
    scale = []
    if info["short"] > 720 and v < 1_800_000:
        scale = ["-vf", "scale=-2:720"]
    for _ in range(3):
        args = ["-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
                "-b:v", str(v), "-maxrate", str(int(v * 1.3)), "-bufsize", str(v * 2), "-pix_fmt", "yuv420p"] + scale
        args += (["-c:a", "aac", "-b:a", "128k"] if has_a else ["-an"])
        run_ffmpeg(args + ["-movflags", "+faststart", dst], D,
                   (lambda c, t: cb.update("🗜 Compressing", c, t, "time")) if cb else None)
        size = os.path.getsize(dst)
        if size <= target:
            return
        v = int(v * (target / size) * 0.93)
    raise RuntimeError("Compress ke baad bhi file 2GB se badi hai")


def extract_audio(path, out, title, cb=None):
    info = ffprobe(path)
    if not info or not info["acodec"]:
        raise RuntimeError("Is video mein audio nahi hai")
    a = ["-c:a", "copy"] if info["acodec"] == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    run_ffmpeg(["-i", path, "-vn", "-map", "0:a:0"] + a +
               ["-metadata", f"title={title}", "-movflags", "+faststart", out], info["duration"], cb)
    return out, ffprobe(out) or info


CRF_RANGE = {"h264": (18, 30), "h265": (20, 34)}


def parse_compress_opts(text):
    t = (text or "").lower()
    # Fixed Smart Compression Defaults: H.264 + SSIM 0.985 (Visual Lossless Quality + Maximum Safe Size Reduction)
    o = {"codec": "h264", "target": 0.985, "tune": "", "max_h": 0}
    if re.search(r"\b(hevc|h\.?265|x265)\b", t):
        o["codec"] = "h265"
    if re.search(r"\b(max\s*quality|best\s*quality|high\s*quality|lossless)\b", t):
        o["target"] = 0.992
    elif re.search(r"\b(extreme|tiny|smallest|max\s*compress)\b", t):
        o["target"] = 0.972
    if re.search(r"\b(anime|animation|cartoon)\b", t):
        o["tune"] = "animation"
    if re.search(r"\b720p\b", t):
        o["max_h"] = 720
    return o


def pick_preset(duration, pixels_per_sec, codec):
    work = duration * pixels_per_sec / (1920 * 1080 * 30)
    if codec == "h265":
        work *= 3
    return "slow" if work <= 150 else "medium" if work <= 700 else "fast" if work <= 2400 else "veryfast"


def detect_animation(path, duration):
    counts = []
    for f in (0.15, 0.35, 0.55, 0.75, 0.9):
        try:
            r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{duration * f:.2f}", "-i", path, "-frames:v", "1",
                                "-vf", "scale=64:36:flags=area", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                               capture_output=True, timeout=60)
        except Exception:
            continue
        b = r.stdout
        if len(b) >= 64 * 36 * 3:
            counts.append(len({(b[i] >> 4, b[i + 1] >> 4, b[i + 2] >> 4) for i in range(0, 64 * 36 * 3, 3)}))
    avg = sum(counts) / len(counts) if counts else 9999
    return avg < 160


def _venc_args(codec, crf, preset, tune, cap, pix):
    if codec == "h265":
        a = ["-c:v", "libx265", "-preset", preset, "-crf", str(crf), "-pix_fmt", pix, "-tag:v", "hvc1",
             "-x265-params", "log-level=error"]
    else:
        a = ["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", pix, "-profile:v", "high"]
    if tune:
        a += ["-tune", tune]
    if cap:
        a += ["-maxrate", str(cap), "-bufsize", str(cap * 2)]
    return a


def _audio_args(info):
    if not info["acodec"]:
        return ["-an"]
    if info["acodec"] == "aac" and (not info["a_bps"] or info["a_bps"] <= 160_000):
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "128k"]


def _sample_pts(duration, n=3, length=5.0):
    if duration <= 20:
        return [(0.0, min(duration, 15.0))]
    return [(max(0.0, duration * (i + 1) / (n + 1) - length / 2), length) for i in range(n)]


def _ssim(src, t, length, sample, vf):
    ref = f"[0:v]{vf + ',' if vf else ''}setpts=PTS-STARTPTS[a]"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-ss", f"{t:.2f}", "-t", f"{length:.2f}", "-i", src,
                        "-i", sample, "-lavfi", f"{ref};[1:v]setpts=PTS-STARTPTS[b];[a][b]ssim", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=900)
    m = re.search(r"All:([0-9.]+)", r.stderr)
    return float(m.group(1)) if m else 0.0


def smart_compress(src, dst, prog, opts):
    info = ffprobe(src)
    if not info or not info["vcodec"] or info["duration"] <= 0:
        raise RuntimeError("Is file mein video nahi mili")
    S, D = os.path.getsize(src), info["duration"]
    if info.get("ctrans") in ("smpte2084", "arib-std-b67"):
        return {"skipped": True, "reason": "HDR video hai, compress se rang bigad sakte hain, isliye original rakha"}
    codec, target, tune = opts.get("codec", "h264"), opts.get("target", 0.985), opts.get("tune", "")
    W, H = info["width"], info["height"]
    mh = opts.get("max_h", 0)
    vb = info["v_bps"] or max(int(S * 8 / D) - (info["a_bps"] or 0), 1)
    if not tune and detect_animation(src, D):
        tune = "animation"
    preset = pick_preset(D, W * H * (info["fps"] or 30), codec)
    sample_preset = "fast" if preset in ("slow", "medium") else preset
    pix = "yuv420p10le" if codec == "h265" and "10" in (info["pix_fmt"] or "") else "yuv420p"
    if mh and min(W, H) > mh:
        vf = f"scale=-2:{mh}" if W >= H else f"scale={mh}:-2"
    else:
        vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2" if (W % 2 or H % 2) else ""
    vf_args = ["-vf", vf] if vf else []
    cap = int(vb * 0.92)
    work = os.path.dirname(dst) or "."
    pts = _sample_pts(D)
    lo, hi = CRF_RANGE[codec]
    steps = math.ceil(math.log2(hi - lo + 1)) * len(pts)
    state = {"done": 0}

    def probe(crf):
        args = _venc_args(codec, crf, sample_preset, tune, cap, pix) + vf_args
        size, secs, vals = 0, 0.0, []
        for k, (t, length) in enumerate(pts):
            sp = os.path.join(work, f"smp_{k}.mp4")
            run_ffmpeg(["-ss", f"{t:.2f}", "-i", src, "-t", f"{length:.2f}", "-map", "0:v:0", "-an"] + args + [sp])
            size += os.path.getsize(sp)
            secs += length
            vals.append(_ssim(src, t, length, sp, vf))
            _rm(sp)
            state["done"] += 1
            prog.update("🔬 Smart Quality Analysis (SSIM Lock)", min(state["done"], steps), steps, "steps")
        return sum(vals) / len(vals), size * 8 / secs

    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        ssim, bps = probe(mid)
        if ssim >= target:
            best, lo = (mid, ssim, bps), mid + 1
        else:
            hi = mid - 1
    if best is None:
        ssim, bps = probe(CRF_RANGE[codec][0])
        best = (CRF_RANGE[codec][0], ssim, bps)
    crf, ssim, bps = best

    # Agar SSIM CRF se size bada aa raha ho to bitrate cap ko 72% par lock karke encode karo taaki size pakka kam ho
    est = bps * D / 8 + (info["a_bps"] or 128_000) * D / 8
    if est >= S * 0.94:
        crf = min(crf + 3, CRF_RANGE[codec][1])
        cap = int(vb * 0.72)

    args = (["-i", src, "-map", "0:v:0", "-map", "0:a:0?"] + _venc_args(codec, crf, preset, tune, cap, pix) + vf_args +
            _audio_args(info) + ["-movflags", "+faststart", dst])
    run_ffmpeg(args, D, lambda c, t: prog.update("🗜 Smart Compressing", c, t, "time"))
    after = os.path.getsize(dst)
    if after >= S * 0.98:
        _rm(dst)
        return {"skipped": True, "reason": "Ye video pehle se extreme compressed hai, isse zyada size kam karne par quality kharab hogi"}
    return {
        "skipped": False, "path": dst, "info": ffprobe(dst) or {}, "before": S, "after": after,
        "crf": crf, "ssim": ssim, "codec": codec, "preset": preset, "tune": tune
    }


def cmp_caption(name, r):
    pct = max(0.0, (1 - r["after"] / r["before"]) * 100)
    codec = "H.265" if r["codec"] == "h265" else "H.264"
    return (
        f"🗜 <b>Smart Compressed!</b>\n\n🎬 <b>Name:</b> <code>{esc(name)}</code>\n"
        f"📦 <b>Size:</b> <code>{humanbytes(r['before'])} → {humanbytes(r['after'])}</code> (<b>-{pct:.1f}% saved</b>)\n"
        f"✨ <b>Quality Lock:</b> <code>{codec} • CRF {r['crf']} • SSIM {r['ssim']:.3f}</code>"
    )[:1000]

class Progress:
    def __init__(self, loop, msg, header=""):
        self.loop, self.msg, self.header = loop, msg, header
        self.t_start = self.t0 = time.time()
        self.cur_stage = ""
        self.last_edit = 0.0
        self.block = 0.0
        self.last_text = ""

    def _ready(self, now, force):
        if now < self.block:
            return False
        el = now - self.t_start
        gap = 4.5 if force else (7.0 if el < 30 else 11.0 if el < 120 else 15.0)
        return now - self.last_edit >= gap

    def update(self, stage, done, total, kind="bytes"):
        now = time.time()
        changed = stage != self.cur_stage
        if changed:
            self.cur_stage, self.t0 = stage, now
        finished = bool(total) and done >= total
        if not self._ready(now, changed or finished):
            return
        self.last_edit = now
        pct = f"{done / total * 100:.1f}%" if total else "…"
        if kind == "time":
            body = f"Progress: <code>{pct}</code>  (<code>{fmt_dur(done)}/{fmt_dur(total)}</code>)"
        elif kind == "steps":
            body = f"Progress: <code>{pct}</code>  (<code>{int(done)}/{int(total)}</code>)"
        else:
            speed = done / max(now - self.t0, 0.1)
            eta = (total - done) / speed if total and speed else 0
            body = (
                f"Progress: <code>{pct}</code>\n"
                f"Size: <code>{humanbytes(done)} / {humanbytes(total)}</code>\n"
                f"Speed: <code>{humanbytes(speed)}/s</code>  ETA: <code>{fmt_dur(eta)}</code>"
            )
        text = f"{self.header}\n⏳ <b>{stage}</b>\n{body}"
        asyncio.run_coroutine_threadsafe(self._edit(text), self.loop)

    async def stage(self, text):
        self.cur_stage = text
        self.last_edit = time.time()
        await self._edit(f"{self.header}\n⏳ <b>{text}</b>")

    async def _edit(self, text):
        if text == self.last_text:
            return
        self.last_text = text
        try:
            await self.msg.edit_text(text)
        except FloodWait as e:
            self.block = time.time() + e.value + 1
            self.last_text = ""
        except Exception:
            pass


# Seal App Reverse-Engineered yt-dlp Options
def base_ydl_opts(headers=None, fast=False, imp="chrome"):
    hdrs = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        hdrs.update(headers)
    o = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "geo_bypass_country": "US",
        "socket_timeout": 20 if fast else 35,
        "retries": 4 if fast else 10,
        "extractor_retries": 4,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 8,
        "http_headers": hdrs,
        "extractor_args": {
            "youtube": {"player_client": ["android", "web", "ios"]},
            "generic": {"impersonate": ["chrome"]},
        },
    }
    if COOKIES_USABLE:
        o["cookiefile"] = COOKIES_FILE
    if PROXY_URL:
        o["proxy"] = PROXY_URL
    if FORCE_IPV4:
        o["source_address"] = "0.0.0.0"
    t = None if (NO_IMP or not imp) else impersonate_target(imp)
    if t:
        o["impersonate"] = t
    return o


def ydl_call(opts, fn, url=None):
    global NO_IMP

    def run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            if url:
                for c in age_cookies_for(url):
                    ydl.cookiejar.set_cookie(c)
            return fn(ydl)

    try:
        return run()
    except Exception as e:
        if "impersonat" in str(e).lower() and "impersonate" in opts:
            NO_IMP = True
            opts.pop("impersonate", None)
            return run()
        raise


def ytdlp_extract(url, headers=None):
    last = None
    ladder = [n for n in IMP_LADDER if not n or (not NO_IMP and impersonate_target(n))] or [""]
    for name in ladder:
        opts = base_ydl_opts(headers, True, name)
        opts["skip_download"] = True
        try:
            info = ydl_call(opts, lambda y: y.extract_info(url, download=False), url)
        except Exception as e:
            last = e
            if not any(k in str(e).lower() for k in BLOCK_HINTS):
                continue
            logging.info(f"[yt-dlp] imp={name or 'none'} retry: {str(e)[:100]}")
            continue
        if info and info.get("_type") == "playlist":
            entries = [e for e in (info.get("entries") or []) if e]
            info = entries[0] if entries else None
        if info:
            info["__imp"] = name
            return info
    if last:
        raise last
    return None


def compact_formats(info):
    keys = ("format_id", "height", "width", "vcodec", "acodec", "tbr", "vbr", "abr", "filesize", "filesize_approx", "url")
    return [{k: f.get(k) for k in keys} for f in (info.get("formats") or [])]


def qheight(f):
    h, w = f.get("height") or 0, f.get("width") or 0
    return min(h, w) if h and w else (h or w)


def est_size(formats, duration, max_h):
    def fsize(f):
        s = f.get("filesize") or f.get("filesize_approx")
        if s:
            return int(s)
        tbr = f.get("tbr") or ((f.get("vbr") or 0) + (f.get("abr") or 0))
        return int(tbr * 1000 / 8 * duration) if tbr and duration else 0

    has_v = lambda f: f.get("vcodec") not in (None, "none")
    has_a = lambda f: f.get("acodec") not in (None, "none")
    vids = [f for f in formats if has_v(f) and 0 < qheight(f) <= max_h]
    if not vids:
        vids = [f for f in formats if 0 < qheight(f) <= max_h]
    if not vids:
        return 0, 0
    best = max(vids, key=lambda f: (qheight(f), f.get("tbr") or 0))
    h = qheight(best)
    if has_a(best):
        return fsize(best), h
    auds = [f for f in formats if has_a(f) and not has_v(f)]
    aud = max(auds, key=lambda f: f.get("abr") or f.get("tbr") or 0) if auds else None
    return fsize(best) + (fsize(aud) if aud else 0), h


def build_quals(formats, duration, max_source_h=0, base_size=0):
    hs = sorted(
        {int(qheight(f)) for f in formats if f.get("vcodec") not in ("none",) and qheight(f)},
        reverse=True
    )
    out = []
    for h in hs:
        size, ah = est_size(formats, duration, h)
        if ah == h or size:
            out.append((h, size))
    if len(out) >= 2:
        return out[:6]

    # Fallback standard resolutions so quality buttons always work
    top_h = hs[0] if hs else (max_source_h or 1080)
    std = [q for q in STANDARD_QUALITIES if q <= max(top_h, 720)]
    if top_h and top_h not in std:
        std = sorted([top_h] + std, reverse=True)
    res = []
    for q in std[:5]:
        est = int(base_size * ((q / top_h) ** 1.5)) if (base_size and top_h) else 0
        res.append((q, est))
    return res


def collect_thumbs(info):
    urls = []
    th = [t for t in (info.get("thumbnails") or []) if t.get("url")]
    th.sort(key=lambda t: ((t.get("preference") or 0), (t.get("width") or 0) * (t.get("height") or 0)), reverse=True)
    urls += [t["url"] for t in th]
    if info.get("thumbnail"):
        urls.append(info["thumbnail"])
    seen, out = set(), []
    for u in urls:
        if u not in seen and not u.startswith("data:"):
            seen.add(u)
            out.append(u)
    return out


def source_from_ytinfo(url, info, headers, cookies):
    s = Source(
        page_url=url, dl_url=url, mode="ytdlp", headers=headers, cookies=cookies,
        title=info.get("title") or name_from_url(url) or "video",
        duration=float(info.get("duration") or 0), thumbs=collect_thumbs(info)
    )
    s.formats = compact_formats(info)
    s.est = {h: est_size(s.formats, s.duration, h) for h in TIERS}
    s.height = s.est[MAX_HEIGHT][1] or int(info.get("height") or 0)
    s.size = s.est[MAX_HEIGHT][0] or int(info.get("filesize") or info.get("filesize_approx") or 0)
    s.quals = build_quals(s.formats, s.duration, s.height, s.size)
    s.imp = info.get("__imp", "chrome")
    return s


async def source_from_media(media_url, page_url, cookies, title=None, thumbs=None, pre=None, url_name=False, alt_urls=None):
    loop = asyncio.get_running_loop()
    kind = classify_media(media_url) or "mp4"
    headers = make_headers(page_url)
    ch = cookie_header(media_url, cookies)
    if ch:
        headers["Cookie"] = ch
    s = Source(
        page_url=page_url, dl_url=media_url, mode="ytdlp" if kind in ("hls", "dash") else "direct",
        headers=headers, cookies=cookies, title=title or name_from_url(media_url) or "video",
        thumbs=list(thumbs or []), use_url_name=url_name, alt_urls=dict(alt_urls or {})
    )
    info = pre or await loop.run_in_executor(None, ffprobe, media_url, headers, 30)
    if info:
        s.duration, s.height, s.size = info["duration"], info["short"], info["size"]
    if s.mode == "ytdlp":
        try:
            yi = await loop.run_in_executor(None, ytdlp_extract, media_url, headers)
            if yi:
                s.formats = compact_formats(yi)
                s.duration = s.duration or float(yi.get("duration") or 0)
                s.est = {h: est_size(s.formats, s.duration, h) for h in TIERS}
                s.height = s.est[MAX_HEIGHT][1] or s.height
                s.size = s.est[MAX_HEIGHT][0] or s.size
        except Exception as e:
            logging.info(f"[yt-dlp] m3u8 info fail: {e}")

    if s.alt_urls:
        s.quals = [(q, 0) for q in sorted(s.alt_urls.keys(), reverse=True)]
    else:
        s.quals = build_quals(s.formats, s.duration, s.height, s.size)
    return s


def scan_html_for_media(text, base):
    text = html.unescape((text or "").replace("\\/", "/").replace("\\u0026", "&"))
    found = []
    for m in re.finditer(r'(?:https?:)?//[^\s"\'<>\\]+?\.(?:m3u8|mp4|webm|mpd)(?:\?[^\s"\'<>\\]*)?', text, re.I):
        found.append(m.group(0))
    for m in re.finditer(r'["\'](/[^"\'\s<>\\]+?\.(?:m3u8|mp4|webm|mpd)(?:\?[^"\'\s<>\\]*)?)["\']', text, re.I):
        found.append(m.group(1))
    out, seen = [], set()
    for u in found:
        a = urljoin(base, u)
        if a.startswith("http") and a not in seen and not is_bad_url(a) and not is_image_url(a):
            seen.add(a)
            out.append(a)
    return out


async def sniff_page(page_url, cookies):
    cfg = get_site_config(page_url)
    cands, seen, netlog, alt_urls = {}, set(), [], {}
    res = {
        "cands": [], "title": None, "thumbs": [], "cookies": [],
        "html": "", "frames": [], "net": netlog, "final": "", "alt_urls": alt_urls
    }

    def add(url, score, kind, size=0, tag="", q=0):
        if not url or not url.startswith("http") or is_image_url(url) or is_bad_url(url):
            return
        if q in STANDARD_QUALITIES and kind == "mp4":
            alt_urls.setdefault(q, url)
        old = cands.get(url)
        if old and old["score"] >= score:
            return
        cands[url] = {"url": url, "score": score, "kind": kind, "size": size, "tag": tag, "q": q}
        logging.info(f"[SNIFF:{tag}] {kind} score={score} q={q} {url[:110]}")

    async def on_response(resp):
        try:
            url = resp.url
            if url in seen:
                return
            seen.add(url)
            hd = resp.headers
            ctype = (hd.get("content-type") or "").split(";")[0].strip().lower()
            if ctype.startswith("image/"):
                return
            total = 0
            mt = re.search(r"/(\d+)$", hd.get("content-range", "") or "")
            if mt:
                total = int(mt.group(1))
            else:
                try:
                    total = int(hd.get("content-length") or 0)
                except ValueError:
                    pass
            rt = resp.request.resource_type
            if rt in ("xhr", "fetch") and total < 2 * MiB and ("json" in ctype or "text" in ctype or "javascript" in ctype):
                try:
                    body = await resp.text()
                    for u in scan_html_for_media(body, url)[:10]:
                        add(u, 175, classify_media(u) or "mp4", 0, "API")
                except Exception:
                    pass
            kind = classify_media(url, ctype, total)
            if not kind and (ctype.startswith(("video/", "audio/")) or url_path(url).endswith(SEGMENT_EXTS)) and len(netlog) < 40:
                netlog.append(url)
            if not kind or is_bad_url(url):
                return
            if kind == "mp4" and 0 < total < 400 * 1024:
                return
            path = url_path(url)
            score = 0
            if kind == "hls":
                score = 160
                try:
                    if total < 3 * MiB and "#EXT-X-STREAM-INF" in (await resp.text()):
                        score = 320
                except Exception:
                    pass
            elif kind == "dash":
                score = 210
            else:
                score = 100
                if ctype.startswith("video/"):
                    score += 50
                if resp.request.resource_type == "media":
                    score += 30
                score += 120 if total > 50 * MiB else 60 if total > 5 * MiB else 20 if total > MiB else 0
            qm = re.search(r"(2160|1440|1080|720|480|360)p?", path)
            qn = int(qm.group(1)) if qm else 0
            score += 110 if qn >= 1080 else 75 if qn == 720 else 35 if qn == 480 else 0
            add(url, score, kind, total, "NET", qn)
        except Exception:
            pass

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=HEADLESS, proxy=({"server": PROXY_URL} if PROXY_URL else None),
                args=[
                    "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled", "--autoplay-policy=no-user-gesture-required",
                ]
            )
            ctx = await browser.new_context(
                user_agent=UA, viewport={"width": 1366, "height": 768},
                locale="en-US", accept_downloads=True, ignore_https_errors=True
            )
            await ctx.add_init_script(STEALTH_JS)
            if cookies:
                try:
                    await ctx.add_cookies(cookies)
                except Exception:
                    pass
            try:
                host = urlparse(page_url).netloc.split(":")[0]
                await ctx.add_cookies([{"name": n, "value": "1", "domain": host, "path": "/"} for n in AGE_COOKIES])
            except Exception:
                pass

            async def route(r):
                if r.request.resource_type in ("image", "font"):
                    await r.abort()
                else:
                    await r.continue_()
            await ctx.route("**/*", route)

            page = await ctx.new_page()
            page.on("response", on_response)
            ctx.on("page", lambda pg: asyncio.ensure_future(pg.close()))

            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)

            async def click_first(frame, selectors, timeout=1200):
                for sel in selectors:
                    try:
                        el = await frame.query_selector(sel)
                        if el and await el.is_visible():
                            await el.click(timeout=timeout)
                            return True
                    except Exception:
                        continue
                return False

            async def age_pass():
                hit = False
                for fr in page.frames:
                    try:
                        if await fr.evaluate(AGE_JS):
                            hit = True
                    except Exception:
                        pass
                return hit

            for _ in range(10):
                try:
                    tl = (await page.title() or "").lower()
                except Exception:
                    break
                if any(k in tl for k in ("just a moment", "attention required", "checking your browser", "verify you are human")):
                    await page.wait_for_timeout(1500)
                else:
                    break
            await click_first(page, CONSENT_SELECTORS)
            for _ in range(3):
                if not await age_pass():
                    break
                await page.wait_for_timeout(900)
            await page.wait_for_timeout(500)

            try:
                res["title"] = await page.evaluate(
                    "(sel)=>{const e=document.querySelector(sel);return (e&&e.textContent.trim())||document.title}",
                    cfg.get("title_selector", "h1")
                )
                res["title"] = (res["title"] or "").strip()[:150] or None
            except Exception:
                pass
            try:
                res["thumbs"] = await page.evaluate(
                    """()=>{const r=[];document.querySelectorAll('meta[property="og:image"],meta[name="twitter:image"]')
                    .forEach(m=>m.content&&r.push(m.content));document.querySelectorAll('video[poster]')
                    .forEach(v=>r.push(v.poster));return r}"""
                )
            except Exception:
                pass

            async def collect_links():
                sels = list(dict.fromkeys(list(cfg.get("download_selectors", [])) + UNIVERSAL_SELECTORS))
                for sel in sels:
                    try:
                        els = await page.query_selector_all(sel)
                    except Exception:
                        continue
                    for el in els[:40]:
                        try:
                            href = await el.get_attribute("href")
                            if not href:
                                continue
                            u = urljoin(page.url, href)
                            if is_image_url(u) or is_bad_url(u) or u in seen:
                                continue
                            if not (classify_media(u) or "get_file" in u.lower() or "/download/" in u.lower()):
                                continue
                            seen.add(u)
                            txt = ""
                            try:
                                txt = (await el.inner_text() or "")[:40]
                            except Exception:
                                pass
                            q = re.search(r"(2160|1440|1080|720|480|360)p?", txt + " " + u)
                            qn = int(q.group(1)) if q else 0
                            score = 250 + (100 if qn >= 1080 else 60 if qn >= 720 else 20 if qn >= 480 else 0)
                            add(u, score, classify_media(u) or "mp4", 0, "HREF", qn)
                        except Exception:
                            continue

            await collect_links()

            async def poke():
                await age_pass()
                for fr in page.frames:
                    try:
                        await fr.evaluate(PLAY_JS)
                    except Exception:
                        pass
                    await click_first(fr, PLAY_SELECTORS, 1500)

            t0, pokes = time.time(), 0
            while time.time() - t0 < 12:
                if any(c["score"] >= 250 for c in cands.values()):
                    break
                el = time.time() - t0
                if (pokes == 0 and el > 0.3) or (pokes == 1 and el > 4.5):
                    await poke()
                    pokes += 1
                await page.wait_for_timeout(500)

            await collect_links()
            for fr in page.frames:
                try:
                    content = await fr.content()
                except Exception:
                    continue
                for u in scan_html_for_media(content, fr.url or page.url):
                    qm = re.search(r"(2160|1440|1080|720|480|360)p?", u)
                    qn = int(qm.group(1)) if qm else 0
                    add(u, 130, classify_media(u) or "mp4", 0, "HTML", qn)
            try:
                dom = await page.evaluate("""()=>{const r=[];document.querySelectorAll('video,video source')
                    .forEach(v=>{if(v.src)r.push(v.src);if(v.currentSrc)r.push(v.currentSrc)});return r}""")
                for u in dom or []:
                    if u.startswith("http"):
                        add(u, 145, classify_media(u) or "mp4", 0, "DOM")
            except Exception:
                pass

            if not cands:
                for sel in ['a:has-text("Download")', 'button:has-text("Download")', 'a[download]', '.download-button']:
                    try:
                        el = await page.query_selector(sel)
                        if not el:
                            continue
                        async with page.expect_download(timeout=4000) as dli:
                            await el.click()
                        dl = await dli.value
                        add(dl.url, 400, "mp4", 0, "DL")
                        try:
                            await dl.cancel()
                        except Exception:
                            pass
                        break
                    except Exception:
                        continue

            res["final"] = page.url
            res["frames"] = [fr.url for fr in page.frames[1:] if (fr.url or "").startswith("http")]
            try:
                res["cookies"] = await ctx.cookies()
            except Exception:
                pass
            if not cands:
                try:
                    res["html"] = (await page.content())[:120000]
                except Exception:
                    pass
            await browser.close()
    except Exception as e:
        logging.error(f"[browser] error: {e}")

    res["cands"] = list(cands.values())
    return res


def gemini_find_url(url, html_text=None, net=None, cands=None):
    if not ai_model:
        return None
    try:
        if not html_text:
            sc = make_session(make_headers(url), load_cookie_list())
            html_text = sc.get(url, timeout=25).text[:120000]
        extra = ""
        if cands:
            extra += "Candidate media URLs seen in the browser:\n" + "\n".join(c["url"] for c in cands[:10]) + "\n\n"
        if net:
            extra += "Other media-like network requests:\n" + "\n".join(net[:15]) + "\n\n"
        prompt = (
            "You are an expert web scraper. Find the MAIN direct video URL (.mp4, .m3u8, .mpd) of this page. "
            "Ignore thumbnails, ads, trailers, previews. Return ONLY the full absolute URL, or NOT_FOUND.\n\n"
            f"Page URL: {url}\n\n{extra}HTML:\n{html_text}"
        )
        txt = ai_model.generate_content(prompt).text.strip()
        m = re.search(r'https?://[^\s"\'<>]+', txt)
        if m and not is_image_url(m.group(0)):
            return m.group(0)
    except Exception as e:
        logging.error(f"Gemini error: {e}")
    return None


INTENT_ACTIONS = (
    "card", "download", "cut", "audio", "status", "help", "start",
    "template", "ask", "chat", "diag", "compress", "none"
)


def gemini_intent(text, has_url, last_title):
    prompt = (
        "You are the control brain of a Telegram video downloader bot. The owner writes in Hinglish, Hindi or English. "
        "Convert the owner's message into ONE JSON command. Output ONLY the JSON object, no markdown.\n"
        'Schema: {"action": "card|download|cut|audio|status|help|start|template|ask|chat", "quality": integer video '
        'height (e.g. 2160, 1080, 720, 480; 4320 for best/highest; 0 if not mentioned), "start": "HH:MM:SS" or null, '
        '"end": "HH:MM:SS" or null, "mode": "exact|fast|null", "arg": "template number as a string, or random, or null", '
        '"reply": "one short Hinglish line telling the owner what you understood, or the question if action=ask, '
        'or your answer if action=chat"}\n'
        f"has_link={str(bool(has_url)).lower()}\nprevious_video_title={last_title or 'none'}\n"
        f"Owner message: {text}"
    )
    out = ai_model.generate_content(prompt).text
    m = re.search(r"\{.*\}", out, re.S)
    return m.group(0) if m else "{}"


def _tsec(v):
    try:
        if v is None or v == "" or v == "null":
            return None
        if isinstance(v, (int, float)):
            return float(v)
        v = str(v).strip()
        return to_sec(v) if ":" in v else float(v)
    except Exception:
        return None


def intent_from_ai(d):
    it = Intent()
    act = str(d.get("action") or "").lower()
    it.action = act if act in INTENT_ACTIONS else "card"
    m = re.search(r"\d+", str(d.get("quality") or ""))
    it.q = int(m.group(0)) if m else 0
    if 0 < it.q < 144:
        it.q = 2160 if it.q == 4 else 1440 if it.q == 2 else 0
    it.start, it.end = _tsec(d.get("start")), _tsec(d.get("end"))
    mode = str(d.get("mode") or "").lower()
    it.mode = mode if mode in ("exact", "fast") else ""
    it.reply = str(d.get("reply") or "").strip()[:300]
    it.arg = re.sub(r"[^\w]", "", str(d.get("arg") or ""))[:12].lower()
    if it.arg == "random":
        it.arg = "0"
    return it


def rule_intent(text, has_url):
    it = Intent(action="card" if has_url else "none")
    t = (text or "").lower()
    rng = parse_range(text)
    qm = re.search(r"(\d{3,4})\s*p\b", t)
    if qm:
        it.q = int(qm.group(1))
    elif re.search(r"\b4k\b", t):
        it.q = 2160
    elif re.search(r"\b(best|highest|max|full\s*hd|fhd)\b", t):
        it.q = MAX_HEIGHT
    elif re.search(r"\b(lowest|low|chhota|small)\b", t):
        it.q = 360
    if has_url and re.search(r"\b(diag|diagnose|debug|jaanch)\b", t):
        it.action = "diag"
        return it
    if re.search(r"\b(compress|compression|shrink|size\s*kam|smaller)\b", t):
        it.action, it.arg = "compress", t
        return it
    if re.search(r"\btemplates?\b", t):
        it.action = "template"
        dm = re.search(r"\b(\d{1,3})\b", t)
        it.arg = dm.group(1) if dm else ("0" if re.search(r"\b(random|rendom|badlo|badal)\b", t) else "")
        it.q = 0
        return it
    if rng:
        it.action, it.start, it.end = "cut", rng[0], rng[1]
    elif re.search(r"\b(audio|mp3|m4a|music|gaana|gana|sound)\b", t):
        it.action = "audio"
    elif re.search(r"\b(status|queue)\b", t):
        it.action = "status"
    elif re.search(r"\b(help|madad|kaise|commands?)\b", t):
        it.action = "help"
    elif re.match(r"\s*(start|shuru|hi|hello|hey|namaste|namaskar)\b", t):
        it.action = "start"
    elif it.q or re.search(r"\b(download|dl|bhej|bhejo)\b", t):
        it.action = "download"
    return it


async def get_intent(text, has_url, last_title):
    base = rule_intent(text, has_url)
    t = (text or "").strip()
    sure = base.action not in ("none", "card") or (base.action == "card" and not t)
    if sure or not ai_model or len(t) < 3:
        return base
    loop = asyncio.get_running_loop()
    try:
        raw = await asyncio.wait_for(loop.run_in_executor(None, gemini_intent, text, has_url, last_title), timeout=25)
        data = json.loads(raw)
        return intent_from_ai(data) if data else base
    except Exception:
        return base


class ResolveError(Exception):
    def __init__(self, msg, diag=None):
        super().__init__(msg)
        self.diag = diag or {}


async def resolve_source(url, status):
    loop = asyncio.get_running_loop()
    cookies = load_cookie_list()
    run = lambda fn, *a: loop.run_in_executor(None, fn, *a)

    if classify_media(url):
        await status("🔗 Direct link mila, check kar raha hoon…")
        return await source_from_media(url, origin_of(url) + "/", cookies, url_name=True)

    generic = None
    err = ""
    await status("🔎 Analyzing… (Seal yt-dlp Engine)")
    try:
        info = await run(ytdlp_extract, url, None)
        if info and (info.get("formats") or info.get("url")):
            s = source_from_ytinfo(url, info, {}, cookies)
            if (info.get("extractor_key") or "").lower() != "generic" or s.duration >= 30 or len(s.formats) > 1:
                return s
            generic = s
    except Exception as e:
        err = str(e)[:300]
        logging.info(f"[yt-dlp] {err[:150]}")

    await status("🌐 Browser Engine se video nikal raha hoon…")
    sn = await sniff_page(url, cookies)
    cookies2 = sn["cookies"] or cookies
    thumbs = [urljoin(url, t) for t in sn["thumbs"] if t]
    cands = sorted(sn["cands"], key=lambda c: -c["score"])[:8]
    alt_urls = sn.get("alt_urls") or {}

    async def try_yt(u, ck):
        h = {"Referer": url}
        if ck:
            h["Cookie"] = ck
        try:
            info = await run(ytdlp_extract, u, h)
        except Exception:
            return None
        if not (info and (info.get("formats") or info.get("url"))):
            return None
        sc = source_from_ytinfo(u, info, h, cookies2)
        sc.thumbs += thumbs
        return sc

    tasks = [try_yt(f, None) for f in sn["frames"][:4]]
    ck = cookie_header(url, cookies2)
    if ck:
        tasks.append(try_yt(url, ck))
    if sn.get("final") and sn["final"] != url:
        tasks.append(try_yt(sn["final"], ck))
    pool = [x for x in await asyncio.gather(*tasks) if x]
    if generic:
        pool.append(generic)

    if not cands:
        try:
            sc = make_session(make_headers(url), cookies)
            txt = (await run(lambda: sc.get(url, timeout=25).text))[:600000]
            for u in scan_html_for_media(txt, url)[:6]:
                cands.append({"url": u, "score": 110, "kind": classify_media(u) or "mp4", "size": 0, "tag": "RAW"})
        except Exception:
            pass

    if cands:
        await status("🧪 Video streams check kar raha hoon…")
        hdr = make_headers(url)

        async def probe(c):
            h = dict(hdr)
            ch = cookie_header(c["url"], cookies2)
            if ch:
                h["Cookie"] = ch
            c["info"] = await run(ffprobe, c["url"], h, 25)
            c["dur"] = (c["info"] or {}).get("duration") or 0

        await asyncio.gather(*[probe(c) for c in cands])
        valid = [c for c in cands if c["info"]]
        best = (
            max(valid, key=lambda c: (c["dur"] >= 15, c["score"], c["dur"]))
            if valid else (cands[0] if cands[0]["score"] >= 150 else None)
        )
        if best:
            pool.append(
                await source_from_media(
                    best["url"], url, cookies2, title=sn["title"], thumbs=thumbs,
                    pre=best.get("info"), alt_urls=alt_urls
                )
            )
    if pool:
        return max(pool, key=lambda x: (x.duration >= 15, len(x.quals), x.duration))

    diag = {
        "url": url, "cands": cands, "net": sn.get("net") or [], "html": sn.get("html") or "",
        "cookies": cookies2, "title": sn["title"], "thumbs": thumbs
    }
    if ai_model:
        await status("🤖 Gemini AI se link nikal raha hoon…")
        u = await run(gemini_find_url, url, sn.get("html"), sn.get("net"), cands)
        if u:
            return await source_from_media(u, url, cookies2, title=sn["title"], thumbs=thumbs)
    raise ResolveError(err or "Video extract nahi ho paaya", diag)


def _largest_media_file(workdir):
    best, size = None, 0
    for f in os.listdir(workdir):
        p = os.path.join(workdir, f)
        if f.endswith((".part", ".ytdl", ".txt", ".ts", ".jpg")) or not os.path.isfile(p):
            continue
        if os.path.getsize(p) > size:
            best, size = p, os.path.getsize(p)
    if not best or size < 50 * 1024:
        raise RuntimeError("Download ke baad valid video file nahi mili")
    return best


# Seal-style strict resolution & format downloader
def ytdlp_download(url, workdir, headers, max_h, prog, audio=False, imp="chrome"):
    def hook(d):
        if d.get("status") == "downloading":
            prog.update(
                "⬇️ Downloading", d.get("downloaded_bytes") or 0,
                d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            )

    sort = [f"res:{max_h}", "vcodec:h264", "acodec:aac", "ext:mp4:m4a", "proto"]
    if audio:
        plans = [("ba/b", None), ("bestaudio/best", None)]
    else:
        f_strict = f"bv*[height<={max_h}]+ba/b[height<={max_h}]/bv*+ba/b"
        plans = [(f_strict, sort), ("bv*+ba/b", sort), ("b", None)]

    last = None
    for (fmt, srt), name in [(p, n) for n in dict.fromkeys([imp, "chrome", ""]) for p in plans]:
        opts = base_ydl_opts(headers, False, name)
        opts.update({
            "outtmpl": os.path.join(workdir, "dl.%(ext)s"),
            "format": fmt,
            "progress_hooks": [hook],
            "noprogress": True,
        })
        if not audio:
            opts["merge_output_format"] = "mp4"
        if srt:
            opts["format_sort"] = srt
        try:
            ydl_call(opts, lambda y: y.extract_info(url, download=True), url)
            return _largest_media_file(workdir)
        except Exception as e:
            last = e
            for f in os.listdir(workdir):
                _rm(os.path.join(workdir, f))
    raise last


def ffmpeg_stream_download(url, out, headers, duration, prog, hls=False):
    args = []
    if hls:
        args += ["-allowed_extensions", "ALL"]
    if PROXY_URL:
        args += ["-http_proxy", PROXY_URL]
    if headers:
        args += ["-headers", _hdr_str(headers)]
    args += ["-i", url, "-c", "copy"]
    if hls:
        args += ["-bsf:a", "aac_adtstoasc"]
    run_ffmpeg(
        args + ["-movflags", "+faststart", out], duration,
        lambda c, t: prog.update("⬇️ Downloading (Stream)", c, t, "time")
    )
    return out


def download_direct(url, out_path, headers, cookies, prog, workers=8):
    s = make_session(headers, cookies)
    label = "⬇️ Downloading"

    def first_request():
        try:
            r = s.get(url, headers={"Range": "bytes=0-"}, stream=True, timeout=(15, 60), allow_redirects=True)
            if r.status_code >= 400:
                raise IOError(f"HTTP {r.status_code}")
            return r
        except Exception:
            return s.get(url, stream=True, timeout=(15, 60), allow_redirects=True)

    r = first_request()
    r.raise_for_status()
    ctype = (r.headers.get("content-type") or "").lower()
    if ctype.startswith("image/"):
        raise RuntimeError(f"Ye image hai, video nahi ({ctype})")
    if "text/html" in ctype:
        raise RuntimeError("HTML mila (link expire/blocked)")
    final = r.url
    mt = re.search(r"/(\d+)$", r.headers.get("content-range", "") or "")
    total = int(mt.group(1)) if mt else int(r.headers.get("content-length") or 0)
    ranged = r.status_code == 206 or (r.headers.get("accept-ranges", "").lower() == "bytes" and total > 0)

    if ranged and total > 32 * MiB:
        r.close()
        try:
            _parallel_download(s, final, out_path, total, workers, lambda d, t: prog.update(label, d, t))
            return out_path
        except Exception as e:
            logging.warning(f"[direct] parallel fail ({e}) -> single stream")
            r = s.get(url, stream=True, timeout=(15, 60), allow_redirects=True)
            r.raise_for_status()

    done = 0
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
                done += len(chunk)
                prog.update(label, done, total)
    if done < 100 * 1024:
        _rm(out_path)
        raise RuntimeError(f"File sirf {humanbytes(done)} hai — video nahi lagti")
    return out_path


def _parallel_download(session, url, out_path, total, workers, cb):
    chunk = max(8 * MiB, total // (workers * 4))
    ranges = [(i, min(i + chunk - 1, total - 1)) for i in range(0, total, chunk)]
    with open(out_path, "wb") as f:
        f.truncate(total)
    lock, done = threading.Lock(), [0]

    def fetch(a, b):
        last = None
        for attempt in range(4):
            try:
                resp = session.get(url, headers={"Range": f"bytes={a}-{b}"}, stream=True, timeout=(15, 60))
                if resp.status_code != 206:
                    raise IOError(f"range unsupported ({resp.status_code})")
                got = 0
                with open(out_path, "r+b") as fh:
                    fh.seek(a)
                    for data in resp.iter_content(1024 * 1024):
                        if data:
                            fh.write(data)
                            got += len(data)
                            with lock:
                                done[0] += len(data)
                                cb(done[0], total)
                if got != b - a + 1:
                    raise IOError("short chunk")
                return
            except Exception as e:
                last = e
                time.sleep(1 + attempt)
        raise last

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in [ex.submit(fetch, a, b) for a, b in ranges]:
            fut.result()


def choose_tier(src: Source, q: int = 0) -> int:
    if q and q > 0:
        return q
    if src.mode == "ytdlp" and src.est:
        size, _ = src.est.get(MAX_HEIGHT, (0, 0))
        if size and size > UPLOAD_LIMIT:
            return FALLBACK_HEIGHT
    return MAX_HEIGHT


def download_media(src: Source, workdir, prog, q=0, audio=False):
    tier = choose_tier(src, q)
    errs = []
    target_url = src.alt_urls.get(tier, src.dl_url) if not audio else src.dl_url

    if src.mode == "ytdlp" and target_url == src.dl_url:
        try:
            return ytdlp_download(target_url, workdir, src.headers, tier, prog, audio, src.imp)
        except Exception as e:
            errs.append(f"yt-dlp: {str(e)[:150]}")
    else:
        try:
            return download_direct(target_url, os.path.join(workdir, "dl.mp4"), src.headers, src.cookies, prog)
        except Exception as e:
            errs.append(f"direct: {str(e)[:150]}")
        try:
            return ytdlp_download(target_url, workdir, src.headers, tier, prog, audio, src.imp)
        except Exception as e:
            errs.append(f"yt-dlp: {str(e)[:150]}")
    kind = classify_media(target_url)
    if kind:
        try:
            return ffmpeg_stream_download(
                target_url, os.path.join(workdir, "stream.mp4"),
                src.headers, src.duration, prog, kind == "hls"
            )
        except Exception as e:
            errs.append(f"ffmpeg: {str(e)[:150]}")
    raise RuntimeError(" | ".join(errs) or "download fail")


def _pixels(path):
    inf = ffprobe(path)
    return (inf["width"] * inf["height"]) if inf else 0


def fetch_thumbnail(job: Job):
    if job.thumb_path and os.path.exists(job.thumb_path):
        return job.thumb_path
    s = job.src
    sess = make_session(make_headers(s.page_url), s.cookies)
    best, bpx = None, -1
    for i, u in enumerate(s.thumbs[:8]):
        try:
            r = sess.get(u, timeout=20, headers={"Accept": "image/*,*/*"})
            ct = (r.headers.get("content-type") or "").lower()
            if r.status_code != 200 or not ct.startswith("image/") or len(r.content) < 6 * 1024:
                continue
            ext = ".png" if "png" in ct else ".webp" if "webp" in ct else ".jpg" if ("jpeg" in ct or "jpg" in ct) else ".img"
            raw = os.path.join(THUMB_DIR, f"{job.id}_c{i}{ext}")
            with open(raw, "wb") as f:
                f.write(r.content)
            if ext == ".img":
                out = os.path.join(THUMB_DIR, f"{job.id}_c{i}.jpg")
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-q:v", "2", out],
                               capture_output=True, timeout=60)
                _rm(raw)
                if not os.path.exists(out):
                    continue
                raw = out
            px = _pixels(raw)
            if px > bpx:
                if best:
                    _rm(best)
                best, bpx = raw, px
            else:
                _rm(raw)
        except Exception:
            continue
    hq = os.path.join(THUMB_DIR, f"{job.id}_hq.jpg")
    if bpx < 900_000:
        if not os.path.exists(hq):
            try:
                grab_frame(s.dl_url, hq, at=max(1, (s.duration or 30) * 0.1), headers=dict(s.headers))
            except Exception:
                pass
        if os.path.exists(hq) and _pixels(hq) > bpx:
            best = hq
    if best:
        job.thumb_path = best
    return best


MODE_LABEL = {"exact": "🎯 Exact (frame-exact)", "fast": "⚡ Fast (keyframe)"}
QUALITY_TAG = {2160: " 4K", 1440: " 2K", 1080: " FHD", 720: " HD", 480: " SD", 360: " Low"}


def quality_options(s: Source) -> list:
    if s.quals:
        return list(s.quals)
    return [(1080, 0), (720, 0), (480, 0), (360, 0)]


def card_text(job: Job) -> str:
    s = job.src
    lines = [f"🎬 <b>{esc(s.title[:120])}</b>", ""]
    lines.append(f"⏱ Length: <code>{fmt_dur(s.duration) if s.duration else 'unknown'}</code>")
    lines.append("")
    for h, z in quality_options(s):
        q_str = f"{h}p{QUALITY_TAG.get(h, '')}" if h else "Best"
        lines.append(f"📺 <code>{q_str}</code>" + (f"  •  <code>~{humanbytes(z)}</code>" if z else ""))
    lines.append("\n👇 <b>Quality button chuno (ya Cut / Audio / Compress):</b>")
    return "\n".join(lines)


def card_kb(job: Job) -> InlineKeyboardMarkup:
    s = job.src
    d = fmt_dur(s.duration) if s.duration else "?"
    opts = quality_options(s)
    btns = [
        InlineKeyboardButton(
            f"⬇️ {h}p{QUALITY_TAG.get(h, '')}" if h else "⬇️ Best Quality",
            callback_data=f"dl:{job.id}:{h or 0}"
        )
        for h, _ in opts
    ]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton(f"✂️ Cut + Download  •  ⏱ {d}", callback_data=f"cut:{job.id}")])
    rows.append([
        InlineKeyboardButton("🎵 Audio (MP3/M4A)", callback_data=f"au:{job.id}"),
        InlineKeyboardButton("🗜 Smart Compress", callback_data=f"zp:{job.id}")
    ])
    return InlineKeyboardMarkup(rows)


def push(job: Job, d):
    attr = "cs" if job.sel == "s" else "ce"
    cur = "000000" if job.fresh else fmt_hms(getattr(job, attr)).replace(":", "")[-6:].rjust(6, "0")
    n = ("0" + cur[:-1]) if d is None else (cur + d)[-6:]
    setattr(job, attr, float(int(n[:2]) * 3600 + int(n[2:4]) * 60 + int(n[4:])))
    job.fresh = False


def panel_text(job: Job) -> str:
    s = job.src
    warn = ""
    if job.ce <= job.cs:
        warn = "\n⚠️ End, Start se bada hona chahiye"
    elif s.duration and job.cs >= s.duration:
        warn = "\n⚠️ Start video se bahar hai"
    return (
        f"✂️ <b>Cut Video</b>\n🎬 <code>{esc(s.title[:80])}</code>\n"
        f"⏱ Total: <code>{fmt_hms(s.duration) if s.duration else 'unknown'}</code>\n"
        f"📏 Clip: <code>{fmt_hms(max(0, job.ce - job.cs))}</code>\n"
        f"📺 Selected Quality: <code>{f'{job.q}p' if job.q else 'Best Auto'}</code>{warn}\n\n"
        f"👇 Start/End chuno, phir keypad se time daalo (HH:MM:SS)"
    )


def panel_kb(job: Job) -> InlineKeyboardMarkup:
    i = job.id
    b = lambda t, d: InlineKeyboardButton(t, callback_data=d)
    mk = lambda f, n, v: b(("✅ " if job.sel == f else "") + f"{n} {fmt_hms(v)}", f"cs:{i}:{f}")
    kp = lambda *ds: [b(d, f"cd:{i}:{d}") for d in ds]
    return InlineKeyboardMarkup([
        [mk("s", "▶️", job.cs), mk("e", "⏹", job.ce)],
        kp("1", "2", "3"), kp("4", "5", "6"), kp("7", "8", "9"),
        [b("00", f"cd:{i}:00"), b("0", f"cd:{i}:0"), b("⌫", f"cb:{i}")],
        [b("🗑 Clear", f"cc:{i}"), b("⏩ End=Full", f"cf:{i}"), b(MODE_LABEL[job.mode].split(" (")[0], f"cm:{i}")],
        [b(f"📺 Quality: {f'{job.q}p' if job.q else 'Auto (Tap to change)'}", f"cq:{i}")],
        [b("✅ Cut + Download", f"cg:{i}")],
        [b("⬅️ Back", f"cx:{i}")],
    ])


def thumb_kb(job: Job, seconds) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        f"🖼 HD Thumbnail  •  ⏱ {fmt_dur(seconds)}", callback_data=f"th:{job.id}")]])


def stage_a(job: Job, cut, workdir, prog: Progress, audio=False, compress=None):
    src = job.src
    path = download_media(src, workdir, prog, job.q, audio)
    if compress is not None and not audio:
        cinfo = ffprobe(path) or {}
        r = smart_compress(path, os.path.join(workdir, "cmp.mp4"), prog, compress)
        if not r["skipped"]:
            return {
                "cmp": r, "full": path, "info": cinfo,
                "dur": cinfo.get("duration") or src.duration, "cut": None, "audio": None
            }
        path = ensure_compatible(path, job.q, lambda c, t: prog.update("🔧 Converting", c, t, "time"))
        info = ffprobe(path) or {}
        return {
            "cmp": r, "full": path, "info": info,
            "dur": info.get("duration") or src.duration, "cut": None, "audio": None, "note": r["reason"]
        }
    if audio:
        out, ainfo = extract_audio(
            path, os.path.join(workdir, "audio.m4a"), src.title,
            lambda c, t: prog.update("🎵 Converting Audio", c, t, "time")
        )
        return {"audio": out, "info": ainfo, "dur": ainfo.get("duration") or src.duration, "cut": None}

    # Ensure Telegram compatibility AND enforce selected quality (job.q) if needed
    path = ensure_compatible(
        path, job.q if (0 < job.q < MAX_HEIGHT) else 0,
        lambda c, t: prog.update("🔧 Optimizing & Scaling", c, t, "time")
    )
    info = ffprobe(path) or {}
    D = info.get("duration") or src.duration or 0
    out = {"full": path, "info": info, "dur": D, "cut": None, "audio": None}
    if cut:
        start, end, mode = cut
        end = min(end, D) if D else end
        if end - start < 0.5:
            raise RuntimeError("Cut range invalid (video se bahar)")
        raw = os.path.join(workdir, "cut_raw.mp4")
        used = cut_video(path, raw, start, end, mode, info, prog)
        fitted, method = fit_to_limit(raw, os.path.join(workdir, "cut_fit.mp4"), prog)
        out["cut"] = {
            "path": fitted, "start": start, "end": end, "mode": used,
            "method": method, "info": ffprobe(fitted) or {}
        }
    final = out["cut"]["path"] if out["cut"] else path
    fdur = (out["cut"]["info"].get("duration") if out["cut"] else D) or D
    try:
        grab_frame(final, os.path.join(THUMB_DIR, f"{job.id}_hq.jpg"), at=max(1, fdur * 0.1))
    except Exception:
        pass
    return out


def fit_to_limit(path, out, prog):
    if os.path.getsize(path) <= UPLOAD_LIMIT:
        return path, "none"
    return out, compress_to_fit(path, out, COMPRESS_TARGET, prog)


def make_caption(name, info, size, extra=""):
    q = f"{info.get('short')}p" if info.get("short") else "?"
    return (
        f"✅ <b>Downloaded!</b>\n\n🎬 <b>Name:</b> <code>{esc(name)}</code>\n"
        f"📺 <b>Quality:</b> <code>{q}</code>\n⏱ <b>Length:</b> <code>{fmt_dur(info.get('duration'))}</code>\n"
        f"📦 <b>Size:</b> <code>{humanbytes(size)}</code>{extra}"
    )[:1000]


async def valid_reply(client, chat_id, mid):
    if not mid:
        return None
    try:
        m = await client.get_messages(chat_id, mid)
        return None if (not m or getattr(m, "empty", False)) else mid
    except Exception:
        return mid


async def send_one(client, chat_id, path, fname, caption, info, kb, prog, label, workdir, reply=None):
    thumb = os.path.join(workdir, f"{label}_thumb.jpg")
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(
        None, lambda: grab_frame(path, thumb, at=max(1, (info.get("duration") or 10) * 0.1), width=320, quality=5)
    )
    await client.send_video(
        chat_id=chat_id, video=path, caption=caption, file_name=fname, supports_streaming=True,
        duration=int(info.get("duration") or 0), width=int(info.get("width") or 0),
        height=int(info.get("height") or 0), thumb=thumb if ok else None, reply_markup=kb,
        reply_to_message_id=reply,
        progress=_up_progress, progress_args=(prog, f"📤 Uploading ({label})"),
    )


async def _up_progress(current, total, prog, label):
    prog.update(label, current, total)


async def run_delivery(client, job: Job, cut, audio=False, compress=None):
    global active_downloads
    loop = asyncio.get_running_loop()
    job.busy = True
    msg = job.msg
    header = f"🎬 <b>{esc(job.src.title[:70])}</b>" + (f" • {job.q}p" if 0 < job.q < MAX_HEIGHT else "")
    prog = Progress(loop, msg, header)
    workdir = os.path.join(DOWNLOAD_DIR, f"{job.id}_{int(time.time())}")
    os.makedirs(workdir, exist_ok=True)
    try:
        if DL_SEM.locked():
            await prog.stage("Queue mein hai… (pehle wale khatam hone do)")
        async with DL_SEM:
            active_downloads += 1
            try:
                await prog.stage("Starting…")
                if compress is not None:
                    async with ENC_SEM:
                        res = await loop.run_in_executor(None, stage_a, job, cut, workdir, prog, audio, compress)
                else:
                    res = await loop.run_in_executor(None, stage_a, job, cut, workdir, prog, audio, compress)
                base = sanitize_filename(
                    (name_from_url(job.src.dl_url) if job.src.use_url_name else job.src.title) or "video"
                )
                reply = await valid_reply(client, job.chat_id, job.reply_to)

                cmpr = res.get("cmp")
                if cmpr and not cmpr["skipped"]:
                    await send_one(
                        client, job.chat_id, cmpr["path"], f"{base}_compressed.mp4",
                        cmp_caption(base, cmpr), cmpr["info"], thumb_kb(job, cmpr["info"].get("duration")),
                        prog, "cmp", workdir, reply
                    )
                elif res.get("audio"):
                    apath = res["audio"]
                    cap = (
                        f"🎵 <b>Audio</b>\n\n🎬 <b>Name:</b> <code>{esc(base)}</code>\n"
                        f"⏱ <b>Length:</b> <code>{fmt_dur(res['dur'])}</code>\n"
                        f"📦 <b>Size:</b> <code>{humanbytes(os.path.getsize(apath))}</code>"
                    )
                    await client.send_audio(
                        chat_id=job.chat_id, audio=apath, caption=cap, file_name=f"{base}.m4a",
                        duration=int(res["dur"] or 0), title=base[:60], reply_to_message_id=reply,
                        progress=_up_progress, progress_args=(prog, "📤 Uploading (audio)")
                    )
                elif res["cut"]:
                    c = res["cut"]
                    csize = os.path.getsize(c["path"])
                    cdur = c["info"].get("duration") or (c["end"] - c["start"])
                    extra = (
                        f"\n✂️ <b>Cut:</b> <code>{fmt_hms(c['start'])} → {fmt_hms(c['end'])}</code>"
                        f" ({'frame-exact' if c['mode'] == 'exact' else 'keyframe'})"
                    )
                    if c["method"] != "none":
                        extra += "\n🗜 2GB limit ke liye compress hui"
                    fname = f"{base}_cut_{fmt_hms(c['start']).replace(':', '-')}_{fmt_hms(c['end']).replace(':', '-')}.mp4"
                    await send_one(
                        client, job.chat_id, c["path"], fname,
                        make_caption(base, c["info"], csize, extra), c["info"],
                        thumb_kb(job, cdur), prog, "cut", workdir, reply
                    )
                else:
                    full = res["full"]
                    method = "none"
                    if os.path.getsize(full) > UPLOAD_LIMIT:
                        full, method = await loop.run_in_executor(
                            None, fit_to_limit, full, os.path.join(workdir, "full_fit.mp4"), prog
                        )
                    finfo = (await loop.run_in_executor(None, ffprobe, full)) or res["info"]
                    extra = f"\nℹ️ {esc(res['note'])}" if res.get("note") else ""
                    if method == "partial":
                        extra += "\n🗜 2GB limit: sirf beech ka hissa compress hua"
                    elif method == "full":
                        extra += "\n🗜 2GB limit ke liye compress hui"
                    q_suffix = f"_{job.q}p" if 0 < job.q < MAX_HEIGHT else ""
                    await send_one(
                        client, job.chat_id, full, f"{base}{q_suffix}.mp4",
                        make_caption(base, finfo, os.path.getsize(full), extra), finfo,
                        thumb_kb(job, finfo.get("duration") or res["dur"]), prog, "full", workdir, reply
                    )
                try:
                    await msg.delete()
                except Exception:
                    pass
            finally:
                active_downloads -= 1
    except Exception as e:
        logging.exception("delivery failed")
        try:
            await msg.edit_text(
                f"❌ <b>Error:</b>\n<code>{esc(str(e)[:350])}</code>\n\n"
                f"Dobara try karne ke liye link phir bhejo."
            )
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        job.busy = False


START_TEXT = (
    "👋 <b>Seal Engine Video Downloader + Post Maker + Smart Compressor</b>\n\n"
    "<b>1️⃣ Video Download (Seal Engine):</b> Kisi bhi website ka link bhejo. Quality select karne ke buttons milenge "
    "(1080p, 720p, 480p, 360p) + <b>✂️ Cut</b> aur <b>🎵 Audio</b>.\n"
    "<b>2️⃣ Post Maker (Single Clean Post):</b> Diskwala/Terabox ya Telegram links (media ke saath ya bina media) bhejo. "
    "Bot bina spam kiye ek hi sundar template mein saare links set karke dega.\n"
    "<b>3️⃣ Auto Smart Compress:</b> Koi bhi video file seedha bot ko bhejo — bot bina quality giraye (SSIM 0.985 Lock) "
    "apne aap uska size kam karke wapas bhej dega!\n\n"
    "🗣 Direct commands: <code>link 720p</code>, <code>link 1:20-2:45</code>, <code>template 5</code>, <code>template random</code>"
)

HELP_TEXT = (
    "<b>📖 Kaise use karein</b>\n\n"
    "• <b>Video Download:</b> Link bhejo -> Quality button dabao (1080p/720p/480p/360p).\n"
    "• <b>Auto Video Compress:</b> Seedha video file forward/send karo -> Bot automatic Fixed Smart Compression se size kam kar dega.\n"
    "• <b>Post Maker:</b> Photo/Video ke saath ya text mein Terabox/Diskwala + Telegram link bhejo -> Ek single post mein template banke aayega.\n"
    "• <b>Template Fix/Random:</b> <code>template 5</code> ya <code>template random</code> likho."
)


def status_text():
    return (
        f"📊 <b>Status</b>\n• Analyzing: <code>{analyzing}</code>\n"
        f"• Active downloads: <code>{active_downloads}</code>"
    )


async def _allowed(_, __, update):
    d = getattr(update, "date", None)
    if START_TS and isinstance(d, datetime) and d.timestamp() < START_TS - STALE_SEC:
        return False
    u = getattr(update, "from_user", None)
    return not ALLOWED or bool(u and u.id in ALLOWED)

allowed = filters.create(_allowed)


def fail_text(err, diag):
    lines = ["❌ <b>Video extract nahi ho paaya.</b>", f"<code>{esc((err or '')[:200])}</code>"]
    if why(err):
        lines.append(f"💡 <b>Wajah:</b> {esc(why(err))}")
    cands = diag.get("cands") or []
    if cands:
        lines.append("\n🔎 <b>Ye links mile (sahi lage to button dabao):</b>")
        for i, c in enumerate(cands[:5], 1):
            lines.append(f"{i}. <code>{esc(c['url'][:140])}</code> ({c['kind']})")
    net = diag.get("net") or []
    if net:
        lines.append("\n📡 <b>Network mein ye media requests dikhi:</b>")
        lines += [f"<code>{esc(u[:140])}</code>" for u in net[:4]]
    return "\n".join(lines)[:3900]


def fail_kb(fid, diag):
    rows = []
    for i, c in enumerate((diag.get("cands") or [])[:5]):
        label = name_from_url(c["url"])[:22] or urlparse(c["url"]).netloc[:22]
        rows.append([InlineKeyboardButton(f"▶️ {i + 1}. {c['kind'].upper()}  {label}", callback_data=f"tc:{fid}:{i}")])
    rows.append([
        InlineKeyboardButton("🤖 Gemini se poochho", callback_data=f"gm:{fid}"),
        InlineKeyboardButton("🩺 Diagnose", callback_data=f"dg:{fid}")
    ])
    return InlineKeyboardMarkup(rows)


async def dispatch_intent(client, job: Job, it: Intent, st):
    D = job.src.duration
    if it.q:
        job.q = it.q
    if it.action == "audio":
        return spawn(run_delivery(client, job, None, True))
    if it.action == "compress":
        return spawn(run_delivery(client, job, None, False, parse_compress_opts(it.arg)))
    if it.action == "download":
        return spawn(run_delivery(client, job, None))
    if it.action == "cut":
        if it.start is not None and it.end is not None and it.end > it.start and not (D and it.start >= D):
            job.cs, job.ce = it.start, (min(it.end, D) if D else it.end)
            if it.mode:
                job.mode = it.mode
            return spawn(run_delivery(client, job, (job.cs, job.ce, job.mode)))
        job.cs, job.ce, job.sel, job.fresh = 0.0, D or 600.0, "s", True
        return await st.edit_text(panel_text(job), reply_markup=panel_kb(job))
    await st.edit_text(card_text(job), reply_markup=card_kb(job))


async def finish_analysis(client, status_msg, chat_id, src, reply_to, intent):
    job = Job(id=INSTANCE + secrets.token_hex(3), chat_id=chat_id, src=src, msg=status_msg, reply_to=reply_to)
    job.ce = src.duration or 600.0
    JOBS[job.id] = job
    LAST[chat_id] = job.id
    await dispatch_intent(client, job, intent, status_msg)


async def retry_failed(client, msg, f, act, arg):
    loop = asyncio.get_running_loop()

    async def status(text):
        try:
            await msg.edit_text(f"{text}\n<code>{esc(f['url'][:60])}</code>")
        except Exception:
            pass

    try:
        if act == "tc":
            u = f["cands"][int(arg)]["url"]
        else:
            await status("🤖 Gemini se link nikal raha hoon…")
            u = await loop.run_in_executor(None, gemini_find_url, f["url"], f.get("html"), f.get("net"), f.get("cands"))
            if not u:
                return await msg.edit_text(
                    "🤖 Gemini ko bhi video link nahi mila.\n\n" + fail_text("", f),
                    reply_markup=fail_kb(f["id"], f)
                )
        await status("🧪 Check kar raha hoon…")
        src = await source_from_media(u, f["url"], f["cookies"], title=f.get("title"), thumbs=f.get("thumbs"))
        await finish_analysis(client, msg, f["chat_id"], src, f["reply_to"], f["intent"])
    except Exception as e:
        try:
            await msg.edit_text(
                f"❌ <code>{esc(str(e)[:200])}</code>\n\n" + fail_text("", f),
                reply_markup=fail_kb(f["id"], f)
            )
        except Exception:
            pass


async def analyze_url(client, status_msg: Message, chat_id: int, url: str, reply_to=None, intent=None, note=""):
    global analyzing
    intent = intent or Intent()
    short = esc(url[:60])

    async def status(text):
        try:
            await status_msg.edit_text(f"{note}{text}\n<code>{short}</code>")
        except Exception:
            pass

    async with ANALYZE_SEM:
        analyzing += 1
        try:
            src = await asyncio.wait_for(resolve_source(url, status), timeout=300)
            await finish_analysis(client, status_msg, chat_id, src, reply_to, intent)
        except Exception as e:
            logging.error(f"[analyze] {url}: {e}")
            diag = getattr(e, "diag", None)
            try:
                if diag:
                    fid = INSTANCE + secrets.token_hex(3)
                    FAILS[fid] = {
                        **diag, "id": fid, "chat_id": chat_id, "reply_to": reply_to,
                        "intent": intent, "created": time.time()
                    }
                    await status_msg.edit_text(fail_text(str(e), diag), reply_markup=fail_kb(fid, diag))
                else:
                    await status_msg.edit_text(
                        "❌ <b>Video extract nahi ho paaya.</b>\n\n"
                        f"<code>{esc(str(e)[:150] or 'timeout')}</code>"
                    )
            except Exception:
                pass
        finally:
            analyzing -= 1


TG_HOSTS = ("t.me", "telegram.me", "telegram.dog", "telesco.pe")
FILEHOSTS = (
    "terabox", "diskwala", "mirrobox", "nephobox", "momerybox", "tibibox", "4funbox", "gibibox",
    "terafileshare", "terasharelink", "teraboxlink", "teraboxurl", "teraboxshare", "mega.nz", "mediafire.com",
    "gofile.io", "pixeldrain.com", "streamtape", "doodstream", "dood.", "mixdrop", "filemoon", "streamwish",
    "vidhide", "krakenfiles", "1fichier", "drive.google.com", "dropbox.com", "mdisk"
)
STYLES = [
    (0x1D400, 0x1D41A, 0x1D7CE), (0x1D5D4, 0x1D5EE, 0x1D7EC), (0x1D468, 0x1D482, 0x1D7CE),
    (0x1D670, 0x1D68A, 0x1D7F6), (0x1D63C, 0x1D656, 0x1D7EC)
]
FRAMES = [
    ("🦢✨", "✨🦢"), ("🍑", "🍑"), ("🔞", "🔞"), ("😈", "😈"), ("💦", "💦"), ("🥵", "🥵"), ("👅", "👅"),
    ("💋", "💋"), ("🍒", "🍒"), ("🍓", "🍓"), ("🌶️", "🌶️"), ("🫦", "🫦"), ("🍆", "🍆"), ("🔥🍑", "🍑🔥"),
    ("😈🔥", "🔥😈"), ("💦🍑", "🍑💦"), ("🔞✨", "✨🔞"), ("🥵💦", "💦🥵"), ("💋🔥", "🔥💋"), ("🍒🍑", "🍑🍒"),
    ("😈💦", "💦😈"), ("🫦💋", "💋🫦"), ("🌶️🔥", "🔥🌶️"), ("🍑🍆", "🍆🍑")
]
TITLES = [
    "18+ EXCLUSIVE", "ADULT ANIMATION", "EXCLUSIVE ANIMATION", "AFTER DARK SPECIAL", "SPICY UPLOAD",
    "HOT & STEAMY", "LATE NIGHT SPECIAL", "PRIVATE SHOW", "VIP ADULT UPLOAD", "NAUGHTY NIGHT",
    "UNCENSORED RELEASE", "MIDNIGHT TREAT", "FRESH & HOT DROP", "ADULTS ONLY", "SECRET SHOW",
    "HOT NEW EPISODE", "STEAMY EPISODE", "EXCLUSIVE PREMIERE", "FULL VIDEO", "NEW UPLOAD", "SPECIAL SHOW",
    "TODAY'S TREAT", "TRENDING NOW", "FRESH EPISODE", "FORBIDDEN FRUIT", "SWEET & SPICY", "LATE NIGHT DROP",
    "VIP EXCLUSIVE", "ONLY HERE", "FULL HD UPLOAD"
]
SEPS = [
    "━" * 18, "─" * 16, "▬" * 12, "•┈┈┈┈•✦•┈┈┈┈•", "═" * 15, "✦━━━━━━━━━✦", "⋆⋅☆⋅⋆⋅☆⋅⋆⋅☆⋅⋆", "┄" * 14,
    "▰▱" * 8, "•" * 18, "◈━━━━━━━━━━◈", "⟡" * 10, "━━━━ ✦ ━━━━", "〰️" * 8, "═══ ◆ ═══", "✧･ﾟ: *✧･ﾟ:* *:･ﾟ✧",
    "🍑" * 7, "🔞 ━━━━━━━━━ 🔞", "💦" * 8, "😈 • • • • • 😈", "🔥━━━━━━━━━━🔥", "💋 ─ ─ ─ ─ ─ 💋"
]
WATCH = [
    ("🎬", "WATCH NOW", "👇🔥"), ("🍑", "WATCH HERE", "👇💦"), ("🔞", "FULL VIDEO", "👇😈"),
    ("😈", "PLAY NOW", "👇🔥"), ("💦", "WATCH FULL VIDEO", "👇"), ("🥵", "CLICK TO WATCH", "👇🔥"),
    ("📺", "STREAM NOW", "👇💋"), ("🎥", "VIDEO LINK", "👇🍑"), ("⬇️", "DOWNLOAD HERE", "👇💦"),
    ("🔥", "GET THE VIDEO", "👇😈")
]
PASS = [
    ("🔐", "PASS LINK", "👇"), ("🗝️", "ACCESS LINK", "👇😈"), ("🔓", "UNLOCK PASS", "👇💦"),
    ("📢", "JOIN CHANNEL", "👇🔥"), ("🔞", "PRIVATE CHANNEL", "👇"), ("😈", "VIP CHANNEL", "👇"),
    ("🍑", "BACKUP CHANNEL", "👇"), ("💋", "MAIN CHANNEL", "👇"), ("🎟️", "ENTRY PASS", "👇🔥"),
    ("📌", "IMPORTANT LINK", "👇")
]
FOOT = [
    ("🍑", "ENJOY THE SHOW", "❤️‍🔥"), ("😈", "HAVE FUN TONIGHT", ""), ("💦", "ENJOY & SHARE", ""),
    ("🔞", "ADULTS ONLY", ""), ("🥵", "STAY TUNED FOR MORE", ""), ("💋", "THANKS FOR WATCHING", ""),
    ("🔥", "ENJOY THE VIDEO", "🍑"), ("🤫", "KEEP IT PRIVATE", ""), ("🍆", "SEE YOU NEXT TIME", "💦"),
    ("🫦", "LIKE & SHARE", "💋")
]
BULLETS = ["🍑", "😈", "💦", "🔥", "💋", "🔞", "🥵", "🍒"]
N_TPL = 999
SAMPLE_VIDEO = "https://example.com/video-link"
SAMPLE_TG = "https://t.me/+example"


def fancy(text, style):
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


def tpl(idx):
    if idx == 1:
        return dict(
            st=STYLES[0], fl="🦢✨", fr="✨🦢", title="EXCLUSIVE ANIMATION", sep="━" * 18,
            w=("🎬", "WATCH NOW", "👇🔥"), p=("🔐", "PASS LINK", "👇"), f=("🍑", "ENJOY THE SHOW", "❤️‍🔥"),
            b="🔗", qb="🔗 ", sp=True
        )
    r = random.Random(idx * 7919 + 13)
    fl, fr = r.choice(FRAMES)
    return dict(
        st=r.choice(STYLES), fl=fl, fr=fr, title=r.choice(TITLES), sep=r.choice(SEPS),
        w=r.choice(WATCH), p=r.choice(PASS), f=r.choice(FOOT), b=r.choice(BULLETS), qb="", sp=False
    )


def quote(x):
    return f"<blockquote>{x}</blockquote>"


def plain_len(x):
    return len(html.unescape(re.sub(r"<[^>]+>", "", x)).encode("utf-16-le")) // 2


def _lab(t, tup):
    pre, text, suf = tup
    return f"{pre} {esc(fancy(text, t['st']))}" + (f" {suf}" if suf else "")


def diagnose(url):
    out = [f"🩺 <b>Diagnose</b>\n<code>{esc(url[:90])}</code>\n"]
    org = ""
    try:
        j = requests.get("https://ipinfo.io/json", timeout=8).json()
        org = (j.get("org") or "").lower()
        out.append(f"🖥 Server IP: <code>{esc(j.get('ip'))}</code> • {esc(j.get('org'))} • {esc(j.get('country'))}")
    except Exception:
        out.append("🖥 Server IP: pata nahi chala")
    dc = any(k in org for k in ("microsoft", "azure", "amazon", "google", "digitalocean", "ovh", "hetzner", "linode", "oracle", "vultr"))
    probes = []
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
        probes.append(("requests", r.status_code, r.headers.get("server", ""), r.headers.get("cf-mitigated", "")))
    except Exception as e:
        probes.append(("requests", f"error {str(e)[:50]}", "", ""))
    try:
        from curl_cffi import requests as cr
        for name in ("chrome", "safari"):
            try:
                r = cr.get(url, impersonate=name, timeout=15, allow_redirects=True)
                probes.append((f"curl_cffi/{name}", r.status_code, r.headers.get("server", ""), r.headers.get("cf-mitigated", "")))
            except Exception as e:
                probes.append((f"curl_cffi/{name}", f"error {str(e)[:50]}", "", ""))
    except Exception:
        pass
    for n, st, srv, cfm in probes:
        out.append(f"🌐 {esc(n)}: <code>{esc(st)}</code>" + (f" • {esc(srv)}" if srv else "") + (" • challenge" if cfm else ""))
    ok, err = False, ""
    try:
        info = ytdlp_extract(url, None)
        ok = bool(info)
        out.append(f"🎞 yt-dlp: ok • {esc(info.get('extractor_key'))} • formats: {len(info.get('formats') or [])}")
    except Exception as e:
        err = str(e)
        out.append(f"🎞 yt-dlp: fail • <code>{esc(err[:220])}</code>")
    codes = [p[1] for p in probes if isinstance(p[1], int)]
    blocked = any(c in (401, 403, 429, 503) for c in codes)
    if ok:
        verdict = "✅ yt-dlp ko video mil rahi hai."
    elif blocked and dc:
        verdict = "🚫 Site datacenter IP ko block kar rahi hai. Residential PROXY_URL lagao."
    elif blocked:
        verdict = "🚫 Site bot ko block kar rahi hai (Cloudflare/captcha). cookies.txt ya proxy try karo."
    elif codes and all(c < 400 for c in codes):
        verdict = "🤔 Page khul raha hai par yt-dlp is site ko nahi samajhta."
    else:
        verdict = "⏳ Site respond nahi kar rahi."
    reason = why(err)
    out.append("\n" + verdict + (f"\n💡 {esc(reason)}" if reason and not ok else ""))
    return "\n".join(out)[:3900]


async def run_diag(st, url):
    loop = asyncio.get_running_loop()
    try:
        text = await asyncio.wait_for(loop.run_in_executor(None, diagnose, url), timeout=150)
    except Exception as e:
        text = f"❌ Diagnose fail: <code>{esc(str(e)[:200])}</code>"
    try:
        await st.edit_text(text)
    except Exception:
        pass


def track_add(chat, rid, mid):
    TRACK.setdefault((chat, rid), []).append(mid)


def track_message(message):
    key = (message.chat.id, message.id)
    TRACK.setdefault(key, [])
    orig = message.reply_text

    async def wrapped(*a, **k):
        m = await orig(*a, **k)
        mid = getattr(m, "id", None)
        if isinstance(mid, int):
            track_add(key[0], key[1], mid)
        return m
    message.reply_text = wrapped


async def bot_replies(client, chat, rid):
    out = []
    for lo in (rid + 1, rid + 101):
        try:
            msgs = await client.get_messages(chat, list(range(lo, lo + 100)))
        except Exception:
            return out or None
        for m in msgs or []:
            if (m and not getattr(m, "empty", False) and getattr(getattr(m, "from_user", None), "is_self", False)
                    and getattr(m, "reply_to_message_id", None) == rid):
                out.append(m.id)
        if out:
            break
    return sorted(out)


async def seen_before(client, message):
    return bool(await bot_replies(client, message.chat.id, message.id))


async def confirm(client, message):
    key = (message.chat.id, message.id)
    if key in LOST:
        return False
    mine = set(TRACK.get(key, []))
    if key in CONFIRMED or not mine:
        return True
    await asyncio.sleep(CLAIM_WAIT)
    ids = await bot_replies(client, key[0], key[1])
    if not ids or ids[0] in mine:
        CONFIRMED.add(key)
        return True
    LOST.add(key)
    try:
        await client.delete_messages(key[0], sorted(mine))
    except Exception:
        pass
    return False


def pack(t, vids, tgs, first_limit):
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


class TgMaybeSent(Exception):
    pass


class TgFlood(Exception):
    def __init__(self, retry):
        super().__init__(f"flood {retry}")
        self.retry = retry


def _tg_call(method, data):
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=data, timeout=(15, 90))
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


async def tg(method, **params):
    loop = asyncio.get_running_loop()
    for _ in range(3):
        try:
            return await loop.run_in_executor(None, _tg_call, method, params)
        except TgFlood as e:
            await asyncio.sleep(e.retry + 1)
    return await loop.run_in_executor(None, _tg_call, method, params)


def unquote_html(x):
    return x.replace("<blockquote>", "<code>").replace("</blockquote>", "</code>")


def reply_params(rid):
    return {"message_id": rid, "allow_sending_without_reply": True}


MEDIA_METHOD = {"photo": "sendPhoto", "animation": "sendAnimation", "video": "sendVideo", "document": "sendDocument"}


async def out_text(client, chat, rid, text):
    try:
        res = await tg(
            "sendMessage", chat_id=chat, text=text, parse_mode="HTML",
            reply_parameters=reply_params(rid), link_preview_options={"is_disabled": True}
        )
        if isinstance(res, dict) and res.get("message_id"):
            track_add(chat, rid, res["message_id"])
        return res
    except TgMaybeSent:
        return None
    except Exception:
        pass
    m = await safe(
        client.send_message, chat, unquote_html(text),
        reply_to_message_id=rid, disable_web_page_preview=True
    )
    if isinstance(getattr(m, "id", None), int):
        track_add(chat, rid, m.id)
    return m


async def out_media(client, chat, rid, kind, fid, caption):
    try:
        res = await tg(
            MEDIA_METHOD[kind], chat_id=chat, **{kind: fid}, caption=caption,
            parse_mode="HTML", reply_parameters=reply_params(rid)
        )
        if isinstance(res, dict) and res.get("message_id"):
            track_add(chat, rid, res["message_id"])
        return res
    except TgMaybeSent:
        return None
    except Exception:
        pass
    sender = {
        "photo": client.send_photo, "animation": client.send_animation,
        "video": client.send_video, "document": client.send_document
    }[kind]
    m = await safe(sender, chat, fid, caption=unquote_html(caption), reply_to_message_id=rid)
    if isinstance(getattr(m, "id", None), int):
        track_add(chat, rid, m.id)
    return m


async def safe(fn, *a, **k):
    for _ in range(3):
        try:
            return await fn(*a, **k)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
    return await fn(*a, **k)


def is_tg(u):
    return urlparse(u).netloc.lower().split(":")[0] in TG_HOSTS


def is_filehost(u):
    host = urlparse(u).netloc.lower()
    return any(k in host for k in FILEHOSTS)


def extract_urls(message):
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


def media_of(m):
    for kind in ("photo", "animation", "video", "document"):
        obj = getattr(m, kind, None)
        if obj:
            return kind, obj.file_id
    return None, None


def is_video_media(m):
    if getattr(m, "video", None):
        return True
    d = getattr(m, "document", None)
    return bool(d and str(getattr(d, "mime_type", "") or "").startswith("video/"))


def route_mode(text, urls, kind, isvid=False):
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


async def send_chunks(client, message, kind, fid, chunks):
    chat, rid = message.chat.id, message.id
    if (chat, rid) in LOST:
        return False
    first, rest = chunks[0], chunks[1:]
    if kind:
        fits = plain_len(first) <= 1024
        await out_media(client, chat, rid, kind, fid, first if fits else "")
        if not fits:
            rest = [first] + rest
    else:
        await out_text(client, chat, rid, first)
    if not await confirm(client, message):
        return False
    for c in rest:
        await asyncio.sleep(0.8)
        await out_text(client, chat, rid, c)
    return True


def split_links(urls):
    return [u for u in urls if not is_tg(u)][:MAX_POST_LINKS], [u for u in urls if is_tg(u)][:6]


def pick_tpl(chat_id):
    return tpl(PREFS.get(chat_id) or random.randint(1, N_TPL))


# Fixed: Never loops or spams multiple templates for a single message
async def post_message(client, message, urls, kind, fid):
    vids, tgs = split_links(urls)
    if not vids:
        return await safe(message.reply_text, "🎬 Video link nahi mila, sirf Telegram link hai.", quote=True)
    t = pick_tpl(message.chat.id)
    limit = 1024 if kind else 4096
    return await send_chunks(client, message, kind, fid, pack(t, vids, tgs, limit))


async def album_post(client, message):
    chat, key, now = message.chat.id, (message.chat.id, message.media_group_id), time.time()
    for k in [k for k, v in ALBUMS.items() if now - v > 120]:
        ALBUMS.pop(k, None)
    if key in ALBUMS:
        return
    ALBUMS[key] = now
    await asyncio.sleep(1.2)
    group = sorted(await client.get_media_group(chat, message.id), key=lambda m: m.id)
    anchor = group[0]
    track_message(anchor)
    if await seen_before(client, anchor):
        return
    urls = []
    for m in group:
        for u in extract_urls(m):
            if u not in urls:
                urls.append(u)
    vids, tgs = split_links(urls)
    if not vids:
        return await safe(anchor.reply_text, "🔗 Is album mein video link nahi mila.", quote=True)
    chunks = pack(pick_tpl(chat), vids, tgs, 1024)
    cls = {"photo": InputMediaPhoto, "video": InputMediaVideo, "document": InputMediaDocument}
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
                track_add(chat, anchor.id, r["message_id"])
    except Exception:
        media = [
            cls[k](f, caption=unquote_html(chunks[0]) if (i == 0 and fits) else "", parse_mode=enums.ParseMode.HTML)
            for i, (k, f) in enumerate(metas)
        ]
        sent = await safe(client.send_media_group, chat, media, reply_to_message_id=anchor.id)
        for r in sent if isinstance(sent, list) else []:
            if isinstance(getattr(r, "id", None), int):
                track_add(chat, anchor.id, r.id)
    if not await confirm(client, anchor):
        return
    for c in (chunks[1:] if fits else chunks):
        await asyncio.sleep(0.8)
        await out_text(client, chat, anchor.id, c)


async def template_reply(message, arg):
    chat = message.chat.id
    if arg.isdigit() and arg != "0":
        n = int(arg)
        if not 1 <= n <= N_TPL:
            return await safe(message.reply_text, f"🎨 Template number 1 se {N_TPL} ke beech likho.", quote=True)
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


async def misc_reply(message: Message, it: Intent) -> bool:
    if it.action == "status":
        await message.reply_text(status_text(), quote=True)
    elif it.action == "help":
        await message.reply_text(HELP_TEXT, quote=True)
    elif it.action == "start":
        await message.reply_text(START_TEXT, quote=True)
    elif it.action == "template":
        await template_reply(message, it.arg)
    elif it.action in ("ask", "chat"):
        await message.reply_text(f"🤖 {esc(it.reply or 'Samajh nahi aaya, thoda aur batao.')}", quote=True)
    else:
        return False
    return True


async def download_message(client, message, text, urls):
    urls = urls[:10]
    it = await get_intent(URL_REGEX.sub(" ", text).strip(), True, "")
    if it.action in ("status", "help", "start", "template", "ask") and await misc_reply(message, it):
        return
    note = f"🤖 {esc(it.reply)}\n\n" if it.reply else ""
    if it.action == "diag":
        sts = [
            await message.reply_text(f"🩺 Check kar raha hoon…\n<code>{esc(u[:60])}</code>", quote=True)
            for u in urls[:3]
        ]
        if not await confirm(client, message):
            return
        for st, u in zip(sts, urls):
            spawn(run_diag(st, u))
        return
    sts = [
        await message.reply_text(f"{note}🔍 <b>Analyzing…</b>\n<code>{esc(u[:60])}</code>", quote=True)
        for u in urls
    ]
    if not await confirm(client, message):
        return
    for st, u in zip(sts, urls):
        spawn(analyze_url(client, st, message.chat.id, u, message.id, it, note))


async def chat_message(client, message, text):
    job = JOBS.get(LAST.get(message.chat.id))
    it = await get_intent(text, False, job.src.title if job else "")
    if await misc_reply(message, it):
        return
    if not job:
        return await message.reply_text("🔗 Pehle video ka link ya video file bhejo.", quote=True)
    if it.action == "none":
        return await message.reply_text(
            "🤖 Aise likho: <code>720p mein download</code>, <code>audio do</code>, "
            "<code>1:20 se 2:45 cut karo</code>, <code>help</code>", quote=True
        )
    if job.busy:
        return await message.reply_text("⏳ Pichla kaam abhi chal raha hai.", quote=True)
    note = f"🤖 {esc(it.reply)}\n\n" if it.reply else ""
    st = await message.reply_text(f"{note}⏳ Shuru kar raha hoon…", quote=True)
    if not await confirm(client, message):
        return
    job.msg = st
    await dispatch_intent(client, job, it, st)


async def compress_run(client, c):
    loop = asyncio.get_running_loop()
    c["busy"] = True
    st = c["st"]
    prog = Progress(loop, st, f"🗜 <b>Smart Compress: {esc(c['name'][:55])}</b>")
    workdir = os.path.join(DOWNLOAD_DIR, f"cmp_{c['id']}_{int(time.time())}")
    os.makedirs(workdir, exist_ok=True)
    try:
        if ENC_SEM.locked():
            await prog.stage("Queue mein hai… (pehla encode khatam hone do)")
        async with ENC_SEM:
            await prog.stage("⬇️ Video download ho rahi hai…")
            path = await client.download_media(
                c["fid"], file_name=os.path.join(workdir, f"in{c['ext']}"),
                progress=_up_progress, progress_args=(prog, "⬇️ Downloading Video")
            )
            r = await loop.run_in_executor(
                None, smart_compress, path, os.path.join(workdir, "out.mp4"), prog, dict(c["opts"])
            )
            if r["skipped"]:
                return await st.edit_text(f"✅ <b>Info:</b> {esc(r['reason'])}.")
            reply = await valid_reply(client, c["chat"], c["rid"])
            await send_one(
                client, c["chat"], r["path"], f"{c['name']}_compressed.mp4",
                cmp_caption(c["name"], r), r["info"], None, prog, "cmp", workdir, reply
            )
            try:
                await st.delete()
            except Exception:
                pass
            CJOBS.pop(c["id"], None)
    except Exception as e:
        logging.exception("compress failed")
        try:
            await st.edit_text(f"❌ <b>Compress fail:</b>\n<code>{esc(str(e)[:300])}</code>")
        except Exception:
            pass
    finally:
        c["busy"] = False
        shutil.rmtree(workdir, ignore_errors=True)


# Fixed Auto Smart Compression: Video bhejte hi seedha compress chalu karega
async def compress_message(client, message, text):
    media = message.video or message.document
    fname = getattr(media, "file_name", None) or "video.mp4"
    c = {
        "id": INSTANCE + secrets.token_hex(3),
        "chat": message.chat.id,
        "rid": message.id,
        "fid": media.file_id,
        "name": sanitize_filename(os.path.splitext(fname)[0]),
        "ext": os.path.splitext(fname)[1] or ".mp4",
        "size": getattr(media, "file_size", 0) or 0,
        "dur": getattr(media, "duration", 0) or 0,
        "w": getattr(media, "width", 0) or 0,
        "h": getattr(media, "height", 0) or 0,
        "opts": parse_compress_opts(text),
        "busy": False,
        "created": time.time(),
    }
    CJOBS[c["id"]] = c
    st = await message.reply_text(
        f"🗜 <b>Fixed Smart Compression Starting…</b>\n"
        f"🎬 <code>{esc(c['name'][:70])}</code>\n"
        f"📦 Original Size: <code>{humanbytes(c['size'])}</code>\n"
        f"✨ Mode: <code>Visual Lossless (SSIM 0.985 Lock)</code>",
        quote=True
    )
    c["st"] = st
    if not await confirm(client, message):
        CJOBS.pop(c["id"], None)
        return
    spawn(compress_run(client, c))


async def single_reply(client, message, text):
    track_message(message)
    if await seen_before(client, message):
        return
    await message.reply_text(text, quote=True)
    await confirm(client, message)


@app.on_message(filters.command("start") & filters.private & allowed)
async def start_cmd(client, message: Message):
    await single_reply(client, message, START_TEXT)


@app.on_message(filters.command("help") & filters.private & allowed)
async def help_cmd(client, message: Message):
    await single_reply(client, message, HELP_TEXT)


@app.on_message(filters.command("queue") & filters.private & allowed)
async def queue_cmd(client, message: Message):
    await single_reply(client, message, status_text())


@app.on_message(filters.private & allowed & ~filters.command(["start", "help", "queue"]))
async def router(client, message: Message):
    now = time.time()
    for k in [k for k, v in list(SEEN.items()) if now - v > 900]:
        SEEN.pop(k, None)
        TRACK.pop(k, None)
        MSG_LOCKS.pop(k, None)
        CONFIRMED.discard(k)
        LOST.discard(k)
    key = (message.chat.id, message.id)
    if key in SEEN:
        return
    SEEN[key] = now
    lock = MSG_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        track_message(message)
        try:
            if message.media_group_id:
                return await album_post(client, message)
            text = message.text or message.caption or ""
            urls = extract_urls(message)
            kind, fid = media_of(message)
            mode = route_mode(text, urls, kind, is_video_media(message))
            logging.info(f"[router] chat={message.chat.id} msg={message.id} mode={mode} links={len(urls)} media={kind}")
            if mode in ("post", "dl", "nolink", "chat", "compress") and await seen_before(client, message):
                return
            if mode == "post":
                await post_message(client, message, urls, kind, fid)
            elif mode == "dl":
                await download_message(client, message, text, urls)
            elif mode == "compress":
                await compress_message(client, message, text)
            elif mode == "nolink":
                await safe(message.reply_text, "🔗 Is message mein koi link nahi mila.", quote=True)
            elif mode == "chat":
                await chat_message(client, message, text)
            await confirm(client, message)
        except Exception as e:
            logging.exception("router failed")
            try:
                await message.reply_text(f"❌ <code>{esc(str(e)[:200])}</code>", quote=True)
            except Exception:
                pass


@app.on_callback_query(allowed)
async def on_callback(client, cq: CallbackQuery):
    parts = (cq.data or "").split(":")
    act = parts[0]
    if len(parts) > 1 and not parts[1].startswith(INSTANCE):
        await asyncio.sleep(2)
        try:
            await cq.answer("⌛ Ye request purani ho gayi — link dobara bhejo.", show_alert=True)
        except Exception:
            pass
        return

    if act in ("tc", "gm", "dg"):
        f = FAILS.get(parts[1]) if len(parts) > 1 else None
        if not f:
            return await cq.answer("⌛ Ye request purani ho gayi — link dobara bhejo.", show_alert=True)
        if act == "gm" and not ai_model:
            return await cq.answer("⚠️ Gemini key set nahi hai (GEMINI_API_KEY)", show_alert=True)
        await cq.answer("⏳ Check kar raha hoon…")
        if act == "dg":
            st = await client.send_message(
                cq.message.chat.id, "🩺 Diagnose ho raha hai…", reply_to_message_id=cq.message.id
            )
            return spawn(run_diag(st, f["url"]))
        return spawn(retry_failed(client, cq.message, f, act, parts[2] if len(parts) > 2 else ""))

    job = JOBS.get(parts[1]) if len(parts) > 1 else None
    if not job:
        return await cq.answer("⌛ Ye request purani ho gayi — link dobara bhejo.", show_alert=True)
    chat = cq.message.chat.id

    if act == "th":
        if job.thumb_sent:
            return await cq.answer("🖼 Thumbnail pehle hi bhej di hai")
        job.thumb_sent = True
        await cq.answer("🖼 HD thumbnail la raha hoon…")
        loop = asyncio.get_running_loop()
        path = await loop.run_in_executor(None, fetch_thumbnail, job)
        reply = await valid_reply(client, chat, job.reply_to)
        if not path:
            job.thumb_sent = False
            return await client.send_message(
                chat, "❌ Is video ka thumbnail nahi mil paaya.", reply_to_message_id=reply
            )
        try:
            await client.send_document(
                chat, path, caption=f"🖼 <b>{esc(job.src.title[:100])}</b>",
                file_name=f"{sanitize_filename(job.src.title)}{os.path.splitext(path)[1] or '.jpg'}",
                force_document=True, reply_to_message_id=reply
            )
        except Exception:
            job.thumb_sent = False
        return

    if job.busy:
        return await cq.answer("⏳ Ye video abhi download/process ho rahi hai…", show_alert=False)

    async def refresh():
        try:
            await cq.message.edit_text(panel_text(job), reply_markup=panel_kb(job))
        except Exception:
            pass

    if act == "dl":
        if len(parts) > 2 and parts[2].isdigit():
            job.q = int(parts[2])
        q_label = f"{job.q}p" if job.q else "Best"
        await cq.answer(f"⬇️ Downloading ({q_label})…")
        return spawn(run_delivery(client, job, None))
    if act == "au":
        await cq.answer("🎵 Audio extraction shuru…")
        return spawn(run_delivery(client, job, None, True))
    if act == "zp":
        await cq.answer("🗜 Smart Compress shuru…")
        return spawn(run_delivery(client, job, None, False, parse_compress_opts("")))
    if act == "cx":
        await cq.answer()
        return await cq.message.edit_text(card_text(job), reply_markup=card_kb(job))
    if act == "cg":
        D = job.src.duration
        if job.ce - job.cs < 1 or (D and job.cs >= D):
            return await cq.answer("⚠️ Start/End time sahi daalo", show_alert=True)
        await cq.answer("✂️ Cut + Download shuru…")
        return spawn(run_delivery(client, job, (job.cs, min(job.ce, D) if D else job.ce, job.mode)))
    if act == "cut":
        job.cs, job.ce, job.sel, job.fresh = 0.0, job.src.duration or 600.0, "s", True
    elif act == "cs":
        job.sel, job.fresh = parts[2], True
    elif act == "cd":
        push(job, parts[2])
    elif act == "cb":
        push(job, None)
    elif act == "cc":
        setattr(job, "cs" if job.sel == "s" else "ce", 0.0)
        job.fresh = True
    elif act == "cf":
        job.ce = job.src.duration or job.ce
    elif act == "cm":
        job.mode = "fast" if job.mode == "exact" else "exact"
    elif act == "cq":
        cyc = [0] + [h for h, _ in quality_options(job.src) if h]
        job.q = cyc[(cyc.index(job.q) + 1) % len(cyc)] if job.q in cyc else 0
    await cq.answer()
    await refresh()


async def janitor():
    while True:
        await asyncio.sleep(600)
        now = time.time()
        for jid, job in list(JOBS.items()):
            if now - job.created > 6 * 3600 and not job.busy:
                JOBS.pop(jid, None)
                for f in os.listdir(THUMB_DIR):
                    if f.startswith(jid + "_"):
                        _rm(os.path.join(THUMB_DIR, f))
        for fid, f in list(FAILS.items()):
            if now - f["created"] > 6 * 3600:
                FAILS.pop(fid, None)
        for cid, c in list(CJOBS.items()):
            if now - c["created"] > 6 * 3600 and not c["busy"]:
                CJOBS.pop(cid, None)
        for d in os.listdir(DOWNLOAD_DIR):
            p = os.path.join(DOWNLOAD_DIR, d)
            if os.path.isdir(p) and now - os.path.getmtime(p) > 4 * 3600:
                shutil.rmtree(p, ignore_errors=True)


async def main():
    global ANALYZE_SEM, DL_SEM, ENC_SEM, START_TS
    ANALYZE_SEM = asyncio.Semaphore(MAX_CONCURRENT)
    DL_SEM = asyncio.Semaphore(MAX_CONCURRENT)
    ENC_SEM = asyncio.Semaphore(1)
    for d in os.listdir(DOWNLOAD_DIR):
        shutil.rmtree(os.path.join(DOWNLOAD_DIR, d), ignore_errors=True)
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        logging.error("❌ ffmpeg/ffprobe install nahi hai! (apt install ffmpeg)")
    START_TS = time.time()
    await app.start()
    logging.info("🚀 Bot started (Seal Engine + Post Maker + Smart Compressor)")
    spawn(janitor())
    try:
        await idle()
    finally:
        await app.stop()


if __name__ == "__main__":
    app.run(main())
