import os
import time
import asyncio
import logging
import cloudscraper
from urllib.parse import urlparse
import yt_dlp
import google.generativeai as genai
from pyrogram import Client, filters
from pyrogram.types import Message

# ==========================================
# 1. ENVIRONMENT VARIABLES (GITHUB SECRETS)
# ==========================================
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

app = Client("video_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    ai_model = genai.GenerativeModel('gemini-3.8-flash')
else:
    logging.warning("GEMINI_API_KEY is missing! AI Scraper Engine disabled.")
    ai_model = None

DOWNLOAD_DIR = "./downloads/"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ==========================================
# 2. HELPER FUNCTIONS
# ==========================================
def humanbytes(size):
    if not size: return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0: return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"

async def progress_status(current, total, status_msg, action_text, start_time):
    now = time.time()
    diff = now - start_time
    if diff == 0: return
    
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
# 3. DOWNLOAD ENGINES
# ==========================================
def download_direct(url, output_path):
    """Direct file download using Cloudscraper with dynamic Referer."""
    parsed_url = urlparse(url)
    referer = f"{parsed_url.scheme}://{parsed_url.netloc}/"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Referer": referer,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive"
    }

    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
    )
    
    response = scraper.get(url, headers=headers, stream=True, timeout=60)
    response.raise_for_status()

    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=1024*1024):
            if chunk:
                f.write(chunk)
                
    return output_path

def download_ytdlp(url, output_template):
    """yt-dlp Engine with Cloudflare Impersonation."""
    parsed_url = urlparse(url)
    referer = f"{parsed_url.scheme}://{parsed_url.netloc}/"
    
    ydl_opts = {
        'outtmpl': output_template,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True,
        'impersonate': 'chrome', # Bypasses Cloudflare on GitHub Servers
        'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Referer': referer,
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

def gemini_extract_link(url):
    """Fallback Engine: Gemini AI parses Webpage HTML to find hidden MP4/M3U8 video links."""
    if not ai_model:
        return None
    try:
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
        )
        response = scraper.get(url, timeout=20)
        html_content = response.text[:120000] # Limit tokens
        
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
        logging.error(f"Gemini AI Web Scraper Error: {e}")
        return None

# ==========================================
# 4. TELEGRAM BOT HANDLERS
# ==========================================
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(_, message: Message):
    await message.reply_text(
        "👋 **Universal AI Video Downloader Bot**\n\n"
        "Mujhe kisi bhi video ka **Webpage URL** ya **Direct Link** bhejo.\n"
        "Bot automatically website ko scrape karke video download karke bhej dega."
    )

@app.on_message(filters.regex(r'https?://[^\s]+') & filters.private)
async def process_url(_, message: Message):
    original_url = message.text.strip()
    status_msg = await message.reply_text("🔍 **Processing Link...**")
    
    timestamp = int(time.time())
    final_file_path = ""
    loop = asyncio.get_event_loop()

    try:
        # Step 1: Attempt yt-dlp first (Best for Webpage URLs)
        try:
            await status_msg.edit_text("🌐 **Extracting via yt-dlp Engine...**")
            out_template = os.path.join(DOWNLOAD_DIR, f"video_{timestamp}.%(ext)s")
            final_file_path = await loop.run_in_executor(None, download_ytdlp, original_url, out_template)
            
        except Exception as e1:
            logging.warning(f"yt-dlp failed: {e1}")
            
            # Step 2: Direct Download check
            is_direct = any(ext in original_url.lower() for ext in [".mp4", ".mkv", ".webm", ".m3u8"])
            
            if is_direct and "v-acctoken=" not in original_url:
                await status_msg.edit_text("⚡ **Attempting Direct Cloudscraper Download...**")
                file_path = os.path.join(DOWNLOAD_DIR, f"video_{timestamp}.mp4")
                final_file_path = await loop.run_in_executor(None, download_direct, original_url, file_path)
            else:
                # Step 3: Trigger Gemini AI Scraper Fallback
                await status_msg.edit_text("🤖 **yt-dlp Failed. Activating Gemini AI Web Scraper...**")
                extracted_url = await loop.run_in_executor(None, gemini_extract_link, original_url)
                
                if extracted_url:
                    await status_msg.edit_text("🧠 **AI extracted video source!** Downloading video file...")
                    file_path = os.path.join(DOWNLOAD_DIR, f"video_ai_{timestamp}.mp4")
                    final_file_path = await loop.run_in_executor(None, download_direct, extracted_url, file_path)
                else:
                    raise Exception(
                        "Video extract nahi ho paaya.\n\n"
                        "💡 **Important:** Expired token link ki jagah main Webpage ka URL paste karein "
                        "(e.g., `https://rule34video.com/video/4651254/...`)."
                    )

        # Step 4: Upload to Telegram
        if not final_file_path or not os.path.exists(final_file_path):
            raise Exception("File save nahi ho saki.")

        await status_msg.edit_text("📤 **Uploading to Telegram...**")
        start_time = time.time()
        
        await message.reply_video(
            video=final_file_path,
            caption="✅ **Downloaded Successfully!**",
            progress=progress_status,
            progress_args=(status_msg, "Uploading", start_time)
        )
        await status_msg.delete()

    except Exception as e:
        await status_msg.edit_text(f"❌ **Error:**\n`{str(e)}`")

    finally:
        # Cleanup server files
        for file in os.listdir(DOWNLOAD_DIR):
            if str(timestamp) in file:
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, file))
                except Exception:
                    pass

if __name__ == "__main__":
    app.run()
