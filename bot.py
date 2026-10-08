import os
import time
import asyncio
import logging
import requests
import yt_dlp
import google.generativeai as genai
from pyrogram import Client, filters
from pyrogram.types import Message

# ==========================================
# 1. GITHUB SECRETS (ENVIRONMENT VARIABLES)
# ==========================================
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Logging Setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Initialize Telegram Bot
app = Client("video_downloader_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Initialize Gemini API
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    ai_model = genai.GenerativeModel('gemini-1.5-flash')
else:
    logging.warning("GEMINI_API_KEY not found! AI Fallback will not work.")

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
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    with requests.get(url, headers=headers, stream=True) as r:
        r.raise_for_status()
        with open(output_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024*1024):
                if chunk: f.write(chunk)
    return output_path

def download_ytdlp(url, output_template):
    ydl_opts = {
        'outtmpl': output_template,
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
        'quiet': True,
        'no_warnings': True,
        'cookiefile': 'cookies.txt' if os.path.exists('cookies.txt') else None
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)

# ==========================================
# 4. AI FALLBACK ENGINE (GEMINI)
# ==========================================
def gemini_extract_link(url):
    try:
        # Fetch Webpage HTML
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(url, headers=headers, timeout=10)
        html_content = response.text[:100000] # Limit to 100k chars to save tokens
        
        prompt = (
            "You are a web scraper. I am giving you the raw HTML of a video hosting webpage. "
            "Find the direct playable video URL (usually ending in .mp4, .m3u8, or a hidden source link). "
            "Return ONLY the raw URL as your response. If you cannot find any video URL, return 'NOT_FOUND'.\n\n"
            f"HTML Snippet:\n{html_content}"
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
# 5. TELEGRAM BOT HANDLERS
# ==========================================
@app.on_message(filters.command("start") & filters.private)
async def start_cmd(_, message: Message):
    await message.reply_text(
        "👋 **Advanced AI Downloader Bot Started!**\n\n"
        "Bhejo koi bhi link. Agar normal downloader fail hua, to mera AI Engine usko bypass karke video nikal lega."
    )

@app.on_message(filters.regex(r'https?://[^\s]+') & filters.private)
async def process_url(_, message: Message):
    original_url = message.text.strip()
    status_msg = await message.reply_text("🔍 **Processing Link...**")
    
    timestamp = int(time.time())
    final_file_path = ""
    loop = asyncio.get_event_loop()

    try:
        # Step 1: Detect Direct Link
        is_direct = any(ext in original_url.lower() for ext in [".mp4", ".mkv", ".webm", ".m3u8"])
        
        if is_direct:
            await status_msg.edit_text("⚡ **Direct Link Detected. Downloading...**")
            file_path = os.path.join(DOWNLOAD_DIR, f"video_{timestamp}.mp4")
            final_file_path = await loop.run_in_executor(None, download_direct, original_url, file_path)
            
        else:
            # Step 2: Try yt-dlp first
            try:
                await status_msg.edit_text("🌐 **Extracting via yt-dlp...**")
                out_template = os.path.join(DOWNLOAD_DIR, f"video_{timestamp}.%(ext)s")
                final_file_path = await loop.run_in_executor(None, download_ytdlp, original_url, out_template)
                
            except Exception as e:
                logging.error(f"yt-dlp failed: {e}")
                await status_msg.edit_text("⚠️ **Extractor Failed. Starting AI Fallback Engine...** 🤖")
                
                # Step 3: Trigger Gemini AI Fallback on error
                extracted_url = await loop.run_in_executor(None, gemini_extract_link, original_url)
                
                if extracted_url:
                    await status_msg.edit_text(f"🧠 **AI found hidden link!** Downloading...\n`{extracted_url[:30]}...`")
                    # Download the AI extracted link
                    file_path = os.path.join(DOWNLOAD_DIR, f"video_ai_{timestamp}.mp4")
                    final_file_path = await loop.run_in_executor(None, download_direct, extracted_url, file_path)
                else:
                    raise Exception("AI could not find the hidden video source. Link might be fully protected or expired.")

        # Step 4: Upload to Telegram
        if not final_file_path or not os.path.exists(final_file_path):
            raise Exception("Download complete nahi ho paya.")

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
        # Step 5: Cleanup Server Storage
        for file in os.listdir(DOWNLOAD_DIR):
            if str(timestamp) in file:
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, file))
                except Exception:
                    pass

if __name__ == "__main__":
    app.run()
