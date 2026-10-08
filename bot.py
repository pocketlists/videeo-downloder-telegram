import os
import time
import asyncio
import logging
from urllib.parse import urlparse
import yt_dlp
import cloudscraper
from pyrogram import Client, filters
from pyrogram.types import Message
from playwright.async_api import async_playwright
import google.generativeai as genai

# ==========================================
# 1. ENVIRONMENT VARIABLES (GITHUB SECRETS)
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

# Gemini AI Setup
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


async def progress_status(current, total, status_msg, action_text, start_time):
    now = time.time()
    diff = now - start_time
    if diff == 0:
        return

    if not hasattr(progress_status, "last_update"):
        progress_status.last_update = 0

    if now - progress_status.last_update > 3 or current == total:
        progress_status.last_update = now
        percentage = (current / total) * 100
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
# 3. PLAYWRIGHT - DOWNLOAD BUTTON CLICK KARKE LINK NIKALNA
# ==========================================
async def extract_video_url_via_browser(page_url: str) -> str:
    """
    Browser kholta hai, download button dhundh ke click karta hai,
    aur direct video URL capture karta hai (network monitoring se bhi).
    """
    captured_url = None

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

            # ---- Network monitor: video URLs capture karo ----
            async def handle_response(response):
                nonlocal captured_url
                try:
                    url = response.url
                    content_type = response.headers.get("content-type", "")
                    if any(ext in url.lower() for ext in ['.mp4', '.m3u8', '.webm', '.mkv']):
                        if not captured_url:
                            captured_url = url
                            logging.info(f"[Playwright] Network captured: {url}")
                    elif "video" in content_type and "html" not in content_type:
                        if not captured_url:
                            captured_url = url
                            logging.info(f"[Playwright] Content-type video: {url}")
                except Exception:
                    pass

            page.on("response", handle_response)

            logging.info(f"[Playwright] Opening: {page_url}")
            await page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

            # ---- Popups / new tabs handle karo ----
            try:
                pages = context.pages
                if len(pages) > 1:
                    page = pages[-1]
            except Exception:
                pass

            # ---- Download button selectors ----
            download_selectors = [
                'a[href*="download"]',
                'a[href*=".mp4"]',
                'a[href*=".m3u8"]',
                'a:has-text("Download")',
                'button:has-text("Download")',
                'a:has-text("Download Video")',
                'a:has-text("DOWNLOAD")',
                'a[download]',
                '.download-button',
                '.btn-download',
                '#download',
                '#btn-download',
                'a.download',
                'button.download',
            ]

            for selector in download_selectors:
                try:
                    element = await page.wait_for_selector(selector, timeout=2000)
                    if element:
                        logging.info(f"[Playwright] Clicking: {selector}")
                        try:
                            async with page.expect_download(timeout=8000) as download_info:
                                await element.click()
                            download = await download_info.value
                            if download.url:
                                captured_url = download.url
                                logging.info(f"[Playwright] Download event URL: {captured_url}")
                                break
                        except Exception:
                            # Download event nahi aaya, bas click karke network se wait karo
                            try:
                                await element.click()
                            except Exception:
                                pass
                            await page.wait_for_timeout(3000)
                            if captured_url:
                                break
                except Exception:
                    continue

            # ---- Agar button se nahi mila, iframes check karo ----
            if not captured_url:
                try:
                    iframes = await page.query_selector_all("iframe")
                    for iframe in iframes:
                        src = await iframe.get_attribute("src")
                        if src and any(ext in src.lower() for ext in ['.mp4', '.m3u8']):
                            captured_url = src
                            break
                except Exception:
                    pass

            # ---- Last resort: page ke saare video/source tags ----
            if not captured_url:
                try:
                    video_src = await page.evaluate("""
                        () => {
                            const v = document.querySelector('video');
                            if (v && v.src) return v.src;
                            const s = document.querySelector('video source');
                            if (s && s.src) return s.src;
                            return null;
                        }
                    """)
                    if video_src:
                        captured_url = video_src
                except Exception:
                    pass

            await browser.close()

    except Exception as e:
        logging.error(f"[Playwright] Error: {e}")

    return captured_url


# ==========================================
# 4. DOWNLOAD ENGINES
# ==========================================
def download_direct(url, output_path):
    """Direct file download via Cloudscraper with dynamic Referer."""
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
    }

    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
    )

    response = scraper.get(url, headers=headers, stream=True, timeout=120)
    response.raise_for_status()

    total = int(response.headers.get("content-length", 0))
    downloaded = 0

    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if total:
                    pct = (downloaded / total) * 100
                    if pct % 20 < 1:
                        logging.info(f"[Direct] {pct:.0f}% downloaded")

    return output_path


def download_ytdlp(url, output_template):
    """yt-dlp Engine with Cloudflare impersonation."""
    parsed_url = urlparse(url)
    referer = f"{parsed_url.scheme}://{parsed_url.netloc}/"

    ydl_opts = {
        'outtmpl': output_template,
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
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


def gemini_extract_link(url):
    """Fallback: Gemini AI HTML parse karke video link nikalta hai."""
    if not ai_model:
        return None
    try:
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
        )
        response = scraper.get(url, timeout=25)
        html_content = response.text[:120000]

        prompt = (
            "You are an expert web scraper. I am giving you the raw HTML of a video hosting webpage. "
            "Find and extract the direct downloadable or playable video URL (ending in .mp4, .m3u8, or a source link inside video/iframe/a tags). "
            "Return ONLY the direct video URL. If no video link is found, return 'NOT_FOUND'.\n\n"
            f"Page URL: {url}\n\nHTML Snippet:\n{html_content}"
        )

        ai_response = ai_model.generate_content(prompt)
        extracted_url = ai_response.text.strip()

        if extracted_url and "http" in extracted_url and extracted_url != "NOT_FOUND":
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
        "Mujhe kisi bhi video ka **Webpage URL** ya **Direct Link** bhejo.\n"
        "Bot khud browser kholke download button click karega aur video bhej dega.\n\n"
        "⚡ **Engines:** Playwright Browser → yt-dlp → Cloudscraper → Gemini AI"
    )


@app.on_message(filters.command("help") & filters.private)
async def help_cmd(_, message: Message):
    await message.reply_text(
        "**📖 Kaise Use Karein:**\n\n"
        "1️⃣ Website ka **video page URL** copy karo\n"
        "2️⃣ Bot ko paste karke bhejo\n"
        "3️⃣ Bot automatically download button click karega\n"
        "4️⃣ Video Telegram par upload ho jayegi\n\n"
        "**Example:**\n"
        "`https://example.com/video/12345/title`"
    )


@app.on_message(filters.regex(r'https?://[^\s]+') & filters.private)
async def process_url(_, message: Message):
    original_url = message.text.strip()
    status_msg = await message.reply_text("🔍 **Processing Link...**")

    timestamp = int(time.time())
    final_file_path = ""
    loop = asyncio.get_event_loop()

    try:
        # ============ STEP 1: Playwright Browser ============
        browser_success = False
        try:
            await status_msg.edit_text("🌐 **Browser open kar raha hoon...**\n(Download button dhundh raha hoon)")
            extracted_url = await extract_video_url_via_browser(original_url)

            if extracted_url:
                await status_msg.edit_text("✅ **Direct video link mil gaya!**\n📥 Download start...")
                file_path = os.path.join(DOWNLOAD_DIR, f"video_{timestamp}.mp4")
                final_file_path = await loop.run_in_executor(
                    None, download_direct, extracted_url, file_path
                )
                browser_success = True
        except Exception as e_browser:
            logging.warning(f"[Playwright] Failed: {e_browser}")

        # ============ STEP 2: yt-dlp Fallback ============
        if not browser_success or not final_file_path:
            try:
                await status_msg.edit_text("🔄 **Browser fail. yt-dlp try kar raha hoon...**")
                out_template = os.path.join(DOWNLOAD_DIR, f"video_{timestamp}.%(ext)s")
                final_file_path = await loop.run_in_executor(
                    None, download_ytdlp, original_url, out_template
                )
                browser_success = True
            except Exception as e1:
                logging.warning(f"yt-dlp failed: {e1}")

        # ============ STEP 3: Direct URL check ============
        if (not browser_success or not final_file_path):
            is_direct = any(
                ext in original_url.lower()
                for ext in [".mp4", ".mkv", ".webm", ".m3u8"]
            )
            if is_direct:
                try:
                    await status_msg.edit_text("⚡ **Direct download attempt...**")
                    file_path = os.path.join(DOWNLOAD_DIR, f"video_{timestamp}.mp4")
                    final_file_path = await loop.run_in_executor(
                        None, download_direct, original_url, file_path
                    )
                    browser_success = True
                except Exception as e2:
                    logging.warning(f"Direct failed: {e2}")

        # ============ STEP 4: Gemini AI Fallback ============
        if (not browser_success or not final_file_path) and ai_model:
            try:
                await status_msg.edit_text("🤖 **Gemini AI se link dhundh raha hoon...**")
                extracted_url = await loop.run_in_executor(
                    None, gemini_extract_link, original_url
                )
                if extracted_url:
                    await status_msg.edit_text("🧠 **AI ne link nikala! Downloading...**")
                    file_path = os.path.join(DOWNLOAD_DIR, f"video_ai_{timestamp}.mp4")
                    final_file_path = await loop.run_in_executor(
                        None, download_direct, extracted_url, file_path
                    )
                    browser_success = True
            except Exception as e3:
                logging.warning(f"Gemini failed: {e3}")

        # ============ FINAL CHECK ============
        if not final_file_path or not os.path.exists(final_file_path):
            raise Exception(
                "❌ Video extract nahi ho paaya.\n\n"
                "💡 **Try karein:**\n"
                "• Expired token link ki jagah **webpage URL** bhejein\n"
                "• e.g., `https://site.com/video/12345/title`"
            )

        # ============ UPLOAD TO TELEGRAM ============
        file_size = os.path.getsize(final_file_path)

        if file_size > 2 * 1024 * 1024 * 1024:
            raise Exception("❌ File 2GB se badi hai. Telegram limit exceed.")

        await status_msg.edit_text(
            f"📤 **Uploading to Telegram...**\n📦 Size: `{humanbytes(file_size)}`"
        )
        start_time = time.time()

        await message.reply_video(
            video=final_file_path,
            caption="✅ **Downloaded Successfully!**",
            progress=progress_status,
            progress_args=(status_msg, "Uploading", start_time)
        )
        await status_msg.delete()

    except Exception as e:
        logging.error(f"[Bot] Error: {e}")
        try:
            await status_msg.edit_text(f"❌ **Error:**\n`{str(e)}`")
        except Exception:
            pass

    finally:
        # Cleanup
        for file in os.listdir(DOWNLOAD_DIR):
            if str(timestamp) in file:
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, file))
                except Exception:
                    pass


if __name__ == "__main__":
    logging.info("🚀 Bot starting...")
    app.run()
