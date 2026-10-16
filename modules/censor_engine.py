import os
import bisect
import math
import time
import logging
import threading
import subprocess

import cv2
import numpy as np

CENSOR_STYLE = os.environ.get("CENSOR_STYLE", "mosaic").lower()
CENSOR_DETECTORS = [x.strip() for x in os.environ.get("CENSOR_DETECTORS", "anime,nudenet").lower().split(",") if x.strip()]
CENSOR_STRICT = int(os.environ.get("CENSOR_STRICT", "2"))
CENSOR_NUDENET_SCORE = float(os.environ.get("CENSOR_MIN_SCORE", "0.18"))
CENSOR_COVERED_SCORE = float(os.environ.get("CENSOR_COVERED_SCORE", "0.45"))
CENSOR_ANIME_SCORE = float(os.environ.get("CENSOR_ANIME_SCORE", "0.15"))
CENSOR_ANIME_LEVEL = os.environ.get("CENSOR_ANIME_LEVEL", "s")
CENSOR_TTA = os.environ.get("CENSOR_TTA", "1") == "1"
CENSOR_PAD = float(os.environ.get("CENSOR_PAD", "0.4"))
CENSOR_MIN_FRAC = float(os.environ.get("CENSOR_MIN_FRAC", "0.08"))
CENSOR_CELLS = int(os.environ.get("CENSOR_CELLS", "4"))
CENSOR_BUDGET_SEC = float(os.environ.get("CENSOR_BUDGET_SEC", "360"))
CENSOR_MAX_SCAN_GAP = float(os.environ.get("CENSOR_MAX_SCAN_GAP", "0.2"))
CENSOR_GAP_FILL = float(os.environ.get("CENSOR_GAP_FILL", "0.6"))
CENSOR_MAX_SEC = float(os.environ.get("CENSOR_MAX_SEC", "180"))
CENSOR_CRF = int(os.environ.get("CENSOR_CRF", "18"))
CENSOR_PRESET = os.environ.get("CENSOR_PRESET", "medium")

STYLES = ("mosaic", "blur", "black", "blend")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")

NUDENET_EXPOSED = {
    "BUTTOCKS_EXPOSED", "FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED", "MALE_GENITALIA_EXPOSED", "MALE_BREAST_EXPOSED",
    "EXPOSED_ANUS", "EXPOSED_BUTTOCKS", "EXPOSED_BREAST_F",
    "EXPOSED_GENITALIA_F", "EXPOSED_GENITALIA_M", "EXPOSED_BREAST_M",
}
NUDENET_COVERED = {
    "FEMALE_GENITALIA_COVERED", "BUTTOCKS_COVERED", "FEMALE_BREAST_COVERED", "ANUS_COVERED",
    "COVERED_GENITALIA_F", "COVERED_BUTTOCKS", "COVERED_BREAST_F", "COVERED_ANUS",
}
NUDENET_LABELS = NUDENET_EXPOSED | (NUDENET_COVERED if CENSOR_STRICT >= 2 else set())

_lock = threading.Lock()
_state = {"ready": False, "anime": None, "nude": None}


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
    s = (s or CENSOR_STYLE or "mosaic").lower()
    return s if s in STYLES else "mosaic"


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


def _merge(boxes, thr=0.3):
    boxes = list(boxes)
    changed = True
    while changed:
        changed = False
        out = []
        for b in boxes:
            for i, o in enumerate(out):
                if _iou(b, o) >= thr:
                    x0, y0 = min(b[0], o[0]), min(b[1], o[1])
                    x1, y1 = max(b[0] + b[2], o[0] + o[2]), max(b[1] + b[3], o[1] + o[3])
                    out[i] = (x0, y0, x1 - x0, y1 - y0)
                    changed = True
                    break
            else:
                out.append(b)
        boxes = out
    return boxes


def _grow(box, W, H):
    x, y, w, h = box
    px, py = w * CENSOR_PAD, h * CENSOR_PAD
    x0, y0, x1, y1 = x - px, y - py, x + w + px, y + h + py
    side = max(24.0, CENSOR_MIN_FRAC * min(W, H))
    if x1 - x0 < side:
        c = (x0 + x1) / 2
        x0, x1 = c - side / 2, c + side / 2
    if y1 - y0 < side:
        c = (y0 + y1) / 2
        y0, y1 = c - side / 2, c + side / 2
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1, y1 = min(W, int(math.ceil(x1))), min(H, int(math.ceil(y1)))
    return (x0, y0, max(1, x1 - x0), max(1, y1 - y0))


def _apply(frame, boxes, style):
    for x, y, w, h in boxes:
        if w < 2 or h < 2:
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


def _fill(keys, samples, gap):
    have = [k for k in keys if samples[k]]
    filled = {}
    for k in keys:
        if samples[k]:
            filled[k] = [b for _, _, b in samples[k]]
            continue
        near = []
        i = bisect.bisect_left(have, k)
        for j in (i - 1, i):
            if 0 <= j < len(have) and abs(have[j] - k) <= gap:
                near.extend(b for _, _, b in samples[have[j]])
        filled[k] = near
    return filled


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
    boxes = [_grow(b, W, H) for b in _merge([r[2] for r in raw])]
    _apply(frame, boxes, style)
    cv2.imwrite(dst, frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
    return {
        "path": dst, "found": len(boxes), "labels": sorted({r[0] for r in raw}),
        "backends": censor_backends(),
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
    filled = _fill(keys, samples, max(1, int(round(fps * CENSOR_GAP_FILL))))
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
    f, pos, broken = 0, 0, False
    try:
        while True:
            _check(prog)
            ok, frame = cap.read()
            if not ok:
                break
            while pos + 1 < len(keys) and keys[pos + 1] <= f:
                pos += 1
            boxes = list(filled[keys[pos]])
            if pos + 1 < len(keys):
                boxes += filled[keys[pos + 1]]
            if boxes:
                if frame.shape[:2] != (H, W):
                    frame = cv2.resize(frame, (W, H))
                _apply(frame, [_grow(b, W, H) for b in _merge(boxes)], style)
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
        "backends": censor_backends(),
    }


def censor_any(path, prog=None, style=None):
    base = os.path.splitext(path)[0]
    if path.lower().endswith(IMAGE_EXTS):
        return censor_image(path, base + "_censored.jpg", style)
    return censor_video(path, base + "_censored.mp4", prog, style)
