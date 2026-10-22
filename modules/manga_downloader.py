import asyncio
import html as ihtml
import json
import os
import re
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

from modules.video_engine import make_headers, make_session, sanitize_filename, humanbytes, spawn

IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp")
BAD_TOKENS = ("logo", "avatar", "banner", "favicon", "placeholder", "spinner",
              "loading-gif", "emoji", "/icons/", "ads-", "/ads/", "ad-", "blank.gif")
MAX_CHAPTERS_AT_ONCE = 100
MAX_PAGES_PER_CHAPTER = 400
TG_DOC_LIMIT = 48 * 1024 * 1024

JOBS = {}
RANGE_WAIT = {}
_tls = threading.local()


def _session(referer=None, cookies=None):
    if getattr(_tls, "sess", None) is None or getattr(_tls, "ref", None) != referer:
        _tls.sess = make_session(make_headers(referer), cookies or [])
        _tls.ref = referer
    return _tls.sess


def _fetch(url, cookies=None, timeout=30, post_data=None):
    sc = make_session(make_headers(url), cookies or [])
    if post_data is not None:
        r = sc.post(url, data=post_data, timeout=timeout)
    else:
        r = sc.get(url, timeout=timeout)
    return r.text or ""


def _num_str(num):
    if float(num) == int(num):
        return f"{int(num):03d}"
    return f"{num:06.1f}".replace(".", "_")


def _chapter_num(href, text):
    s = (href or "") + " " + (text or "")
    for pat in (r"chapter[-_/\s]*(\d+(?:\.\d+)?)", r"/ch[-_](\d+(?:\.\d+)?)(?:[-_/]|$)",
                r"episode[-_/\s]*(\d+(?:\.\d+)?)", r"\bch\.?\s*(\d+(?:\.\d+)?)\b",
                r"chapter\s*(\d+(?:\.\d+)?)"):
        m = re.search(pat, s, re.I)
        if m:
            try:
                return float(m.group(1))
            except ValueError:
                pass
    return None


def _scan_chapters(txt, base):
    host = urlparse(base).netloc
    found = {}
    for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', txt or "", re.I | re.S):
        href = urljoin(base, ihtml.unescape(m.group(1)))
        pr = urlparse(href)
        if pr.netloc != host or "#" in m.group(1):
            continue
        if href.rstrip("/") == base.rstrip("/"):
            continue
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", ihtml.unescape(m.group(2)))).strip()
        num = _chapter_num(href, text)
        if num is None or num <= 0 or num > 100000:
            continue
        if "comment" in href.lower() or "respond" in href.lower():
            continue
        label = text[:60] if text else f"Chapter {num:g}"
        old = found.get(num)
        if old is None or len(label) > len(old[1]):
            found[num] = (href, label)
    return [{"num": n, "url": u, "label": l} for n, (u, l) in sorted(found.items())]


def _series_title(txt, url):
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', txt, re.I)
    if not m:
        m = re.search(r'<h1[^>]*>(.*?)</h1>', txt, re.I | re.S)
    if not m:
        m = re.search(r"<title>(.*?)</title>", txt, re.I | re.S)
    t = ""
    if m:
        t = re.sub(r"<[^>]+>", " ", ihtml.unescape(m.group(1)))
        t = re.sub(r"\s+", " ", t).strip()
        for sep in (" - ", " | ", " – ", " — "):
            if sep in t and len(t.split(sep)[0]) >= 3:
                t = t.split(sep)[0].strip()
                break
    return t or urlparse(url).path.strip("/").split("/")[-1].replace("-", " ").title() or "Manga"


def parse_series(url, cookies=None):
    txt = _fetch(url, cookies)
    if not txt:
        raise RuntimeError("Series page khul nahi paaya")
    un = ihtml.unescape(txt.replace("\\/", "/").replace("\\u0026", "&"))
    chapters = _scan_chapters(un, url)
    if len(chapters) < 3 and ("wp-manga" in txt or "madara" in txt.lower()):
        for ep in (url.rstrip("/") + "/ajax/chapters/",):
            try:
                t2 = _fetch(ep, cookies, post_data={})
                more = _scan_chapters(ihtml.unescape(t2.replace("\\/", "/")), url)
                if len(more) > len(chapters):
                    chapters = more
            except Exception:
                pass
    if len(chapters) < 3:
        seen_pages = set()
        for m in re.finditer(r'<a\b[^>]*?href=["\']([^"\']*[?&](?:page|paged)=\d+[^"\']*)["\']', un, re.I):
            pg = urljoin(url, ihtml.unescape(m.group(1)))
            if urlparse(pg).netloc == urlparse(url).netloc and pg not in seen_pages and len(seen_pages) < 3:
                seen_pages.add(pg)
                try:
                    t3 = _fetch(pg, cookies)
                    for c in _scan_chapters(ihtml.unescape(t3.replace("\\/", "/")), url):
                        if c["num"] not in {x["num"] for x in chapters}:
                            chapters.append(c)
                except Exception:
                    pass
        chapters.sort(key=lambda c: c["num"])
    thumb = ""
    m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', txt, re.I)
    if m:
        thumb = urljoin(url, ihtml.unescape(m.group(1)))
    return {"title": _series_title(txt, url), "url": url, "chapters": chapters, "thumb": thumb}


def _img_ok(u):
    low = u.lower()
    if any(t in low for t in BAD_TOKENS):
        return False
    path = urlparse(u).path.lower()
    return path.endswith(IMG_EXTS) or "cdn" in low or "/uploads/" in low or "/images/" in low or "/image/" in low


def _walk_json(obj, out):
    if isinstance(obj, str):
        if obj.startswith("http") and _img_ok(obj):
            out.append(obj)
    elif isinstance(obj, list):
        for v in obj:
            _walk_json(v, out)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_json(v, out)


def chapter_images(chapter_url, cookies=None):
    txt = _fetch(chapter_url, cookies)
    if not txt:
        return []
    un = ihtml.unescape(txt.replace("\\/", "/").replace("\\u0026", "&"))
    dom = []
    for m in re.finditer(r"<img\b[^>]*>", un, re.I):
        tag = m.group(0)
        cand = None
        for attr in ("data-src", "data-lazy-src", "data-original", "data-url", "src"):
            am = re.search(attr + r'=["\']([^"\']+)["\']', tag, re.I)
            if am and not am.group(1).startswith("data:"):
                cand = am.group(1)
                break
        if not cand:
            sm = re.search(r'srcset=["\']([^"\']+)["\']', tag, re.I)
            if sm:
                parts = [p.strip().split(" ")[0] for p in sm.group(1).split(",") if p.strip()]
                cand = parts[-1] if parts else None
        if cand:
            u = urljoin(chapter_url, cand.strip())
            if _img_ok(u):
                dom.append(u)
    dom = list(dict.fromkeys(dom))
    script_urls = []
    m = re.search(r"ts_reader\.run\((\{.*?\})\)\s*;", un, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
            srcs = data.get("sources") or []
            if srcs:
                script_urls = [urljoin(chapter_url, x) for x in (srcs[0].get("images") or []) if isinstance(x, str)]
        except Exception:
            pass
    if not script_urls:
        m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', un, re.S)
        if m:
            try:
                _walk_json(json.loads(m.group(1)), script_urls)
            except Exception:
                pass
    if not script_urls:
        for m in re.finditer(r'["\'](https?://[^"\']+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\']*)?)["\']', un):
            u = m.group(1)
            if _img_ok(u):
                script_urls.append(u)
        script_urls = list(dict.fromkeys(script_urls))
    if len(script_urls) > len(dom) * 1.5 and len(script_urls) >= 3:
        return script_urls[:MAX_PAGES_PER_CHAPTER]
    return (dom or script_urls)[:MAX_PAGES_PER_CHAPTER]


def _ext_from_bytes(data):
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if len(data) > 12 and data[4:8] == b"ftyp":
        return ".avif"
    return None


def _download_one(args):
    idx, u, chapter_url, folder, width, cookies = args
    sess = _session(chapter_url, cookies)
    last = None
    for _try in range(3):
        try:
            r = sess.get(u, headers={"Referer": chapter_url}, timeout=40)
            data = r.content or b""
            ext = _ext_from_bytes(data)
            if ext and len(data) > 2500:
                path = os.path.join(folder, f"{idx:0{width}d}{ext}")
                with open(path, "wb") as f:
                    f.write(data)
                return idx, path, len(data)
            last = f"bad image ({len(data)}b)"
        except Exception as e:
            last = str(e)[:80]
        time.sleep(1.0)
    return idx, None, last


def download_chapter(chapter, dest_root, cookies=None, progress=None):
    imgs = chapter_images(chapter["url"], cookies)
    if not imgs:
        return None, 0, 0, "images nahi mile"
    folder = os.path.join(dest_root, f"Chapter {_num_str(chapter['num'])}")
    os.makedirs(folder, exist_ok=True)
    width = max(3, len(str(len(imgs))))
    jobs = [(i + 1, u, chapter["url"], folder, width, cookies) for i, u in enumerate(imgs)]
    ok, total = 0, 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for idx, path, res in ex.map(_download_one, jobs):
            if path:
                ok += 1
                total += res
            if progress:
                progress(idx, len(imgs))
    if ok == 0:
        return None, 0, 0, "saari images fail ho gayin"
    return folder, ok, total, ""


def build_zip(series_title, chapter_folders, out_path):
    root = sanitize_filename(series_title) or "Manga"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as z:
        for label, folder in chapter_folders:
            for fn in sorted(os.listdir(folder)):
                z.write(os.path.join(folder, fn), arcname=f"{root}/{label}/{fn}")
        info = f"{series_title}\nChapters: {', '.join(l for l, _ in chapter_folders)}\nPacked by videeo bot\n"
        z.writestr(f"{root}/info.txt", info)
    return out_path


def build_cbz(series_title, label, folder, out_path):
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as z:
        for fn in sorted(os.listdir(folder)):
            z.write(os.path.join(folder, fn), arcname=fn)
    return out_path


def parse_range(text, chapters):
    text = (text or "").strip().lower()
    if not text or text == "all":
        return list(chapters)
    by_num = {c["num"]: c for c in chapters}
    picked, seen = [], set()
    for tok in re.split(r"[,\s]+", text):
        if not tok:
            continue
        m = re.fullmatch(r"(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)", tok)
        if m:
            a, b = float(m.group(1)), float(m.group(2))
            for c in chapters:
                if a <= c["num"] <= b and c["num"] not in seen:
                    seen.add(c["num"])
                    picked.append(c)
            continue
        m = re.fullmatch(r"\d+(?:\.\d+)?", tok)
        if m:
            v = float(tok)
            c = by_num.get(v)
            if c is None and v == int(v) and 1 <= int(v) <= len(chapters):
                c = chapters[int(v) - 1]
            if c and c["num"] not in seen:
                seen.add(c["num"])
                picked.append(c)
    picked.sort(key=lambda c: c["num"])
    return picked


def range_keyboard(job_id):
    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 First 10", callback_data=f"mg:{job_id}:f10"),
         InlineKeyboardButton("🆕 Latest 10", callback_data=f"mg:{job_id}:l10")],
        [InlineKeyboardButton("📚 All Chapters", callback_data=f"mg:{job_id}:all"),
         InlineKeyboardButton("✏️ Custom Range", callback_data=f"mg:{job_id}:custom")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"mg:{job_id}:x")],
    ])


def _job_id(chat_id):
    return f"{chat_id}_{int(time.time()) % 100000}"


async def manga_command(client, message):
    parts = (message.text or "").split(maxsplit=1)
    url = ""
    if len(parts) > 1:
        m = re.search(r"https?://[^\s]+", parts[1])
        url = m.group(0) if m else ""
    if not url and message.reply_to_message:
        m = re.search(r"https?://[^\s]+", message.reply_to_message.text or "")
        url = m.group(0) if m else ""
    if not url:
        return await message.reply_text(
            "📚 <b>Manga / Manhwa Downloader</b>\n\nSeries ka link bhejo:\n"
            "<code>/manga https://site.com/manga/series-name</code>\n\n"
            "Phir chapters choose karke <b>ZIP</b> me organized images milengi.",
        )
    st = await message.reply_text("🔎 Series parse kar raha hoon…")
    loop = asyncio.get_running_loop()
    try:
        series = await loop.run_in_executor(None, parse_series, url, None)
    except Exception as e:
        return await st.edit_text(f"❌ Parse fail: <code>{str(e)[:200]}</code>")
    if not series["chapters"]:
        return await st.edit_text(
            "❌ Is page par chapters nahi mile.\nSeries ke <b>main page</b> ka link bhejo (chapter ka nahi), "
            "aur site par chapter list khuli honi chahiye."
        )
    jid = _job_id(message.chat.id)
    JOBS[jid] = {"series": series, "chat_id": message.chat.id, "busy": False, "msg": st}
    chs = series["chapters"]
    await st.edit_text(
        f"📚 <b>{series['title']}</b>\n\n"
        f"📖 Chapters: <b>{len(chs)}</b> (Ch {chs[0]['num']:g} → Ch {chs[-1]['num']:g})\n\n"
        "Kitne chapters download karun?",
        reply_markup=range_keyboard(jid),
    )


async def manga_callback(client, cq, parts):
    if len(parts) < 3:
        return
    jid, act = parts[1], parts[2]
    job = JOBS.get(jid)
    if not job:
        return await cq.answer("⌛ Ye session purana ho gaya, /manga dobara bhejo.", show_alert=True)
    if act == "x":
        JOBS.pop(jid, None)
        RANGE_WAIT.pop(cq.message.chat.id, None)
        await cq.answer("❌ Cancel kar diya")
        try:
            await cq.message.edit_text("❌ Manga download cancel kar diya.")
        except Exception:
            pass
        return
    if act == "custom":
        RANGE_WAIT[cq.message.chat.id] = jid
        await cq.answer("✏️ Range bhejo")
        return await cq.message.reply_text(
            "✏️ <b>Range bhejo</b> — jaise <code>1-10</code> ya <code>5</code> ya <code>1,3,7-9</code>\n"
            "Chapter numbers series wale hi use honge."
        )
    chs = job["series"]["chapters"]
    if act == "f10":
        sel = chs[:10]
    elif act == "l10":
        sel = chs[-10:]
    else:
        sel = chs[:MAX_CHAPTERS_AT_ONCE]
    if job["busy"]:
        return await cq.answer("⏳ Download pehle se chal raha hai.", show_alert=True)
    await cq.answer("⬇️ Download shuru…")
    job["msg"] = cq.message
    return spawn(run_manga_job(client, job, sel), job["chat_id"])


async def try_range_input(client, message):
    jid = RANGE_WAIT.get(message.chat.id)
    if not jid:
        return False
    text = (message.text or "").strip()
    if not re.fullmatch(r"[\d\s,\.\-]+|all", text.lower()):
        return False
    job = JOBS.get(jid)
    RANGE_WAIT.pop(message.chat.id, None)
    if not job:
        await message.reply_text("⌛ Session purana ho gaya, /manga dobara bhejo.")
        return True
    sel = parse_range(text, job["series"]["chapters"])
    if not sel:
        await message.reply_text("❌ Is range me koi chapter nahi mila. Dobara try karo: /manga")
        return True
    if job["busy"]:
        await message.reply_text("⏳ Download pehle se chal raha hai.")
        return True
    st = await message.reply_text("⬇️ Download shuru…")
    job["msg"] = st
    spawn(run_manga_job(client, job, sel), job["chat_id"])
    return True


async def run_manga_job(client, job, sel):
    from modules.video_engine import DOWNLOAD_DIR
    job["busy"] = True
    msg = job["msg"]
    series = job["series"]
    loop = asyncio.get_running_loop()
    workdir = os.path.join(DOWNLOAD_DIR, f"manga_{int(time.time())}")
    os.makedirs(workdir, exist_ok=True)
    title = sanitize_filename(series["title"]) or "Manga"
    done, failed = [], []
    total_bytes = 0
    try:
        for i, ch in enumerate(sel, 1):
            try:
                await msg.edit_text(
                    f"⬇️ <b>{series['title']}</b>\nChapter {ch['num']:g} download ho raha hai… ({i}/{len(sel)})"
                )
            except Exception:
                pass
            folder, pages, size, err = await loop.run_in_executor(
                None, download_chapter, ch, workdir, None, None)
            if folder:
                done.append((f"Chapter {_num_str(ch['num'])}", folder, ch))
                total_bytes += size
            else:
                failed.append(f"Ch {ch['num']:g} ({err})")
        if not done:
            return await msg.edit_text("❌ Ek bhi chapter download nahi hua.\n" + "\n".join(failed[:5]))
        rng = f"{sel[0]['num']:g}-{sel[-1]['num']:g}" if len(sel) > 1 else f"{sel[0]['num']:g}"
        est = total_bytes
        sent = []
        if est <= TG_DOC_LIMIT or len(done) == 1 and est <= TG_DOC_LIMIT:
            out = os.path.join(workdir, f"{title}_Ch{rng}.zip")
            await loop.run_in_executor(None, build_zip, series["title"],
                                       [(l, f) for l, f, _c in done], out)
            cap = (f"📚 <b>{series['title']}</b>\n📖 Chapters: <b>{rng}</b> ({len(done)})\n"
                   f"🖼 Pages: <b>{sum(len(os.listdir(f)) for _l, f, _c in done)}</b>\n"
                   f"📦 Size: <code>{humanbytes(os.path.getsize(out))}</code>")
            await client.send_document(job["chat_id"], out, caption=cap)
            sent.append(out)
        else:
            await msg.edit_text("📦 ZIP bada hai — har chapter alag CBZ me bhej raha hoon…")
            for label, folder, ch in done:
                out = os.path.join(workdir, f"{title}_Ch{ch['num']:g}.cbz")
                await loop.run_in_executor(None, build_cbz, series["title"], label, folder, out)
                if os.path.getsize(out) > TG_DOC_LIMIT:
                    continue
                await client.send_document(
                    job["chat_id"], out,
                    caption=f"📚 {series['title']} — Chapter {ch['num']:g} ({len(os.listdir(folder))} pages)")
                sent.append(out)
        note = f"✅ <b>Ho gaya!</b> {len(done)} chapters bhej diye."
        if failed:
            note += "\n⚠️ Fail: " + ", ".join(failed[:5])
        await msg.edit_text(note)
    except Exception as e:
        try:
            await msg.edit_text(f"❌ Manga download fail: <code>{str(e)[:250]}</code>")
        except Exception:
            pass
    finally:
        job["busy"] = False
        shutil.rmtree(workdir, ignore_errors=True)
