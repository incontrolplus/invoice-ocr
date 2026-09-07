"""Image preprocessing, binarization, deskewing, and enhancement algorithms."""
from __future__ import annotations

import logging
import math
import warnings
from typing import Any

import cv2
import numpy as np
from PIL import Image
import pytesseract
from pytesseract import Output

from .models import PageImage, PageTransform

logger = logging.getLogger("invoice_ocr")

def to_grayscale(img: np.ndarray) -> np.ndarray:
    """Convert to grayscale if the image has colour channels."""
    if len(img.shape) == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img.copy()


def detect_orientation(img: np.ndarray, min_conf: float = 5.0) -> int:
    """Detect rotation needed to make image upright (0, 90, 180, 270).

    Uses pytesseract.image_to_osd with output_type=Output.DICT.
    Catches pytesseract.TesseractError and all exceptions gracefully,
    returning 0 on error or if orientation_conf < min_conf.
    """
    if img is None or img.size == 0:
        return 0
    try:
        # Downscale large images (e.g. 2480x3508) for OSD to reduce CPU overhead by ~3x
        max_dim = max(img.shape[:2])
        if max_dim > 1600:
            scale = 1600.0 / max_dim
            osd_img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        else:
            osd_img = img

        data = pytesseract.image_to_osd(osd_img, output_type=Output.DICT)
        rotate_deg = int(data.get("rotate", 0))
        conf = float(data.get("orientation_conf", 0.0))
        if conf >= min_conf and rotate_deg in (90, 180, 270):
            return rotate_deg
    except pytesseract.TesseractError as exc:
        logger.debug("OSD skipped (insufficient text or unreadable): %s", exc)
    except Exception as exc:
        logger.warning("Unexpected error during OSD orientation detection: %s", exc)
    return 0


def apply_orientation(img: np.ndarray, rotate_deg: int) -> np.ndarray:
    """Apply 90/180/270° rotation using OpenCV."""
    if rotate_deg == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    elif rotate_deg == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    elif rotate_deg == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


def check_and_fix_orientation(img: np.ndarray) -> np.ndarray:
    """Detect and correct 90/180/270° rotation, returning upright image.

    Falls back to original image if OSD fails or confidence is low.
    """
    rot = detect_orientation(img)
    if rot != 0:
        logger.info("Corrected orientation by %d°", rot)
        return apply_orientation(img, rot)
    return img


def upscale_if_needed(img: np.ndarray, min_height: int = 2000) -> np.ndarray:
    """Upscale image if its height is below *min_height*."""
    h = img.shape[0]
    if h < min_height:
        scale = min_height / h
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        logger.info("Upscaled image by %.2fx to %dx%d", scale, img.shape[1], img.shape[0])
    return img


def denoise_bilateral(
    img: np.ndarray,
    d: int = 5,
    sigma_color: float = 50.0,
    sigma_space: float = 50.0,
) -> np.ndarray:
    """Apply Cyrillic-safe edge-preserving bilateral filtering to suppress background noise.

    Preserves fine character edges, Cyrillic diacritics ('й', 'Й', 'ѝ'), dots, and decimal commas.
    """
    if img is None or img.size == 0:
        return img
    gray = img if len(img.shape) == 2 else to_grayscale(img)
    return cv2.bilateralFilter(gray, d=d, sigmaColor=sigma_color, sigmaSpace=sigma_space)


def denoise(img: np.ndarray, strength: int = 10) -> np.ndarray:
    """Edge-preserving denoising (backward-compatible wrapper)."""
    return denoise_bilateral(img)


def enhance_contrast_clahe(
    img: np.ndarray,
    clip_limit: float = 2.0,
    tile_grid_size: tuple[int, int] = (8, 8),
) -> np.ndarray:
    """Apply CLAHE contrast enhancement on CIELAB L* luminance channel for BGR (or directly for grayscale).

    Prevents chromatic distortion and color fringes.
    """
    if img is None or img.size == 0:
        return img

    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=tile_grid_size)

    # 1-channel Grayscale
    if len(img.shape) == 2:
        return clahe.apply(img)

    # 3-channel BGR
    if len(img.shape) == 3 and img.shape[2] == 3:
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        cl = clahe.apply(l)
        return cv2.cvtColor(cv2.merge((cl, a, b)), cv2.COLOR_LAB2BGR)

    # 4-channel BGRA
    if len(img.shape) == 3 and img.shape[2] == 4:
        bgr = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
        return enhance_contrast_clahe(bgr, clip_limit=clip_limit, tile_grid_size=tile_grid_size)

    return img


def enhance_contrast(img: np.ndarray) -> np.ndarray:
    """Apply CLAHE contrast-limited adaptive histogram equalisation."""
    return enhance_contrast_clahe(img)


def adaptive_threshold(img: np.ndarray) -> np.ndarray:
    """Adaptive Gaussian thresholding."""
    gray = img if len(img.shape) == 2 else to_grayscale(img)
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2,
    )


def binarize_otsu(img: np.ndarray) -> np.ndarray:
    """Otsu's global binarisation."""
    if img is None or img.size == 0:
        return img
    gray = img if len(img.shape) == 2 else to_grayscale(img)
    return cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]


def global_threshold(img: np.ndarray) -> np.ndarray:
    """Otsu's binarisation (backward-compatible alias)."""
    return binarize_otsu(img)


def detect_deskew_angle(
    img: np.ndarray,
    max_angle: float = 15.0,
    min_angle: float = 0.2,
) -> float:
    """Detect skew angle using contour-filtered text-line detection.

    Steps:
    1. Grayscale + Otsu threshold on inverted image.
    2. Horizontal morphological dilation to bridge character gaps into line strips.
    3. Find contours; filter for valid text lines (width >= 50, aspect_ratio >= 2.5).
    4. Extract minAreaRect angle for each line and calculate the median angle.
    5. Guard against 90° flips: reject if high variance (std > 4.0°) or contours < 5.
    6. Clamp angle strictly within [-max_angle, max_angle]; return 0.0 if abs(angle) < min_angle.
    """
    if img is None or img.size == 0:
        return 0.0

    try:
        gray = img if len(img.shape) == 2 else to_grayscale(img)
        h, w = gray.shape[:2]
        if h < 20 or w < 20:
            return 0.0

        # Invert so text is foreground
        inverted = cv2.bitwise_not(gray)
        thresh = cv2.threshold(inverted, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]

        # Horizontal dilation to connect characters within lines
        kernel_w = max(15, int(w * 0.01))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, 3))
        dilated = cv2.dilate(thresh, kernel, iterations=1)

        contours, _ = cv2.findContours(dilated, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        angles: list[float] = []

        min_w = max(50, int(w * 0.03))
        max_w = int(w * 0.95)
        min_h = max(5, int(h * 0.003))
        max_h = max(60, int(h * 0.04))

        for cnt in contours:
            if len(cnt) < 5:
                continue

            bx, by, bw, bh = cv2.boundingRect(cnt)
            # If the contour's axis-aligned bounding box is predominantly vertical,
            # it represents a vertical structure (e.g. table border) or a vertical
            # text line resulting from near-90° tilt. Discard from horizontal deskew.
            if bh > bw and (bh / max(1, bw)) >= 1.5:
                continue

            (cx, cy), (rw, rh), r_angle = cv2.minAreaRect(cnt)
            if rw >= rh:
                long_len, short_len = rw, rh
                line_angle = r_angle
            else:
                long_len, short_len = rh, rw
                line_angle = r_angle + 90.0

            while line_angle > 90.0:
                line_angle -= 180.0
            while line_angle < -90.0:
                line_angle += 180.0

            # Only consider contours that are horizontal line-like (|angle| <= 45°)
            if abs(line_angle) > 45.0:
                continue

            if long_len >= min_w and long_len <= max_w and min_h <= short_len <= max_h and (long_len / max(1.0, short_len)) >= 2.5:
                angles.append(line_angle)

        if len(angles) < 5:
            return 0.0

        arr = np.array(angles)
        if float(np.std(arr)) > 4.0:
            # High angular dispersion indicates inconsistent line directions
            return 0.0

        med = float(np.median(arr))
        if abs(med) < min_angle or abs(med) > max_angle:
            return 0.0
        return med
    except Exception as exc:
        logger.warning("Deskew angle detection failed: %s", exc)
        return 0.0


def apply_deskew(
    img: np.ndarray,
    angle_deg: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rotate image by angle_deg with white background border filling.

    Returns (deskewed_image, affine_matrix_2x3, inverse_affine_matrix_2x3).
    """
    h, w = img.shape[:2]
    if abs(angle_deg) < 1e-4:
        identity = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]], dtype=np.float32)
        return img, identity, identity

    center = (w / 2.0, h / 2.0)
    M = cv2.getRotationMatrix2D(center, angle_deg, 1.0)
    M_inv = cv2.invertAffineTransform(M)
    border_val = (255, 255, 255) if len(img.shape) == 3 else 255

    rotated = cv2.warpAffine(
        img, M, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border_val,
    )
    return rotated, M, M_inv


def deskew_image(img: np.ndarray, angle: float | None = None) -> np.ndarray:
    """Deskew image by detected or provided angle.

    Uses cv2.getRotationMatrix2D and cv2.warpAffine with cv2.BORDER_CONSTANT
    and borderValue=(255, 255, 255).
    """
    if img is None or img.size == 0:
        return img
    if angle is None:
        angle = detect_deskew_angle(img)
    if abs(angle) < 1e-4:
        return img
    rotated, _, _ = apply_deskew(img, angle)
    logger.info("Deskewed image by %.2f°", angle)
    return rotated


def normalize_page_geometry(page: PageImage) -> tuple[PageImage, PageTransform]:
    """Execute 2-stage geometry normalization (OSD + deskew) on a PageImage.

    Normalizes orientation and skew once per page before generating variants.
    """
    orig_h, orig_w = page.image.shape[:2]
    transform = PageTransform(
        page_number=page.page_number,
        original_width=orig_w,
        original_height=orig_h,
        normalized_width=orig_w,
        normalized_height=orig_h,
    )

    work_img = page.image

    # Stage 1: OSD Orientation Correction
    rot_needed = detect_orientation(work_img)
    if rot_needed in (90, 180, 270):
        work_img = apply_orientation(work_img, rot_needed)
        transform.orientation_rotate_deg = rot_needed
        logger.info("Page %d: Corrected orientation by %d°", page.page_number, rot_needed)

    # Stage 2: Contour-Based Deskewing
    skew_angle = detect_deskew_angle(work_img)
    if abs(skew_angle) >= 0.2:
        work_img, M, M_inv = apply_deskew(work_img, skew_angle)
        transform.deskew_angle_deg = skew_angle
        transform.affine_matrix = M
        transform.inv_affine_matrix = M_inv
        logger.info("Page %d: Deskewed by %.2f°", page.page_number, skew_angle)

    norm_h, norm_w = work_img.shape[:2]
    transform.normalized_width = norm_w
    transform.normalized_height = norm_h

    normalized_page = PageImage(
        page_number=page.page_number,
        image=work_img,
        width=norm_w,
        height=norm_h,
    )
    return normalized_page, transform


def morphological_cleanup(img: np.ndarray) -> np.ndarray:
    """Deprecated: Removed to prevent eroding black text and corrupting decimal commas.

    Returns the image unchanged.
    """
    warnings.warn(
        "morphological_cleanup is deprecated and returns image unchanged. It will be removed in a future release.",
        DeprecationWarning,
        stacklevel=2,
    )
    return img


def sauvola_threshold(
    gray: np.ndarray,
    window_size: int = 31,
    k: float = 0.12,
    r: float = 128.0,
) -> np.ndarray:
    """Sauvola adaptive thresholding for degraded and watermarked documents.

    T = m * (1 + k * (s / r - 1))
    Where m is local mean, s is local standard deviation, r is dynamic range of std (128 for 8-bit),
    and k is a control parameter (typically 0.1 - 0.2).
    Implemented via OpenCV boxFilter for O(1) per-pixel speed.
    """
    gray_img = gray if len(gray.shape) == 2 else to_grayscale(gray)
    gray_f = gray_img.astype(np.float32)
    mean = cv2.boxFilter(gray_f, ddepth=-1, ksize=(window_size, window_size), borderType=cv2.BORDER_REFLECT)
    sq_mean = cv2.boxFilter(gray_f * gray_f, ddepth=-1, ksize=(window_size, window_size), borderType=cv2.BORDER_REFLECT)
    variance = np.maximum(sq_mean - mean * mean, 0)
    std = np.sqrt(variance)
    threshold = mean * (1.0 + k * ((std / r) - 1.0))
    return np.where(gray_f < threshold, 0, 255).astype(np.uint8)


def binarize_dotmatrix_bridged(gray: np.ndarray, window_size: int = 31, k: float = 0.12, threshold: int = 224) -> np.ndarray:
    """Specialized binarization for dot-matrix printer text on pre-printed watermarked forms.

    Applies Sauvola local variance thresholding to cleanly remove background watermarks,
    then executes directional horizontal closing (1x2 kernel) to bridge 9-pin/24-pin printer
    dots without bleeding into decimal points or commas.
    """
    gray_img = gray if len(gray.shape) == 2 else to_grayscale(gray)
    sauvola = sauvola_threshold(gray_img, window_size=window_size, k=k)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 2))
    bridged = cv2.morphologyEx(sauvola, cv2.MORPH_CLOSE, kernel)
    return bridged


def binarize_dotmatrix(gray: np.ndarray, threshold: int = 224) -> np.ndarray:
    """Deprecated: Alias to binarize_dotmatrix_bridged for backward compatibility."""
    warnings.warn(
        "binarize_dotmatrix is deprecated; use binarize_dotmatrix_bridged instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    bin_img = np.where(gray < threshold, 0, 255).astype(np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    bridged = cv2.erode(bin_img, kernel, iterations=1)
    return bridged



def generate_preprocessing_variants(raw_img: np.ndarray) -> list[tuple[str, np.ndarray]]:
    """Generate complementary image preprocessing variants for multi-pass OCR.

    Produces fast, high-quality variants:
        1. 'minimal'                  — Grayscale only (preserves sharp vector/high-res text)
        2. 'standard'                 — CIELAB L* CLAHE grayscale (continuous tones for LSTM)
        3. 'enhanced_otsu'            — CLAHE + Bilateral Denoise + Otsu Binarization
        4. 'dotmatrix_bridged'        — Dot-matrix pin bridging + watermark removal
        5. 'mobile_shadow_attenuated' — Illumination normalization + shadow removal + dot bridging
    """
    gray_base = raw_img if len(raw_img.shape) == 2 else to_grayscale(raw_img)
    variants: list[tuple[str, np.ndarray]] = []

    # 1. Minimal (Clean Grayscale)
    variants.append(("minimal", gray_base))

    # 2. Standard / CLAHE Grayscale (Continuous tones for Tesseract LSTM)
    try:
        clahe_enhanced = enhance_contrast_clahe(raw_img)
        clahe_gray = clahe_enhanced if len(clahe_enhanced.shape) == 2 else to_grayscale(clahe_enhanced)
        variants.append(("standard", clahe_gray))
    except Exception as exc:
        logger.warning("CLAHE grayscale variant failed: %s", exc)
        clahe_gray = gray_base
        variants.append(("standard", gray_base))

    # 3. Enhanced Otsu (CLAHE + Bilateral Denoise + Otsu Binarization)
    try:
        denoised = denoise_bilateral(clahe_gray, d=5, sigma_color=50.0, sigma_space=50.0)
        binary_otsu = binarize_otsu(denoised)
        variants.append(("enhanced_otsu", binary_otsu))
    except Exception as exc:
        logger.warning("Enhanced Otsu variant failed: %s", exc)

    # 4. Dot-matrix pin bridging and watermark removal variant
    try:
        binary_dm = binarize_dotmatrix_bridged(gray_base, threshold=224)
        variants.append(("dotmatrix_bridged", binary_dm))
    except Exception as exc:
        logger.warning("Dot-matrix bridged variant failed: %s", exc)

    return variants




def order_quadrilateral_points(pts: np.ndarray) -> np.ndarray:
    """Order quadrilateral points: top-left, top-right, bottom-right, bottom-left."""
    rect = np.zeros((4, 2), dtype="float32")
    pts_arr = pts.reshape(4, 2).astype("float32")
    s = pts_arr.sum(axis=1)
    rect[0] = pts_arr[np.argmin(s)]
    rect[2] = pts_arr[np.argmax(s)]

    diff = np.diff(pts_arr, axis=1)
    rect[1] = pts_arr[np.argmin(diff)]
    rect[3] = pts_arr[np.argmax(diff)]

    return rect


def _is_valid_quadrilateral(pts: np.ndarray) -> bool:
    """Check that 4 points form a plausible rectangular document (angles between ~45 and ~135 degrees)."""
    for i in range(4):
        p_prev = pts[(i - 1) % 4]
        p_curr = pts[i]
        p_next = pts[(i + 1) % 4]
        v1 = p_prev - p_curr
        v2 = p_next - p_curr
        norm1 = float(np.linalg.norm(v1))
        norm2 = float(np.linalg.norm(v2))
        if norm1 < 1e-3 or norm2 < 1e-3:
            return False
        cos_theta = float(np.dot(v1, v2)) / (norm1 * norm2)
        if abs(cos_theta) > 0.70:
            return False
    return True


def _is_image_border(pts: np.ndarray, w: int, h: int, margin: float = 0.02) -> bool:
    """Check if quadrilateral points simply represent the outer image boundary."""
    margin_x = w * margin
    margin_y = h * margin
    on_border = 0
    for pt in pts:
        x, y = pt[0], pt[1]
        if (x <= margin_x or x >= w - margin_x) and (y <= margin_y or y >= h - margin_y):
            on_border += 1
    return on_border >= 3


def detect_document_quadrilateral(
    img: np.ndarray,
    min_area_ratio: float = 0.15,
    max_area_ratio: float = 0.98,
) -> np.ndarray | None:
    """Detect the 4 corners of a physical document page in an image (e.g. mobile photo).

    Returns ordered 4x2 float32 array [[tl_x, tl_y], [tr_x, tr_y], [br_x, br_y], [bl_x, bl_y]]
    or None if no prominent quadrilateral is found.
    """
    h, w = img.shape[:2]
    proc_dim = 1000
    scale = 1.0
    if max(h, w) > proc_dim:
        scale = float(proc_dim) / float(max(h, w))
        small = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    else:
        small = img

    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if len(small.shape) == 3 else small
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)

    # Multi-strategy contour search: Canny edges followed by Otsu threshold
    edges = cv2.Canny(blurred, 50, 150)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    edges = cv2.dilate(edges, kernel, iterations=1)
    edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        thresh_edges = cv2.Canny(thresh, 50, 150)
        contours, _ = cv2.findContours(thresh_edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates: list[tuple[float, np.ndarray]] = []
    scaled_total = float(small.shape[0] * small.shape[1])

    for cnt in contours:
        area = cv2.contourArea(cnt)
        area_ratio = area / scaled_total
        if area_ratio < min_area_ratio or area_ratio > max_area_ratio:
            continue

        peri = cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, 0.02 * peri, True)

        if len(approx) == 4 and cv2.isContourConvex(approx):
            candidates.append((area, approx))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0], reverse=True)
    best_poly = candidates[0][1]

    poly_orig = (best_poly.reshape(4, 2).astype("float32")) / scale
    ordered = order_quadrilateral_points(poly_orig)

    if not _is_valid_quadrilateral(ordered):
        return None
    if _is_image_border(ordered, w, h):
        return None

    return ordered


def four_point_perspective_transform(
    img: np.ndarray,
    pts: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Perform 4-point perspective warp on img given 4 quadrilateral corners.

    Returns (warped_img, M, M_inv).
    """
    rect = order_quadrilateral_points(pts)
    tl, tr, br, bl = rect

    width_a = np.linalg.norm(br - bl)
    width_b = np.linalg.norm(tr - tl)
    max_width = max(int(width_a), int(width_b))

    height_a = np.linalg.norm(tr - br)
    height_b = np.linalg.norm(tl - bl)
    max_height = max(int(height_a), int(height_b))

    dst = np.array([
        [0, 0],
        [max_width - 1, 0],
        [max_width - 1, max_height - 1],
        [0, max_height - 1]
    ], dtype="float32")

    M = cv2.getPerspectiveTransform(rect, dst)
    M_inv = cv2.getPerspectiveTransform(dst, rect)

    warped = cv2.warpPerspective(img, M, (max_width, max_height), flags=cv2.INTER_LANCZOS4)
    return warped, M, M_inv


def transform_bbox_perspective(
    bbox: tuple[int, int, int, int],
    M_inv: np.ndarray,
) -> tuple[int, int, int, int]:
    """Transform bounding box (x, y, w, h) from warped image coordinates
    back to original image coordinates using inverse homography matrix M_inv.
    """
    x, y, w, h = bbox
    corners = np.array([
        [x, y],
        [x + w, y],
        [x + w, y + h],
        [x, y + h]
    ], dtype="float32").reshape(-1, 1, 2)

    orig_pts = cv2.perspectiveTransform(corners, M_inv).reshape(-1, 2)
    min_x = int(np.floor(np.min(orig_pts[:, 0])))
    min_y = int(np.floor(np.min(orig_pts[:, 1])))
    max_x = int(np.ceil(np.max(orig_pts[:, 0])))
    max_y = int(np.ceil(np.max(orig_pts[:, 1])))
    return (max(0, min_x), max(0, min_y), max(1, max_x - min_x), max(1, max_y - min_y))


def preprocess_mobile_photo(
    img: np.ndarray,
    target_dim: int = 2400,
    apply_perspective: bool = True,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Comprehensive preprocessing pipeline for real-world mobile camera photos.
    
    Transforms degraded mobile photos (perspective skew, low resolution, shadows,
    dot-matrix gaps) into high-contrast, rectified, binarized text ready for OCR.
    """
    orig_h, orig_w = img.shape[:2]
    meta: dict[str, Any] = {
        "original_shape": (orig_w, orig_h),
        "perspective_warped": False,
        "upscaled": False,
        "shadow_removed": True,
        "dot_matrix_bridged": True,
    }

    work_img = img

    # Stage 0: 4-Point Document Quadrilateral Perspective Rectification
    if apply_perspective:
        corners = detect_document_quadrilateral(work_img)
        if corners is not None:
            warped, M, M_inv = four_point_perspective_transform(work_img, corners)
            work_img = warped
            meta["perspective_warped"] = True
            meta["homography_matrix"] = M.tolist()
            meta["inverse_homography_matrix"] = M_inv.tolist()
            meta["detected_corners"] = corners.tolist()
            meta["warped_shape"] = (warped.shape[1], warped.shape[0])

    # Stage 1: Smart High-Fidelity Upscaling
    cur_h, cur_w = work_img.shape[:2]
    if max(cur_h, cur_w) < 2200:
        scale = float(target_dim) / float(max(cur_h, cur_w))
        new_w = int(cur_w * scale)
        new_h = int(cur_h * scale)
        work_img = cv2.resize(work_img, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
        meta["upscaled"] = True
        meta["scale_factor"] = scale
        meta["new_shape"] = (new_w, new_h)

    # Stage 2: Illumination Normalization & Shadow Attenuation
    gray = cv2.cvtColor(work_img, cv2.COLOR_BGR2GRAY) if len(work_img.shape) == 3 else work_img
    ksize = int(max(work_img.shape[:2]) * 0.015) | 1
    dilated = cv2.morphologyEx(gray, cv2.MORPH_DILATE, cv2.getStructuringElement(cv2.MORPH_RECT, (ksize, ksize)))
    bg_diff = 255 - cv2.absdiff(gray, dilated)
    norm = cv2.normalize(bg_diff, None, 0, 255, cv2.NORM_MINMAX, dtype=cv2.CV_8U)

    # Stage 3: Sauvola Adaptive Binarization
    sauv = sauvola_threshold(norm, window_size=31, k=0.15)

    # Stage 4: Horizontal Dot-Matrix Pin Bridging
    kernel_close = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    bridged = 255 - cv2.morphologyEx(255 - sauv, cv2.MORPH_CLOSE, kernel_close)

    return bridged, meta

