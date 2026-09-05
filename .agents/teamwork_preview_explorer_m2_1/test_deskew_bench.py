#!/usr/bin/env python3
import sys
import time
import math
import cv2
import numpy as np
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from invoice_ocr import load_document, to_grayscale

def get_deskew_angle_existing(img: np.ndarray) -> float:
    """The existing algorithm in invoice_ocr.py."""
    work = img if len(img.shape) == 2 else to_grayscale(img)
    inverted = cv2.bitwise_not(work)
    thresh = cv2.threshold(inverted, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    coords = np.column_stack(np.where(thresh > 0))
    if len(coords) < 50:
        return 0.0
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
    return angle

def get_deskew_angle_contour_filtered(img: np.ndarray) -> float:
    """Contour-based deskewing using filtered text-line contours.
    
    1. Grayscale + Otsu threshold.
    2. Morphological horizontal dilate to join characters in lines.
    3. Find contours.
    4. Filter contours: width > 50, aspect_ratio > 3, not spanning entire page.
    5. Compute minAreaRect angle for each valid text-line contour.
    6. Return median angle.
    """
    gray = img if len(img.shape) == 2 else to_grayscale(img)
    h, w = gray.shape[:2]
    # Invert so text is white on black
    thresh = cv2.threshold(cv2.bitwise_not(gray), 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    
    # Dilate horizontally to bridge letter gaps into solid line strips
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 3))
    dilated = cv2.dilate(thresh, kernel, iterations=1)
    
    contours, _ = cv2.findContours(dilated, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    angles = []
    
    for cnt in contours:
        if len(cnt) < 5:
            continue
        # Get rotated rect
        rect = cv2.minAreaRect(cnt)
        (cx, cy), (rw, rh), r_angle = rect
        
        # Ensure rw is the longer dimension
        if rw < rh:
            rw, rh = rh, rw
            r_angle = r_angle + 90.0 if r_angle < 0 else r_angle - 90.0
            
        # Standardize angle to [-45, 45]
        while r_angle > 45.0:
            r_angle -= 90.0
        while r_angle < -45.0:
            r_angle += 90.0
            
        # Filter for text-line like contours:
        # - long enough (rw > 100)
        # - thin enough (rh < 100)
        # - high aspect ratio (rw / rh >= 3.0)
        # - not a huge page border (rw < 0.95 * w, rh < 0.95 * h)
        if rw > 100 and 5 < rh < 80 and (rw / max(1.0, rh)) >= 3.0:
            if rw < 0.95 * w and rh < 0.95 * h:
                angles.append(r_angle)
                
    if len(angles) < 5:
        return 0.0
        
    median_angle = float(np.median(angles))
    return median_angle

def get_deskew_angle_hough(img: np.ndarray) -> float:
    """Hough Line Transform for deskewing.
    
    Detects prominent line segments (table borders, separator lines, text baselines).
    Filters for near-horizontal segments (|angle| <= 15°).
    Returns median angle.
    """
    gray = img if len(img.shape) == 2 else to_grayscale(img)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=100, minLineLength=100, maxLineGap=10)
    if lines is None or len(lines) == 0:
        return 0.0
        
    angles = []
    for line in lines:
        if len(line.shape) == 1:
            x1, y1, x2, y2 = line
        else:
            x1, y1, x2, y2 = line[0]
        dx = x2 - x1
        dy = y2 - y1
        if dx == 0:
            continue
        deg = math.degrees(math.atan2(dy, dx))
        # Normalize to [-90, 90]
        while deg > 90:
            deg -= 180
        while deg < -90:
            deg += 180
        # Only keep near-horizontal lines (|deg| <= 15)
        if abs(deg) <= 15.0:
            angles.append(deg)
            
    if len(angles) < 5:
        return 0.0
    return float(np.median(angles))

def run_skew_benchmarks():
    pdf_path = Path("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf")
    pages = load_document(pdf_path)
    base_img = pages[0].image
    h, w = base_img.shape[:2]
    print(f"Loaded {pdf_path.name}: {w}x{h}")
    
    test_angles = [-10.0, -5.0, -3.0, -1.0, 0.0, 1.0, 3.0, 5.0, 10.0]
    
    print("\n--- Benchmarking Deskewing Methods across Artificial Skews ---")
    print(f"{'Target Skew':<12} | {'Existing':<12} | {'Contour Filtered':<18} | {'Hough Lines':<12}")
    print("-" * 65)
    
    center = (w // 2, h // 2)
    for target in test_angles:
        # Create rotated image: positive angle rotates CCW in standard math
        # cv2.getRotationMatrix2D(center, angle, 1.0) rotates CCW by angle
        M = cv2.getRotationMatrix2D(center, target, 1.0)
        skewed = cv2.warpAffine(base_img, M, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
        
        # Test algorithms
        t0 = time.perf_counter()
        ang_exist = get_deskew_angle_existing(skewed)
        t_exist = time.perf_counter() - t0
        
        t0 = time.perf_counter()
        ang_cnt = get_deskew_angle_contour_filtered(skewed)
        t_cnt = time.perf_counter() - t0
        
        t0 = time.perf_counter()
        ang_hough = get_deskew_angle_hough(skewed)
        t_hough = time.perf_counter() - t0
        
        print(f"{target:>10.1f}° | {ang_exist:>10.2f}° | {ang_cnt:>16.2f}° | {ang_hough:>10.2f}°")

if __name__ == "__main__":
    run_skew_benchmarks()
