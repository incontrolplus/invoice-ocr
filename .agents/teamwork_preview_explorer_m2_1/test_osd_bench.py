#!/usr/bin/env python3
import sys
import time
import cv2
import numpy as np
import pytesseract
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from invoice_ocr import load_document

def test_osd_on_invoice():
    pdf_path = Path("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf")
    pages = load_document(pdf_path)
    img = pages[0].image
    h, w = img.shape[:2]
    print(f"Original image size: {w}x{h}, shape: {img.shape}")

    # Test raw pytesseract.image_to_osd
    print("\n--- Test raw image_to_osd (string output) ---")
    t0 = time.perf_counter()
    raw_osd_str = pytesseract.image_to_osd(img)
    t1 = time.perf_counter()
    print(f"Time: {t1-t0:.3f}s")
    print("String output:\n", raw_osd_str)

    print("\n--- Test image_to_osd with Output.DICT ---")
    t0 = time.perf_counter()
    osd_dict = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
    t1 = time.perf_counter()
    print(f"Time: {t1-t0:.3f}s")
    print("Dict output:\n", osd_dict)

    # Test with downscaled versions
    print("\n--- Test image_to_osd on downscaled images ---")
    for max_dim in [1500, 1000, 800]:
        scale = max_dim / max(h, w)
        small = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        try:
            small_dict = pytesseract.image_to_osd(small, output_type=pytesseract.Output.DICT)
            t1 = time.perf_counter()
            print(f"Downscaled to {small.shape[1]}x{small.shape[0]} (scale={scale:.2f}): time={t1-t0:.3f}s, rotate={small_dict.get('rotate')}, conf={small_dict.get('orientation_conf')}")
        except Exception as exc:
            t1 = time.perf_counter()
            print(f"Downscaled to {small.shape[1]}x{small.shape[0]} (scale={scale:.2f}): failed in {t1-t0:.3f}s: {exc}")

    # Test rotations: 90, 180, 270
    rotations = [
        ("90_CW", cv2.ROTATE_90_CLOCKWISE, 90),
        ("180", cv2.ROTATE_180, 180),
        ("270_CW (90_CCW)", cv2.ROTATE_90_COUNTERCLOCKWISE, 270)
    ]
    print("\n--- Test Rotations on Full Image vs Downscaled ---")
    for name, rot_code, expected_angle in rotations:
        rot_img = cv2.rotate(img, rot_code)
        # full
        t0 = time.perf_counter()
        try:
            res_full = pytesseract.image_to_osd(rot_img, output_type=pytesseract.Output.DICT)
            t_full = time.perf_counter() - t0
            full_str = f"rotate={res_full.get('rotate')}, conf={res_full.get('orientation_conf')}, time={t_full:.3f}s"
        except Exception as exc:
            t_full = time.perf_counter() - t0
            full_str = f"failed in {t_full:.3f}s: {exc}"

        # downscaled (max dim 1500)
        scale = 1500.0 / max(rot_img.shape[:2])
        small_rot = cv2.resize(rot_img, (int(rot_img.shape[1] * scale), int(rot_img.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        t0 = time.perf_counter()
        try:
            res_small = pytesseract.image_to_osd(small_rot, output_type=pytesseract.Output.DICT)
            t_small = time.perf_counter() - t0
            small_str = f"rotate={res_small.get('rotate')}, conf={res_small.get('orientation_conf')}, time={t_small:.3f}s"
        except Exception as exc:
            t_small = time.perf_counter() - t0
            small_str = f"failed in {t_small:.3f}s: {exc}"

        print(f"Rotated {name} (expected {expected_angle}°):")
        print(f"  Full ({rot_img.shape[1]}x{rot_img.shape[0]}): {full_str}")
        print(f"  Downscaled ({small_rot.shape[1]}x{small_rot.shape[0]}): {small_str}")

    # Test failure modes: sparse/blank images
    print("\n--- Test Failure Modes (Blank / Sparse / Noise) ---")
    blank = np.ones((500, 500, 3), dtype=np.uint8) * 255
    try:
        pytesseract.image_to_osd(blank, output_type=pytesseract.Output.DICT)
        print("Blank: Succeeded unexpectedly")
    except Exception as exc:
        print(f"Blank correctly raised: {type(exc).__name__}: {exc}")

    tiny_text = np.ones((100, 200, 3), dtype=np.uint8) * 255
    cv2.putText(tiny_text, "Hi", (10, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
    try:
        pytesseract.image_to_osd(tiny_text, output_type=pytesseract.Output.DICT)
        print("Tiny text: Succeeded unexpectedly")
    except Exception as exc:
        print(f"Tiny text correctly raised: {type(exc).__name__}: {exc}")

if __name__ == "__main__":
    test_osd_on_invoice()
