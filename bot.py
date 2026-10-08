import os
import re
import time
import asyncio
import logging
import http.cookiejar
from urllib.parse import urlparse
import yt_dlp
import cloudscraper
from pyrogram import Client, filters
from pyrogram.types import Message
from playwright.async_api import async_playwright
import google.generativeai as genai

# ==========================================
# 1. ENVIRONMENT VARIABLES
# ==========================================
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

app = Client("video_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        ai_model = genai.GenerativeModel('gemini-1.5-flash')
        logging.info("✅ Gemini AI initialized")
    except Exception as e:
        logging.error(f"Gemini init failed: {e}")
        ai_model = None
else:
    logging.warning("⚠️ GEMINI_API_KEY missing! AI fallback disabled.")
    ai_model = None

DOWNLOAD_DIR = "./downloads/"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Extensions / keywords
IMAGE_EXTS = ['.jpg', '.jpeg', '.png', '.webp', '.gif', '.svg', '.ico', '.bmp']
VIDEO_EXTS = ['.mp4', '.m3u8', '.webm', '.mkv', '.ts', '.mov', '.avi']
BAD_KEYWORDS = ['preview', 'thumb', 'poster', 'sprite', 'trailer', 'placeholder', 'banner']

# ==========================================
# SITE-SPECIFIC CONFIG (Permanent for rule34video.com)
# ==========================================
SITE_SELECTORS = {
    "rule34video.com": {
        "download_selectors": [
            'a[href*="/get_file/"]',
            'a[href*="download"]',
            'a[href*=".mp4"]',
            'a:has-text("1080p")',
            'a:has-text("720p")',
            'a:has-text("480p")',
            'a:has-text("Download")',
            'a[download]',
            '.download-button',
            '.btn-download',
            '#download',
            '#btn-download',
        ],
        "title_selector": "h1, .video-title, .title",
    },
    # Baaki sites ke liye universal fallback
    "default": {
        "download_selectors": [
            'a[href*="download"]',
            'a[href*=".mp4"]',
            'a[href*=".m3u8"]',
            'a:has-text("Download")',
            'a:has-text("Download Video")',
            'a[download]',
            '.download-button',
            '.btn-download',
            '#download',
        ],
        "title_selector": "h1, title",
    }
}


def get_site_config(url: str) -> dict:
    """URL ke domain ke hisaab se config return karta hai."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower().replace("www.", "")
    for site, config in SITE_SELECTORS.items():
        if site in domain:
            return config
    return SITE_SELECTORS["default"]


# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================
def humanbytes(size):
    if not size:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


def is_image_url(url: str) -> bool:
    path = url.lower().split("?")[0]
    return any(ext in path for ext in IMAGE_EXTS)


def is_video_url(url: str) -> bool:
    path = url.lower().split("?")[0]
    return (
        any(ext in path for ext in VIDEO_EXTS)
        or "/get_file/" in path
        or "/get_video/" in path
        or "videoplayback" in path
        or "/download/" in path
    )


def has_bad_keyword(url: str) -> bool:
    path = url.lower()
    return any(bad in path for bad in BAD_KEYWORDS)


def make_absolute(url: str, base: str) -> str:
    if not url:
        return url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("//"):
        return "https:" + url
    if not base:
        return url
    if url.startswith("/"):
        return base.rstrip("/") + url
    return base.rstrip("/") + "/" + url


def extract_quality_from_url(url: str) -> str:
    """URL se quality detect karta hai."""
    url_lower = url.lower()
    for q in ["2160p", "1440p", "1080p", "720p", "480p", "360p", "240p"]:
        if q in url_lower:
            return q
    return "Unknown"


def sanitize_filename(name: str) -> str:
    """Filename se invalid characters hatata hai."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    if len(name) > 150:
        name = name[:150]
    return name


def parse_filename_from_url(video_url: str) -> str:
    """Video URL se original filename nikalta hai."""
    try:
        parsed = urlparse(video_url)
        path = parsed.path.rstrip("/")
        basename = os.path.basename(path)
        # Agar filename mein .mp4 hai to use karo
        if basename and ('.' in basename):
            return sanitize_filename(basename)
        # Warna URL ke aakhri hisse se banao
        if basename:
            return sanitize_filename(basename)
    except Exception:
        pass
    return None


async def progress_status(current, total, status_msg, action_text, start_time):
    now = time.time()
    diff = now - start_time
    if diff == 0:
        return

    if not hasattr(progress_status, "last_update"):
        progress_status.last_update = 0

    if now - progress_status.last_update > 3 or current == total:
        progress_status.last_update = now
        percentage = (current / total) * 100 if total else 0
        speed = current / diff
        text = (
            f"⏳ **{action_text}...**\n"
            f"Progress: `{percentage:.1f}%`\n"
            f"Size: `{humanbytes(current)} / {humanbytes(total)}`\n"
            f"Speed: `{humanbytes(speed)}/s`"
        )
        try:
            await status_msg.edit_text(text)
        except Exception:
            pass


# ==========================================
# 3. PLAYWRIGHT - UNIVERSAL VIDEO URL EXTRACTION
# ==========================================
async def extract_video_url_via_browser(page_url: str) -> dict:
    """
    Returns: {
        "url": str,
        "title": str,
        "quality": str
    }
    Site-specific selectors use karta hai (rule34video.com permanent).
    """
    captured_urls = []   # (score, url)
    seen_urls = set()
    base_url = None
    page_title = None
    site_config = get_site_config(page_url)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                ]
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
                accept_downloads=True,
            )
            page = await context.new_page()

            # ---------- NETWORK MONITOR ----------
            async def handle_response(response):
                try:
                    url = response.url
                    if url in seen_urls:
                        return
                    seen_urls.add(url)

                    req = response.request
                    rtype = req.resource_type
                    headers = response.headers
                    ctype = headers.get("content-type", "").lower()
                    try:
                        clen = int(headers.get("content-length", 0) or 0)
                    except Exception:
                        clen = 0

                    if ctype.startswith("image/"):
                        return
                    if is_image_url(url) or has_bad_keyword(url):
                        return
                    if 0 < clen < 500 * 1024:
                        return

                    path = url.lower().split("?")[0]
                    is_vid = (
                        ctype.startswith("video/")
                        or ctype in ("application/octet-stream", "application/x-mpegurl", "application/vnd.apple.mpegurl")
                        or is_video_url(url)
                    )
                    if not is_vid:
                        return

                    score = 0
                    if '1080' in path: score += 100
                    elif '720' in path: score += 80
                    elif '480' in path: score += 60
                    elif '360' in path: score += 40
                    if rtype == "media": score += 30
                    if ctype.startswith("video/"): score += 50
                    if clen > 5 * 1024 * 1024: score += 60
                    elif clen > 1 * 1024 * 1024: score += 30
                    if '/get_file/' in path: score += 20

                    captured_urls.append((score, url))
                    logging.info(f"[NET] 🎬 candidate (score={score}, size={clen}): {url[:140]}")
                except Exception as e:
                    logging.debug(f"Response handler err: {e}")

            page.on("response", handle_response)

            logging.info(f"[Playwright] Opening: {page_url}")
            try:
                await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                logging.warning(f"[Playwright] goto warning: {e}")

            try:
                base_url = await page.evaluate("() => location.origin")
                logging.info(f"[Playwright] Base origin: {base_url}")
            except Exception:
                parsed = urlparse(page_url)
                base_url = f"{parsed.scheme}://{parsed.netloc}"

            await page.wait_for_timeout(3000)

            # ---------- TITLE EXTRACT ----------
            try:
                title_sel = site_config.get("title_selector", "h1")
                page_title = await page.evaluate(f"""
                    () => {{
                        const el = document.querySelector('{title_sel}');
                        return el ? el.textContent.trim() : document.title;
                    }}
                """)
                logging.info(f"[Playwright] Title: {page_title[:80]}")
            except Exception:
                page_title = None

            # ---------- HREF EXTRACTION (SITE-SPECIFIC) ----------
            download_selectors = site_config.get("download_selectors", [])
            # Universal selectors bhi add karo
            download_selectors += [
                'a[href*="1080"]', 'a[href*="720"]', 'a[href*="get_file"]',
                'a[href*=".mp4"]', 'a[href*="download"]',
                'a:has-text("1080p")', 'a:has-text("720p")',
                'a:has-text("MP4")', 'a:has-text("Download")',
                'a[download]', '.download-button', '.btn-download',
                '#download', '#btn-download',
            ]
            # Deduplicate selectors
            download_selectors = list(dict.fromkeys(download_selectors))

            for selector in download_selectors:
                try:
                    elements = await page.query_selector_all(selector)
                    for el in elements:
                        try:
                            href = await el.get_attribute("href")
                            if not href:
                                continue

                            abs_url = make_absolute(href, base_url)
                            if is_image_url(abs_url) or has_bad_keyword(abs_url):
                                continue
                            if not (is_video_url(abs_url) or 'get_file' in abs_url.lower()):
                                continue
                            if abs_url in seen_urls:
                                continue
                            seen_urls.add(abs_url)

                            score = 250
                            if '1080' in abs_url: score += 100
                            elif '720' in abs_url: score += 50
                            elif '480' in abs_url: score += 20
                            captured_urls.append((score, abs_url))
                            logging.info(f"[HREF] 🎯 score={score} {abs_url[:130]}")
                        except Exception:
                            continue
                except Exception:
                    continue

            # ✅ 1080p mil gaya? Turant return
            best_1080 = [u for s, u in captured_urls if '1080' in u.lower()]
            if best_1080:
                logging.info("[Playwright] ✅ 1080p HREF mil gaya, browser band kar raha hoon")
                await browser.close()
                return {
                    "url": best_1080[0],
                    "title": page_title,
                    "quality": extract_quality_from_url(best_1080[0])
                }

            # ---------- CLICK ONLY IF NO HREF ----------
            if not captured_urls:
                logging.info("[Playwright] HREF nahi mila, click try kar raha hoon...")
                click_selectors = [
                    'a:has-text("1080p")', 'a:has-text("Download")',
                    'button:has-text("Download")', 'a[download]', '.download-button',
                ]
                clicked = False
                for selector in click_selectors:
                    if clicked:
                        break
                    try:
                        el = await page.query_selector(selector)
                        if not el:
                            continue
                        try:
                            async with page.expect_download(timeout=3000) as dl_info:
                                await el.click()
                            dl = await dl_info.value
                            if dl.url and not is_image_url(dl.url):
                                captured_urls.append((500, dl.url))
                                logging.info(f"[DL-EVENT] 🎯 {dl.url}")
                                clicked = True
                        except Exception:
                            try:
                                await el.click()
                            except Exception:
                                pass
                            await page.wait_for_timeout(2000)
                            if captured_urls:
                                clicked = True
                    except Exception:
                        continue

            # ---------- DOM FALLBACK ----------
            if not captured_urls:
                try:
                    dom_urls = await page.evaluate("""
                        () => {
                            const r = [];
                            document.querySelectorAll('video').forEach(v => {
                                if (v.src) r.push(v.src);
                                if (v.currentSrc) r.push(v.currentSrc);
                            });
                            document.querySelectorAll('video source').forEach(s => {
                                if (s.src) r.push(s.src);
                            });
                            document.querySelectorAll('a').forEach(a => {
                                const h = a.href || '';
                                if (h.match(/\\.(mp4|m3u8|webm|mkv)/i) || h.includes('get_file')) r.push(h);
                            });
                            return [...new Set(r)];
                        }
                    """)
                    for vs in (dom_urls or []):
                        if not vs:
                            continue
                        abs_u = make_absolute(vs, base_url)
                        if is_image_url(abs_u) or has_bad_keyword(abs_u):
                            continue
                        if abs_u in seen_urls:
                            continue
                        seen_urls.add(abs_u)
                        if is_video_url(abs_u):
                            score = 150 if '1080' in abs_u else 100
                            captured_urls.append((score, abs_u))
                            logging.info(f"[DOM] 🎯 {abs_u[:130]}")
                except Exception as e:
                    logging.debug(f"DOM eval err: {e}")

            await browser.close()

    except Exception as e:
        logging.error(f"[Playwright] Error: {e}")

    # ---------- BEST URL SELECT ----------
    if not captured_urls:
        logging.warning("[Playwright] ❌ Koi valid video URL nahi mila")
        return None

    captured_urls.sort(reverse=True, key=lambda x: x[0])
    best = captured_urls[0][1]

    if base_url and not best.startswith("http"):
        best = make_absolute(best, base_url)

    if is_image_url(best):
        logging.error(f"[Playwright] ❌ Best URL image hai: {best}")
        return None

    logging.info(f"[Playwright] ✅ FINAL URL: {best}")
    return {
        "url": best,
        "title": page_title,
        "quality": extract_quality_from_url(best)
    }


# ==========================================
# 4. DOWNLOAD ENGINES
# ==========================================
def download_direct(url, output_path):
    """Direct download with Cloudscraper + cookies + image skip."""
    parsed_url = urlparse(url)
    referer = f"{parsed_url.scheme}://{parsed_url.netloc}/"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": referer,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
        "Range": "bytes=0-",
    }

    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
    )

    if os.path.exists('cookies.txt'):
        try:
            cj = http.cookiejar.MozillaCookieJar('cookies.txt')
            cj.load(ignore_discard=True, ignore_expires=True)
            scraper.cookies = cj
            logging.info(f"[Direct] ✅ Loaded {len(cj)} cookies")
        except Exception as e:
            logging.warning(f"[Direct] Cookie load err: {e}")

    response = scraper.get(url, headers=headers, stream=True, timeout=120, allow_redirects=True)
    response.raise_for_status()

    ctype = response.headers.get("content-type", "").lower()
    if ctype.startswith("image/"):
        raise Exception(f"❌ Ye thumbnail/image hai, video nahi! (Content-Type: {ctype})")
    if "text/html" in ctype:
        raise Exception(f"❌ HTML mila, video nahi. Content-Type: {ctype}")

    total = int(response.headers.get("content-length", 0) or 0)
    downloaded = 0

    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)

    if downloaded < 500 * 1024:
        try:
            os.remove(output_path)
        except Exception:
            pass
        raise Exception(f"❌ File sirf {humanbytes(downloaded)} hai — thumbnail lagti hai!")

    logging.info(f"[Direct] ✅ Downloaded {humanbytes(downloaded)}")
    return output_path


def download_ytdlp(url, output_template):
    """yt-dlp with Cloudflare impersonation + ORIGINAL FILENAME preserve."""
    parsed_url = urlparse(url)
    referer = f"{parsed_url.scheme}://{parsed_url.netloc}/"

    ydl_opts = {
        'outtmpl': output_template,  # '%(title)s.%(ext)s' se original title aata hai
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None,
        'http_headers': {
            'User-Agent': (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            'Referer': referer,
        },
        'retries': 3,
        'fragment_retries': 3,
        'restrictfilenames': False,  # Original name preserve
        'windowsfilenames': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        return {
            "path": filename,
            "title": info.get('title', 'Unknown'),
            "quality": f"{info.get('height', '?')}p" if info.get('height') else "Unknown"
        }


def gemini_extract_link(url):
    """Gemini AI HTML parse fallback."""
    if not ai_model:
        return None
    try:
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
        )
        response = scraper.get(url, timeout=25)
        html_content = response.text[:120000]

        prompt = (
            "You are an expert web scraper. Find the direct downloadable video URL "
            "(ending in .mp4, .m3u8, or inside video/iframe/a tags with download keyword). "
            "IGNORE thumbnails (.jpg, .png, .webp). "
            "Return ONLY the direct video URL. If not found, return 'NOT_FOUND'.\n\n"
            f"Page URL: {url}\n\nHTML:\n{html_content}"
        )

        ai_response = ai_model.generate_content(prompt)
        extracted_url = ai_response.text.strip()

        if extracted_url and "http" in extracted_url and extracted_url != "NOT_FOUND":
            if is_image_url(extracted_url):
                return None
            return extracted_url
        return None
    except Exception as e:
        logging.error(f"Gemini AI Error: {e}")
        return None


# ==========================================
# 5. TELEGRAM HANDLERS
# ==========================================
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(_, message: Message):
    await message.reply_text(
        "👋 **Universal AI Video Downloader Bot**\n\n"
        "✅ **Multiple links support!** Ek message mein jitne links chaho bhej do.\n"
        "✅ **Original filename preserve** hota hai\n"
        "✅ **Quality + Size caption** mein dikhta hai\n\n"
        "⚡ **Engines:** Playwright → yt-dlp → Cloudscraper → Gemini AI\n\n"
        "📌 **Special:** rule34video.com ke liye permanent selectors"
    )


@app.on_message(filters.command("help") & filters.private)
async def help_cmd(_, message: Message):
    await message.reply_text(
        "**📖 Kaise Use Karein:**\n\n"
        "1️⃣ Website ka **video page URL** copy karo\n"
        "2️⃣ Bot ko paste karke bhejo\n"
        "3️⃣ Bot automatically download button click karega\n"
        "4️⃣ Video original name + quality + size ke saath upload hogi\n\n"
        "**Multiple links:** Ek message mein 10 links bhi bhej sakte ho!"
    )


@app.on_message(filters.command("queue") & filters.private)
async def queue_cmd(_, message: Message):
    await message.reply_text(
        f"📊 **Queue Status**\n"
        f"• Pending: `{download_queue.qsize()}`\n"
        f"• Active: `{active_downloads}`"
    )


# ==========================================
# GLOBAL QUEUE
# ==========================================
download_queue = asyncio.Queue()
active_downloads = 0
MAX_CONCURRENT = 2

URL_REGEX = re.compile(r'https?://[^\s<>"\']+')


# ==========================================
# PROCESS SINGLE URL
# ==========================================
async def process_single_url(client, chat_id, original_url, reply_to_msg):
    """Ek URL process karke video bhejta hai — original filename + quality + size ke saath."""
    global active_downloads
    active_downloads += 1

    timestamp = int(time.time() * 1000) + active_downloads
    final_file_path = ""
    video_title = "video"
    video_quality = "Unknown"
    loop = asyncio.get_event_loop()
    status_msg = await client.send_message(chat_id, f"🔍 **Processing:** `{original_url[:60]}...`")

    try:
        success = False

        # ---------- STEP 1: Playwright ----------
        try:
            await status_msg.edit_text(f"🌐 **Browser opening...**\n`{original_url[:60]}`")
            result = await extract_video_url_via_browser(original_url)

            if result and result.get("url"):
                extracted_url = result["url"]
                video_title = result.get("title") or "video"
                video_quality = result.get("quality") or "Unknown"

                # Original filename URL se bhi nikal sakte hain
                url_filename = parse_filename_from_url(extracted_url)
                if url_filename:
                    video_title = url_filename.rsplit('.', 1)[0]

                await status_msg.edit_text("✅ **Link mila! Downloading...**")
                file_path = os.path.join(DOWNLOAD_DIR, f"video_{timestamp}.mp4")
                final_file_path = await loop.run_in_executor(
                    None, download_direct, extracted_url, file_path
                )
                success = True
        except Exception as e_browser:
            logging.warning(f"[Playwright] Failed: {e_browser}")

        # ---------- STEP 2: yt-dlp ----------
        if not success or not final_file_path:
            try:
                await status_msg.edit_text("🔄 **yt-dlp try kar raha hoon...**")
                # Original title preserve ke liye template
                out_template = os.path.join(DOWNLOAD_DIR, f"%(title)s_{timestamp}.%(ext)s")
                result = await loop.run_in_executor(
                    None, download_ytdlp, original_url, out_template
                )
                if result and result.get("path"):
                    final_file_path = result["path"]
                    video_title = result.get("title", video_title)
                    video_quality = result.get("quality", video_quality)
                success = True
            except Exception as e1:
                logging.warning(f"yt-dlp failed: {e1}")

        # ---------- STEP 3: Direct URL ----------
        if (not success or not final_file_path) and is_video_url(original_url) and not is_image_url(original_url):
            try:
                await status_msg.edit_text("⚡ **Direct download attempt...**")
                file_path = os.path.join(DOWNLOAD_DIR, f"video_{timestamp}.mp4")
                final_file_path = await loop.run_in_executor(
                    None, download_direct, original_url, file_path
                )
                # URL se filename nikaalo
                url_fn = parse_filename_from_url(original_url)
                if url_fn:
                    video_title = url_fn.rsplit('.', 1)[0]
                video_quality = extract_quality_from_url(original_url)
                success = True
            except Exception as e2:
                logging.warning(f"Direct failed: {e2}")

        # ---------- STEP 4: Gemini AI ----------
        if (not success or not final_file_path) and ai_model:
            try:
                await status_msg.edit_text("🤖 **Gemini AI se link...**")
                extracted_url = await loop.run_in_executor(
                    None, gemini_extract_link, original_url
                )
                if extracted_url:
                    file_path = os.path.join(DOWNLOAD_DIR, f"video_ai_{timestamp}.mp4")
                    final_file_path = await loop.run_in_executor(
                        None, download_direct, extracted_url, file_path
                    )
                    video_quality = extract_quality_from_url(extracted_url)
                    success = True
            except Exception as e3:
                logging.warning(f"Gemini failed: {e3}")

        # ---------- FINAL CHECK ----------
        if not final_file_path or not os.path.exists(final_file_path):
            raise Exception(
                "❌ Video extract nahi ho paaya.\n\n"
                "💡 **Try karein:**\n"
                "• Expired token link ki jagah **webpage URL** bhejein\n"
                "• e.g., `https://site.com/video/12345/title`"
            )

        # ---------- FILE SIZE ----------
        file_size = os.path.getsize(final_file_path)

        if file_size > 2 * 1024 * 1024 * 1024:
            raise Exception("❌ File 2GB se badi hai. Telegram limit exceed.")

        # ---------- CAPTION BUILD ----------
        # Original filename (extension ke saath)
        orig_filename = os.path.basename(final_file_path)
        # Agar timestamp suffix hai to hata do caption ke liye
        clean_name = re.sub(r'_\d+\.(mp4|mkv|webm|avi|mov)$', '', orig_filename, flags=re.IGNORECASE)
        if not clean_name:
            clean_name = sanitize_filename(video_title)

        # Quality detect
        if video_quality == "Unknown" and '1080' in orig_filename:
            video_quality = "1080p"
        elif video_quality == "Unknown" and '720' in orig_filename:
            video_quality = "720p"
        elif video_quality == "Unknown" and '480' in orig_filename:
            video_quality = "480p"

        caption = (
            f"✅ **Downloaded Successfully!**\n\n"
            f"🎬 **Name:** `{clean_name}`\n"
            f"📺 **Quality:** `{video_quality}`\n"
            f"📦 **Size:** `{humanbytes(file_size)}`\n"
            f"🔗 **Source:** `{original_url[:80]}`"
        )

        # ---------- UPLOAD ----------
        await status_msg.edit_text(
            f"📤 **Uploading to Telegram...**\n"
            f"🎬 Name: `{clean_name}`\n"
            f"📦 Size: `{humanbytes(file_size)}`"
        )
        start_time = time.time()

        # file_name parameter se Telegram par original name dikhega
        await client.send_video(
            chat_id=chat_id,
            video=final_file_path,
            file_name=orig_filename,  # ✅ Original filename preserve
            caption=caption,
            reply_to_message_id=reply_to_msg,
            progress=progress_status,
            progress_args=(status_msg, "Uploading", start_time)
        )
        await status_msg.delete()

    except Exception as e:
        logging.error(f"[Bot] Error: {e}")
        try:
            await status_msg.edit_text(f"❌ **Error:**\n`{str(e)[:300]}`")
        except Exception:
            pass

    finally:
        for file in os.listdir(DOWNLOAD_DIR):
            if str(timestamp) in file:
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, file))
                except Exception:
                    pass
        active_downloads -= 1


# ==========================================
# QUEUE WORKER
# ==========================================
async def queue_worker(client):
    """Queue se ek-ek karke URL uthata hai aur process karta hai."""
    while True:
        try:
            job = await download_queue.get()
            if job is None:
                break
            chat_id, url, reply_to = job
            try:
                await process_single_url(client, chat_id, url, reply_to)
            except Exception as e:
                logging.error(f"[Worker] Job failed: {e}")
            download_queue.task_done()
        except Exception as e:
            logging.error(f"[Worker] Error: {e}")
            await asyncio.sleep(2)


# ==========================================
# MAIN HANDLER — Multiple URLs Extract
# ==========================================
@app.on_message(filters.regex(r'https?://') & filters.private)
async def process_url(client, message: Message):
    text = message.text or ""
    urls = URL_REGEX.findall(text)
    urls = list(dict.fromkeys(urls))

    if not urls:
        return

    if len(urls) == 1:
        await message.reply_text("📥 **Queue mein add ho gaya...**")
        await download_queue.put((message.chat.id, urls[0], message.id))
        return

    await message.reply_text(
        f"📥 **{len(urls)} links queue mein add ho gaye!**\n"
        f"📊 Active: `{active_downloads}` | Pending: `{download_queue.qsize()}`\n"
        f"⏳ Ek-ek karke process honge..."
    )
    for url in urls:
        await download_queue.put((message.chat.id, url, message.id))


# ==========================================
# MAIN — App start + worker tasks
# ==========================================
async def main():
    await app.start()
    logging.info("🚀 Bot started, launching queue workers...")

    workers = [asyncio.create_task(queue_worker(app)) for _ in range(MAX_CONCURRENT)]

    from pyrogram import idle
    await idle()

    for w in workers:
        w.cancel()


if __name__ == "__main__":
    logging.info("🚀 Bot starting...")
    app.run(main())
