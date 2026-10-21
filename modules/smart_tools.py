import os
import re
import math
from urllib.parse import urlparse, urljoin

import cv2
import numpy as np

PREVIEW_HINTS = ("preview", "trailer", "_prev", "-prev", "/prev", "sample_", "_sample", "teaser")
HARD_BAD = ("thumb", "poster", "sprite", "placeholder", "banner", "/ads/", "advert", "favicon")
RES_STEPS = (2160, 1440, 1080, 720, 480, 360, 240, 144)


def is_preview_url(u):
    low = (u or "").lower()
    return any(h in low for h in PREVIEW_HINTS)


def is_hard_bad_url(u):
    low = (u or "").lower()
    return any(h in low for h in HARD_BAD)


def core_id(u):
    stem = os.path.splitext(os.path.basename(urlparse(u).path))[0].lower()
    stem = re.sub(r"(preview|trailer|sample|teaser|prev)", "", stem)
    stem = re.sub(r"[^a-z0-9]", "", stem)
    digits = re.findall(r"\d{4,}", stem)
    return digits[0] if digits else (stem if len(stem) >= 6 else "")


def same_video_urls(urls, preview_url):
    cid = core_id(preview_url)
    if not cid:
        return []
    out = []
    for u in urls or []:
        if u == preview_url or is_preview_url(u) or is_hard_bad_url(u):
            continue
        if cid in re.sub(r"[^a-z0-9]", "", urlparse(u).path.lower()):
            out.append(u)
    return out


def preview_hd_variants(u):
    out = []

    def push(x):
        if x and x != u and x not in out and x.startswith("http"):
            out.append(x)

    p = urlparse(u)
    path = p.path
    no_prev = re.sub(r"(?i)([_\-./]?)(preview|trailer|teaser|sample)(?=[_\-./]|$)", "", path)
    no_prev = re.sub(r"__+", "_", re.sub(r"\.\.+", ".", no_prev)).replace("_.", ".").replace("-.", ".").replace("//", "/")
    if no_prev != path:
        push(p._replace(path=no_prev).geturl())
    seg = re.sub(r"(?i)/(preview|trailer|teaser|sample)/", "/", path)
    if seg != path:
        push(p._replace(path=seg).geturl())
        push(p._replace(path=re.sub(r"(?i)/(preview|trailer|teaser|sample)/", "/full/", path)).geturl())
    m = re.search(r"(?<!\d)(144|240|360|480|540)(p)?(?!\d)", path)
    if m:
        for r in (1080, 720):
            push(p._replace(path=path[:m.start()] + str(r) + (m.group(2) or "") + path[m.end():]).geturl())
            if no_prev != path:
                np2 = re.sub(r"(?<!\d)(144|240|360|480|540)(p)?(?!\d)", str(r) + (m.group(2) or ""), no_prev, count=1)
                push(p._replace(path=np2).geturl())
    base = p._replace(query="", fragment="").geturl()
    if base != u:
        push(base)
    return out[:10]


def download_best_thumbnail(urls, session, out_dir, prefix):
    from PIL import Image
    best, best_key = None, None
    seen = set()
    for i, u in enumerate(urls or []):
        if not u or u in seen or u.startswith("data:"):
            continue
        seen.add(u)
        try:
            r = session.get(u, timeout=20, headers={"Accept": "image/*,*/*"})
            ct = (r.headers.get("content-type") or "").lower()
            if r.status_code != 200 or len(r.content) < 8 * 1024:
                continue
            if not (ct.startswith("image/") or u.lower().split("?")[0].endswith((".jpg", ".jpeg", ".png", ".webp", ".avif"))):
                continue
            raw = os.path.join(out_dir, f"{prefix}_t{i}.img")
            with open(raw, "wb") as f:
                f.write(r.content)
            try:
                with Image.open(raw) as im:
                    w, h = im.size
                    im.verify()
            except Exception:
                os.remove(raw)
                continue
            if w < 200 or h < 150:
                os.remove(raw)
                continue
            key = (w * h, len(r.content))
            if best_key is None or key > best_key:
                if best:
                    try:
                        os.remove(best)
                    except OSError:
                        pass
                best, best_key = raw, key
            else:
                os.remove(raw)
        except Exception:
            continue
    if not best:
        return None, 0, 0
    jpg = os.path.join(out_dir, f"{prefix}_hd.jpg")
    try:
        with Image.open(best) as im:
            w, h = im.size
            im.convert("RGB").save(jpg, "JPEG", quality=94)
        if best != jpg:
            os.remove(best)
        return jpg, w, h
    except Exception:
        return best, 0, 0


def heatmap_start(heatmap, duration, length):
    pts = []
    for e in heatmap or []:
        try:
            pts.append((float(e.get("value") or 0), float(e.get("start_time") or 0), float(e.get("end_time") or 0)))
        except Exception:
            continue
    if not pts or not duration:
        return None
    v, s, e = max(pts)
    if v <= 0:
        return None
    center = (s + e) / 2
    return max(0.0, min(center - length / 2, max(0.0, duration - length)))


def motion_scores(path, duration, step=None, stop=None):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = step or max(1.0, duration / 240.0 if duration else 1.0)
    every = max(1, int(round(fps * step)))
    scores, prev, idx, seen = [], None, 0, 0
    try:
        while True:
            if stop is not None and stop.is_set():
                break
            if not cap.grab():
                break
            if idx % every == 0:
                ok, fr = cap.retrieve()
                if ok:
                    g = cv2.cvtColor(cv2.resize(fr, (160, 90)), cv2.COLOR_BGR2GRAY)
                    sc = 0.0 if prev is None else float(np.mean(cv2.absdiff(g, prev)))
                    scores.append((seen * step, sc))
                    prev = g
                    seen += 1
            idx += 1
            if total and idx > total:
                break
    finally:
        cap.release()
    return scores


def best_window(scores, duration, length):
    if not scores or not duration:
        return max(0.0, duration * 0.2) if duration else 0.0
    step = scores[1][0] - scores[0][0] if len(scores) > 1 else 1.0
    n = max(1, int(round(length / max(step, 0.1))))
    best_s, best_v = scores[0][0], -1.0
    run = 0.0
    vals = [v for _, v in scores]
    for i, v in enumerate(vals):
        run += v
        if i >= n:
            run -= vals[i - n]
        if run > best_v:
            best_v, best_s = run, scores[max(0, i - n + 1)][0]
    return max(0.0, min(best_s, max(0.0, duration - length)))


def highlight_start(path, duration, heatmap=None, length=30.0, stop=None):
    hs = heatmap_start(heatmap, duration, length)
    if hs is not None:
        return hs, "most-replayed"
    sc = motion_scores(path, duration, stop=stop)
    return best_window(sc, duration, length), "motion+scene"


def nude_window(path, duration, window=10.0):
    import modules.censor_engine as cen
    cen._load()
    if not cen.censor_backends():
        return None, 0
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None, 0
    step = max(1.0, duration / 150.0) if duration else 2.0
    hits = {}
    t = 0.5
    try:
        while t < max(duration, 1.0):
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, fr = cap.read()
            if ok:
                n = len(cen.detect_frame(fr))
                if n:
                    b = int(t // window)
                    hits[b] = hits.get(b, 0) + n
            t += step
    finally:
        cap.release()
    if not hits:
        return None, 0
    b = max(hits, key=hits.get)
    start = max(0.0, min(b * window, max(0.0, duration - window)))
    return start, hits[b]
