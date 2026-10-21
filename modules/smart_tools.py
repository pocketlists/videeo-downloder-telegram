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


def ahash_gray(gray):
    try:
        import cv2
        import numpy as np
        small = cv2.resize(gray, (8, 8), interpolation=cv2.INTER_AREA)
        avg = float(np.mean(small))
        bits = (small > avg).flatten()
        h = 0
        for b in bits:
            h = (h << 1) | int(bool(b))
        return h
    except Exception:
        return None


def ahash_file(path):
    try:
        import cv2
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        return ahash_gray(img)
    except Exception:
        return None


def ahash_bytes(data):
    try:
        import cv2
        import numpy as np
        if not data:
            return None
        arr = np.frombuffer(data, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return None
        return ahash_gray(img)
    except Exception:
        return None


def ahash_video(path, frames=6):
    out = []
    try:
        import cv2
        cap = cv2.VideoCapture(str(path))
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total > 0:
            idxs = [int(total * (i + 0.5) / frames) for i in range(frames)]
        else:
            idxs = [0]
        for ix in idxs:
            try:
                cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, ix))
            except Exception:
                pass
            ok, frame = cap.read()
            if ok and frame is not None:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                h = ahash_gray(gray)
                if h is not None:
                    out.append(h)
        cap.release()
    except Exception:
        pass
    return out


def hash_sim(a, b):
    if a is None or b is None:
        return 0.0
    return 1.0 - (bin(int(a) ^ int(b)).count("1") / 64.0)


def _tease_score(labels):
    score = 0.0
    for label in labels:
        up = str(label).upper()
        if "GENITALIA" in up or "ANUS" in up:
            score -= 6.0
        elif "EXPOSED" in up:
            score += 1.5 if "BELLY" in up else -4.0
        elif "COVERED" in up or "BELLY" in up or "ARMPIT" in up:
            score += 2.0
        elif "BREAST" in up or "BUTTOCK" in up:
            score += 1.0
    return score


def teasing_segments(path, duration, seg_len=8.0, count=4, stop=None):
    import modules.censor_engine as cen
    cen._load()
    if not cen.censor_backends() or not duration:
        return []
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return []
    step = max(1.0, duration / 160.0)
    scores = []
    t = 0.5
    try:
        while t < max(duration, 1.0):
            if stop is not None and stop.is_set():
                break
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, fr = cap.read()
            if ok:
                s = _tease_score([r[0] for r in cen.detect_frame(fr)])
                if s > 0:
                    scores.append((t, s))
            t += step
    finally:
        cap.release()
    if not scores:
        return []
    wins = []
    for tc, _s in scores:
        total = sum(s for t, s in scores if tc - seg_len / 2 <= t < tc + seg_len / 2)
        wins.append((total, tc))
    wins.sort(reverse=True)
    picked = []
    for total, tc in wins:
        start = max(0.0, min(tc - seg_len / 2, max(0.0, duration - seg_len)))
        if all(abs(start - p) >= seg_len * 1.5 for p, _ in picked):
            picked.append((start, total))
        if len(picked) >= count:
            break
    picked.sort()
    return picked


def motion_segments(path, duration, seg_len=8.0, count=4, stop=None):
    scores = motion_scores(path, duration, stop=stop)
    if not scores or not duration:
        return []
    step = scores[1][0] - scores[0][0] if len(scores) > 1 else 1.0
    n = max(1, int(round(seg_len / max(step, 0.1))))
    vals = [v for _, v in scores]
    cand = []
    for i in range(len(vals)):
        cand.append((sum(vals[i:i + n]), scores[i][0]))
    cand.sort(reverse=True)
    picked = []
    for total, t0 in cand:
        start = max(0.0, min(t0, max(0.0, duration - seg_len)))
        if all(abs(start - p) >= seg_len * 1.5 for p, _ in picked):
            picked.append((start, total))
        if len(picked) >= count:
            break
    picked.sort()
    return picked
