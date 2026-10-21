import os
import re
import math
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
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from urllib.parse import urlparse, urljoin, unquote

import yt_dlp
import cloudscraper
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from pyrogram.errors import FloodWait
from playwright.async_api import async_playwright

from modules.post_maker import remember_sent, template_reply, URL_REGEX
from modules.smart_tools import (
    is_preview_url, is_hard_bad_url, preview_hd_variants, same_video_urls,
    download_best_thumbnail, highlight_start, nude_window,
    teasing_segments, motion_segments,
)
from modules.censor_engine import (
    censor_image, censor_any, video_seconds, CENSOR_MAX_SEC,
)

try:
    from yt_dlp.utils import DownloadCancelled
except Exception:
    class DownloadCancelled(Exception):
        pass

try:
    import google.generativeai as genai
except Exception:
    genai = None

try:
    from pyrogram import StopTransmission
except Exception:
    try:
        from pyrogram.errors import StopTransmission
    except Exception:
        class StopTransmission(Exception):
            pass

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()
PROXY_URL = os.environ.get("PROXY_URL", "")
FALLBACK_PROXY = os.environ.get("FALLBACK_PROXY", "")

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", 2))
MiB = 1024 * 1024
UPLOAD_LIMIT = int(os.environ.get("UPLOAD_LIMIT_MB", 1990)) * MiB
COMPRESS_TARGET = int(os.environ.get("COMPRESS_TARGET_MB", 1900)) * MiB
MAX_HEIGHT = 4320
FALLBACK_HEIGHT = 720
TIERS = (MAX_HEIGHT, FALLBACK_HEIGHT)
STANDARD_QUALITIES = (2160, 1440, 1080, 720, 480, 360)
COOKIES_FILE = "cookies.txt"
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


class Cancelled(Exception):
    pass


GEMINI_SKIP = (
    "lite", "image", "tts", "thinking", "exp", "live", "audio",
    "vision", "embedding", "learnlm", "gemma", "robotics", "computer"
)
GEMINI_DEAD = (
    "404", "not found", "no longer available", "not supported",
    "deprecated", "no longer supported"
)


def _ver_key(name):
    return tuple(int(x) for x in re.findall(r"\d+", name)) or (0,)


def gemini_candidates():
    names = []
    if GEMINI_MODEL:
        names.append(GEMINI_MODEL)
    names.append("gemini-flash-latest")
    found = []
    try:
        with ThreadPoolExecutor(max_workers=1) as ex:
            fut = ex.submit(lambda: [
                m.name.split("/")[-1] for m in genai.list_models()
                if "generateContent" in (getattr(m, "supported_generation_methods", None) or [])
            ])
            found = fut.result(timeout=20)
    except Exception as e:
        logging.warning(f"[ai] list_models fail: {str(e)[:120]}")
    flash = [n for n in found if "flash" in n and not any(k in n for k in GEMINI_SKIP)]
    flash.sort(key=_ver_key, reverse=True)
    names += flash
    names += ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    out = []
    for n in names:
        if n and n not in out:
            out.append(n)
    return out


class _AI:
    def __init__(self, names):
        self.names = list(names or [])
        self.i = 0
        self.cache = {}
        self._lock = threading.Lock()

    def generate_content(self, prompt, **kw):
        if not self.names:
            raise RuntimeError("No Gemini models configured")
        last = None
        for _ in range(len(self.names)):
            with self._lock:
                name = self.names[self.i]
                m = self.cache.get(name)
                if m is None:
                    m = self.cache[name] = genai.GenerativeModel(name)
            try:
                return m.generate_content(prompt, **kw)
            except Exception as e:
                last = e
                if any(k in str(e).lower() for k in GEMINI_DEAD):
                    logging.warning(f"[ai] {name} unavailable, next model")
                    with self._lock:
                        if self.names[self.i] == name:
                            self.i = (self.i + 1) % len(self.names)
                    continue
                raise
        raise (last or RuntimeError("Gemini generate_content failed"))


ai_model = None
if GEMINI_API_KEY and genai:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        ai_model = _AI(gemini_candidates())
        logging.info(f"✅ Gemini ready ({ai_model.names[:3]})")
    except Exception as e:
        logging.error(f"Gemini init failed: {e}")

STOP = {}
CHAT_TASKS = {}
_tl = threading.local()
NETERR_RE = re.compile(
    r"timed out|timeout|connection|max retries|unreachable|refused|reset|forbidden|403|429|unable to download|ssl|proxy", re.I
)

JOBS = {}
FAILS = {}
LAST = {}
CJOBS = {}
BG_TASKS = set()

ANALYZE_SEM = None
DL_SEM = None
ENC_SEM = None
active_downloads = 0
analyzing = 0
NO_IMP = False
INSTANCE = secrets.token_hex(2)

TIME_PAT = r"\d{1,2}(?::\d{1,2}){1,2}"
RANGE_RE = re.compile(rf"(?<![\d:])({TIME_PAT})\s*(?:-|–|—|to|se)\s*({TIME_PAT})(?![\d:])", re.I)


def _is_own_dir(name: str) -> bool:
    return (
        name.startswith(INSTANCE)
        or name.startswith(f"cz_{INSTANCE}")
        or name.startswith(f"cmp_{INSTANCE}")
        or name.startswith(f"ci_{INSTANCE}")
    )


def _ensure_sems():
    global ANALYZE_SEM, DL_SEM, ENC_SEM
    if ANALYZE_SEM is None:
        ANALYZE_SEM = asyncio.Semaphore(MAX_CONCURRENT)
    if DL_SEM is None:
        DL_SEM = asyncio.Semaphore(MAX_CONCURRENT)
    if ENC_SEM is None:
        ENC_SEM = asyncio.Semaphore(1)


def init_engine():
    global ANALYZE_SEM, DL_SEM, ENC_SEM
    ANALYZE_SEM = asyncio.Semaphore(MAX_CONCURRENT)
    DL_SEM = asyncio.Semaphore(MAX_CONCURRENT)
    ENC_SEM = asyncio.Semaphore(1)
    now = time.time()
    for d in os.listdir(DOWNLOAD_DIR):
        p = os.path.join(DOWNLOAD_DIR, d)
        if not os.path.isdir(p):
            continue
        try:
            age = now - os.path.getmtime(p)
        except OSError:
            age = 0
        if _is_own_dir(d) or age > 24 * 3600:
            shutil.rmtree(p, ignore_errors=True)


def spawn(coro, chat=None):
    t = asyncio.create_task(coro)
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)
    if chat is not None:
        CHAT_TASKS.setdefault(chat, set()).add(t)

        def _cleanup(x, c=chat):
            s = CHAT_TASKS.get(c)
            if s is not None:
                s.discard(x)
                if not s:
                    CHAT_TASKS.pop(c, None)

        t.add_done_callback(_cleanup)
    return t


def stop_event(chat_id):
    ev = STOP.get(chat_id)
    if ev is None or ev.is_set():
        ev = STOP[chat_id] = threading.Event()
    return ev


def cancel_chat(chat_id):
    ev = STOP.pop(chat_id, None)
    if ev:
        ev.set()
    tasks = [t for t in CHAT_TASKS.get(chat_id, set()) if not t.done()]
    for t in tasks:
        t.cancel()
    return len(tasks)


def active_count(chat_id):
    return len([t for t in CHAT_TASKS.get(chat_id, set()) if not t.done()])


def _proxy():
    return getattr(_tl, "proxy", None) or PROXY_URL


def _http_proxy():
    p = _proxy()
    return p if p and p.lower().startswith("http") else ""


async def cancel_note(msg):
    try:
        await msg.edit_text("🛑 <b>Cancel ho gaya.</b>")
    except Exception:
        pass


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


SITE_SELECTORS = {
    "pornhub.com": {
        "download_selectors": [
            'a[href*=".mp4"]', 'a[href*=".m3u8"]', 'a[href*="download"]',
            'a[download]', '.downloadBtn', 'a:has-text("Download")',
        ],
        "title_selector": "h1.title, h1, .videoTitle, title",
    },
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
BAD_KEYWORDS = ('thumb', 'poster', 'sprite', 'placeholder', 'banner')
SEG_RE = re.compile(r'(?:^|[/_\-.])(?:seg(?:ment)?|chunk|frag(?:ment)?)[-_]?\d+', re.I)


def get_site_config(url: str) -> dict:
    domain = urlparse(url).netloc.lower().replace("www.", "")
    for site, cfg in SITE_SELECTORS.items():
        if site != "default" and site in domain:
            return cfg
    return SITE_SELECTORS["default"]


def url_path(u: str) -> str:
    return urlparse(u).path.lower()


def is_image_url(u: str) -> bool:
    return url_path(u).endswith(IMAGE_EXTS)


def is_bad_url(u: str) -> bool:
    low = u.lower()
    return any(b in low for b in BAD_KEYWORDS)


AD_HOST_HINTS = (
    "ads", "adserver", "advert", "doubleclick", "googlesyndication", "exoclick",
    "trafficjunky", "juicyads", "adsterra", "propellerads", "clickadu", "admaven",
    "popads", "popcash", "tsyndicate", "a-ads", "adcash", "hilltopads", "evadav",
    "galaksion", "richads", "push.house", "adgebra", "mgid", "revcontent",
)
AD_PATH_HINTS = (
    "/ads/", "/ad/", "advert", "popunder", "pop-up", "popup", "sponsor", "promo_",
    "_promo", "campaign", "zoneid", "banner", "creative", "prebid", "vast", "vpaid",
)


AD_BLOCK_HOSTS = {
    "doubleclick.net", "googlesyndication.com", "googleadservices.com", "exoclick.com",
    "trafficjunky.com", "trafficjunky.net", "juicyads.com", "adsterra.com", "propellerads.com",
    "clickadu.com", "admaven.com", "popads.net", "popcash.net", "hilltopads.com", "evadav.com",
    "galaksion.com", "adcash.com", "tsyndicate.com", "a-ads.com", "mgid.com", "revcontent.com",
    "outbrain.com", "taboola.com", "amazon-adsystem.com", "smartadserver.com", "pubmatic.com",
    "rubiconproject.com", "openx.net", "spotxchange.com", "teads.tv", "unruly.co", "aniview.com",
    "springserve.com", "districtm.io", "connatix.com", "playwire.com", "sovrn.com", "sharethrough.com",
    "triplelift.com", "indexww.com", "casalemedia.com", "contextweb.com", "advertising.com",
    "adform.net", "criteo.com", "criteo.net", "smaato.net", "bidswitch.net", "adsafeprotected.com",
    "doubleverify.com", "moatads.com", "ads.yahoo.com", "yieldmo.com", "nativo.com", "seedtag.com",
    "adgebra.com", "clickaine.com", "bitmedia.io", "coinzilla.com", "adnow.com", "adskeeper.com",
    "marketgid.com", "poponclick.com", "onclickads.net", "zeropark.com", "trafficfactory.com",
    "trafficstars.com", "exdynsrv.com", "exosrv.com", "plugrush.com", "poweredbyexoclick.com",
}


def adblock_hit(u: str) -> bool:
    if is_ad_url(u):
        return True
    try:
        host = urlparse(u).netloc.lower().split(":")[0]
    except Exception:
        return False
    if not host:
        return False
    parts = host.split(".")
    base = ".".join(parts[-2:]) if len(parts) >= 2 else host
    if host in AD_BLOCK_HOSTS or base in AD_BLOCK_HOSTS:
        return True
    if parts[0] in ("ads", "ad", "adv", "advert", "adserver") or parts[0].startswith("ads-") or parts[0].startswith("ad-"):
        return True
    return False


AD_TOKENS = {
    "ad", "ads", "adv", "advert", "adverts", "promo", "promos", "sponsor", "sponsored",
    "banner", "banners", "popunder", "popup", "preroll", "midroll", "postroll", "adroll",
    "vast", "vpaid", "ima", "pubads", "gampad", "pagead", "syndication", "adbreak",
    "adserver", "adsense", "adtech", "creative", "campaign", "click", "tracker", "tracking",
}


def cand_sort_key(c, exp_d=0):
    dur = c.get("dur") or 0
    master = 1 if c.get("tag") in ("HLSM", "MAIN") or c.get("kind") == "dash" else 0
    if exp_d and dur:
        ratio = dur / exp_d
        close = 1.0 - min(abs(1.0 - ratio), 1.0)
        return (0 if ratio < 0.35 else 1, master, close, dur, c.get("score") or 0)
    bucket = 1.0 if dur >= 120 else 0.8 if dur >= 45 else 0.5 if dur >= 15 else 0.2
    return (master, bucket, dur, c.get("score") or 0)


def is_real_video(info) -> bool:
    if not info:
        return False
    if (info.get("vcodec") or "").lower() == "gif":
        return False
    if (info.get("width") or 0) <= 16 or (info.get("height") or 0) <= 16:
        return False
    return True


def is_ad_url(u: str) -> bool:
    try:
        pr = urlparse(u)
    except Exception:
        return False
    host = pr.netloc.lower()
    path = pr.path.lower()
    if any(h in host for h in AD_HOST_HINTS):
        return True
    if any(h in path for h in AD_PATH_HINTS):
        return True
    if path.endswith(".gif"):
        return True
    toks = set(re.split(r"[^a-z0-9]+", host + path))
    return bool(toks & AD_TOKENS)


def classify_media(url: str, ctype: str = "", total: int = 0):
    p = url_path(url)
    ctype = (ctype or "").lower()
    if p.endswith(".gif") or ctype.startswith("image/"):
        return None
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
    except Exception:
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
    except Exception:
        pass
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
    if _proxy():
        s.proxies = {"http": _proxy(), "https": _proxy()}
    for c in cookies or []:
        try:
            s.cookies.set(c["name"], c["value"], domain=c.get("domain") or None, path=c.get("path") or "/")
        except Exception:
            pass
    return s


@lru_cache(maxsize=8)
def impersonate_target(name="chrome"):
    try:
        import curl_cffi  # noqa: F401
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
        return "Country/region block hai. PROXY_URL lagana padega."
    if "sign in" in m or "log in" in m or "login" in m or "private" in m:
        return "Is video ke liye login/cookies chahiye."
    if "403" in m or "forbidden" in m or "cloudflare" in m or "captcha" in m:
        return "Site ne server IP ko block kiya hai. PROXY_URL ya cookies.txt try karo."
    if "429" in m:
        return "Site ne zyada requests ki wajah se rok diya."
    if "timed out" in m or "timeout" in m:
        return "Site bahut slow hai (timeout)."
    if "404" in m or "not found" in m:
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
    heatmap: list = field(default_factory=list)
    is_preview: bool = False
    preview_url: str = ""
    alt_urls: dict = field(default_factory=dict)


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
        if _http_proxy():
            cmd += ["-http_proxy", _http_proxy()]
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


def run_ffmpeg(args, duration=0.0, on_progress=None, stop=None):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats"] + [str(a) for a in args]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    err = []
    t = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
    t.start()
    try:
        for line in p.stdout:
            if stop is not None and stop.is_set():
                p.kill()
                p.wait()
                raise Cancelled()
            if on_progress and line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    cur = int(line.split("=", 1)[1]) / 1_000_000
                    on_progress(min(max(cur, 0), duration or cur), duration or 0.0)
                except Cancelled:
                    p.kill()
                    p.wait()
                    raise
                except Exception:
                    pass
        p.wait()
    finally:
        t.join(timeout=5)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg fail: {(err[0] if err else '')[-400:]}")


def keyframe_times(path, stop=None):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(path)
    ]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out = []
    t0 = time.time()
    try:
        for line in p.stdout:
            if stop is not None and stop.is_set():
                p.kill()
                p.wait()
                raise Cancelled()
            if time.time() - t0 > 900:
                p.kill()
                p.wait()
                break
            parts = line.strip().split(",")
            if len(parts) >= 2 and "K" in parts[1]:
                try:
                    out.append(float(parts[0]))
                except ValueError:
                    pass
        p.wait(timeout=10)
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
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


def _clear_files(workdir):
    for f in os.listdir(workdir):
        _rm(os.path.join(workdir, f))


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


def _assemble(parts, src, dst, a_ss, a_dur, has_a, dur, stop=None):
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
        run_ffmpeg(args, dur, stop=stop)
    finally:
        for p in parts + [lst]:
            _rm(p)


def smart_cut(src, dst, start, end, info, cb=None):
    dur = end - start
    has_a = bool(info["acodec"])
    enc = _venc(info, 18)
    stop = getattr(cb, "stop", None)
    prog = (lambda c, t: cb.update("✂️ Cutting", c, t, "time")) if cb else None
    kfs = keyframe_times(src, stop=stop) if info["vcodec"] == "h264" and dur > 40 else []
    k1 = next((k for k in kfs if k >= start), None)
    k2 = max((k for k in kfs if k <= end), default=None)
    if k1 is None or k2 is None or k2 - k1 < 6:
        run_ffmpeg(
            ["-ss", f"{start:.3f}", "-i", src, "-t", f"{dur:.3f}", "-map", "0:v:0", "-map", "0:a:0?"] + enc +
            (["-c:a", "copy"] if has_a else ["-an"]) + ["-movflags", "+faststart", dst],
            dur, prog, stop=stop
        )
        return
    work = os.path.dirname(dst) or "."
    a, b, c = (os.path.join(work, n) for n in ("cut_a.ts", "cut_b.ts", "cut_c.ts"))
    vmap = ["-map", "0:v:0", "-an"]
    parts = []
    if k1 - start > 0.05:
        run_ffmpeg(["-ss", f"{start:.3f}", "-i", src, "-t", f"{k1 - start:.3f}"] + vmap + enc + ["-f", "mpegts", a], stop=stop)
        parts.append(a)
    run_ffmpeg(
        ["-ss", f"{k1 + 0.005:.3f}", "-i", src, "-t", f"{k2 - k1:.3f}"] + vmap +
        ["-c:v", "copy", "-f", "mpegts", b], k2 - k1, prog, stop=stop
    )
    parts.append(b)
    if end - k2 > 0.05:
        run_ffmpeg(["-ss", f"{k2:.3f}", "-i", src, "-t", f"{end - k2:.3f}"] + vmap + enc + ["-f", "mpegts", c], stop=stop)
        parts.append(c)
    _assemble(parts, src, dst, start, dur, has_a, dur, stop=stop)


def cut_video(src, dst, start, end, mode, info, cb=None):
    dur = max(0.5, end - start)
    stop = getattr(cb, "stop", None)
    if mode == "fast":
        run_ffmpeg(
            ["-ss", f"{start:.3f}", "-i", src, "-t", f"{dur:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
             "-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", dst],
            dur, (lambda c, t: cb.update("✂️ Cutting", c, t, "time")) if cb else None, stop=stop
        )
        out = ffprobe(dst)
        if out and out["duration"] >= dur * 0.5:
            return "fast"
    smart_cut(src, dst, start, end, info, cb)
    return "exact"


def compress_to_fit(src, dst, target, on_progress=None):
    info = ffprobe(src)
    if not info or info["duration"] <= 0:
        raise RuntimeError("Compress: duration nahi mili")
    D = info["duration"]
    has_a = bool(info["acodec"])
    a = 128_000 if has_a else 0
    v = max(int(target * 8 * 0.96 / D - a), 200_000)
    scale = ["-vf", "scale=-2:720"] if (info["short"] > 720 and v < 1_800_000) else []
    stop = getattr(on_progress, "stop", None)
    for _ in range(3):
        args = ["-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
                "-b:v", str(v), "-maxrate", str(int(v * 1.3)), "-bufsize", str(v * 2), "-pix_fmt", "yuv420p"] + scale
        args += (["-c:a", "aac", "-b:a", "128k"] if has_a else ["-an"])
        run_ffmpeg(
            args + ["-movflags", "+faststart", dst], D,
            (lambda c, t: on_progress.update("🗜 Compressing", c, t, "time")) if on_progress else None,
            stop=stop
        )
        size = os.path.getsize(dst)
        if size <= target:
            return "full"
        v = int(v * (target / size) * 0.93)
    raise RuntimeError("Compress ke baad bhi file 2GB se badi hai")


def extract_audio(path, out, title, cb=None):
    info = ffprobe(path)
    if not info or not info["acodec"]:
        raise RuntimeError("Is video mein audio nahi hai")
    a = ["-c:a", "copy"] if info["acodec"] == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    run_ffmpeg(
        ["-i", path, "-vn", "-map", "0:a:0"] + a +
        ["-metadata", f"title={title}", "-movflags", "+faststart", out], info["duration"], cb
    )
    return out, ffprobe(out) or info


CRF_RANGE = {"h264": (18, 30), "h265": (20, 34)}


def parse_compress_opts(text):
    t = (text or "").lower()
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


def detect_animation(path, duration, stop=None):
    counts = []
    for f in (0.15, 0.35, 0.55, 0.75, 0.9):
        if stop is not None and stop.is_set():
            raise Cancelled()
        try:
            r = subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", f"{duration * f:.2f}", "-i", path, "-frames:v", "1",
                 "-vf", "scale=64:36:flags=area", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
                capture_output=True, timeout=60
            )
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


def _ssim(src, t, length, sample, vf, stop=None):
    if stop is not None and stop.is_set():
        raise Cancelled()
    ref = f"[0:v]{vf + ',' if vf else ''}setpts=PTS-STARTPTS[a]"
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-ss", f"{t:.2f}", "-t", f"{length:.2f}", "-i", src,
         "-i", sample, "-lavfi", f"{ref};[1:v]setpts=PTS-STARTPTS[b];[a][b]ssim", "-f", "null", "-"],
        capture_output=True, text=True, timeout=900
    )
    m = re.search(r"All:([0-9.]+)", r.stderr)
    return float(m.group(1)) if m else 0.0


def smart_compress(src, dst, prog, opts):
    prog.check()
    info = ffprobe(src)
    if not info or not info["vcodec"] or info["duration"] <= 0:
        raise RuntimeError("Is file mein video nahi mili")
    S, D = os.path.getsize(src), info["duration"]
    if info.get("ctrans") in ("smpte2084", "arib-std-b67"):
        return {"skipped": True, "reason": "HDR video hai, compress se rang bigad sakte hain"}
    codec, target, tune = opts.get("codec", "h264"), opts.get("target", 0.985), opts.get("tune", "")
    W, H = info["width"], info["height"]
    mh = opts.get("max_h", 0)
    vb = info["v_bps"] or max(int(S * 8 / D) - (info["a_bps"] or 0), 1)
    if not tune and detect_animation(src, D, stop=prog.stop):
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
            prog.check()
            sp = os.path.join(work, f"smp_{k}.mp4")
            run_ffmpeg(
                ["-ss", f"{t:.2f}", "-i", src, "-t", f"{length:.2f}", "-map", "0:v:0", "-an"] + args + [sp],
                length, lambda c, tot: prog.check(), stop=prog.stop
            )
            size += os.path.getsize(sp)
            secs += length
            vals.append(_ssim(src, t, length, sp, vf, stop=prog.stop))
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

    est = bps * D / 8 + (info["a_bps"] or 128_000) * D / 8
    if est >= S * 0.94:
        crf = min(crf + 3, CRF_RANGE[codec][1])
        cap = int(vb * 0.72)

    args = (["-i", src, "-map", "0:v:0", "-map", "0:a:0?"] + _venc_args(codec, crf, preset, tune, cap, pix) + vf_args +
            _audio_args(info) + ["-movflags", "+faststart", dst])
    run_ffmpeg(args, D, lambda c, t: prog.update("🗜 Smart Compressing", c, t, "time"), stop=prog.stop)
    after = os.path.getsize(dst)
    if after >= S * 0.98:
        _rm(dst)
        return {"skipped": True, "reason": "Ye video pehle se extreme compressed hai, original rakha"}
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
    def __init__(self, loop, msg, header="", stop=None):
        self.loop, self.msg, self.header, self.stop = loop, msg, header, stop
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

    def check(self):
        if self.stop is not None and self.stop.is_set():
            raise Cancelled()

    def update(self, stage, done, total, kind="bytes"):
        self.check()
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
        self.check()
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


def base_ydl_opts(headers=None, fast=False, imp="chrome"):
    hdrs = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        hdrs.update(headers)
    cookie_hdr = ""
    for k in [k for k in hdrs if k.lower() == "cookie"]:
        cookie_hdr = hdrs.pop(k) or cookie_hdr
    o = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "nocheckcertificate": True,
        "geo_bypass": True, "geo_bypass_country": "US",
        "socket_timeout": 10 if fast else 35,
        "retries": 1 if fast else 10, "extractor_retries": 1 if fast else 3, "fragment_retries": 10,
        "concurrent_fragment_downloads": 8,
        "format_sort": ["res", "vcodec:h264", "fps", "ext"],
        "http_headers": hdrs,
        "extractor_args": {
            "youtube": {"player_client": ["android", "web", "ios"]},
            "generic": {"impersonate": ["chrome"]},
        },
    }
    if cookie_hdr:
        o["_cookie_header"] = cookie_hdr
    if COOKIES_USABLE:
        o["cookiefile"] = COOKIES_FILE
    if _proxy():
        o["proxy"] = _proxy()
    if FORCE_IPV4:
        o["source_address"] = "0.0.0.0"
    t = None if (NO_IMP or not imp) else impersonate_target(imp)
    if t:
        o["impersonate"] = t
    return o


def cookies_from_header(url, header):
    host = urlparse(url).netloc.split(":")[0]
    out = []
    for part in (header or "").split(";"):
        if "=" not in part:
            continue
        n, v = part.strip().split("=", 1)
        out.append(http.cookiejar.Cookie(0, n, v, None, False, host, True, False, "/", True, False, None, False, None, None, {}))
    return out


def ydl_call(opts, fn, url=None):
    global NO_IMP

    def run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            if url:
                for c in age_cookies_for(url):
                    ydl.cookiejar.set_cookie(c)
                for c in cookies_from_header(url, opts.get("_cookie_header")):
                    ydl.cookiejar.set_cookie(c)
            return fn(ydl)

    try:
        return run()
    except (Cancelled, DownloadCancelled):
        raise
    except Exception as e:
        if "impersonat" in str(e).lower() and "impersonate" in opts:
            NO_IMP = True
            opts.pop("impersonate", None)
            return run()
        raise


def ytdlp_extract(url, headers=None):
    last = None
    ladder = [n for n in IMP_LADDER if not n or (not NO_IMP and impersonate_target(n))] or [""]
    ladder = ladder[:1] + ([""] if ladder and ladder[0] else [])
    for name in ladder:
        opts = base_ydl_opts(headers, True, name)
        opts["skip_download"] = True
        try:
            info = ydl_call(opts, lambda y: y.extract_info(url, download=False), url)
        except Exception as e:
            last = e
            if not any(k in str(e).lower() for k in BLOCK_HINTS):
                break
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
    s.heatmap = info.get("heatmap") or []
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
    if info and not is_real_video(info):
        info = None
        s.invalid_media = True
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
        except Exception:
            pass
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
        if a.startswith("http") and a not in seen and not is_bad_url(a) and not is_image_url(a) and not is_ad_url(a):
            seen.add(a)
            out.append(a)
    return out


async def sniff_page(page_url, cookies):
    cfg = get_site_config(page_url)
    cands, seen, netlog, alt_urls = {}, set(), [], {}
    res = {
        "cands": [], "title": None, "thumbs": [], "cookies": [],
        "html": "", "frames": [], "net": netlog, "final": "", "alt_urls": alt_urls,
        "exp_dur": 0.0, "blocked": 0, "buttons": []
    }

    def add(url, score, kind, size=0, tag="", q=0):
        if not url or not url.startswith("http") or is_image_url(url) or is_bad_url(url) or is_ad_url(url):
            return
        if q in STANDARD_QUALITIES and kind == "mp4":
            alt_urls.setdefault(q, url)
        old = cands.get(url)
        if old and old["score"] >= score:
            return
        cands[url] = {"url": url, "score": score, "kind": kind, "size": size, "tag": tag, "q": q}

    async def on_response(resp):
        try:
            url = resp.url
            if url in seen:
                return
            seen.add(url)
            hd = resp.headers
            ctype = (hd.get("content-type") or "").split(";")[0].strip().lower()
            if ctype.startswith("image/"):
                try:
                    sz = int(hd.get("content-length") or 0)
                except ValueError:
                    sz = 0
                if sz >= 25000 and not is_hard_bad_url(url) and len(res["thumbs"]) < 14:
                    res["thumbs"].append(url)
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
            tag = "NET"
            if kind == "hls":
                score = 160
                try:
                    if total < 3 * MiB and "#EXT-X-STREAM-INF" in (await resp.text()):
                        score = 320
                        tag = "HLSM"
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
            if is_preview_url(url):
                score -= 80
            add(url, score, kind, total, tag, qn)
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
                if r.request.resource_type in ("font",):
                    await r.abort()
                elif adblock_hit(r.request.url):
                    res["blocked"] += 1
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
                dv = await page.evaluate(
                    """()=>{const m=document.querySelector('meta[property="og:video:duration"]');
                    if(m&&m.content)return parseFloat(m.content)||0;
                    for(const sc of document.querySelectorAll('script[type="application/ld+json"]')){
                    const mt=(sc.textContent||'').match(/"duration"\\s*:\\s*"?PT(?:(\\d+)H)?(?:(\\d+)M)?(?:([\\d.]+)S)?/);
                    if(mt)return (parseInt(mt[1]||0)*3600+parseInt(mt[2]||0)*60+parseFloat(mt[3]||0))}return 0}"""
                )
                res["exp_dur"] = float(dv or 0)
            except Exception:
                pass
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
                    """()=>{const r=[];document.querySelectorAll('meta[property="og:image"],meta[property="og:image:secure_url"],meta[name="twitter:image"],meta[name="twitter:image:src"]')
                    .forEach(m=>m.content&&r.push(m.content));document.querySelectorAll('link[rel="image_src"]')
                    .forEach(m=>m.href&&r.push(m.href));document.querySelectorAll('video[poster]')
                    .forEach(v=>v.poster&&r.push(v.poster));const im=[...document.querySelectorAll('img')]
                    .filter(i=>i.naturalWidth>=480&&i.naturalHeight>=270).sort((a,b)=>b.naturalWidth*b.naturalHeight-a.naturalWidth*a.naturalHeight);
                    im.slice(0,3).forEach(i=>r.push(i.currentSrc||i.src));return [...new Set(r)]}"""
                )
            except Exception:
                pass
            try:
                res["buttons"] = await page.evaluate(
                    """()=>{const out=[];for(const a of document.querySelectorAll('a[href]')){const t=(a.innerText||a.textContent||'').trim().replace(/\s+/g,' ');if(/download|⬇/i.test(t)||/download/i.test(a.href)){const q=(t.match(/(2160|1440|1080|720|480|360|240)\s*p/)||[])[1]||0;const sm=t.match(/([\d]+(?:[.\,]\d+)?)\s*(GB|MB|KB)/i);let sz=0;if(sm){const v=parseFloat(sm[1].replace(',','.'));sz=Math.round(v*(sm[2].toUpperCase()==='GB'?1073741824:sm[2].toUpperCase()==='MB'?1048576:1024));}out.push({url:a.href,label:t.slice(0,60),q:parseInt(q)||0,size_hint:sz});}if(out.length>=12)break;}return out}"""
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
                            score = 250 + (100 if qn >= 1080 else 60 if qn >= 720 else 20 if qn >= 480 else 0) - (90 if is_preview_url(u) else 0)
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
            try:
                mains = await page.evaluate(
                    """()=>{const out=[];document.querySelectorAll('video').forEach(v=>{
                    const r=v.getBoundingClientRect();const u=v.currentSrc||v.src;
                    if(u&&u.startsWith('http')&&r.width>=200&&r.height>=120)
                    out.push({u,d:v.duration||0,a:r.width*r.height})});return out}"""
                )
                for m in mains or []:
                    add(m["u"], 600, classify_media(m["u"]) or "mp4", 0, "MAIN")
                    if m.get("d") and not res["exp_dur"]:
                        res["exp_dur"] = float(m["d"])
            except Exception:
                pass
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
    except Exception:
        pass

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
    except Exception:
        pass
    return None


INTENT_ACTIONS = (
    "card", "download", "cut", "audio", "status", "help", "start",
    "template", "ask", "chat", "diag", "compress", "none"
)


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


class ResolveError(Exception):
    def __init__(self, msg, diag=None):
        super().__init__(msg)
        self.diag = diag or {}



def _btn_size(text):
    m = re.search(r"([\d]+(?:[.,]\d+)?)\s*(GB|MB|KB)\b", text or "", re.I)
    if not m:
        return 0
    v = float(m.group(1).replace(",", "."))
    u = m.group(2).upper()
    return int(v * (1024 ** 3 if u == "GB" else 1024 ** 2 if u == "MB" else 1024))


def _extract_download_buttons(txt, base):
    out, seen = [], set()
    for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', txt or "", re.I | re.S):
        href = urljoin(base, html.unescape(m.group(1)))
        if not href.startswith("http") or href in seen:
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(m.group(2)))).strip()
        low_t, low_h = text.lower(), href.lower()
        qm = re.search(r"(2160|1440|1080|720|480|360|240)\s*p\b", low_t) or re.search(r"[_-](2160|1440|1080|720|480|360|240)p?(?:\.|_)", low_h)
        is_dl = ("download" in low_t or "⬇" in text or "/download" in low_h or "download=" in low_h)
        kind = classify_media(href)
        if kind and (is_dl or qm) and not is_ad_url(href) and not is_hard_bad_url(href):
            seen.add(href)
            out.append({"url": href, "label": text[:60], "q": int(qm.group(1)) if qm else 0,
                        "size_hint": _btn_size(text)})
        elif is_dl and not kind and "login" not in low_h and "signup" not in low_h and "register" not in low_h:
            seen.add(href)
            out.append({"page": href, "label": text[:60]})
    return out[:12]


def quick_html_probe(url, cookies):
    try:
        sc = make_session(make_headers(url), cookies)
        txt = sc.get(url, timeout=25).text[:900000]
    except Exception:
        return None
    if not txt:
        return None
    un = html.unescape(txt.replace("\\/", "/").replace("\\u0026", "&"))

    def meta(prop):
        m = re.search(r'(?:property|name)=["\']' + re.escape(prop) + r'["\'][^>]*content=["\']([^"\']+)', un, re.I)
        if not m:
            m = re.search(r'content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']' + re.escape(prop) + r'["\']', un, re.I)
        return m.group(1) if m else ""

    media = []
    for prop in ("og:video:secure_url", "og:video", "og:video:url", "twitter:player:stream"):
        v = meta(prop)
        if v:
            media.append(urljoin(url, v))
    for m in re.finditer(r'"contentUrl"\s*:\s*"([^"]+)"', un):
        media.append(urljoin(url, m.group(1)))
    for key in ("videoUrl", "masterM3U8", "hlsUrl", "hls_url", "streamUrl", "stream_url", "mp4Url", "video_url", "playUrl"):
        for m in re.finditer(r'"' + key + r'"\s*:\s*"(https?://[^"]+?\.(?:m3u8|mp4|webm|mpd)(?:\?[^"]*)?)"', un):
            media.append(urljoin(url, m.group(1)))
    api_urls = []
    for m in re.finditer(r'"(?:videoUrl|mediaUrl|apiUrl)"\s*:\s*"(https?://[^"]*(?:get_media|media_definitions|mediaDefinitions)[^"]*)"', un):
        api_urls.append(urljoin(url, m.group(1)))
    for m in re.finditer(r'https?://[^\s"\'<>]*(?:get_media|/media/)[^\s"\'<>]*', un):
        if "pornhub" in m.group(0) or "get_media" in m.group(0):
            api_urls.append(m.group(0))
    for au in list(dict.fromkeys(api_urls))[:2]:
        try:
            r2 = sc.get(au, timeout=25)
            t2 = html.unescape((r2.text or "").replace("\\/", "/").replace("\\u0026", "&"))[:600000]
        except Exception:
            continue
        for m in re.finditer(r'"link"\s*:\s*"(https?://[^"]+)"', t2):
            media.append(m.group(1))
        media += scan_html_for_media(t2, au)
        for m in re.finditer(r'https?://[^\s"\'<>]+?\.(?:m3u8|mp4)(?:\?[^\s"\'<>]*)?', t2):
            media.append(m.group(0))
    for m in re.finditer(r'<source[^>]+src=["\']([^"\']+)', un, re.I):
        media.append(urljoin(url, m.group(1)))
    media += scan_html_for_media(un, url)
    thumbs = []
    for prop in ("og:image:secure_url", "og:image", "twitter:image", "twitter:image:src"):
        v = meta(prop)
        if v:
            thumbs.append(urljoin(url, v))
    for m in re.finditer(r'"thumbnailUrl"\s*:\s*"(https?://[^"]+)"', un):
        thumbs.append(m.group(1))
    title = meta("og:title") or meta("twitter:title")
    dur = 0.0
    dv = meta("og:video:duration") or meta("twitter:player:stream:duration")
    if dv:
        try:
            dur = float(dv)
        except ValueError:
            dur = 0.0
    if not dur:
        md = re.search(r'"duration"\s*:\s*"?PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?"?', un)
        if md:
            dur = (int(md.group(1) or 0) * 3600 + int(md.group(2) or 0) * 60 + float(md.group(3) or 0))
    if not dur:
        md = re.search(r'"duration"\s*:\s*(\d{2,6})(?:\.\d+)?\b', un)
        if md:
            dur = float(md.group(1))
    out, seen = [], set()
    for u in media:
        k = classify_media(u)
        if u.startswith("http") and u not in seen and k and not is_hard_bad_url(u) and not is_ad_url(u):
            seen.add(u)
            out.append((u, k, is_preview_url(u)))
    out.sort(key=lambda x: (x[2], 0 if x[1] == "hls" else 1, x[1] != "mp4"))
    buttons = _extract_download_buttons(un, url)
    btn_pages = [b["page"] for b in buttons if b.get("page")][:2]
    buttons = [b for b in buttons if b.get("url")]
    for pg in btn_pages:
        try:
            t3 = html.unescape(sc.get(pg, timeout=20).text[:600000].replace("\\/", "/").replace("\\u0026", "&"))
        except Exception:
            continue
        for b in _extract_download_buttons(t3, pg):
            if b.get("url"):
                buttons.append(b)
    seen_b = set()
    buttons = [b for b in buttons if not (b["url"] in seen_b or seen_b.add(b["url"]))]
    return {"media": out, "thumbs": list(dict.fromkeys(thumbs)), "title": title or None, "dur": dur,
            "buttons": buttons[:8]}


async def hd_from_preview(run, preview_url, page_url, cookies, all_urls, thumbs, title):
    tries = preview_hd_variants(preview_url) + same_video_urls(all_urls, preview_url)
    base_info = await run(ffprobe, preview_url, make_headers(page_url), 25)
    base_dur = (base_info or {}).get("duration") or 0
    base_h = (base_info or {}).get("short") or 0
    for u in dict.fromkeys(tries):
        try:
            info = await run(ffprobe, u, make_headers(page_url), 25)
        except Exception:
            info = None
        if not is_real_video(info):
            info = None
        if not info:
            try:
                yi = await run(ytdlp_extract, u, {"Referer": page_url})
            except Exception:
                yi = None
            if yi and (yi.get("formats") or yi.get("url")):
                src = source_from_ytinfo(u, yi, {"Referer": page_url}, cookies)
                src.is_preview, src.preview_url = True, preview_url
                src.thumbs = list(dict.fromkeys((src.thumbs or []) + list(thumbs or [])))
                if title:
                    src.title = title
                return src
            continue
        dur, hh = info.get("duration") or 0, info.get("short") or 0
        if (base_dur and dur > base_dur * 1.4) or (base_h and hh > base_h) or (not base_dur and not base_h):
            src = await source_from_media(u, page_url, cookies, title=title, thumbs=thumbs, pre=info)
            src.is_preview, src.preview_url = True, preview_url
            return src
    return None


async def resolve_source(url, status):
    loop = asyncio.get_running_loop()
    cookies = load_cookie_list()
    run = lambda fn, *a: loop.run_in_executor(None, fn, *a)

    if classify_media(url):
        await status("🔗 Direct link mila, check kar raha hoon…")
        s = await source_from_media(url, origin_of(url) + "/", cookies, url_name=True)
        if not getattr(s, "invalid_media", False):
            return s

    async def _yt_stage():
        generic = None
        err = ""
        try:
            info = await run(ytdlp_extract, url, None)
            if info and (info.get("formats") or info.get("url")):
                s = source_from_ytinfo(url, info, {}, cookies)
                if (info.get("extractor_key") or "").lower() != "generic":
                    return s, generic, err
                generic = s
                if s.duration and s.duration >= 30 and not s.is_preview:
                    return s, generic, err
        except Exception as e:
            err = str(e)[:300]
        return None, generic, err

    async def _page_stage():
        await status("⚡ Page ke andar ka video dhoondh raha hoon…")
        try:
            qp = await run(quick_html_probe, url, cookies)
        except Exception:
            qp = None
        if qp and (qp["media"] or qp.get("buttons")):
            btns = [b for b in (qp.get("buttons") or []) if b.get("url")]
            if btns:
                async def _bprobe(b):
                    info = await run(ffprobe, b["url"], make_headers(url), 25)
                    return b, info if is_real_video(info) else None

                bok = [(b, i) for b, i in await asyncio.gather(*[_bprobe(b) for b in btns[:6]]) if i]
                if bok:
                    bok.sort(key=lambda x: -((x[1].get("short") or 0)))
                    alts, sizes = {}, {}
                    for b, i in bok:
                        h = i.get("short") or b.get("q") or 0
                        if h and h not in alts:
                            alts[h] = b["url"]
                            sizes[h] = i.get("size") or b.get("size_hint") or 0
                    mb, mi = bok[0]
                    src = await source_from_media(mb["url"], url, cookies, title=qp["title"], thumbs=qp["thumbs"], pre=mi, alt_urls=alts)
                    if sizes:
                        src.quals = sorted([(h, sizes.get(h, 0)) for h in alts], reverse=True)
                        src.est = {h: (sizes.get(h, 0), h) for h in alts}
                    src.from_buttons = True
                    return src
            async def _qprobe(item):
                mu, kind, is_pv = item
                info = await run(ffprobe, mu, make_headers(url), 25)
                return mu, is_pv, info

            probed_q = []
            for mu, is_pv, info in await asyncio.gather(*[_qprobe(it) for it in qp["media"][:8]]):
                if not is_real_video(info):
                    continue
                if qp.get("dur") and (info.get("duration") or 0) and info["duration"] < qp["dur"] * 0.35 and len(qp["media"]) > 1:
                    continue
                probed_q.append((info.get("duration") or 0, mu, is_pv, info))
            probed_q.sort(key=lambda x: -x[0])
            for _qd, mu, is_pv, info in probed_q:
                if is_pv:
                    hd = await hd_from_preview(run, mu, url, cookies, [m for m, _, _ in qp["media"]], qp["thumbs"], qp["title"])
                    if hd:
                        return hd
                src = await source_from_media(mu, url, cookies, title=qp["title"], thumbs=qp["thumbs"], pre=info)
                src.is_preview = bool(is_pv)
                if is_pv:
                    src.preview_url = mu
                return src
        return None

    async def _sniff_stage():
        await status("🌐 Browser Engine se video nikal raha hoon…")
        sn = await sniff_page(url, cookies)
        cookies2 = sn["cookies"] or cookies
        thumbs = [urljoin(url, t) for t in sn["thumbs"] if t]
        cands = sorted(sn["cands"], key=lambda c: -c["score"])[:8]
        for b in (sn.get("buttons") or [])[:6]:
            if b.get("url") and classify_media(b["url"]):
                cands.append({"url": b["url"], "score": 420, "kind": classify_media(b["url"]) or "mp4", "size": 0, "tag": "BTN"})
        alt_urls = sn.get("alt_urls") or {}
        _fin, generic, err = await t_yt

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
                if not is_real_video(c["info"]):
                    c["info"] = None
                c["dur"] = (c["info"] or {}).get("duration") or 0
                if not c["info"] and c["kind"] in ("hls", "dash"):
                    try:
                        yi = await run(ytdlp_extract, c["url"], h)
                    except Exception:
                        yi = None
                    if yi:
                        c["dur"] = float(yi.get("duration") or 0) or c["dur"]
                        if c["dur"]:
                            c["info"] = {"duration": c["dur"], "short": 0, "size": 0}

            await asyncio.gather(*[probe(c) for c in cands])
            valid = [c for c in cands if c["info"]]
            exp_d = sn.get("exp_dur") or (generic.duration if generic else 0) or 0

            cand_key = lambda c: cand_sort_key(c, exp_d)

            best = max(valid, key=cand_key) if valid else (cands[0] if cands[0]["score"] >= 150 else None)
            if best:
                if is_preview_url(best["url"]):
                    all_u = [c["url"] for c in cands] + list(alt_urls.values())
                    hd = await hd_from_preview(run, best["url"], url, cookies2, all_u, thumbs, sn["title"])
                    if hd:
                        pool.append(hd)
                src_b = await source_from_media(
                    best["url"], url, cookies2, title=sn["title"], thumbs=thumbs,
                    pre=best.get("info"), alt_urls=alt_urls
                )
                if is_preview_url(best["url"]):
                    src_b.is_preview, src_b.preview_url = True, best["url"]
                if not getattr(src_b, "invalid_media", False):
                    pool.append(src_b)
        if pool:
            exp_p = (generic.duration if generic else 0) or sn.get("exp_dur") or 0
            if exp_p:
                return max(pool, key=lambda x: (x.duration >= exp_p * 0.5, -abs((x.duration or 0) - exp_p), len(x.quals)))
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

    await status("🔎 Analyzing… (saare engines ek saath chal rahe hain)")
    t_yt = asyncio.ensure_future(_yt_stage())
    t_page = asyncio.ensure_future(_page_stage())
    t_sniff = asyncio.ensure_future(_sniff_stage())
    try:
        final, _g, _e = await t_yt
        if final:
            return final
        src = await t_page
        if src:
            return src
        return await t_sniff
    finally:
        for t in (t_yt, t_page, t_sniff):
            if not t.done():
                t.cancel()


def _largest_media_file(workdir, since=0.0):
    best, size = None, 0
    for f in os.listdir(workdir):
        p = os.path.join(workdir, f)
        if f.endswith((".part", ".ytdl", ".txt", ".ts", ".jpg")) or not os.path.isfile(p):
            continue
        if since and os.path.getmtime(p) + 0.5 < since:
            continue
        sz = os.path.getsize(p)
        if sz > size:
            best, size = p, sz
    if not best or size < 50 * 1024:
        raise RuntimeError("Download ke baad valid video file nahi mili")
    return best


def ytdlp_download(url, workdir, headers, max_h, prog, audio=False, imp="chrome"):
    def hook(d):
        try:
            prog.check()
            if d.get("status") == "downloading":
                prog.update(
                    "⬇️ Downloading", d.get("downloaded_bytes") or 0,
                    d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                )
        except Cancelled:
            raise DownloadCancelled("User cancelled")

    sort = [f"res:{max_h}", "vcodec:h264", "acodec:aac", "ext:mp4:m4a", "proto"]
    if audio:
        plans = [("ba/b", None), ("bestaudio/best", None)]
    else:
        f_strict = f"bv*[height<={max_h}]+ba/b[height<={max_h}]/bv*+ba/b"
        plans = [(f_strict, sort), ("bv*+ba/b", sort), ("b", None)]

    last = None
    for (fmt, srt), name in [(p, n) for n in dict.fromkeys([imp, "chrome", ""]) for p in plans]:
        prog.check()
        _clear_files(workdir)
        t_attempt = time.time()
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
            return _largest_media_file(workdir, since=t_attempt)
        except (Cancelled, DownloadCancelled):
            _clear_files(workdir)
            raise Cancelled()
        except Exception as e:
            last = e
            _clear_files(workdir)
    raise last


def ffmpeg_stream_download(url, out, headers, duration, prog, hls=False):
    base = []
    if hls:
        base += ["-allowed_extensions", "ALL"]
    if _http_proxy():
        base += ["-http_proxy", _http_proxy()]
    if headers:
        base += ["-headers", _hdr_str(headers)]
    base += ["-i", url]
    cb = lambda c, t: prog.update("⬇️ Downloading (Stream)", c, t, "time")
    try:
        args = base + ["-c", "copy"]
        if hls:
            args += ["-bsf:a", "aac_adtstoasc"]
        run_ffmpeg(args + ["-movflags", "+faststart", out], duration, cb, stop=prog.stop)
    except Cancelled:
        raise
    except Exception:
        _rm(out)
        args = base + ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
                       "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k"]
        run_ffmpeg(args + ["-movflags", "+faststart", out], duration, cb, stop=prog.stop)
    return out


def download_direct(url, out_path, headers, cookies, prog, workers=8):
    prog.check()
    s = make_session(headers, cookies)
    label = "⬇️ Downloading"

    def first_request():
        try:
            r = s.get(url, headers={"Range": "bytes=0-"}, stream=True, timeout=(15, 60), allow_redirects=True)
            if r.status_code >= 400:
                raise IOError(f"HTTP {r.status_code}")
            return r
        except Cancelled:
            raise
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
            _parallel_download(s, final, out_path, total, workers, lambda d, t: prog.update(label, d, t), stop=prog.stop)
            return out_path
        except Cancelled:
            _rm(out_path)
            raise
        except Exception:
            _rm(out_path)
            r = s.get(url, stream=True, timeout=(15, 60), allow_redirects=True)
            r.raise_for_status()

    done = 0
    try:
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                prog.check()
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    prog.update(label, done, total)
    except Cancelled:
        _rm(out_path)
        raise
    if done < 100 * 1024:
        _rm(out_path)
        raise RuntimeError(f"File sirf {humanbytes(done)} hai — video nahi lagti")
    return out_path


def _parallel_download(session, url, out_path, total, workers, cb, stop=None):
    chunk = max(8 * MiB, total // (workers * 4))
    ranges = [(i, min(i + chunk - 1, total - 1)) for i in range(0, total, chunk)]
    with open(out_path, "wb") as f:
        f.truncate(total)
    lock, done = threading.Lock(), [0]
    abort = threading.Event()

    def fetch(a, b):
        last = None
        for attempt in range(4):
            if abort.is_set() or (stop is not None and stop.is_set()):
                raise Cancelled()
            got = 0
            try:
                resp = session.get(url, headers={"Range": f"bytes={a}-{b}"}, stream=True, timeout=(15, 60))
                if resp.status_code != 206:
                    raise IOError(f"range unsupported ({resp.status_code})")
                with open(out_path, "r+b") as fh:
                    fh.seek(a)
                    for data in resp.iter_content(1024 * 1024):
                        if abort.is_set() or (stop is not None and stop.is_set()):
                            raise Cancelled()
                        if data:
                            fh.write(data)
                            got += len(data)
                            with lock:
                                done[0] += len(data)
                                cb(done[0], total)
                if got != b - a + 1:
                    raise IOError("short chunk")
                return
            except Cancelled:
                abort.set()
                raise
            except Exception as e:
                if got:
                    with lock:
                        done[0] = max(0, done[0] - got)
                last = e
                for _ in range((1 + attempt) * 10):
                    if abort.is_set() or (stop is not None and stop.is_set()):
                        raise Cancelled()
                    time.sleep(0.1)
        raise last

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(fetch, a, b) for a, b in ranges]
        for fut in futs:
            fut.result()


def choose_tier(src: Source, q: int = 0) -> int:
    if q and q > 0:
        return q
    if src.mode == "ytdlp" and src.est:
        size, _ = src.est.get(MAX_HEIGHT, (0, 0))
        if size and size > UPLOAD_LIMIT:
            return FALLBACK_HEIGHT
    return MAX_HEIGHT


def _pick_alt(alt_urls: dict, tier: int, default_url: str) -> str:
    if not alt_urls:
        return default_url
    if tier in alt_urls:
        return alt_urls[tier]
    lower = [k for k in alt_urls if k <= tier]
    if lower:
        return alt_urls[max(lower)]
    return alt_urls[min(alt_urls)]


def _page_fallback_download(src: Source, workdir, prog, q=0, audio=False):
    tier = choose_tier(src, q)
    _clear_files(workdir)
    return ytdlp_download(src.page_url, workdir, {"Referer": src.page_url}, tier, prog, audio, "chrome")


LIST_SKIP = (
    "login", "signup", "sign-up", "upload", "categories", "category", "channels", "channel/",
    "pornstars", "models", "tags", "/tag/", "members", "community", "albums", "playlists",
    "/user", "search", "blog", "news", "premium", "support", "terms", "privacy", "dmca",
    "contact", "about", "logout", "register", "forgot", "settings", "profile", "inbox",
)


def _video_page_hint(href: str, page_host: str) -> bool:
    try:
        pr = urlparse(href)
    except Exception:
        return False
    if pr.netloc and page_host and pr.netloc.split(":")[0].lower() != page_host:
        return False
    path = (pr.path or "").lower()
    if any(x in path for x in LIST_SKIP):
        return False
    low = path + "?" + (pr.query or "").lower()
    if any(h in low for h in ("/video", "viewkey", "/watch", "/v/", "/vid", "videos/")):
        return True
    return bool(re.search(r"/[a-z0-9][a-z0-9\-_]{5,}$", path) and re.search(r"\d", path))


def _img_thumb_from_tag(tag: str):
    for attr in ("data-src", "data-thumb-url", "data-original", "data-lazy-src", "data-image"):
        m = re.search(attr + r'=["\']([^"\']+)', tag, re.I)
        if m and not m.group(1).startswith("data:"):
            return m.group(1)
    m = re.search(r'srcset=["\']([^"\',\s]+)', tag, re.I)
    if m and not m.group(1).startswith("data:"):
        return m.group(1)
    m = re.search(r'src=["\']([^"\']+)', tag, re.I)
    if m:
        u = m.group(1)
        low = u.lower()
        if not u.startswith("data:") and not any(x in low for x in ("blank", "spinner", "lazy", "placeholder", "1x1", ".svg")):
            return u
    return None


def scan_listing(url, limit=80):
    entries, seen = [], set()
    page_host = urlparse(url).netloc.split(":")[0].lower()

    def add(page, title, thumb):
        if not page or not thumb:
            return
        page = urljoin(url, html.unescape(str(page)))
        thumb = urljoin(url, html.unescape(str(thumb)))
        if not page.startswith("http") or not thumb.startswith("http"):
            return
        if page in seen or is_ad_url(page) or is_ad_url(thumb) or is_bad_url(page):
            return
        if not _video_page_hint(page, page_host):
            return
        seen.add(page)
        entries.append({"page": page, "title": (str(title or "").strip() or "Video")[:120], "thumb": thumb})

    try:
        import yt_dlp
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": "in_playlist", "playlistend": limit,
            "socket_timeout": 20, "retries": 1,
        }
        if cookies_ok():
            opts["cookiefile"] = COOKIES_FILE
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        for e in (info or {}).get("entries") or []:
            if not isinstance(e, dict):
                continue
            thumb = e.get("thumbnail")
            if not thumb and e.get("thumbnails"):
                thumb = e["thumbnails"][-1].get("url")
            add(e.get("webpage_url") or e.get("url"), e.get("title"), thumb)
    except Exception:
        pass
    if len(entries) < 3:
        try:
            sc = make_session(make_headers(url), load_cookie_list())
            r = sc.get(url, timeout=30)
            text = r.text or ""
            for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', text, re.I | re.S):
                href, inner = m.group(1), m.group(2)
                im = re.search(r"<img\b[^>]*>", inner, re.I)
                if not im:
                    continue
                thumb = _img_thumb_from_tag(im.group(0))
                if not thumb:
                    continue
                tm = re.search(r'alt=["\']([^"\']+)', im.group(0), re.I)
                title = tm.group(1) if tm else re.sub(r"<[^>]+>", " ", inner).strip()
                add(href, title, thumb)
                if len(entries) >= limit:
                    break
        except Exception:
            pass
    return entries[:limit]


def rank_listing_matches(entries, preview_hashes, base_url):
    from modules.smart_tools import ahash_bytes, hash_sim
    if not preview_hashes:
        return []
    sc = make_session(make_headers(base_url), load_cookie_list())
    scored = []
    for e in entries:
        try:
            r = sc.get(e["thumb"], timeout=15)
            data = r.content if r is not None else b""
        except Exception:
            continue
        if not data or len(data) > 4 * MiB:
            continue
        h = ahash_bytes(data)
        if h is None:
            continue
        score = max(hash_sim(h, ph) for ph in preview_hashes)
        scored.append({**e, "score": round(float(score), 3)})
    scored.sort(key=lambda x: -x["score"])
    return scored


def download_media(src: Source, workdir, prog, q=0, audio=False):
    if is_ad_url(src.dl_url or "") and src.page_url and src.page_url != src.dl_url:
        return _page_fallback_download(src, workdir, prog, q, audio)
    try:
        return _download_media(src, workdir, prog, q, audio)
    except Cancelled:
        raise
    except Exception as e:
        if src.page_url and src.page_url != src.dl_url and not audio:
            try:
                _clear_files(workdir)
                return _page_fallback_download(src, workdir, prog, q, audio)
            except Cancelled:
                raise
            except Exception:
                pass
        if not FALLBACK_PROXY or PROXY_URL or not NETERR_RE.search(str(e)):
            raise
        logging.warning("[dl] direct failed, retrying via fallback proxy")
        _clear_files(workdir)
        _tl.proxy = FALLBACK_PROXY
        try:
            return _download_media(src, workdir, prog, q, audio)
        except Cancelled:
            raise
        except Exception as e2:
            raise RuntimeError(f"{str(e)[:300]} || proxy: {str(e2)[:300]}")
        finally:
            _tl.proxy = None


def _download_media(src: Source, workdir, prog, q=0, audio=False):
    tier = choose_tier(src, q)
    errs = []
    target_url = _pick_alt(src.alt_urls, tier, src.dl_url) if not audio else src.dl_url

    if src.mode == "ytdlp" and target_url == src.dl_url:
        try:
            _clear_files(workdir)
            return ytdlp_download(target_url, workdir, src.headers, tier, prog, audio, src.imp)
        except Cancelled:
            raise
        except Exception as e:
            errs.append(f"yt-dlp: {str(e)[:150]}")
    else:
        try:
            _clear_files(workdir)
            return download_direct(target_url, os.path.join(workdir, "dl.mp4"), src.headers, src.cookies, prog)
        except Cancelled:
            raise
        except Exception as e:
            errs.append(f"direct: {str(e)[:150]}")
        try:
            _clear_files(workdir)
            return ytdlp_download(target_url, workdir, src.headers, tier, prog, audio, src.imp)
        except Cancelled:
            raise
        except Exception as e:
            errs.append(f"yt-dlp: {str(e)[:150]}")
    kind = classify_media(target_url)
    if kind:
        try:
            _clear_files(workdir)
            return ffmpeg_stream_download(
                target_url, os.path.join(workdir, "stream.mp4"),
                src.headers, src.duration, prog, kind == "hls"
            )
        except Cancelled:
            raise
        except Exception as e:
            errs.append(f"ffmpeg: {str(e)[:150]}")
    raise RuntimeError(" | ".join(errs) or "download fail")


def _pixels(path):
    inf = ffprobe(path)
    return (inf["width"] * inf["height"]) if inf else 0


def _direct_frame_url(s: Source) -> str:
    if s.mode == "direct" or classify_media(s.dl_url):
        return s.dl_url
    if s.alt_urls:
        return s.alt_urls[max(s.alt_urls)]
    vids = [
        f for f in (s.formats or [])
        if (f.get("url") or "").startswith("http") and f.get("vcodec") not in (None, "none")
    ]
    if vids:
        best = max(vids, key=lambda f: (qheight(f) <= 1080, qheight(f), f.get("tbr") or 0))
        return best["url"]
    return ""


def fetch_thumbnail(job: Job):
    if job.thumb_path and os.path.exists(job.thumb_path):
        return job.thumb_path
    s = job.src
    sess = make_session(make_headers(s.page_url), s.cookies)
    best, bw, bh = download_best_thumbnail(s.thumbs[:12], sess, THUMB_DIR, job.id)
    if best and bw >= 480:
        job.thumb_path = best
        return best
    frame_url = _direct_frame_url(s)
    if frame_url:
        hdrs = dict(s.headers) if s.headers else make_headers(s.page_url)
        if "Referer" not in hdrs and s.page_url:
            hdrs["Referer"] = s.page_url
        dur = s.duration or 30
        fbest, fscore = None, -1.0
        for k, frac in enumerate((0.08, 0.25, 0.5, 0.75)):
            cand = os.path.join(THUMB_DIR, f"{job.id}_fr{k}.jpg")
            try:
                grab_frame(frame_url, cand, at=max(1.0, dur * frac), headers=hdrs, quality=1)
            except Exception:
                continue
            if not os.path.exists(cand):
                continue
            sc = _img_quality(cand)
            if sc > fscore:
                if fbest:
                    _rm(fbest)
                fbest, fscore = cand, sc
            else:
                _rm(cand)
        if fbest and (not best or fscore > bw * bh * 1.2):
            if best:
                _rm(best)
            best = fbest
        elif fbest:
            _rm(fbest)
    if best:
        job.thumb_path = best
    return best


def post_kb(job: Job) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✂️ Cut", callback_data=f"pc:{job.id}"),
            InlineKeyboardButton("🔞 Censor", callback_data=f"pz:{job.id}"),
            InlineKeyboardButton("🖼 Thumbnail", callback_data=f"th:{job.id}"),
        ],
        [
            InlineKeyboardButton("🔥 Smart Highlight", callback_data=f"hl:{job.id}"),
            InlineKeyboardButton("🎬 10s Preview", callback_data=f"pv:{job.id}"),
        ],
    ])


def stitch_segments(seg_paths, out, target_h=0, prog=None):
    if len(seg_paths) == 1 and not target_h:
        shutil.copyfile(seg_paths[0], out)
        return out
    lst = out + ".list.txt"
    with open(lst, "w") as f:
        for pth in seg_paths:
            f.write("file '" + os.path.abspath(pth) + "'\n")
    vf = []
    if target_h:
        vf.append(f"scale=-2:min({target_h}\\,ih)")
    vf.append("setsar=1")
    args = ["-f", "concat", "-safe", "0", "-i", lst,
            "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out]
    run_ffmpeg(args, None, lambda c, t: prog.update("🧩 Parts jod raha hoon", c, t, "time") if prog else None,
               stop=prog.stop if prog else None)
    _rm(lst)
    return out


def highlight_quality_kb(job):
    hs = [h for h in (1080, 720, 480, 360) if not job.src.height or job.src.height >= h]
    rows = [[InlineKeyboardButton("🎞 Original", callback_data=f"hlq:{job.id}:0")]]
    if hs:
        rows[0] += [InlineKeyboardButton(f"{h}p", callback_data=f"hlq:{job.id}:{h}") for h in hs[:2]]
    if len(hs) > 2:
        rows.append([InlineKeyboardButton(f"{h}p", callback_data=f"hlq:{job.id}:{h}") for h in hs[2:]])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"hlx:{job.id}")])
    return InlineKeyboardMarkup(rows)


async def run_smart_clip(client, job: Job, kind: str):
    global active_downloads
    _ensure_sems()
    loop = asyncio.get_running_loop()
    job.busy = True
    msg = job.msg
    label = "🎬 10s Preview" if kind == "preview" else "🔥 Smart Highlight"
    prog = Progress(loop, msg, f"{label}: <b>{esc(job.src.title[:55])}</b>", stop_event(job.chat_id))
    if job.src.is_preview:
        try:
            await prog.stage("🔎 Source preview clip hai — pehle FULL video le raha hoon…")
            full = await asyncio.wait_for(resolve_source(job.src.page_url, prog.stage), timeout=240)
            if full and (full.duration or 0) >= (job.src.duration or 0):
                job.src = full
        except Exception:
            pass
    workdir = os.path.join(DOWNLOAD_DIR, f"sc_{job.id}_{int(time.time())}")
    os.makedirs(workdir, exist_ok=True)
    try:
        async with DL_SEM:
            active_downloads += 1
            try:
                await prog.stage("⬇️ Video laa raha hoon…")
                path = await loop.run_in_executor(None, download_media, job.src, workdir, prog, job.q, False)
                info = ffprobe(path) or {}
                dur = info.get("duration") or job.src.duration or 0
                if not dur:
                    raise RuntimeError("Video duration nahi mili")
                base = sanitize_filename(job.src.title or "video")
                if kind == "preview":
                    length = min(10.0, dur)
                    await prog.stage("🔎 Sabse hot hissa dhoondh raha hoon…")
                    nstart, hits = await loop.run_in_executor(None, nude_window, path, dur, 10.0)
                    if nstart is not None:
                        start, why = nstart, f"nudity-part • {hits} hits"
                    else:
                        start, why = await loop.run_in_executor(
                            None, lambda: highlight_start(path, dur, job.src.heatmap, length, prog.stop))
                    start = max(0.0, min(start, max(0.0, dur - length)))
                    end = min(dur, start + length)
                    raw = os.path.join(workdir, "prev_raw.mp4")
                    await loop.run_in_executor(None, cut_video, path, raw, start, end, "exact", info, prog)
                    if ENC_SEM.locked():
                        await prog.stage("Queue mein hai… (pehla encode khatam hone do)")
                    async with ENC_SEM:
                        await prog.stage("🩹 Preview ko sticker-censor kar raha hoon (Telegram-safe)…")
                        r = await loop.run_in_executor(None, censor_any, raw, prog, None)
                    cinfo = ffprobe(r["path"]) or {}
                    cap = (
                        f"🎬 <b>10s Preview (Censored)</b>\n\n🎬 <b>Name:</b> <code>{esc(base)}</code>\n"
                        f"📍 <b>Part:</b> <code>{fmt_hms(start)} → {fmt_hms(end)}</code> ({esc(why)})\n"
                        f"🩹 <b>Regions hidden:</b> <code>{r.get('found', 0)}</code>"
                    )
                    await send_one(client, job.chat_id, r["path"], f"{base}_preview10s.mp4", cap,
                                   cinfo, post_kb(job), prog, "pv", workdir, job.reply_to)
                else:
                    seg_len = 8.0 if dur >= 120 else 6.0 if dur >= 45 else max(3.0, dur / 4.0)
                    nseg = 4 if dur >= 120 else 3 if dur >= 45 else 2 if dur >= 20 else 1
                    await prog.stage("🔎 Sabse teasing hisse dhoondh raha hoon…")
                    segs = await loop.run_in_executor(
                        None, lambda: teasing_segments(path, dur, seg_len, nseg, prog.stop))
                    why = "teasing parts"
                    if not segs:
                        segs = await loop.run_in_executor(
                            None, lambda: motion_segments(path, dur, seg_len, nseg, prog.stop))
                        why = "sabse zyada action/movement wale parts"
                    if not segs:
                        segs = [(max(0.0, dur * 0.2), 0)]
                    parts = []
                    for i, (stt, _sc) in enumerate(segs):
                        sp = os.path.join(workdir, f"seg_{i}.mp4")
                        await loop.run_in_executor(
                            None, cut_video, path, sp, stt, min(dur, stt + seg_len), "exact", info, prog)
                        parts.append((sp, stt))
                    outp = os.path.join(workdir, "highlight.mp4")
                    target_h = int(getattr(job, "hl_q", 0) or 0)
                    await loop.run_in_executor(
                        None, stitch_segments, [p for p, _ in parts], outp, target_h, prog)
                    cinfo = ffprobe(outp) or {}
                    plist = ", ".join(fmt_hms(stt) for _p, stt in parts)
                    cap = (
                        f"🔥 <b>Smart Highlight</b>\n\n🎬 <b>Name:</b> <code>{esc(base)}</code>\n"
                        f"📍 <b>Parts:</b> <code>{esc(plist)}</code> ({esc(why)})\n"
                        f"🎞 <b>Quality:</b> <code>{(str(target_h) + 'p') if target_h else 'Original'}</code>\n"
                        f"📦 <b>Size:</b> <code>{humanbytes(os.path.getsize(outp))}</code>"
                    )
                    await send_one(client, job.chat_id, outp, f"{base}_highlight.mp4", cap,
                                   cinfo, post_kb(job), prog, "hl", workdir, job.reply_to)
                try:
                    await msg.delete()
                except Exception:
                    pass
            finally:
                active_downloads -= 1
    except Cancelled:
        await cancel_note(msg)
    except asyncio.CancelledError:
        await cancel_note(msg)
        raise
    except Exception as e:
        logging.exception("smart clip failed")
        try:
            await msg.edit_text(f"❌ <b>Smart clip fail:</b>\n<code>{esc(str(e)[:300])}</code>")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        job.busy = False


async def run_censor(client, job: Job, mode=None):
    global active_downloads
    _ensure_sems()
    loop = asyncio.get_running_loop()
    job.busy = True
    msg = job.msg
    prog = Progress(loop, msg, f"🔞 <b>Censor: {esc(job.src.title[:55])}</b>", stop_event(job.chat_id))
    workdir = os.path.join(DOWNLOAD_DIR, f"cz_{job.id}_{int(time.time())}")
    os.makedirs(workdir, exist_ok=True)
    try:
        if job.src.duration and job.src.duration > CENSOR_MAX_SEC:
            return await msg.edit_text(
                f"✂️ Video <code>{int(job.src.duration)}s</code> ki hai. Censor ke liye max "
                f"<code>{int(CENSOR_MAX_SEC)}s</code> — pehle ✂️ Cut karke chhota hissa lo."
            )
        async with DL_SEM:
            active_downloads += 1
            try:
                await prog.stage("⬇️ Video laa raha hoon…")
                path = await loop.run_in_executor(None, download_media, job.src, workdir, prog, job.q, False)
                secs = await loop.run_in_executor(None, video_seconds, path)
                if secs and secs > CENSOR_MAX_SEC:
                    return await msg.edit_text(
                        f"✂️ Video <code>{int(secs)}s</code> ki nikli. Censor ke liye max "
                        f"<code>{int(CENSOR_MAX_SEC)}s</code> — pehle ✂️ Cut karo."
                    )
                if ENC_SEM.locked():
                    await prog.stage("Queue mein hai… (pehla censor khatam hone do)")
                async with ENC_SEM:
                    await prog.stage("🔎 Anime/3D nudity scan + censor…")
                    r = await loop.run_in_executor(None, censor_any, path, prog, mode)
                info = ffprobe(r["path"]) or {}
                base = sanitize_filename(job.src.title or "video")
                cap = (
                    f"🔞 <b>Censored</b>\n\n🎬 <b>Name:</b> <code>{esc(base)}</code>\n"
                    f"🩹 <b>Regions hidden:</b> <code>{r.get('found', 0)}</code>\n"
                    f"🧠 <b>Detector:</b> <code>{'+'.join(r.get('backends') or [])}</code>\n"
                    f"📦 <b>Size:</b> <code>{humanbytes(os.path.getsize(r['path']))}</code>"
                )
                await send_one(
                    client, job.chat_id, r["path"], f"{base}_censored.mp4",
                    cap, info, post_kb(job), prog, "cz", workdir, job.reply_to
                )
                try:
                    await msg.delete()
                except Exception:
                    pass
            finally:
                active_downloads -= 1
    except Cancelled:
        await cancel_note(msg)
    except asyncio.CancelledError:
        await cancel_note(msg)
        raise
    except Exception as e:
        logging.exception("censor failed")
        try:
            await msg.edit_text(f"❌ <b>Censor fail:</b>\n<code>{esc(str(e)[:300])}</code>")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        job.busy = False


async def censor_photo_message(client, message: Message, mode=None):
    """User ne image bheji + censor maanga -> download, censor, wapas bhejo."""
    media = message.photo or message.document
    if not media:
        return
    loop = asyncio.get_running_loop()
    st = await message.reply_text("🔞 Image censor ho rahi hai…", quote=True)
    if getattr(st, "id", None):
        remember_sent(message.chat.id, st.id)
    workdir = os.path.join(DOWNLOAD_DIR, f"ci_{INSTANCE}_{int(time.time())}")
    os.makedirs(workdir, exist_ok=True)
    try:
        path = await client.download_media(media, file_name=os.path.join(workdir, "in.jpg"))
        r = await loop.run_in_executor(None, censor_image, path, os.path.join(workdir, "out.jpg"), mode)
        cap = (
            f"🔞 <b>Censored image</b>\n🩹 Regions hidden: <code>{r.get('found', 0)}</code>\n"
            f"🧠 Detector: <code>{'+'.join(r.get('backends') or [])}</code>"
        )
        m = await client.send_photo(message.chat.id, r["path"], caption=cap, reply_to_message_id=message.id)
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


def _img_quality(path):
    """Thumbnail score: resolution x brightness x detail (kaali/blank frame neeche)."""
    try:
        from PIL import Image, ImageStat
        with Image.open(path) as im:
            g = im.convert("L")
            px = g.width * g.height
            st = ImageStat.Stat(g)
            mean, var = st.mean[0], st.var[0]
        bright = 1.0 if 30 <= mean <= 230 else 0.2
        return px * bright * (1.0 + min(var, 4000) / 4000.0)
    except Exception:
        return float(_pixels(path))


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
    if s.is_preview:
        lines.append("🔎 <b>Note:</b> Site par preview clip mila tha — usi video ka <b>HD full version</b> ready hai, quality button dabao.")
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
        InlineKeyboardButton("🎵 Audio", callback_data=f"au:{job.id}"),
        InlineKeyboardButton("🗜 Smart Compress", callback_data=f"zp:{job.id}")
    ])
    rows.append([
        InlineKeyboardButton("🖼 HD Thumbnail", callback_data=f"th:{job.id}"),
        InlineKeyboardButton("🔥 Smart Highlight", callback_data=f"hl:{job.id}"),
        InlineKeyboardButton("🎬 10s Preview", callback_data=f"pv:{job.id}"),
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
        f"📺 Quality: <code>{f'{job.q}p' if job.q else 'Best Auto'}</code>{warn}\n\n"
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


def fit_to_limit(path, out, prog):
    if os.path.getsize(path) <= UPLOAD_LIMIT:
        return path, "none"
    return out, compress_to_fit(path, out, COMPRESS_TARGET, prog)


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


def make_caption(name, info, size, extra=""):
    q = f"{info.get('short')}p" if info.get("short") else "?"
    return (
        f"✅ <b>Downloaded!</b>\n\n🎬 <b>Name:</b> <code>{esc(name)}</code>\n"
        f"📺 <b>Quality:</b> <code>{q}</code>\n⏱ <b>Length:</b> <code>{fmt_dur(info.get('duration'))}</code>\n"
        f"📦 <b>Size:</b> <code>{humanbytes(size)}</code>{extra}"
    )[:1000]


async def send_one(client, chat_id, path, fname, caption, info, kb, prog, label, workdir, reply=None):
    thumb = os.path.join(workdir, f"{label}_thumb.jpg")
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(
        None, lambda: grab_frame(path, thumb, at=max(1, (info.get("duration") or 10) * 0.1), width=320, quality=5)
    )
    m = await client.send_video(
        chat_id=chat_id, video=path, caption=caption, file_name=fname, supports_streaming=True,
        duration=int(info.get("duration") or 0), width=int(info.get("width") or 0),
        height=int(info.get("height") or 0), thumb=thumb if ok else None, reply_markup=kb,
        reply_to_message_id=reply,
        progress=_up_progress, progress_args=(prog, f"📤 Uploading ({label})"),
    )
    if getattr(m, "id", None):
        remember_sent(chat_id, m.id)
    elif prog.stop is not None and prog.stop.is_set():
        raise Cancelled()


async def _up_progress(current, total, prog, label):
    try:
        prog.update(label, current, total)
    except Cancelled:
        raise StopTransmission()


async def run_delivery(client, job: Job, cut, audio=False, compress=None):
    global active_downloads
    _ensure_sems()
    loop = asyncio.get_running_loop()
    job.busy = True
    msg = job.msg
    header = f"🎬 <b>{esc(job.src.title[:70])}</b>" + (f" • {job.q}p" if 0 < job.q < MAX_HEIGHT else "")
    prog = Progress(loop, msg, header, stop_event(job.chat_id))
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
                reply = job.reply_to

                cmpr = res.get("cmp")
                if cmpr and not cmpr["skipped"]:
                    await send_one(
                        client, job.chat_id, cmpr["path"], f"{base}_compressed.mp4",
                        cmp_caption(base, cmpr), cmpr["info"], post_kb(job),
                        prog, "cmp", workdir, reply
                    )
                elif res.get("audio"):
                    apath = res["audio"]
                    cap = (
                        f"🎵 <b>Audio</b>\n\n🎬 <b>Name:</b> <code>{esc(base)}</code>\n"
                        f"⏱ <b>Length:</b> <code>{fmt_dur(res['dur'])}</code>\n"
                        f"📦 <b>Size:</b> <code>{humanbytes(os.path.getsize(apath))}</code>"
                    )
                    m = await client.send_audio(
                        chat_id=job.chat_id, audio=apath, caption=cap, file_name=f"{base}.m4a",
                        duration=int(res["dur"] or 0), title=base[:60], reply_to_message_id=reply,
                        progress=_up_progress, progress_args=(prog, "📤 Uploading (audio)")
                    )
                    if getattr(m, "id", None):
                        remember_sent(job.chat_id, m.id)
                elif res["cut"]:
                    c = res["cut"]
                    csize = os.path.getsize(c["path"])
                    extra = (
                        f"\n✂️ <b>Cut:</b> <code>{fmt_hms(c['start'])} → {fmt_hms(c['end'])}</code>"
                        f" ({'frame-exact' if c['mode'] == 'exact' else 'keyframe'})"
                    )
                    fname = f"{base}_cut_{fmt_hms(c['start']).replace(':', '-')}_{fmt_hms(c['end']).replace(':', '-')}.mp4"
                    await send_one(
                        client, job.chat_id, c["path"], fname,
                        make_caption(base, c["info"], csize, extra), c["info"],
                        post_kb(job), prog, "cut", workdir, reply
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
                    if method != "none":
                        extra += "\n🗜 2GB limit ke liye compress hui"
                    q_suffix = f"_{job.q}p" if 0 < job.q < MAX_HEIGHT else ""
                    await send_one(
                        client, job.chat_id, full, f"{base}{q_suffix}.mp4",
                        make_caption(base, finfo, os.path.getsize(full), extra), finfo,
                        post_kb(job), prog, "full", workdir, reply
                    )
                try:
                    await msg.delete()
                except Exception:
                    pass
            finally:
                active_downloads -= 1
    except Cancelled:
        await cancel_note(msg)
    except asyncio.CancelledError:
        await cancel_note(msg)
        raise
    except Exception as e:
        logging.exception("delivery failed")
        try:
            await msg.edit_text(f"❌ <b>Error:</b>\n<code>{esc(str(e)[:350])}</code>")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        job.busy = False


def status_text():
    return (
        f"📊 <b>Status</b>\n• Analyzing: <code>{analyzing}</code>\n"
        f"• Active downloads: <code>{active_downloads}</code>"
    )


def fail_text(err, diag):
    lines = ["❌ <b>Video extract nahi ho paaya.</b>", f"<code>{esc((err or '')[:200])}</code>"]
    if why(err):
        lines.append(f"💡 <b>Wajah:</b> {esc(why(err))}")
    cands = diag.get("cands") or []
    if cands:
        lines.append("\n🔎 <b>Ye links mile (sahi lage to button dabao):</b>")
        for i, c in enumerate(cands[:5], 1):
            lines.append(f"{i}. <code>{esc(c['url'][:140])}</code> ({c['kind']})")
    return "\n".join(lines)[:3900]


def fail_kb(fid, diag):
    rows = []
    for i, c in enumerate((diag.get("cands") or [])[:5]):
        label = name_from_url(c["url"])[:22] or urlparse(c["url"]).netloc[:22]
        rows.append([InlineKeyboardButton(f"▶️ {i + 1}. {c['kind'].upper()}  {label}", callback_data=f"tc:{fid}:{i}")])
    rows.append([InlineKeyboardButton("🤖 Gemini se poochho", callback_data=f"gm:{fid}")])
    return InlineKeyboardMarkup(rows)


async def dispatch_intent(client, job: Job, it: Intent, st):
    D = job.src.duration
    if it.q:
        job.q = it.q
    if it.action == "audio":
        return spawn(run_delivery(client, job, None, True), job.chat_id)
    if it.action == "compress":
        return spawn(run_delivery(client, job, None, False, parse_compress_opts(it.arg)), job.chat_id)
    if it.action == "download":
        return spawn(run_delivery(client, job, None), job.chat_id)
    if it.action == "cut":
        if it.start is not None and it.end is not None and it.end > it.start and not (D and it.start >= D):
            job.cs, job.ce = it.start, (min(it.end, D) if D else it.end)
            if it.mode:
                job.mode = it.mode
            return spawn(run_delivery(client, job, (job.cs, job.ce, job.mode)), job.chat_id)
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
    try:
        if act == "tc":
            u = f["cands"][int(arg)]["url"]
        else:
            await msg.edit_text("🤖 Gemini se link nikal raha hoon…")
            u = await loop.run_in_executor(None, gemini_find_url, f["url"], f.get("html"), f.get("net"), f.get("cands"))
            if not u:
                return await msg.edit_text(
                    "🤖 Gemini ko bhi video link nahi mila.\n\n" + fail_text("", f),
                    reply_markup=fail_kb(f["id"], f)
                )
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


async def analyze_url(client, status_msg: Message, chat_id: int, url: str, reply_to=None, intent=None):
    global analyzing
    _ensure_sems()
    intent = intent or Intent()
    short = esc(url[:60])

    async def status(text):
        try:
            await status_msg.edit_text(f"{text}\n<code>{short}</code>")
        except Exception:
            pass

    async with ANALYZE_SEM:
        analyzing += 1
        try:
            src = await asyncio.wait_for(resolve_source(url, status), timeout=300)
            await finish_analysis(client, status_msg, chat_id, src, reply_to, intent)
        except asyncio.CancelledError:
            await cancel_note(status_msg)
            raise
        except Exception as e:
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
                        f"❌ <b>Video extract nahi ho paaya.</b>\n<code>{esc(str(e)[:150] or 'timeout')}</code>"
                    )
            except Exception:
                pass
        finally:
            analyzing -= 1


async def download_message(client, message: Message, text: str, urls: list):
    urls = urls[:10]
    it = rule_intent(URL_REGEX.sub(" ", text).strip(), True)
    if it.action == "template":
        return await template_reply(message, it.arg)
    for u in urls:
        st = await message.reply_text(f"🔍 <b>Analyzing…</b>\n<code>{esc(u[:60])}</code>", quote=True)
        if getattr(st, "id", None):
            remember_sent(message.chat.id, st.id)
        spawn(analyze_url(client, st, message.chat.id, u, message.id, it), message.chat.id)


async def chat_message(client, message: Message, text: str, start_text: str, help_text: str):
    it = rule_intent(text, False)
    if it.action == "status":
        m = await message.reply_text(status_text(), quote=True)
        if getattr(m, "id", None):
            remember_sent(message.chat.id, m.id)
        return
    if it.action == "help":
        m = await message.reply_text(help_text, quote=True)
        if getattr(m, "id", None):
            remember_sent(message.chat.id, m.id)
        return
    if it.action == "start":
        m = await message.reply_text(start_text, quote=True)
        if getattr(m, "id", None):
            remember_sent(message.chat.id, m.id)
        return
    if it.action == "template":
        return await template_reply(message, it.arg)

    job = JOBS.get(LAST.get(message.chat.id))
    if not job:
        m = await message.reply_text("🔗 Pehle video ka link ya video file bhejo.", quote=True)
        if getattr(m, "id", None):
            remember_sent(message.chat.id, m.id)
        return
    if it.action == "none":
        m = await message.reply_text(
            "🤖 Aise likho: <code>720p mein download</code>, <code>audio do</code>, "
            "<code>1:20 se 2:45 cut karo</code>, <code>help</code>", quote=True
        )
        if getattr(m, "id", None):
            remember_sent(message.chat.id, m.id)
        return
    if job.busy:
        m = await message.reply_text("⏳ Pichla kaam abhi chal raha hai.", quote=True)
        if getattr(m, "id", None):
            remember_sent(message.chat.id, m.id)
        return
    st = await message.reply_text("⏳ Shuru kar raha hoon…", quote=True)
    if getattr(st, "id", None):
        remember_sent(message.chat.id, st.id)
    job.msg = st
    await dispatch_intent(client, job, it, st)


async def compress_run(client, c):
    _ensure_sems()
    loop = asyncio.get_running_loop()
    c["busy"] = True
    st = c["st"]
    prog = Progress(loop, st, f"🗜 <b>Smart Compress: {esc(c['name'][:55])}</b>", stop_event(c["chat"]))
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
            if not path or not os.path.exists(path):
                raise Cancelled()
            r = await loop.run_in_executor(
                None, smart_compress, path, os.path.join(workdir, "out.mp4"), prog, dict(c["opts"])
            )
            if r["skipped"]:
                return await st.edit_text(f"✅ <b>Info:</b> {esc(r['reason'])}.")
            await send_one(
                client, c["chat"], r["path"], f"{c['name']}_compressed.mp4",
                cmp_caption(c["name"], r), r["info"], None, prog, "cmp", workdir, c["rid"]
            )
            try:
                await st.delete()
            except Exception:
                pass
            CJOBS.pop(c["id"], None)
    except Cancelled:
        await cancel_note(st)
    except asyncio.CancelledError:
        await cancel_note(st)
        raise
    except Exception as e:
        logging.exception("compress failed")
        try:
            await st.edit_text(f"❌ <b>Compress fail:</b>\n<code>{esc(str(e)[:300])}</code>")
        except Exception:
            pass
    finally:
        c["busy"] = False
        shutil.rmtree(workdir, ignore_errors=True)


async def compress_message(client, message: Message, text: str):
    media = (
        message.video
        or message.document
        or getattr(message, "animation", None)
        or getattr(message, "video_note", None)
    )
    if not media:
        m = await message.reply_text("📹 Compress karne ke liye video file ke saath likho.", quote=True)
        if getattr(m, "id", None):
            remember_sent(message.chat.id, m.id)
        return
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
    if getattr(st, "id", None):
        remember_sent(message.chat.id, st.id)
    c["st"] = st
    spawn(compress_run(client, c), c["chat"])


async def handle_callback(client, cq: CallbackQuery):
    parts = (cq.data or "").split(":")
    act = parts[0]
    if len(parts) > 1 and not parts[1].startswith(INSTANCE):
        try:
            await cq.answer("⌛ Ye request purani ho gayi — link dobara bhejo.", show_alert=True)
        except Exception:
            pass
        return

    if act in ("tc", "gm"):
        f = FAILS.get(parts[1]) if len(parts) > 1 else None
        if not f:
            return await cq.answer("⌛ Ye request purani ho gayi — link dobara bhejo.", show_alert=True)
        await cq.answer("⏳ Check kar raha hoon…")
        return spawn(retry_failed(client, cq.message, f, act, parts[2] if len(parts) > 2 else ""), cq.message.chat.id)

    job = JOBS.get(parts[1]) if len(parts) > 1 else None
    if not job:
        return await cq.answer("⌛ Ye request purani ho gayi — link dobara bhejo.", show_alert=True)
    chat = cq.message.chat.id

    if act == "th":
        await cq.answer("🖼 HD thumbnail la raha hoon…")
        loop = asyncio.get_running_loop()
        path = await loop.run_in_executor(None, fetch_thumbnail, job)
        if not path:
            m = await client.send_message(chat, "❌ Is video ka thumbnail nahi mil paaya.", reply_to_message_id=job.reply_to)
            if getattr(m, "id", None):
                remember_sent(chat, m.id)
            return
        try:
            m = await client.send_document(
                chat, path, caption=f"🖼 <b>{esc(job.src.title[:100])}</b>",
                file_name=f"{sanitize_filename(job.src.title)}{os.path.splitext(path)[1] or '.jpg'}",
                force_document=True, reply_to_message_id=job.reply_to
            )
            if getattr(m, "id", None):
                remember_sent(chat, m.id)
        except Exception:
            pass
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
        return spawn(run_delivery(client, job, None), job.chat_id)
    if act == "au":
        await cq.answer("🎵 Audio extraction shuru…")
        return spawn(run_delivery(client, job, None, True), job.chat_id)
    if act == "zp":
        await cq.answer("🗜 Smart Compress shuru…")
        return spawn(run_delivery(client, job, None, False, parse_compress_opts("")), job.chat_id)
    if act == "cx":
        await cq.answer()
        return await cq.message.edit_text(card_text(job), reply_markup=card_kb(job))
    if act == "cg":
        D = job.src.duration
        if job.ce - job.cs < 1 or (D and job.cs >= D):
            return await cq.answer("⚠️ Start/End time sahi daalo", show_alert=True)
        await cq.answer("✂️ Cut + Download shuru…")
        return spawn(run_delivery(client, job, (job.cs, min(job.ce, D) if D else job.ce, job.mode)), job.chat_id)
    if act == "pc":
        await cq.answer("✂️ Cut panel khol raha hoon…")
        job.cs, job.ce, job.sel, job.fresh = 0.0, job.src.duration or 600.0, "s", True
        return await refresh()
    if act == "pz":
        await cq.answer("🔞 Censor shuru…")
        return spawn(run_censor(client, job, None), job.chat_id)
    if act == "hl":
        await cq.answer("🔥 Quality chuno…")
        m = await client.send_message(
            job.chat_id,
            "🔥 <b>Smart Highlight</b> — kis quality me banaun?\n"
            "Video ke sabse <b>teasing parts</b> jod kar ek highlight banege.",
            reply_markup=highlight_quality_kb(job),
        )
        if getattr(m, "id", None):
            remember_sent(job.chat_id, m.id)
        return
    if act == "hlq":
        if job.busy:
            return await cq.answer("⏳ Pichla kaam abhi chal raha hai.", show_alert=True)
        try:
            job.hl_q = int(parts[2]) if len(parts) > 2 else 0
        except Exception:
            job.hl_q = 0
        job.msg = cq.message
        await cq.answer("🔥 Highlight bana raha hoon…")
        return spawn(run_smart_clip(client, job, "highlight"), job.chat_id)
    if act == "hlx":
        await cq.answer("❌ Cancel kar diya")
        try:
            await cq.message.delete()
        except Exception:
            pass
        return
    if act == "pv":
        await cq.answer("🎬 10s Preview bana raha hoon…")
        return spawn(run_smart_clip(client, job, "preview"), job.chat_id)
    if act == "pb":
        await cq.answer("🎨 Censor (blend) shuru…")
        return spawn(run_censor(client, job, "blend"), job.chat_id)
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
            if not os.path.isdir(p):
                continue
            try:
                age = now - os.path.getmtime(p)
            except OSError:
                continue
            if (_is_own_dir(d) and age > 4 * 3600) or age > 24 * 3600:
                shutil.rmtree(p, ignore_errors=True)
