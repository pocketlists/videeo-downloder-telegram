#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Universal Video Downloader Bot  (v12)
=====================================
Flow:
  1. Link bhejo  ->  bot analyze karta hai (yt-dlp -> browser sniff -> HTML scan -> Gemini)
  2. Analysis ke BAAD buttons aate hain:  [⬇️ Download • ⏱ length]  [✂️ Cut + Download • ⏱ length]
  3. Cut panel: Start/End ko +/- buttons se set karo (ya type karo: 1:30-5:45)
  4. Chat mein 2 videos aati hain: ✂️ cut wali + 🎬 full wali (har video pe 🖼 Thumbnail button)

Quality rules:
  * Default best quality, max 1080p (720p se kam nahi, jab tak site khud na de)
  * Size > 2GB estimate ho  -> chup-chap 720p
  * Phir bhi > 2GB          -> sirf beech ka hissa compress (shuru/end original quality)
  * 2GB se chhoti file ko kabhi compress nahi karta

Requirements: ffmpeg + ffprobe (system), pip: pyrogram tgcrypto yt-dlp cloudscraper
              playwright google-generativeai  (optional: curl_cffi for Cloudflare sites)
"""
import os
import re
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
from pyrogram import Client, filters, enums, idle
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)
from playwright.async_api import async_playwright

try:
    import google.generativeai as genai
except Exception:  # optional
    genai = None

# ==========================================
# 1. CONFIG
# ==========================================
API_ID = int(os.environ.get("API_ID", 0) or 0)
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", 2))
MiB = 1024 * 1024
UPLOAD_LIMIT = int(os.environ.get("UPLOAD_LIMIT_MB", 1990)) * MiB      # isse upar = compress
COMPRESS_TARGET = int(os.environ.get("COMPRESS_TARGET_MB", 1900)) * MiB  # compress ke baad target
MAX_HEIGHT = 1080
FALLBACK_HEIGHT = 720
COOKIES_FILE = "cookies.txt"

DOWNLOAD_DIR = "./downloads/"
THUMB_DIR = "./thumbs/"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

try:
    app = Client(
        "video_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
        parse_mode=enums.ParseMode.HTML, max_concurrent_transmissions=4,
    )
except TypeError:  # purana pyrogram
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

# ---- global state ----
JOBS = {}          # job_id -> Job
ACTIVE_CUT = {}    # chat_id -> job_id (jiska cut panel khula hai)
ANALYZE_SEM = None
DL_SEM = None
active_downloads = 0
analyzing = 0
BG_TASKS = set()
URL_REGEX = re.compile(r'https?://[^\s<>"\']+')


def spawn(coro):
    t = asyncio.create_task(coro)
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)
    return t


# ==========================================
# 2. SITE CONFIG
# ==========================================
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
    'a[href*="1080"]', 'a[href*="720"]', 'a[href*="get_file"]', 'a[href*=".mp4"]',
    'a[href*="download"]', 'a:has-text("1080p")', 'a:has-text("720p")',
    'a:has-text("MP4")', 'a:has-text("Download")', 'a[download]',
    '.download-button', '.btn-download', '#download', '#btn-download',
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


# ==========================================
# 3. SMALL HELPERS
# ==========================================
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


_T = r'(?:\d+:\d{1,2}(?::\d{1,2})?(?:\.\d+)?|\d+h(?:\d+m)?(?:\d+s)?|\d+m(?:\d+s)?|\d+(?:\.\d+)?s?)'
RANGE_RE = re.compile(rf'^\s*({_T})\s*(?:-|–|—|→|to|,)\s*({_T})\s*$', re.I)


def parse_time(tok: str):
    tok = (tok or "").strip().lower()
    if not tok:
        return None
    try:
        if ":" in tok:
            sec = 0.0
            for p in tok.split(":"):
                sec = sec * 60 + float(p)
            return sec
        m = re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s?)?', tok)
        if not m or not any(m.groups()):
            return None
        h, mi, s = m.groups()
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0)
    except Exception:
        return None


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
    """'hls' | 'dash' | 'mp4' | None   (segments ko ignore karta hai)"""
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
    if any(k in p for k in ("/get_file/", "/get_video/", "videoplayback")):
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
    for c in cookies or []:
        try:
            s.cookies.set(c["name"], c["value"], domain=c.get("domain") or None, path=c.get("path") or "/")
        except Exception:
            pass
    return s


@lru_cache(maxsize=1)
def impersonate_target():
    """curl_cffi installed ho to yt-dlp Cloudflare wali sites par browser jaisa banega."""
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget
        return ImpersonateTarget.from_str("chrome")
    except Exception:
        return None


# ==========================================
# 4. DATA MODELS
# ==========================================
@dataclass
class Source:
    page_url: str
    dl_url: str
    mode: str                       # 'ytdlp' | 'direct'
    headers: dict
    cookies: list
    title: str = "video"
    duration: float = 0.0
    height: int = 0
    size: int = 0
    formats: list = field(default_factory=list)
    est: dict = field(default_factory=dict)      # {1080: (bytes, height), 720: (...)}
    thumbs: list = field(default_factory=list)
    use_url_name: bool = False


@dataclass
class Job:
    id: str
    chat_id: int
    src: Source
    msg: Message = None
    created: float = field(default_factory=time.time)
    cs: float = 0.0
    ce: float = 0.0
    sel: str = "s"                  # panel mein kaunsa field edit ho raha hai
    mode: str = "auto"              # auto | fast | accurate
    busy: bool = False
    thumb_path: str = ""


# ==========================================
# 5. MEDIA TOOLS (ffprobe / ffmpeg)
# ==========================================
def ffprobe(target, headers=None, timeout=40):
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams"]
    if str(target).startswith("http"):
        cmd += ["-rw_timeout", "20000000"]
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
    return {
        "duration": num(fmt.get("duration")) or num((v or {}).get("duration")),
        "size": int(num(fmt.get("size"))),
        "width": w, "height": h, "short": min(w, h) if w and h else 0,
        "vcodec": (v or {}).get("codec_name"), "pix_fmt": (v or {}).get("pix_fmt"),
        "fps": fps, "v_bps": int(num((v or {}).get("bit_rate"))),
        "acodec": (a or {}).get("codec_name"), "a_bps": int(num((a or {}).get("bit_rate"))),
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


def ensure_compatible(path, on_progress=None):
    """Telegram ke liye mp4 + h264(yuv420p) + aac. Zaroorat ho tabhi convert."""
    info = ffprobe(path)
    if not info:
        raise RuntimeError("Downloaded file invalid hai (ffprobe fail)")
    ok_v = info["vcodec"] == "h264" and info["pix_fmt"] in ("yuv420p", "yuvj420p")
    ok_a = info["acodec"] in (None, "aac", "mp3")
    if ok_v and ok_a and path.lower().endswith(".mp4"):
        return path
    out = os.path.splitext(path)[0] + "_tg.mp4"
    maps = ["-map", "0:v:0", "-map", "0:a:0?"]
    if ok_v and ok_a:
        args = ["-i", path] + maps + ["-c", "copy"]
    elif ok_v:
        args = ["-i", path] + maps + ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k"]
    else:
        args = ["-i", path] + maps + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                                      "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k"]
    run_ffmpeg(args + ["-movflags", "+faststart", out], info["duration"], on_progress)
    try:
        os.remove(path)
    except OSError:
        pass
    return out


def cut_video(src, dst, start, end, mode="auto", on_progress=None):
    """Returns actual mode used: 'fast' (stream copy) ya 'accurate' (re-encode)."""
    dur = max(0.5, end - start)
    if mode == "auto":
        mode = "accurate" if dur <= 300 else "fast"
    maps = ["-map", "0:v:0", "-map", "0:a:0?"]
    if mode == "fast":
        run_ffmpeg(["-ss", f"{start:.3f}", "-i", src, "-t", f"{dur:.3f}"] + maps +
                   ["-c", "copy", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", dst],
                   dur, on_progress)
        info = ffprobe(dst)
        if info and info["duration"] >= dur * 0.5:
            return "fast"
        mode = "accurate"       # copy ne kharab output diya -> re-encode
    run_ffmpeg(["-ss", f"{start:.3f}", "-i", src, "-t", f"{dur:.3f}"] + maps +
               ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", dst],
               dur, on_progress)
    return "accurate"


# ----------------- compression (sirf zaroorat par, sirf beech ka hissa) -----------------
def compress_to_fit(src, dst, target, on_progress=None):
    """
    File ko `target` bytes ke andar lata hai. Returns 'partial' | 'full'.
    PARTIAL: shuru + end original (stream copy), sirf beech ka utna hissa re-encode
             jitna size girane ke liye zaroori ho. Fail ho to FULL (poori video, ek saath).
    """
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
    a_new = 128_000 if has_a else 0
    a_old = info["a_bps"] or a_new
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
    for attempt in range(3):
        mid_start = (D - Dm) / 2
        k1 = next((k for k in kfs if k >= mid_start), None)
        k2 = next((k for k in kfs if k >= mid_start + Dm), None)
        if k1 is None or k2 is None or k2 - k1 < 2 or k2 > D - 0.5:
            return False
        _build_spliced(src, dst, info, k1, k2, vm, work, cb)
        out = ffprobe(dst)
        size = os.path.getsize(dst)
        if not out or abs(out["duration"] - D) > max(3.0, D * 0.02):
            logging.warning("[compress] spliced duration mismatch")
            return False
        if size <= target:
            logging.info(f"[compress] partial OK: {humanbytes(S)} -> {humanbytes(size)} "
                         f"(compressed {k2 - k1:.0f}s of {D:.0f}s @ {vm/1000:.0f}kbps)")
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
    has_a = bool(info["acodec"])
    audio_copy = info["acodec"] == "aac"
    head, mid, tail = (os.path.join(work, n) for n in ("seg_head.ts", "seg_mid.ts", "seg_tail.ts"))
    lst = os.path.join(work, "seg_list.txt")
    if not has_a:
        a_copy, a_enc = ["-an"], ["-an"]
    else:
        a_copy = ["-c:a", "copy"] if audio_copy else ["-c:a", "aac", "-b:a", "128k"]
        a_enc = ["-c:a", "aac", "-b:a", "128k"]
        if info["ar"]:
            a_enc += ["-ar", str(info["ar"])]
            if not audio_copy:
                a_copy += ["-ar", str(info["ar"])]
        if info["ch"]:
            a_enc += ["-ac", str(info["ch"])]
            if not audio_copy:
                a_copy += ["-ac", str(info["ch"])]
    maps = ["-map", "0:v:0", "-map", "0:a:0?"]
    D = info["duration"]
    # head & tail: original quality (stream copy)
    run_ffmpeg(["-i", src, "-t", f"{k1:.3f}"] + maps + ["-c:v", "copy"] + a_copy + ["-f", "mpegts", head])
    run_ffmpeg(["-ss", f"{k2 + 0.005:.3f}", "-i", src] + maps + ["-c:v", "copy"] + a_copy + ["-f", "mpegts", tail])
    # middle: re-encode
    vb = int(vm)
    run_ffmpeg(["-ss", f"{k1:.3f}", "-i", src, "-t", f"{k2 - k1:.3f}"] + maps +
               ["-c:v", "libx264", "-preset", "veryfast", "-b:v", str(vb), "-maxrate", str(int(vb * 1.3)),
                "-bufsize", str(vb * 2), "-pix_fmt", "yuv420p"] + a_enc + ["-f", "mpegts", mid],
               k2 - k1, (lambda c, t: cb.update("🗜 Compressing (beech ka hissa)", c, t, "time")) if cb else None)
    with open(lst, "w") as f:
        for p in (head, mid, tail):
            f.write("file '" + os.path.abspath(p).replace("'", "'\\''") + "'\n")
    bsf = ["-bsf:a", "aac_adtstoasc"] if has_a else []
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", lst, "-map", "0", "-c", "copy"] + bsf +
               ["-movflags", "+faststart", dst], D)
    for p in (head, mid, tail, lst):
        try:
            os.remove(p)
        except OSError:
            pass


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


# ==========================================
# 6. PROGRESS (thread-safe message updater)
# ==========================================
class Progress:
    def __init__(self, loop, msg, header=""):
        self.loop, self.msg, self.header = loop, msg, header
        self.last = 0.0
        self.t0 = time.time()
        self.cur_stage = ""

    def update(self, stage, done, total, kind="bytes"):
        now = time.time()
        if stage != self.cur_stage:
            self.cur_stage, self.t0, self.last = stage, now, 0.0
        finished = bool(total) and done >= total
        if now - self.last < 3 and not finished:
            return
        self.last = now
        pct = f"{done / total * 100:.1f}%" if total else "…"
        if kind == "time":
            body = f"Progress: <code>{pct}</code>  (<code>{fmt_dur(done)}/{fmt_dur(total)}</code>)"
        else:
            speed = done / max(now - self.t0, 0.1)
            body = (f"Progress: <code>{pct}</code>\n"
                    f"Size: <code>{humanbytes(done)} / {humanbytes(total)}</code>\n"
                    f"Speed: <code>{humanbytes(speed)}/s</code>")
        text = f"{self.header}\n⏳ <b>{stage}</b>\n{body}"
        asyncio.run_coroutine_threadsafe(self._edit(text), self.loop)

    async def stage(self, text):
        self.cur_stage = text
        await self._edit(f"{self.header}\n⏳ <b>{text}</b>")

    async def _edit(self, text):
        try:
            await self.msg.edit_text(text)
        except Exception:
            pass


# ==========================================
# 7. EXTRACTION
# ==========================================
def base_ydl_opts(headers=None):
    h = dict(headers or {})
    h.setdefault("User-Agent", UA)
    o = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "nocheckcertificate": True,
        "socket_timeout": 30, "retries": 5, "fragment_retries": 10, "http_headers": h,
    }
    if os.path.exists(COOKIES_FILE):
        o["cookiefile"] = COOKIES_FILE
    imp = impersonate_target()
    if imp:
        o["impersonate"] = imp
    return o


def ytdlp_extract(url, headers=None):
    opts = base_ydl_opts(headers)
    opts.update({"skip_download": True})
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info and info.get("_type") == "playlist":
        info = next((e for e in (info.get("entries") or []) if e), None)
    return info


def compact_formats(info):
    keys = ("height", "width", "vcodec", "acodec", "tbr", "vbr", "abr", "filesize", "filesize_approx")
    return [{k: f.get(k) for k in keys} for f in (info.get("formats") or [])]


def est_size(formats, duration, max_h):
    """(estimated_bytes, height) — max_h tak ka best video + best audio."""
    def fsize(f):
        s = f.get("filesize") or f.get("filesize_approx")
        if s:
            return int(s)
        tbr = f.get("tbr") or ((f.get("vbr") or 0) + (f.get("abr") or 0))
        return int(tbr * 1000 / 8 * duration) if tbr and duration else 0

    has_v = lambda f: f.get("vcodec") not in (None, "none")
    has_a = lambda f: f.get("acodec") not in (None, "none")
    vids = [f for f in formats if has_v(f) and 0 < (f.get("height") or 0) <= max_h]
    if not vids:
        return 0, 0
    best = max(vids, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
    h = best.get("height") or 0
    if has_a(best):
        return fsize(best), h
    auds = [f for f in formats if has_a(f) and not has_v(f)]
    aud = max(auds, key=lambda f: f.get("abr") or f.get("tbr") or 0) if auds else None
    return fsize(best) + (fsize(aud) if aud else 0), h


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
    s = Source(page_url=url, dl_url=url, mode="ytdlp", headers=headers, cookies=cookies,
               title=info.get("title") or name_from_url(url) or "video",
               duration=float(info.get("duration") or 0), thumbs=collect_thumbs(info))
    s.formats = compact_formats(info)
    s.est = {h: est_size(s.formats, s.duration, h) for h in (MAX_HEIGHT, FALLBACK_HEIGHT)}
    s.height = s.est[MAX_HEIGHT][1] or int(info.get("height") or 0)
    return s


async def source_from_media(media_url, page_url, cookies, title=None, thumbs=None, pre=None, url_name=False):
    loop = asyncio.get_running_loop()
    kind = classify_media(media_url) or "mp4"
    headers = make_headers(page_url)
    ch = cookie_header(media_url, cookies)
    if ch:
        headers["Cookie"] = ch
    s = Source(page_url=page_url, dl_url=media_url, mode="ytdlp" if kind in ("hls", "dash") else "direct",
               headers=headers, cookies=cookies, title=title or name_from_url(media_url) or "video",
               thumbs=list(thumbs or []), use_url_name=url_name)
    info = pre or await loop.run_in_executor(None, ffprobe, media_url, headers, 30)
    if info:
        s.duration, s.height, s.size = info["duration"], info["short"], info["size"]
    if s.mode == "ytdlp":
        try:
            yi = await loop.run_in_executor(None, ytdlp_extract, media_url, headers)
            if yi:
                s.formats = compact_formats(yi)
                s.duration = s.duration or float(yi.get("duration") or 0)
                s.est = {h: est_size(s.formats, s.duration, h) for h in (MAX_HEIGHT, FALLBACK_HEIGHT)}
                s.height = s.est[MAX_HEIGHT][1] or s.height
        except Exception as e:
            logging.info(f"[yt-dlp] m3u8 info fail: {e}")
    return s


def scan_html_for_media(text, base):
    text = html.unescape((text or "").replace("\\/", "/"))
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
    """Headless browser se network + DOM scan. Returns dict(cands, title, thumbs, cookies, html)."""
    cfg = get_site_config(page_url)
    cands, seen = {}, set()
    res = {"cands": [], "title": None, "thumbs": [], "cookies": [], "html": ""}

    def add(url, score, kind, size=0, tag=""):
        if not url or not url.startswith("http") or is_image_url(url) or is_bad_url(url):
            return
        old = cands.get(url)
        if old and old["score"] >= score:
            return
        cands[url] = {"url": url, "score": score, "kind": kind, "size": size, "tag": tag}
        logging.info(f"[SNIFF:{tag}] {kind} score={score} {url[:120]}")

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
            kind = classify_media(url, ctype, total)
            if not kind or is_bad_url(url):
                return
            if kind == "mp4" and 0 < total < 500 * 1024:
                return
            path = url_path(url)
            score = 0
            if kind == "hls":
                score = 150
                try:
                    if total < 3 * MiB and "#EXT-X-STREAM-INF" in (await resp.text()):
                        score = 300
                except Exception:
                    pass
            elif kind == "dash":
                score = 200
            else:
                score = 100
                if ctype.startswith("video/"):
                    score += 50
                if resp.request.resource_type == "media":
                    score += 30
                score += 120 if total > 50 * MiB else 60 if total > 5 * MiB else 20 if total > MiB else 0
            score += 100 if "1080" in path else 70 if "720" in path else 30 if "480" in path else 0
            add(url, score, kind, total, "NET")
        except Exception as e:
            logging.debug(f"response handler: {e}")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=[
                "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled", "--autoplay-policy=no-user-gesture-required",
            ])
            ctx = await browser.new_context(user_agent=UA, viewport={"width": 1366, "height": 768},
                                            locale="en-US", accept_downloads=True, ignore_https_errors=True)
            await ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
            if cookies:
                try:
                    await ctx.add_cookies(cookies)
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
            ctx.on("page", lambda pg: asyncio.ensure_future(pg.close()))   # popup ads band

            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                logging.warning(f"[browser] goto: {e}")
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

            await click_first(page, CONSENT_SELECTORS)      # age/consent gate
            await page.wait_for_timeout(500)

            try:
                res["title"] = await page.evaluate(
                    "(sel)=>{const e=document.querySelector(sel);return (e&&e.textContent.trim())||document.title}",
                    cfg.get("title_selector", "h1"))
                res["title"] = (res["title"] or "").strip()[:150] or None
            except Exception:
                pass
            try:
                res["thumbs"] = await page.evaluate(
                    """()=>{const r=[];document.querySelectorAll('meta[property="og:image"],meta[name="twitter:image"]')
                    .forEach(m=>m.content&&r.push(m.content));document.querySelectorAll('video[poster]')
                    .forEach(v=>r.push(v.poster));return r}""")
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
                            q = re.search(r"(\d{3,4})p", txt + " " + u)
                            qn = int(q.group(1)) if q else 0
                            score = 250 + (100 if qn >= 1080 else 60 if qn >= 720 else 20 if qn >= 480 else 0)
                            add(u, score, classify_media(u) or "mp4", 0, "HREF")
                        except Exception:
                            continue

            await collect_links()

            async def poke():
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

            if not cands:                                    # DOM / inline JS player configs
                await collect_links()
                for fr in page.frames:
                    try:
                        content = await fr.content()
                    except Exception:
                        continue
                    for u in scan_html_for_media(content, fr.url or page.url):
                        k = classify_media(u) or "mp4"
                        add(u, 120, k, 0, "HTML")
                try:
                    dom = await page.evaluate("""()=>{const r=[];document.querySelectorAll('video,video source')
                        .forEach(v=>{if(v.src)r.push(v.src);if(v.currentSrc)r.push(v.currentSrc)});return r}""")
                    for u in dom or []:
                        if u.startswith("http"):
                            add(u, 140, classify_media(u) or "mp4", 0, "DOM")
                except Exception:
                    pass

            if not cands:                                    # last: download button click
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


def gemini_find_url(url, html_text=None):
    if not ai_model:
        return None
    try:
        if not html_text:
            sc = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "desktop": True})
            html_text = sc.get(url, timeout=25).text[:120000]
        prompt = (
            "You are an expert web scraper. Find the MAIN direct video URL (.mp4, .m3u8, .mpd) of this page. "
            "Ignore thumbnails, ads, trailers, previews. Return ONLY the URL, or NOT_FOUND.\n\n"
            f"Page URL: {url}\n\nHTML:\n{html_text}"
        )
        txt = ai_model.generate_content(prompt).text.strip()
        m = re.search(r'https?://[^\s"\'<>]+', txt)
        if m and not is_image_url(m.group(0)):
            return m.group(0)
    except Exception as e:
        logging.error(f"Gemini error: {e}")
    return None


async def resolve_source(url, status):
    """Link -> Source. Order: direct link -> yt-dlp -> browser sniff -> HTML scan -> Gemini."""
    loop = asyncio.get_running_loop()
    cookies = load_cookie_list()
    run = lambda fn, *a: loop.run_in_executor(None, fn, *a)

    # 0) direct media link
    if classify_media(url):
        await status("🔗 Direct link mila, check kar raha hoon…")
        return await source_from_media(url, origin_of(url) + "/", cookies, url_name=True)

    # 1) yt-dlp (1000+ sites, fast)
    generic = None
    await status("🔎 Analyzing… (yt-dlp)")
    try:
        hdr = make_headers(url)
        info = await run(ytdlp_extract, url, hdr)
        if info and (info.get("formats") or info.get("url")):
            s = source_from_ytinfo(url, info, hdr, cookies)
            if (info.get("extractor_key") or "").lower() != "generic" or s.duration >= 90:
                return s
            generic = s
    except Exception as e:
        logging.info(f"[yt-dlp] {str(e)[:150]}")

    # 2) browser sniff
    await status("🌐 Browser se video dhoondh raha hoon…")
    sn = await sniff_page(url, cookies)
    cookies2 = sn["cookies"] or cookies
    thumbs = [urljoin(url, t) for t in sn["thumbs"] if t]
    cands = sorted(sn["cands"], key=lambda c: -c["score"])[:5]

    if not cands:
        # 3) raw HTML scan
        try:
            sc = make_session(make_headers(url), cookies)
            txt = (await run(lambda: sc.get(url, timeout=25).text))[:600000]
            for u in scan_html_for_media(txt, url)[:5]:
                cands.append({"url": u, "score": 100, "kind": classify_media(u) or "mp4", "size": 0, "tag": "RAW"})
        except Exception as e:
            logging.info(f"[html-scan] {e}")

    if cands:
        await status("🧪 Candidates check kar raha hoon…")
        hdr = make_headers(url)

        async def probe(c):
            h = dict(hdr)
            ch = cookie_header(c["url"], cookies2)
            if ch:
                h["Cookie"] = ch
            c["info"] = await run(ffprobe, c["url"], h, 25)
            c["dur"] = (c["info"] or {}).get("duration") or 0
        await asyncio.gather(*[probe(c) for c in cands])
        best = max(cands, key=lambda c: (c["dur"] >= 30, c["score"], c["dur"]))
        s = await source_from_media(best["url"], url, cookies2, title=sn["title"], thumbs=thumbs, pre=best.get("info"))
        if generic and generic.duration >= s.duration:
            return generic
        return s
    if generic:
        generic.thumbs += thumbs
        return generic

    # 4) Gemini
    if ai_model:
        await status("🤖 Gemini se link nikal raha hoon…")
        u = await run(gemini_find_url, url, sn.get("html"))
        if u:
            return await source_from_media(u, url, cookies2, title=sn["title"], thumbs=thumbs)
    raise RuntimeError("Video extract nahi ho paaya")


# ==========================================
# 8. DOWNLOAD ENGINES
# ==========================================
def _largest_media_file(workdir):
    best, size = None, 0
    for f in os.listdir(workdir):
        p = os.path.join(workdir, f)
        if f.endswith((".part", ".ytdl", ".txt", ".ts", ".jpg")) or not os.path.isfile(p):
            continue
        if os.path.getsize(p) > size:
            best, size = p, os.path.getsize(p)
    if not best or size < 100 * 1024:
        raise RuntimeError("Download ke baad file nahi mili")
    return best


def ytdlp_download(url, workdir, headers, max_h, prog):
    def hook(d):
        if d.get("status") == "downloading":
            prog.update("⬇️ Downloading", d.get("downloaded_bytes") or 0,
                        d.get("total_bytes") or d.get("total_bytes_estimate") or 0)

    opts = base_ydl_opts(headers)
    opts.update({
        "outtmpl": os.path.join(workdir, "dl.%(ext)s"),
        "format": "bv*+ba/b",
        "format_sort": [f"res:{max_h}", "vcodec:h264", "acodec:aac", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 8,
        "progress_hooks": [hook],
        "noprogress": True,
    })
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    return _largest_media_file(workdir)


def ffmpeg_hls_download(url, out, headers, duration, prog):
    run_ffmpeg(["-allowed_extensions", "ALL", "-headers", _hdr_str(headers), "-i", url, "-c", "copy",
                "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", out],
               duration, lambda c, t: prog.update("⬇️ Downloading (ffmpeg)", c, t, "time"))
    return out


def download_direct(url, out_path, headers, cookies, prog, workers=8):
    """Multi-connection direct download (Range support ho to 8x tez), warna single stream."""
    s = make_session(headers, cookies)
    label = "⬇️ Downloading"

    def first_request():
        try:
            r = s.get(url, headers={"Range": "bytes=0-"}, stream=True, timeout=(15, 60), allow_redirects=True)
            if r.status_code >= 400:
                raise IOError(f"HTTP {r.status_code}")
            return r
        except Exception:
            return s.get(url, stream=True, timeout=(15, 60), allow_redirects=True)   # bina Range ke retry

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
    if done < 300 * 1024:
        os.remove(out_path)
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


def choose_tier(src: Source) -> int:
    """Estimate 2GB se upar ho to chup-chap 720p."""
    if src.mode == "ytdlp" and src.est:
        size, _ = src.est.get(MAX_HEIGHT, (0, 0))
        if size and size > UPLOAD_LIMIT:
            return FALLBACK_HEIGHT
    return MAX_HEIGHT


def download_media(src: Source, workdir, prog):
    tier = choose_tier(src)
    errs = []
    if src.mode == "ytdlp":
        try:
            return ytdlp_download(src.dl_url, workdir, src.headers, tier, prog)
        except Exception as e:
            errs.append(f"yt-dlp: {str(e)[:150]}")
        if classify_media(src.dl_url) == "hls":
            try:
                return ffmpeg_hls_download(src.dl_url, os.path.join(workdir, "hls.mp4"),
                                           src.headers, src.duration, prog)
            except Exception as e:
                errs.append(f"ffmpeg: {str(e)[:150]}")
    else:
        try:
            return download_direct(src.dl_url, os.path.join(workdir, "dl.mp4"), src.headers, src.cookies, prog)
        except Exception as e:
            errs.append(f"direct: {str(e)[:150]}")
        try:
            return ytdlp_download(src.dl_url, workdir, src.headers, tier, prog)
        except Exception as e:
            errs.append(f"yt-dlp: {str(e)[:150]}")
    raise RuntimeError(" | ".join(errs) or "download fail")


def fetch_thumbnail(job: Job):
    """HQ thumbnail: page/yt-dlp thumbnail -> saved frame -> source se frame."""
    if job.thumb_path and os.path.exists(job.thumb_path):
        return job.thumb_path
    s = job.src
    sess = make_session(make_headers(s.page_url), s.cookies)
    for u in s.thumbs[:8]:
        try:
            r = sess.get(u, timeout=20, headers={"Accept": "image/*,*/*"})
            ct = (r.headers.get("content-type") or "").lower()
            if r.status_code != 200 or not ct.startswith("image/") or len(r.content) < 6 * 1024:
                continue
            ext = ".png" if "png" in ct else ".jpg" if ("jpeg" in ct or "jpg" in ct) else ".img"
            raw = os.path.join(THUMB_DIR, f"{job.id}_src{ext}")
            with open(raw, "wb") as f:
                f.write(r.content)
            if ext == ".img":                       # webp/avif -> jpg
                out = os.path.join(THUMB_DIR, f"{job.id}_src.jpg")
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-q:v", "2", out],
                               capture_output=True, timeout=60)
                os.remove(raw)
                if not os.path.exists(out):
                    continue
                raw = out
            job.thumb_path = raw
            return raw
        except Exception:
            continue
    hq = os.path.join(THUMB_DIR, f"{job.id}_hq.jpg")
    if os.path.exists(hq):
        job.thumb_path = hq
        return hq
    try:
        h = dict(s.headers)
        if grab_frame(s.dl_url, hq, at=max(1, (s.duration or 30) * 0.1), headers=h):
            job.thumb_path = hq
            return hq
    except Exception:
        pass
    return None


# ==========================================
# 9. UI (cards, cut panel)
# ==========================================
MODE_LABEL = {
    "auto": "🤖 Auto (chhota=Accurate, lamba=Fast)",
    "fast": "⚡ Fast (keyframe, instant)",
    "accurate": "🎯 Accurate (frame-exact)",
}


def quality_label(src: Source) -> str:
    h = 0
    if src.mode == "ytdlp" and src.est:
        h = src.est.get(choose_tier(src), (0, 0))[1]
    return f"{(h or src.height)}p" if (h or src.height) else "Best available"


def card_text(job: Job) -> str:
    s = job.src
    lines = [f"🎬 <b>{esc(s.title[:120])}</b>", ""]
    lines.append(f"⏱ Length: <code>{fmt_dur(s.duration) if s.duration else 'unknown'}</code>")
    lines.append(f"📺 Quality: <code>{quality_label(s)}</code>")
    sz = (s.est.get(choose_tier(s), (0, 0))[0] if s.est else 0) or s.size
    if sz:
        lines.append(f"📦 Size: <code>~{humanbytes(sz)}</code>")
    lines.append("\n👇 <b>Kya karna hai?</b>")
    return "\n".join(lines)


def card_kb(job: Job) -> InlineKeyboardMarkup:
    d = fmt_dur(job.src.duration) if job.src.duration else "?"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⬇️ Download  •  ⏱ {d}", callback_data=f"dl:{job.id}")],
        [InlineKeyboardButton(f"✂️ Cut + Download  •  ⏱ {d}", callback_data=f"cut:{job.id}")],
    ])


def panel_text(job: Job) -> str:
    s = job.src
    mark = lambda f, icon: "👉" if job.sel == f else icon
    return (
        f"✂️ <b>Cut Video</b>\n🎬 <code>{esc(s.title[:80])}</code>\n"
        f"⏱ Total: <code>{fmt_hms(s.duration) if s.duration else 'unknown'}</code>\n\n"
        f"{mark('s', '▶️')} Start: <code>{fmt_hms(job.cs)}</code>\n"
        f"{mark('e', '⏹')} End:   <code>{fmt_hms(job.ce)}</code>\n"
        f"📏 Clip: <code>{fmt_hms(job.ce - job.cs)}</code>\n"
        f"⚙️ Mode: {MODE_LABEL[job.mode]}\n\n"
        f"💡 Ya seedha type karo: <code>1:30-5:45</code>"
    )


def panel_kb(job: Job) -> InlineKeyboardMarkup:
    i = job.id
    b = lambda t, d: InlineKeyboardButton(t, callback_data=d)
    return InlineKeyboardMarkup([
        [b(("✅ " if job.sel == "s" else "") + "▶️ Start", f"cs:{i}:s"),
         b(("✅ " if job.sel == "e" else "") + "⏹ End", f"cs:{i}:e")],
        [b("−10m", f"ca:{i}:-600"), b("−1m", f"ca:{i}:-60"), b("−10s", f"ca:{i}:-10"), b("−1s", f"ca:{i}:-1")],
        [b("+1s", f"ca:{i}:1"), b("+10s", f"ca:{i}:10"), b("+1m", f"ca:{i}:60"), b("+10m", f"ca:{i}:600")],
        [b("🔄 Reset", f"cr:{i}"), b("⚙️ Mode", f"cm:{i}")],
        [b("✅ Cut + Download", f"cg:{i}")],
        [b("⬅️ Back", f"cx:{i}")],
    ])


def adjust(job: Job, delta: float):
    D = job.src.duration
    hi = D if D else 10 ** 7
    if job.sel == "s":
        job.cs = max(0.0, min(job.cs + delta, job.ce - 1))
    else:
        job.ce = max(job.cs + 1, min(job.ce + delta, hi))


def thumb_kb(job: Job, seconds) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        f"🖼 Thumbnail  •  ⏱ {fmt_dur(seconds)}", callback_data=f"th:{job.id}")]])


# ==========================================
# 10. PIPELINE
# ==========================================
def stage_a(job: Job, cut, workdir, prog: Progress):
    """Download -> compat -> HQ frame -> (cut + fit). Thread mein chalta hai."""
    src = job.src
    path = download_media(src, workdir, prog)
    path = ensure_compatible(path, lambda c, t: prog.update("🔧 Converting", c, t, "time"))
    info = ffprobe(path) or {}
    D = info.get("duration") or src.duration or 0
    try:                                           # thumbnail button ke liye HQ frame save
        grab_frame(path, os.path.join(THUMB_DIR, f"{job.id}_hq.jpg"), at=max(1, D * 0.1))
    except Exception:
        pass
    out = {"full": path, "info": info, "dur": D, "cut": None}
    if cut:
        start, end, mode = cut
        end = min(end, D) if D else end
        if end - start < 0.5:
            raise RuntimeError("Cut range invalid (video se bahar)")
        raw = os.path.join(workdir, "cut_raw.mp4")
        used = cut_video(path, raw, start, end, mode,
                         lambda c, t: prog.update("✂️ Cutting", c, t, "time"))
        fitted, method = fit_to_limit(raw, os.path.join(workdir, "cut_fit.mp4"), prog)
        out["cut"] = {"path": fitted, "start": start, "end": end, "mode": used, "method": method,
                      "info": ffprobe(fitted) or {}}
    return out


def fit_to_limit(path, out, prog):
    if os.path.getsize(path) <= UPLOAD_LIMIT:
        return path, "none"
    return out, compress_to_fit(path, out, COMPRESS_TARGET, prog)


def make_caption(name, info, size, extra=""):
    q = f"{info.get('short')}p" if info.get("short") else "?"
    return (f"✅ <b>Downloaded!</b>\n\n🎬 <b>Name:</b> <code>{esc(name)}</code>\n"
            f"📺 <b>Quality:</b> <code>{q}</code>\n⏱ <b>Length:</b> <code>{fmt_dur(info.get('duration'))}</code>\n"
            f"📦 <b>Size:</b> <code>{humanbytes(size)}</code>{extra}")[:1000]


async def send_one(client, chat_id, path, fname, caption, info, kb, prog, label, workdir):
    thumb = os.path.join(workdir, f"{label}_thumb.jpg")
    loop = asyncio.get_running_loop()
    ok = await loop.run_in_executor(
        None, lambda: grab_frame(path, thumb, at=max(1, (info.get("duration") or 10) * 0.1), width=320, quality=5))
    await client.send_video(
        chat_id=chat_id, video=path, caption=caption, file_name=fname, supports_streaming=True,
        duration=int(info.get("duration") or 0), width=int(info.get("width") or 0),
        height=int(info.get("height") or 0), thumb=thumb if ok else None, reply_markup=kb,
        progress=_up_progress, progress_args=(prog, f"📤 Uploading ({label})"),
    )


async def _up_progress(current, total, prog, label):
    prog.update(label, current, total)


async def run_delivery(client, job: Job, cut):
    global active_downloads
    loop = asyncio.get_running_loop()
    job.busy = True
    ACTIVE_CUT.pop(job.chat_id, None)
    msg = job.msg
    header = f"🎬 <b>{esc(job.src.title[:70])}</b>"
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
                res = await loop.run_in_executor(None, stage_a, job, cut, workdir, prog)
                base = sanitize_filename(
                    (name_from_url(job.src.dl_url) if job.src.use_url_name else job.src.title) or "video")
                dur = res["dur"]

                if res["cut"]:                                    # 1) cut video pehle (jaldi milti hai)
                    c = res["cut"]
                    csize = os.path.getsize(c["path"])
                    cdur = c["info"].get("duration") or (c["end"] - c["start"])
                    extra = (f"\n✂️ <b>Cut:</b> <code>{fmt_hms(c['start'])} → {fmt_hms(c['end'])}</code>"
                             f" ({'frame-exact' if c['mode'] == 'accurate' else 'keyframe'})")
                    if c["method"] != "none":
                        extra += "\n🗜 2GB limit ke liye compress hui"
                    fname = f"{base}_cut_{fmt_hms(c['start']).replace(':', '-')}_{fmt_hms(c['end']).replace(':', '-')}.mp4"
                    await send_one(client, job.chat_id, c["path"], fname,
                                   make_caption(base, c["info"], csize, extra), c["info"],
                                   thumb_kb(job, cdur), prog, "cut", workdir)

                # 2) full video (zaroorat par hi compress)
                full = res["full"]
                method = "none"
                if os.path.getsize(full) > UPLOAD_LIMIT:
                    full, method = await loop.run_in_executor(
                        None, fit_to_limit, full, os.path.join(workdir, "full_fit.mp4"), prog)
                finfo = (await loop.run_in_executor(None, ffprobe, full)) or res["info"]
                extra = ""
                if method == "partial":
                    extra = "\n🗜 2GB limit: sirf beech ka hissa compress hua (shuru/end original quality)"
                elif method == "full":
                    extra = "\n🗜 2GB limit ke liye compress hui"
                await send_one(client, job.chat_id, full, f"{base}.mp4",
                               make_caption(base, finfo, os.path.getsize(full), extra), finfo,
                               thumb_kb(job, finfo.get("duration") or dur), prog, "full", workdir)
                try:
                    await msg.delete()
                except Exception:
                    pass
            finally:
                active_downloads -= 1
    except Exception as e:
        logging.exception("delivery failed")
        try:
            await msg.edit_text(f"❌ <b>Error:</b>\n<code>{esc(str(e)[:350])}</code>\n\n"
                                f"Dobara try karne ke liye link phir bhejo.")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        job.busy = False


async def analyze_url(client, status_msg: Message, chat_id: int, url: str):
    global analyzing
    short = esc(url[:60])

    async def status(text):
        try:
            await status_msg.edit_text(f"{text}\n<code>{short}</code>")
        except Exception:
            pass

    async with ANALYZE_SEM:
        analyzing += 1
        try:
            src = await asyncio.wait_for(resolve_source(url, status), timeout=240)
            job = Job(id=secrets.token_hex(4), chat_id=chat_id, src=src, msg=status_msg)
            job.ce = src.duration or 600.0
            JOBS[job.id] = job
            # buttons ab aate hain — analysis (length etc.) complete hone ke BAAD
            await status_msg.edit_text(card_text(job), reply_markup=card_kb(job))
        except Exception as e:
            logging.error(f"[analyze] {url}: {e}")
            await status_msg.edit_text(
                "❌ <b>Video extract nahi ho paaya.</b>\n\n"
                "💡 Try karein:\n• Expired token link ki jagah <b>webpage URL</b> bhejein\n"
                "• Login wali site ho to <code>cookies.txt</code> add karein\n"
                f"\n<code>{esc(str(e)[:150])}</code>")
        finally:
            analyzing -= 1


# ==========================================
# 11. TELEGRAM HANDLERS
# ==========================================
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(_, message: Message):
    await message.reply_text(
        "👋 <b>Universal Video Downloader</b>\n\n"
        "🔗 Link bhejo (ek message mein kai links bhi chalenge)\n"
        "🧠 Analyze ke baad buttons aate hain: <b>Download</b> / <b>Cut + Download</b>\n"
        "✂️ Cut mein Start/End buttons se set karo — chat mein <b>cut + full</b> dono videos aati hain\n"
        "📺 Best quality (1080p/720p). 2GB se badi ho tabhi compress\n"
        "🖼 Har video ke saath <b>Thumbnail</b> button (HQ thumbnail chahiye to dabao)\n\n"
        "⚡ Engines: yt-dlp → Browser → HTML scan → Gemini AI"
    )


@app.on_message(filters.command("help") & filters.private)
async def help_cmd(_, message: Message):
    await message.reply_text(
        "<b>📖 Kaise use karein</b>\n\n"
        "1️⃣ Video page ka URL bhejo\n"
        "2️⃣ Bot analyze karke length ke saath buttons dega\n"
        "3️⃣ <b>⬇️ Download</b> = sirf full video\n"
        "4️⃣ <b>✂️ Cut</b> = Start/End set karo (ya type: <code>1:30-5:45</code>), phir ✅\n"
        "5️⃣ Video ke neeche <b>🖼 Thumbnail</b> dabao to HQ thumbnail milega\n\n"
        "<b>Cut modes:</b> ⚡ Fast (keyframe pe, instant) • 🎯 Accurate (frame-exact) • 🤖 Auto\n"
        "<b>Login wali sites:</b> browser se <code>cookies.txt</code> (Netscape format) bot folder mein rakho"
    )


@app.on_message(filters.command("queue") & filters.private)
async def queue_cmd(_, message: Message):
    await message.reply_text(
        f"📊 <b>Status</b>\n• Analyzing: <code>{analyzing}</code>\n• Active downloads: <code>{active_downloads}</code>"
    )


@app.on_message(filters.private & filters.regex(r"https?://"))
async def url_handler(client, message: Message):
    text = message.text or message.caption or ""
    urls = list(dict.fromkeys(u.rstrip(").,;]") for u in URL_REGEX.findall(text)))[:10]
    if not urls:
        return
    for u in urls:
        st = await message.reply_text(f"🔍 <b>Analyzing…</b>\n<code>{esc(u[:60])}</code>")
        spawn(analyze_url(client, st, message.chat.id, u))


def _range_filter(_, __, m):
    t = getattr(m, "text", None)
    return bool(t) and m.chat.id in ACTIVE_CUT and not URL_REGEX.search(t) and RANGE_RE.match(t) is not None


@app.on_message(filters.private & filters.create(_range_filter))
async def range_text_handler(client, message: Message):
    job = JOBS.get(ACTIVE_CUT.get(message.chat.id))
    if not job or job.busy:
        return
    m = RANGE_RE.match(message.text)
    a, b = parse_time(m.group(1)), parse_time(m.group(2))
    D = job.src.duration
    if a is None or b is None or b <= a or (D and a >= D):
        return await message.reply_text("⚠️ Range galat hai. Example: <code>1:30-5:45</code>")
    job.cs, job.ce = a, (min(b, D) if D else b)
    try:
        await job.msg.edit_text(panel_text(job), reply_markup=panel_kb(job))
    except Exception:
        pass
    await message.reply_text(f"✅ Range set: <code>{fmt_hms(job.cs)} → {fmt_hms(job.ce)}</code>")


@app.on_callback_query()
async def on_callback(client, cq: CallbackQuery):
    parts = (cq.data or "").split(":")
    act = parts[0]
    job = JOBS.get(parts[1]) if len(parts) > 1 else None
    if not job:
        return await cq.answer("⌛ Ye request purani ho gayi — link dobara bhejo.", show_alert=True)

    if act == "th":                                           # thumbnail
        await cq.answer("🖼 Thumbnail la raha hoon…")
        loop = asyncio.get_running_loop()
        path = await loop.run_in_executor(None, fetch_thumbnail, job)
        if not path:
            return await client.send_message(cq.message.chat.id, "❌ Is video ka thumbnail nahi mil paaya.")
        cap = f"🖼 <b>{esc(job.src.title[:100])}</b>"
        try:
            await client.send_photo(cq.message.chat.id, path, caption=cap)
        except Exception as e:
            logging.info(f"photo send fail: {e}")
        try:
            await client.send_document(cq.message.chat.id, path, caption="📎 Original quality file",
                                       force_document=True)
        except Exception as e:
            logging.info(f"doc send fail: {e}")
        return

    if job.busy:
        return await cq.answer("⏳ Ye already process ho raha hai", show_alert=False)

    async def refresh():
        try:
            await cq.message.edit_text(panel_text(job), reply_markup=panel_kb(job))
        except Exception:
            pass

    if act == "dl":
        await cq.answer("⬇️ Shuru ho gaya…")
        spawn(run_delivery(client, job, None))
    elif act == "cut":
        job.cs, job.ce, job.sel = 0.0, (job.src.duration or 600.0), "s"
        ACTIVE_CUT[job.chat_id] = job.id
        await cq.answer()
        await refresh()
    elif act == "cs":
        job.sel = parts[2]
        await cq.answer("▶️ Start edit" if job.sel == "s" else "⏹ End edit")
        await refresh()
    elif act == "ca":
        adjust(job, float(parts[2]))
        await cq.answer()
        await refresh()
    elif act == "cm":
        order = ["auto", "fast", "accurate"]
        job.mode = order[(order.index(job.mode) + 1) % 3]
        await cq.answer(MODE_LABEL[job.mode])
        await refresh()
    elif act == "cr":
        job.cs, job.ce = 0.0, (job.src.duration or 600.0)
        await cq.answer("Reset")
        await refresh()
    elif act == "cx":
        ACTIVE_CUT.pop(job.chat_id, None)
        await cq.answer()
        await cq.message.edit_text(card_text(job), reply_markup=card_kb(job))
    elif act == "cg":
        if job.ce - job.cs < 1:
            return await cq.answer("⚠️ Clip kam se kam 1 second ki honi chahiye", show_alert=True)
        await cq.answer("✂️ Shuru ho gaya…")
        spawn(run_delivery(client, job, (job.cs, job.ce, job.mode)))


# ==========================================
# 12. HOUSEKEEPING + MAIN
# ==========================================
async def janitor():
    while True:
        await asyncio.sleep(600)
        now = time.time()
        for jid, job in list(JOBS.items()):
            if now - job.created > 6 * 3600 and not job.busy:
                JOBS.pop(jid, None)
                for f in os.listdir(THUMB_DIR):
                    if f.startswith(jid + "_"):
                        try:
                            os.remove(os.path.join(THUMB_DIR, f))
                        except OSError:
                            pass
        for d in os.listdir(DOWNLOAD_DIR):
            p = os.path.join(DOWNLOAD_DIR, d)
            if os.path.isdir(p) and now - os.path.getmtime(p) > 4 * 3600:
                shutil.rmtree(p, ignore_errors=True)


async def main():
    global ANALYZE_SEM, DL_SEM
    ANALYZE_SEM = asyncio.Semaphore(MAX_CONCURRENT)
    DL_SEM = asyncio.Semaphore(MAX_CONCURRENT)
    for d in os.listdir(DOWNLOAD_DIR):                       # purane leftover saaf
        shutil.rmtree(os.path.join(DOWNLOAD_DIR, d), ignore_errors=True)
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        logging.error("❌ ffmpeg/ffprobe install nahi hai! (apt install ffmpeg)")
    await app.start()
    logging.info("🚀 Bot started")
    spawn(janitor())
    try:
        await idle()
    finally:
        await app.stop()


if __name__ == "__main__":
    app.run(main())
