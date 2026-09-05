#!/usr/bin/env python3
import sys
import time
import math
import cv2
import numpy as np
import pytesseract
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from invoice_ocr import load_document, to_grayscale

def detect_orientation(img: np.ndarray, min_conf: float = 5.0) -> int:
    """Detect rotation needed to make image upright (0, 90, 180, 270).
    
    Returns 0 if upright or if confidence is below min_conf or if OSD fails.
    """
    try:
        data = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
        rotate_deg = int(data.get("rotate", 0))
        conf = float(data.get("orientation_conf", 0.0))
        if conf >= min_conf and rotate_deg in (90, 180, 270):
            return rotate_deg
    except pytesseract.TesseractError:
        pass
    except Exception:
        pass
    return 0

def apply_orientation(img: np.ndarray, rotate_deg: int) -> np.ndarray:
    """Apply 90/180/270 rotation to make image upright."""
    if rotate_deg == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    elif rotate_deg == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    elif rotate_deg == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img

def detect_deskew_angle(img: np.ndarray, max_angle: float = 15.0, min_angle: float = 0.2) -> float:
    """Detect skew angle in degrees using filtered text-line contours.
    
    Safe bounds: [-max_angle, max_angle].
    Ignores skews below min_angle.
    """
    gray = to_grayscale(img)
    h, w = gray.shape[:2]
    
    # Invert so text is foreground
    thresh = cv2.threshold(cv2.bitwise_not(gray), 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    
    # Horizontal morphological bridge
    kernel_w = max(15, int(w * 0.01))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, 3))
    dilated = cv2.dilate(thresh, kernel, iterations=1)
    
    contours, _ = cv2.findContours(dilated, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    angles = []
    
    min_w = max(50, int(w * 0.03))
    max_w = int(w * 0.95)
    min_h = max(5, int(h * 0.003))
    max_h = max(60, int(h * 0.04))
    
    for cnt in contours:
        if len(cnt) < 5:
            continue
        (cx, cy), (rw, rh), r_angle = cv2.minAreaRect(cnt)
        if rw < rh:
            rw, rh = rh, rw
            r_angle = r_angle + 90.0 if r_angle < 0 else r_angle - 90.0
            
        while r_angle > 45.0:
            r_angle -= 90.0
        while r_angle < -45.0:
            r_angle += 90.0
            
        if min_w <= rw <= max_w and min_h <= rh <= max_h and (rw / max(1.0, rh)) >= 2.5:
            angles.append(r_angle)
            
    if len(angles) < 5:
        return 0.0
        
    arr = np.array(angles)
    # Check consistency
    if np.std(arr) > 4.0:
        # High angular dispersion -> unreliable
        return 0.0
        
    med = float(np.median(arr))
    if abs(med) < min_angle or abs(med) > max_angle:
        return 0.0
    return med

def apply_deskew(img: np.ndarray, angle: float) -> tuple[np.ndarray, np.ndarray]:
    """Rotate image by angle (in degrees) with white background padding.
    
    Returns (rotated_image, affine_matrix_2x3).
    """
    if abs(angle) < 1e-4:
        return img, np.eye(2, 3, dtype=np.float32)
        
    h, w = img.shape[:2]
    center = (w / 2.0, h / 2.0)
    # Notice: if text is tilted CCW by angle > 0, we must rotate CW (by -angle) to level it!
    # In cv2.getRotationMatrix2D, positive angle rotates CCW.
    # Therefore, to cancel out a detected skew angle, we rotate by angle or -angle?
    # Let's verify!
    M = cv2.getRotationMatrix2D(center, angle, 1.0)
    border_val = (255, 255, 255) if len(img.shape) == 3 else 255
    rotated = cv2.warpAffine(
        img, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=border_val
    )
    return rotated, M

def test_pipeline():
    pdf_path = Path("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf")
    pages = load_document(pdf_path)
    orig = pages[0].image
    h, w = orig.shape[:2]
    print(f"Original image size: {w}x{h}")
    
    # Synthesize skewed + rotated image:
    # 1. Rotate 90 CW
    # 2. Skew by 3.5 deg
    rot90 = cv2.rotate(orig, cv2.ROTATE_90_CLOCKWISE)
    rh, rw = rot90.shape[:2]
    M_skew = cv2.getRotationMatrix2D((rw / 2.0, rh / 2.0), 3.5, 1.0)
    distorted = cv2.warpAffine(rot90, M_skew, (rw, rh), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
    print(f"Distorted image size: {rw}x{rh}")
    
    # Step 1: Detect and fix OSD orientation
    t0 = time.perf_counter()
    rot_needed = detect_orientation(distorted)
    dt_osd = time.perf_counter() - t0
    print(f"OSD detected rotation: {rot_needed}° (in {dt_osd:.3f}s)")
    assert rot_needed == 270, f"Expected 270, got {rot_needed}"
    
    step1_upright = apply_orientation(distorted, rot_needed)
    print(f"After OSD, size is: {step1_upright.shape[1]}x{step1_upright.shape[0]}")
    
    # Step 2: Detect and fix Deskew
    t0 = time.perf_counter()
    skew_angle = detect_deskew_angle(step1_upright)
    dt_deskew = time.perf_counter() - t0
    print(f"Contour deskew detected angle: {skew_angle:.2f}° (in {dt_deskew:.3f}s)")
    
    step2_deskewed, M_aff = apply_deskew(step1_upright, skew_angle)
    
    # Re-verify final image is upright and deskewed
    final_osd = detect_orientation(step2_deskewed)
    final_skew = detect_deskew_angle(step2_deskewed)
    print(f"Final image: OSD needed={final_osd}°, residual skew={final_skew:.2f}°")
    assert final_osd == 0, f"Expected final OSD=0, got {final_osd}"
    assert abs(final_skew) < 0.2, f"Expected residual skew < 0.2, got {final_skew}"
    print("Pipeline Integration Test PASSED 100%!")

if __name__ == "__main__":
    test_pipeline()
