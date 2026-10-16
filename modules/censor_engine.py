import os
import bisect
import math
import time
import logging
import threading
import subprocess

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Configuration (No "black" style; defaults to clean skin redraw / blur)
# ---------------------------------------------------------------------------
CENSOR_STYLE = os.environ.get("CENSOR_STYLE", "blend").lower()
if CENSOR_STYLE == "black":
    CENSOR_STYLE = "blend"

CENSOR_DETECTORS = [
    x.strip()
    for x in os.environ.get("CENSOR_DETECTORS", "anime,nudenet").lower().split(",")
    if x.strip()
]
CENSOR_STRICT = int(os.environ.get("CENSOR_STRICT", "2"))
CENSOR_NUDENET_SCORE = float(os.environ.get("CENSOR_MIN_SCORE", "0.16"))
# Higher threshold for covered (bra/underwear) so normal innerwear isn't over-censored
CENSOR_COVERED_SCORE = float(os.environ.get("CENSOR_COVERED_SCORE", "0.62"))
CENSOR_COVERED_MIN_SKIN_RATIO = float(os.environ.get("CENSOR_COVERED_MIN_SKIN_RATIO", "0.55"))
CENSOR_ANIME_SCORE = float(os.environ.get("CENSOR_ANIME_SCORE", "0.14"))
CENSOR_ANIME_LEVEL = os.environ.get("CENSOR_ANIME_LEVEL", "s")
CENSOR_TTA = os.environ.get("CENSOR_TTA", "1") == "1"
CENSOR_PAD = float(os.environ.get("CENSOR_PAD", "0.35"))
CENSOR_MIN_FRAC = float(os.environ.get("CENSOR_MIN_FRAC", "0.07"))
CENSOR_CELLS = int(os.environ.get("CENSOR_CELLS", "4"))
CENSOR_BUDGET_SEC = float(os.environ.get("CENSOR_BUDGET_SEC", "360"))
CENSOR_MAX_SCAN_GAP = float(os.environ.get("CENSOR_MAX_SCAN_GAP", "0.18"))
CENSOR_GAP_FILL = float(os.environ.get("CENSOR_GAP_FILL", "0.70"))
CENSOR_MAX_SEC = float(os.environ.get("CENSOR_MAX_SEC", "180"))
CENSOR_CRF = int(os.environ.get("CENSOR_CRF", "18"))
CENSOR_PRESET = os.environ.get("CENSOR_PRESET", "medium")

# "black" removed — only clean styles kept
STYLES = ("blend", "blur", "mosaic")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")

NUDENET_EXPOSED = {
    "BUTTOCKS_EXPOSED", "FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED", "MALE_GENITALIA_EXPOSED",
    "EXPOSED_ANUS", "EXPOSED_BUTTOCKS", "EXPOSED_BREAST_F",
    "EXPOSED_GENITALIA_F", "EXPOSED_GENITALIA_M",
    "NIPPLE", "NIPPLES", "AREOLA", "PENIS", "VAGINA", "PUSSY", "COCK",
    "SEX", "ORAL", "ANAL", "CUM", "SEMEN", "FLUID",
}

# Only breast covered is checked when heavy skin is exposed; normal panties/bra skipped
NUDENET_COVERED = {
    "FEMALE_BREAST_COVERED", "COVERED_BREAST_F", "CLEAVAGE",
}

EXPOSED_KEYWORDS = (
    "EXPOSED", "NIPPLE", "AREOLA", "GENITAL", "PENIS", "VAGIN", "PUSSY",
    "ANUS", "BREAST_F", "FEMALE_BREAST_EXPOSED", "SEX", "CUM", "SEMEN",
)
COVERED_BOOB_KEYWORDS = (
    "FEMALE_BREAST_COVERED", "COVERED_BREAST_F", "CLEAVAGE", "UNDERBOOB", "SIDEBOOB",
)
SKIP_UNDERWEAR_KEYWORDS = (
    "PANTIES", "UNDERWEAR", "BUTTOCKS_COVERED", "COVERED_BUTTOCKS",
    "FEMALE_GENITALIA_COVERED", "COVERED_GENITALIA_F", "BELLY", "ARMPIT", "FACE",
)

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
    s = (s or CENSOR_STYLE or "blend").lower()
    if s == "black":
        return "blend"
    return s if s in STYLES else "blend"


# ---------------------------------------------------------------------------
# Skin, Cloth & Cum/Fluid Pixel Segmentation Engine
# ---------------------------------------------------------------------------
def _extract_skin_and_cloth_masks(roi):
    """
    Separates bare skin from clothing & outlines inside an ROI:
    - skin_mask: 255 only on bare skin (anime peach/pink/tan + real skin).
    - cloth_mask: 255 on clothing, straps, bra/panties fabric, and dark line-art.
    Guarantees cloth pixels are NEVER included in skin_mask.
    """
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    ycrcb = cv2.cvtColor(roi, cv2.COLOR_BGR2YCrCb)

    H_ch, S_ch, V_ch = cv2.split(hsv)

    # 1. Bare Skin Detection (Anime/3D peach, warm tan, soft pink + YCrCb human skin)
    skin_hsv1 = cv2.inRange(hsv, np.array([0, 12, 95], dtype=np.uint8), np.array([26, 170, 255], dtype=np.uint8))
    skin_hsv2 = cv2.inRange(hsv, np.array([160, 10, 105], dtype=np.uint8), np.array([180, 155, 255], dtype=np.uint8))
    skin_ycc = cv2.inRange(ycrcb, np.array([70, 132, 78], dtype=np.uint8), np.array([250, 178, 132], dtype=np.uint8))
    skin_mask = cv2.bitwise_or(cv2.bitwise_or(skin_hsv1, skin_hsv2), skin_ycc)

    # 2. Cloth & Line-Art Protection Mask
    # Non-skin hues (green, blue, purple, cyan, bright yellow, deep magenta fabrics)
    cloth_hue = cv2.inRange(H_ch, 28, 158)
    # High saturation fabrics (bright red/pink/orange bras, bikinis, clothes)
    cloth_high_sat = cv2.inRange(S_ch, 172, 255)
    # Dark clothing, black straps, and anime line-art outlines
    cloth_dark = cv2.inRange(V_ch, 0, 68)
    # Crisp fabric textures / edges (bra lace, seams, borders)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 70, 160)

    cloth_mask = cv2.bitwise_or(cloth_hue, cloth_high_sat)
    cloth_mask = cv2.bitwise_or(cloth_mask, cloth_dark)
    cloth_mask = cv2.bitwise_or(cloth_mask, edges)

    # Slightly dilate cloth mask so blur/redraw never bleeds onto cloth edges
    k_cloth = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    cloth_guard = cv2.dilate(cloth_mask, k_cloth, iterations=1)

    # Remove all cloth pixels from skin_mask
    skin_mask = cv2.bitwise_and(skin_mask, cv2.bitwise_not(cloth_guard))

    # Clean up skin mask into smooth organic regions
    k_skin = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    skin_mask = cv2.morphologyEx(skin_mask, cv2.MORPH_OPEN, k_skin, iterations=1)
    skin_mask = cv2.morphologyEx(skin_mask, cv2.MORPH_CLOSE, k_skin, iterations=2)

    return skin_mask, cloth_guard


def _extract_fluid_mask(roi, skin_mask):
    """
    Detects cum / milky white / glossy fluid regions strictly where they exist:
    - Low saturation, high brightness creamy/white/translucent blobs near or on skin.
    """
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    lab = cv2.cvtColor(roi, cv2.COLOR_BGR2LAB)

    # Bright, low-saturation milky/white/creamy pixels
    fluid_hsv = cv2.inRange(hsv, np.array([0, 0, 192], dtype=np.uint8), np.array([180, 52, 255], dtype=np.uint8))
    fluid_lab = cv2.inRange(lab, np.array([195, 118, 118], dtype=np.uint8), np.array([255, 140, 142], dtype=np.uint8))
    fluid = cv2.bitwise_and(fluid_hsv, fluid_lab)

    # Keep only fluid blobs that are adjacent to or inside skin (avoids white background walls/shirts)
    if np.count_nonzero(skin_mask) > 30:
        k_near = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
        near_skin = cv2.dilate(skin_mask, k_near, iterations=2)
        fluid = cv2.bitwise_and(fluid, near_skin)

    k_clean = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    fluid = cv2.morphologyEx(fluid, cv2.MORPH_OPEN, k_clean, iterations=1)
    fluid = cv2.morphologyEx(fluid, cv2.MORPH_CLOSE, k_clean, iterations=2)

    # Filter out tiny specular highlights or huge white backgrounds
    area = fluid.shape[0] * fluid.shape[1]
    cnt_px = np.count_nonzero(fluid)
    if cnt_px < 18 or cnt_px > area * 0.65:
        return np.zeros_like(fluid)
    return fluid


def _smooth_curved_alpha(binary_mask, cloth_guard=None):
    """
    Converts any binary mask into a non-boxy, smooth curved contour alpha (0.0 -> 1.0):
    1. Multiplies by an elliptical radial dome so rectangular box corners are always 0.
    2. Smoothes contour boundaries with Gaussian feathering.
    3. Strictly zeroes out cloth pixels so clothing is never touched.
    """
    h, w = binary_mask.shape[:2]
    if h < 4 or w < 4:
        return np.zeros((h, w, 1), dtype=np.float32)

    # Smooth elliptical radial dome (zero at box borders/corners, 1 in smooth oval interior)
    yy, xx = np.ogrid[:h, :w]
    cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
    ry, rx = max(1.0, h / 2.0), max(1.0, w / 2.0)
    dist_sq = ((yy - cy) / ry) ** 2 + ((xx - cx) / rx) ** 2
    ellipse_dome = np.clip(1.0 - np.power(dist_sq, 1.6), 0.0, 1.0).astype(np.float32)

    base = (binary_mask.astype(np.float32) / 255.0) * ellipse_dome

    # Feather for ultra-clean curved transitions
    k = max(11, (min(w, h) // 5) | 1)
    soft = cv2.GaussianBlur(base, (k, k), 0)

    # Strictly protect cloth pixels from any feather bleed
    if cloth_guard is not None:
        cloth_keep = 1.0 - (cv2.GaussianBlur(cloth_guard.astype(np.float32), (5, 5), 0) / 255.0)
        soft = soft * np.clip(cloth_keep, 0.0, 1.0)

    return np.expand_dims(np.clip(soft, 0.0, 1.0), axis=-1)


def _sample_surrounding_skin_color(roi, skin_mask):
    """Returns median BGR color of clean surrounding skin inside the ROI."""
    if np.count_nonzero(skin_mask) < 20:
        return np.array([180, 198, 232], dtype=np.float32)  # fallback warm skin tone

    # Prefer outer ring of the skin mask (clean surrounding skin around nipple/genitals)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    eroded = cv2.erode(skin_mask, k, iterations=2)
    ring = cv2.bitwise_and(skin_mask, cv2.bitwise_not(eroded))
    pts = roi[ring > 0] if np.count_nonzero(ring) >= 15 else roi[skin_mask > 0]
    return np.median(pts, axis=0).astype(np.float32)


def _censor_fluid_region(roi, fluid_mask, skin_mask):
    """
    Censors cum/fluid strictly on its own contour:
    - Changes its color to match surrounding skin tone + applies localized smooth blur.
    - Leaves surrounding pixels 100% untouched.
    """
    if np.count_nonzero(fluid_mask) < 18:
        return roi

    skin_bgr = _sample_surrounding_skin_color(roi, skin_mask)
    h, w = roi.shape[:2]

    # Recolor fluid toward surrounding skin tone while keeping subtle shading
    roi_f = roi.astype(np.float32)
    lum = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY).astype(np.float32) / 255.0
    lum_scale = np.expand_dims(np.clip(0.82 + 0.22 * lum, 0.75, 1.05), axis=-1)
    recolored = np.clip(skin_bgr.reshape(1, 1, 3) * lum_scale, 0, 255).astype(np.uint8)

    # Smooth localized blur on the recolored fluid
    k = max(11, (min(w, h) // 4) | 1)
    recolored_blur = cv2.GaussianBlur(recolored, (k, k), 0)

    # Tight curved alpha only on the fluid contour (no surrounding blur)
    k_dil = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    tight_fluid = cv2.dilate(fluid_mask, k_dil, iterations=1)
    ks = max(7, (min(w, h) // 7) | 1)
    alpha = cv2.GaussianBlur(tight_fluid.astype(np.float32) / 255.0, (ks, ks), 0)
    alpha = np.expand_dims(np.clip(alpha, 0.0, 1.0), axis=-1)

    return np.uint8(roi_f * (1.0 - alpha) + recolored_blur.astype(np.float32) * alpha)


def _redraw_clean_skin(roi, skin_mask, cloth_guard, style):
    """
    Redraws exposed nudity (nipples, areola, genital details) using surrounding skin colors:
    1. Detects anatomical detail pixels (pink/red/brown nipple/areola, dark creases) inside bare skin.
    2. Uses Telea Inpainting + Surrounding Skin Color Gradient to redraw them cleanly.
    3. Applies smooth curved contour blending so cloth is never touched and no boxy edges appear.
    """
    h, w = roi.shape[:2]
    if np.count_nonzero(skin_mask) < 25:
        return roi

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    skin_bgr = _sample_surrounding_skin_color(roi, skin_mask)

    # Identify inner anatomical details (nipple/areola saturation or darker skin creases) inside bare skin
    detail_sat = cv2.inRange(hsv, np.array([0, 42, 45], dtype=np.uint8), np.array([25, 255, 235], dtype=np.uint8))
    detail_pink = cv2.inRange(hsv, np.array([155, 30, 50], dtype=np.uint8), np.array([180, 255, 240], dtype=np.uint8))
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    lap = cv2.convertScaleAbs(cv2.Laplacian(gray, cv2.CV_16S, ksize=3))
    detail_edge = cv2.threshold(lap, 18, 255, cv2.THRESH_BINARY)[1]

    detail_mask = cv2.bitwise_or(cv2.bitwise_or(detail_sat, detail_pink), detail_edge)
    detail_mask = cv2.bitwise_and(detail_mask, skin_mask)
    k_det = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    detail_mask = cv2.dilate(detail_mask, k_det, iterations=2)
    detail_mask = cv2.bitwise_and(detail_mask, skin_mask)

    # Step 1: Inpaint anatomical details using surrounding bare skin pixels
    if np.count_nonzero(detail_mask) > 10:
        inpainted = cv2.inpaint(roi, detail_mask, inpaintRadius=6, flags=cv2.INPAINT_TELEA)
    else:
        inpainted = roi.copy()

    # Step 2: Reconstruct smooth skin surface using surrounding skin color + soft shading
    k_blur = max(17, (min(w, h) // 2) | 1)
    smooth_base = cv2.GaussianBlur(inpainted, (k_blur, k_blur), 0)
    smooth_base = cv2.GaussianBlur(smooth_base, (k_blur, k_blur), 0)

    if style == "blend":
        # Clean redraw: blend smooth inpainted gradient with surrounding skin tone (barbie-clean finish)
        color_fill = np.full_like(roi, skin_bgr.astype(np.uint8))
        target = cv2.addWeighted(smooth_base, 0.65, color_fill, 0.35, 0)
    elif style == "blur":
        # Smooth skin-only blur (reinforced with inpainted base so dark nipples don't show through)
        target = cv2.addWeighted(smooth_base, 0.85, np.full_like(roi, skin_bgr.astype(np.uint8)), 0.15, 0)
    else:
        # Mosaic strictly inside curved skin contour
        cell = max(6, min(w, h) // max(2, CENSOR_CELLS))
        small = cv2.resize(inpainted, (max(1, w // cell), max(1, h // cell)), interpolation=cv2.INTER_AREA)
        target = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)

    alpha = _smooth_curved_alpha(skin_mask, cloth_guard)
    return np.uint8(roi.astype(np.float32) * (1.0 - alpha) + target.astype(np.float32) * alpha)


# ---------------------------------------------------------------------------
# Detectors (Strict on Exposed, Relaxed on Bra/Underwear unless Heavily Exposed)
# ---------------------------------------------------------------------------
def _is_heavily_exposed_skin(frame, box):
    """Checks if a covered detection (like bra/cleavage) actually has >55% bare exposed skin."""
    H, W = frame.shape[:2]
    x, y, w, h = box
    x0, y0 = max(0, int(x)), max(0, int(y))
    x1, y1 = min(W, int(x + w)), min(H, int(y + h))
    if x1 - x0 < 8 or y1 - y0 < 8:
        return False
    roi = frame[y0:y1, x0:x1]
    skin_mask, _ = _extract_skin_and_cloth_masks(roi)
    ratio = float(np.count_nonzero(skin_mask)) / float(skin_mask.size)
    return ratio >= CENSOR_COVERED_MIN_SKIN_RATIO


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
        out.append((str(label).upper(), float(score), (x0, y0, x1 - x0, y1 - y0)))
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
        if not box:
            continue
        x, y, w, h = [int(v) for v in box[:4]]

        # Skip normal panties/underwear/covered buttocks/belly
        if any(k in label for k in SKIP_UNDERWEAR_KEYWORDS):
            continue

        # Exposed nudity or cum/fluid -> always detect
        if label in NUDENET_EXPOSED or any(k in label for k in EXPOSED_KEYWORDS):
            if score >= CENSOR_NUDENET_SCORE:
                out.append((label, score, (x, y, w, h)))
            continue

        # Covered breasts / bra -> ONLY censor if score is high AND boobs are heavily exposed
        if CENSOR_STRICT >= 2 and (label in NUDENET_COVERED or any(k in label for k in COVERED_BOOB_KEYWORDS)):
            if score >= CENSOR_COVERED_SCORE and _is_heavily_exposed_skin(frame, (x, y, w, h)):
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


def _merge(boxes, thr=0.28):
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
    """
    Applies clean, non-boxy censoring to each region:
    1. Isolates bare skin and protects all clothing (bra/panties/cloth never blurred).
    2. Isolates any cum/fluid in the region and recolors + blurs ONLY the fluid contour.
    3. Redraws/blurs exposed bare skin using surrounding skin colors along a smooth curve.
    """
    style = _style(style)
    for x, y, w, h in boxes:
        if w < 4 or h < 4:
            continue
        roi = frame[y:y + h, x:x + w]
        if roi.size == 0:
            continue

        skin_mask, cloth_guard = _extract_skin_and_cloth_masks(roi)
        fluid_mask = _extract_fluid_mask(roi, skin_mask)

        # 1. Recolor & locally blur cum/fluid strictly on its own contour
        if np.count_nonzero(fluid_mask) >= 18:
            roi = _censor_fluid_region(roi, fluid_mask, skin_mask)

        # 2. Redraw / blur exposed bare skin while keeping clothing 100% untouched
        if np.count_nonzero(skin_mask) >= 25:
            roi = _redraw_clean_skin(roi, skin_mask, cloth_guard, style)

        frame[y:y + h, x:x + w] = roi


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
