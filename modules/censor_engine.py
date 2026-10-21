import os
import glob
import math
import time
import bisect
import logging
import threading
import subprocess

import cv2
import numpy as np

CENSOR_STYLE = os.environ.get("CENSOR_STYLE", "sticker").lower()
CENSOR_DETECTORS = [x.strip() for x in os.environ.get("CENSOR_DETECTORS", "anime,nudenet").lower().split(",") if x.strip()]
CENSOR_STRICT = int(os.environ.get("CENSOR_STRICT", "2"))
CENSOR_NUDENET_SCORE = float(os.environ.get("CENSOR_MIN_SCORE", "0.18"))
CENSOR_COVERED_SCORE = float(os.environ.get("CENSOR_COVERED_SCORE", "0.35"))
CENSOR_ANIME_SCORE = float(os.environ.get("CENSOR_ANIME_SCORE", "0.15"))
CENSOR_ANIME_LEVEL = os.environ.get("CENSOR_ANIME_LEVEL", "s")
CENSOR_TTA = os.environ.get("CENSOR_TTA", "1") == "1"
CENSOR_PAD = float(os.environ.get("CENSOR_PAD", "0.10"))
CENSOR_MIN_FRAC = float(os.environ.get("CENSOR_MIN_FRAC", "0.025"))
CENSOR_CELLS = int(os.environ.get("CENSOR_CELLS", "4"))
CENSOR_BUDGET_SEC = float(os.environ.get("CENSOR_BUDGET_SEC", "360"))
CENSOR_MAX_SCAN_GAP = float(os.environ.get("CENSOR_MAX_SCAN_GAP", "0.2"))
CENSOR_GAP_FILL = float(os.environ.get("CENSOR_GAP_FILL", "0.6"))
CENSOR_MAX_SEC = float(os.environ.get("CENSOR_MAX_SEC", "180"))
CENSOR_CRF = int(os.environ.get("CENSOR_CRF", "18"))
CENSOR_PRESET = os.environ.get("CENSOR_PRESET", "medium")

STYLES = ("sticker", "emoji", "mosaic", "blur", "black", "blend")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")

NUDENET_EXPOSED = {
    "BUTTOCKS_EXPOSED", "FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED", "MALE_GENITALIA_EXPOSED", "MALE_BREAST_EXPOSED",
    "EXPOSED_ANUS", "EXPOSED_BUTTOCKS", "EXPOSED_BREAST_F",
    "EXPOSED_GENITALIA_F", "EXPOSED_GENITALIA_M", "EXPOSED_BREAST_M",
}
NUDENET_COVERED = {
    "FEMALE_GENITALIA_COVERED", "BUTTOCKS_COVERED", "FEMALE_BREAST_COVERED", "ANUS_COVERED",
    "MALE_GENITALIA_COVERED", "MALE_BREAST_COVERED",
    "COVERED_GENITALIA_F", "COVERED_GENITALIA_M", "COVERED_BUTTOCKS",
    "COVERED_BREAST_F", "COVERED_BREAST_M", "COVERED_ANUS",
}
NUDENET_LABELS = NUDENET_EXPOSED | (NUDENET_COVERED if CENSOR_STRICT >= 2 else set())

STICKER_EMOJI = [
    ("BREAST", "🙈"),
    ("GENITALIA", "😂"),
    ("BUTTOCK", "🫣"),
    ("ANUS", "🫣"),
    ("BELLY", "😜"),
]
DEFAULT_EMOJI = "🔞"

STICKER_DIRS = []
if os.environ.get("CENSOR_STICKER_DIR"):
    STICKER_DIRS.append(os.environ["CENSOR_STICKER_DIR"])
STICKER_DIRS += [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "stickers"),
    os.path.join(os.getcwd(), "assets", "stickers"),
    os.path.join(os.getcwd(), "stickers"),
]
_folder_cache = {}


def _category(label):
    up = (label or "").upper()
    if "BREAST" in up:
        return "breast"
    if "GENITALIA" in up:
        return "genitalia"
    if "BUTTOCK" in up:
        return "buttocks"
    if "ANUS" in up:
        return "anus"
    if "BELLY" in up:
        return "belly"
    if "FACE" in up:
        return "face"
    return "default"


def _folder_files(cat):
    if cat in _folder_cache:
        return _folder_cache[cat]
    files = []
    for d in STICKER_DIRS:
        if d and os.path.isdir(d):
            files = sorted(glob.glob(os.path.join(d, cat + "*.png")))
            if not files:
                files = sorted(glob.glob(os.path.join(d, "*.png")))
            if files:
                break
    _folder_cache[cat] = files
    return files


def _file_sticker(label, w, h, seed):
    files = _folder_files(_category(label))
    if not files:
        return None
    import zlib
    path = files[zlib.crc32(f"{label}|{seed}".encode()) % len(files)]
    key = ("file", path, w // 24, h // 24)
    if key in _sticker_cache:
        st = _sticker_cache[key]
        return cv2.resize(st, (w, h), interpolation=cv2.INTER_AREA) if st.shape[1] != w or st.shape[0] != h else st
    img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if img is None or img.ndim != 3 or img.shape[2] != 4:
        return None
    img = img[:, :, [2, 1, 0, 3]]
    _sticker_cache[key] = img
    if img.shape[1] != w or img.shape[0] != h:
        return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
    return img

_lock = threading.Lock()
_state = {"ready": False, "anime": None, "nude": None}
_sticker_cache = {}
_emoji_font = None
_emoji_font_tried = False


def _blank():
    return np.zeros((320, 320, 3), dtype=np.uint8)


def _load():
    if _state["ready"]:
        return
    with _lock:
        if _state["ready"]:
            return
        if "anime" in CENSOR_DETECTORS:
            try:
                from imgutils.detect.censor import detect_censors
                from PIL import Image
                detect_censors(Image.fromarray(_blank()), level=CENSOR_ANIME_LEVEL)
                _state["anime"] = detect_censors
            except Exception as e:
                logging.warning(f"[censor] anime detector load fail: {str(e)[:160]}")
        if "nudenet" in CENSOR_DETECTORS:
            try:
                from nudenet import NudeDetector
                det = NudeDetector()
                det.detect(_blank())
                _state["nude"] = det
            except Exception as e:
                logging.warning(f"[censor] nudenet load fail: {str(e)[:160]}")
        _state["ready"] = True
        logging.info(f"[censor] backends: {censor_backends()}")


def censor_backends():
    out = []
    if _state["anime"]:
        out.append("anime")
    if _state["nude"]:
        out.append("nudenet")
    return out


def _require_backends():
    _load()
    if not (_state["anime"] or _state["nude"]):
        raise RuntimeError("Censor model load nahi hua (imgutils/nudenet) — uncensored video nahi bhejunga")


def _style(s):
    s = (s or CENSOR_STYLE or "sticker").lower()
    if s == "emoji":
        s = "sticker"
    return s if s in STYLES else "sticker"


def _emoji_for(label):
    up = (label or "").upper()
    for key, em in STICKER_EMOJI:
        if key in up:
            return em
    return DEFAULT_EMOJI


def _find_emoji_font():
    global _emoji_font, _emoji_font_tried
    if _emoji_font_tried:
        return _emoji_font
    _emoji_font_tried = True
    pats = [
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/truetype/noto-color-emoji/NotoColorEmoji.ttf",
        "/usr/share/fonts/noto/NotoColorEmoji.ttf",
        "/System/Library/Fonts/Apple Color Emoji.ttc",
        "C:/Windows/Fonts/seguiemj.ttf",
    ]
    pats += glob.glob("/usr/share/fonts/**/NotoColorEmoji*", recursive=True)
    pats += glob.glob("/usr/share/fonts/**/*[Ee]moji*", recursive=True)
    for p in pats:
        if p and os.path.exists(p):
            _emoji_font = p
            return p
    return None


def _fallback_sticker(w, h):
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = max(4, min(w, h) // 14)
    d.rounded_rectangle([m, m, w - m, h - m], radius=min(w, h) // 4,
                        fill=(255, 45, 120, 255), outline=(255, 255, 255, 255), width=max(3, m))
    txt = "18+"
    try:
        fp = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        font = ImageFont.truetype(fp, int(min(w, h) * 0.42)) if os.path.exists(fp) else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
    box = d.textbbox((0, 0), txt, font=font)
    d.text(((w - (box[2] - box[0])) / 2, (h - (box[3] - box[1])) / 2 - box[1]),
           txt, font=font, fill=(255, 255, 255, 255))
    return np.array(img)


def _render_sticker(emoji, w, h):
    w, h = max(8, int(w)), max(8, int(h))
    key = (emoji, w // 24, h // 24)
    if key in _sticker_cache:
        st = _sticker_cache[key]
        return cv2.resize(st, (w, h), interpolation=cv2.INTER_AREA) if st.shape[1] != w or st.shape[0] != h else st
    rgba = None
    font_path = _find_emoji_font()
    if font_path:
        try:
            from PIL import Image, ImageDraw, ImageFont
            S = 512
            img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            font = None
            for size in (128, 109, 96, 440, 64):
                try:
                    font = ImageFont.truetype(font_path, size)
                    break
                except Exception:
                    continue
            if font is None:
                raise RuntimeError("emoji font unusable")
            try:
                d.text((S // 2, S // 2), emoji, font=font, anchor="mm", embedded_color=True)
            except TypeError:
                d.text((20, 0), emoji, font=font, embedded_color=True)
            bbox = img.getbbox()
            if bbox:
                rgba = np.array(img.crop(bbox))
        except Exception:
            rgba = None
    if rgba is None or rgba.ndim != 3 or rgba.shape[2] != 4:
        rgba = _fallback_sticker(256, 256)
    pad = max(8, int(min(rgba.shape[1], rgba.shape[0]) * 0.14))
    H0, W0 = rgba.shape[:2]
    canvas = np.zeros((H0 + pad * 2, W0 + pad * 2, 4), dtype=np.uint8)
    canvas[pad:pad + H0, pad:pad + W0] = rgba
    mask = (canvas[:, :, 3] > 12).astype(np.uint8)
    bw = max(5, pad // 2)
    border = cv2.dilate(mask, np.ones((bw, bw), np.uint8))
    out = np.zeros_like(canvas)
    out[border > 0] = (255, 255, 255, 255)
    a = canvas[:, :, 3:4].astype(np.float32) / 255.0
    out[:, :, :3] = (canvas[:, :, :3].astype(np.float32) * a + out[:, :, :3].astype(np.float32) * (1 - a)).astype(np.uint8)
    out[:, :, 3] = np.maximum(out[:, :, 3], canvas[:, :, 3])
    _sticker_cache[key] = out
    if out.shape[1] != w or out.shape[0] != h:
        return cv2.resize(out, (w, h), interpolation=cv2.INTER_AREA)
    return out


def _paste_sticker(frame, x, y, w, h, label):
    st = _file_sticker(label, w, h, (x // 30, y // 30))
    if st is None:
        st = _render_sticker(_emoji_for(label), w, h)
    if st.shape[0] != h or st.shape[1] != w:
        st = cv2.resize(st, (w, h), interpolation=cv2.INTER_AREA)
    roi = frame[y:y + h, x:x + w]
    if roi.shape[0] != h or roi.shape[1] != w:
        return
    a = (st[:, :, 3:4].astype(np.float32) / 255.0)
    rgb = st[:, :, :3].astype(np.float32)
    bgr = rgb[:, :, ::-1]
    frame[y:y + h, x:x + w] = (bgr * a + roi.astype(np.float32) * (1 - a)).astype(np.uint8)


def _anime_once(frame):
    from PIL import Image
    pil = Image.fromarray(frame[:, :, ::-1])
    fn = _state["anime"]
    try:
        dets = fn(pil, level=CENSOR_ANIME_LEVEL, conf_threshold=CENSOR_ANIME_SCORE)
    except TypeError:
        dets = fn(pil)
    out = []
    for box, label, score in dets or []:
        if float(score) < CENSOR_ANIME_SCORE:
            continue
        x0, y0, x1, y1 = [int(round(v)) for v in box]
        out.append((str(label), float(score), (x0, y0, x1 - x0, y1 - y0)))
    return out


def _nude_once(frame):
    out = []
    for d in _state["nude"].detect(frame) or []:
        label = str(d.get("class") or d.get("label") or "").upper()
        try:
            score = float(d.get("score") or 0)
        except Exception:
            score = 0.0
        box = d.get("box") or d.get("bbox")
        if not box or label not in NUDENET_LABELS:
            continue
        need = CENSOR_NUDENET_SCORE if label in NUDENET_EXPOSED else CENSOR_COVERED_SCORE
        if score >= need:
            x, y, w, h = [int(v) for v in box[:4]]
            out.append((label, score, (x, y, w, h)))
    return out


def _with_flip(fn, frame):
    out = fn(frame)
    if CENSOR_TTA:
        W = frame.shape[1]
        for label, score, (x, y, w, h) in fn(frame[:, ::-1].copy()):
            out.append((label, score, (W - x - w, y, w, h)))
    return out


def _detect_anime(frame):
    return _with_flip(_anime_once, frame)


def _detect_nudenet(frame):
    return _with_flip(_nude_once, frame)


def detect_frame(frame):
    H, W = frame.shape[:2]
    raw = []
    for name, fn in (("anime", _detect_anime), ("nude", _detect_nudenet)):
        if not _state[name]:
            continue
        try:
            raw.extend(fn(frame))
        except Exception as e:
            logging.warning(f"[censor] {name} detect fail: {str(e)[:120]}")
    out = []
    for label, score, (x, y, w, h) in raw:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(W, x + w), min(H, y + h)
        if x1 - x0 > 2 and y1 - y0 > 2:
            out.append((label, score, (x0, y0, x1 - x0, y1 - y0)))
    return out


def _iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _overlap_frac(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x0, y0 = max(ax, bx), max(ay, by)
    x1, y1 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    return inter / min(aw * ah, bw * bh) if aw * ah and bw * bh else 0.0


def _merge_tagged(items, thr=0.45):
    items = list(items)
    changed = True
    while changed:
        changed = False
        out = []
        for label, box in items:
            for i, (ol, ob) in enumerate(out):
                if _iou(box, ob) >= thr or _overlap_frac(box, ob) >= 0.72:
                    x0, y0 = min(box[0], ob[0]), min(box[1], ob[1])
                    x1, y1 = max(box[0] + box[2], ob[0] + ob[2]), max(box[1] + box[3], ob[1] + ob[3])
                    out[i] = (ol, (x0, y0, x1 - x0, y1 - y0))
                    changed = True
                    break
            else:
                out.append((label, box))
        items = out
    return items


def _grow(box, W, H):
    x, y, w, h = box
    px, py = w * CENSOR_PAD, h * CENSOR_PAD
    x0, y0, x1, y1 = x - px, y - py, x + w + px, y + h + py
    side = max(18.0, CENSOR_MIN_FRAC * min(W, H))
    if x1 - x0 < side:
        c = (x0 + x1) / 2
        x0, x1 = c - side / 2, c + side / 2
    if y1 - y0 < side:
        c = (y0 + y1) / 2
        y0, y1 = c - side / 2, c + side / 2
    max_w, max_h = W * 0.48, H * 0.48
    if x1 - x0 > max_w:
        c = (x0 + x1) / 2
        x0, x1 = c - max_w / 2, c + max_w / 2
    if y1 - y0 > max_h:
        c = (y0 + y1) / 2
        y0, y1 = c - max_h / 2, c + max_h / 2
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(W, int(math.ceil(x1))), min(H, int(math.ceil(y1)))
    return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))


def prepare(raw, W, H):
    tagged = [(r[0], r[-1]) for r in raw]
    return [(label, _grow(box, W, H)) for label, box in _merge_tagged(tagged)]


def _apply(frame, tagged_boxes, style):
    H, W = frame.shape[:2]
    for label, (x, y, w, h) in tagged_boxes:
        x, y = max(0, x), max(0, y)
        w, h = min(w, W - x), min(h, H - y)
        if w < 4 or h < 4:
            continue
        if style == "sticker":
            _paste_sticker(frame, x, y, w, h, label)
            continue
        roi = frame[y:y + h, x:x + w]
        if style == "black":
            roi[:] = 0
        elif style == "blend":
            roi[:] = roi.reshape(-1, 3).mean(axis=0).astype(np.uint8)
        elif style == "blur":
            k = max(15, (min(w, h) // 2) | 1)
            frame[y:y + h, x:x + w] = cv2.GaussianBlur(cv2.GaussianBlur(roi, (k, k), 0), (k, k), 0)
        else:
            cell = max(6, min(w, h) // max(2, CENSOR_CELLS))
            small = cv2.resize(roi, (max(1, w // cell), max(1, h // cell)), interpolation=cv2.INTER_AREA)
            frame[y:y + h, x:x + w] = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)


def _boxes_at(f, keys, samples, gap_frames):
    samples = {k: [(r[0], r[-1]) for r in (v or [])] for k, v in samples.items()}
    i = bisect.bisect_right(keys, f) - 1
    if i < 0:
        i = 0
    a_key = keys[i]
    a = samples.get(a_key) or []
    if not a:
        for k in keys:
            if abs(k - f) <= gap_frames and samples.get(k):
                return samples[k]
        return []
    if i + 1 >= len(keys):
        return a
    b_key = keys[i + 1]
    b = samples.get(b_key) or []
    if not b or b_key - a_key > gap_frames * 2:
        return a if f - a_key <= gap_frames else []
    if len(a) != len(b):
        return a
    t = (f - a_key) / max(1, b_key - a_key)
    out = []
    used = set()
    for label, box in a:
        best_j, best_d = None, None
        for j, (bl, bb) in enumerate(b):
            if j in used:
                continue
            d = abs((bb[0] + bb[2] / 2) - (box[0] + box[2] / 2)) + abs((bb[1] + bb[3] / 2) - (box[1] + box[3] / 2))
            if best_d is None or d < best_d:
                best_d, best_j = d, j
        if best_j is None:
            out.append((label, box))
            continue
        used.add(best_j)
        bb = b[best_j][1]
        out.append((label, tuple(int(round(box[k] + (bb[k] - box[k]) * t)) for k in range(4))))
    return out


def _audio_codec(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_name",
             "-of", "default=nw=1:nk=1", path],
            capture_output=True, text=True, timeout=30
        )
        return r.stdout.strip().lower()
    except Exception:
        return ""


def video_seconds(path):
    cap = cv2.VideoCapture(path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        return float(n / fps) if fps and n else 0.0
    finally:
        cap.release()


def _check(prog):
    if prog is not None and hasattr(prog, "check"):
        prog.check()


def censor_image(src, dst, style=None):
    style = _style(style)
    _require_backends()
    frame = cv2.imread(src, cv2.IMREAD_COLOR)
    if frame is None:
        from PIL import Image
        frame = np.array(Image.open(src).convert("RGB"))[:, :, ::-1].copy()
    H, W = frame.shape[:2]
    raw = detect_frame(frame)
    tagged = prepare(raw, W, H)
    _apply(frame, tagged, style)
    cv2.imwrite(dst, frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return {
        "path": dst, "found": len(tagged), "labels": sorted({r[0] for r in raw}),
        "backends": censor_backends(), "style": style,
    }


def censor_video(src, dst, prog=None, style=None):
    style = _style(style)
    _require_backends()
    cap = cv2.VideoCapture(src)
    if not cap.isOpened():
        raise RuntimeError("Censor: video open nahi hui")
    ok, first = cap.read()
    if not ok:
        cap.release()
        raise RuntimeError("Censor: video frames nahi padh paaya")
    H, W = first.shape[:2]
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    if not (1.0 <= fps <= 120.0):
        fps = 25.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0

    t0 = time.time()
    samples = {0: detect_frame(first)}
    t_det = max(time.time() - t0, 0.005)
    step = 1
    if total:
        step = max(1, math.ceil(total * t_det / CENSOR_BUDGET_SEC))
        step = min(step, max(1, int(round(fps * CENSOR_MAX_SCAN_GAP))))

    idx = 1
    try:
        while True:
            _check(prog)
            if not cap.grab():
                break
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if ok:
                    samples[idx] = detect_frame(frame)
                if prog and total:
                    prog.update("🔎 Scanning frames", idx, total, "steps")
            idx += 1
    finally:
        cap.release()
    n_frames = idx

    keys = sorted(samples)
    gap_frames = max(1, int(round(fps * CENSOR_GAP_FILL)))
    hits = sum(1 for k in keys if samples[k])
    found = sum(len(samples[k]) for k in keys)

    acodec = _audio_codec(src)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", f"{fps}", "-i", "-",
        "-i", src, "-map", "0:v:0",
    ]
    if acodec:
        cmd += ["-map", "1:a:0", "-c:a", "copy" if acodec in ("aac", "mp3") else "aac"]
        if acodec not in ("aac", "mp3"):
            cmd += ["-b:a", "192k"]
    cmd += [
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
        "-c:v", "libx264", "-preset", CENSOR_PRESET, "-crf", str(CENSOR_CRF), "-tune", "animation",
        "-pix_fmt", "yuv420p", "-shortest", "-movflags", "+faststart", dst,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    cap = cv2.VideoCapture(src)
    f, broken = 0, False
    try:
        while True:
            _check(prog)
            ok, frame = cap.read()
            if not ok:
                break
            tagged = _boxes_at(f, keys, samples, gap_frames)
            if tagged:
                if frame.shape[:2] != (H, W):
                    frame = cv2.resize(frame, (W, H))
                _apply(frame, prepare(tagged, W, H), style)
            elif frame.shape[:2] != (H, W):
                frame = cv2.resize(frame, (W, H))
            try:
                proc.stdin.write(frame.tobytes())
            except (BrokenPipeError, OSError):
                broken = True
                break
            f += 1
            if prog and n_frames:
                prog.update("🎨 Censoring + encoding", f, n_frames, "steps")
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    finally:
        cap.release()
    try:
        proc.stdin.close()
    except Exception:
        pass
    rc = proc.wait()
    if broken or rc != 0 or not os.path.exists(dst):
        raise RuntimeError("Censor encode fail")
    return {
        "path": dst, "found": found, "frames": f, "hit_frames": hits, "step": step,
        "backends": censor_backends(), "style": style,
    }


def censor_any(path, prog=None, style=None):
    base = os.path.splitext(path)[0]
    if path.lower().endswith(IMAGE_EXTS):
        return censor_image(path, base + "_censored.jpg", style)
    return censor_video(path, base + "_censored.mp4", prog, style)
