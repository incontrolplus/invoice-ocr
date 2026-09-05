#!/usr/bin/env python3
"""Benchmark and verify Pixmap to NumPy BGR array conversion strategies."""
import time
import cv2
import fitz
import numpy as np

KAPINA_FILE = "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"

def benchmark_conversions():
    doc = fitz.open(KAPINA_FILE)
    page = doc[0]
    
    # Rasterize at 300 DPI with alpha=False
    t0 = time.perf_counter()
    pix_rgb = page.get_pixmap(dpi=300, alpha=False)
    t_raster = time.perf_counter() - t0
    print(f"Rasterization (300 DPI, alpha=False): {t_raster*1000:.2f} ms | shape: {pix_rgb.width}x{pix_rgb.height}, n={pix_rgb.n}")

    # Method 1: np.frombuffer + cv2.cvtColor(COLOR_RGB2BGR)
    times_m1 = []
    for _ in range(10):
        t0 = time.perf_counter()
        img1 = np.frombuffer(pix_rgb.samples, dtype=np.uint8).reshape((pix_rgb.height, pix_rgb.width, 3))
        bgr1 = cv2.cvtColor(img1, cv2.COLOR_RGB2BGR)
        times_m1.append(time.perf_counter() - t0)
    print(f"Method 1 (np.frombuffer + cv2.cvtColor RGB2BGR): {np.median(times_m1)*1000:.2f} ms (contiguous: {bgr1.flags['C_CONTIGUOUS']})")

    # Method 2: np.frombuffer + slice arr[:, :, ::-1] without copy
    times_m2 = []
    for _ in range(10):
        t0 = time.perf_counter()
        img2 = np.frombuffer(pix_rgb.samples, dtype=np.uint8).reshape((pix_rgb.height, pix_rgb.width, 3))
        bgr2 = img2[:, :, ::-1]
        times_m2.append(time.perf_counter() - t0)
    print(f"Method 2 (np.frombuffer + slice [::-1]): {np.median(times_m2)*1000:.2f} ms (contiguous: {bgr2.flags['C_CONTIGUOUS']})")

    # Method 3: np.frombuffer + slice arr[:, :, ::-1] + np.ascontiguousarray
    times_m3 = []
    for _ in range(10):
        t0 = time.perf_counter()
        img3 = np.frombuffer(pix_rgb.samples, dtype=np.uint8).reshape((pix_rgb.height, pix_rgb.width, 3))
        bgr3 = np.ascontiguousarray(img3[:, :, ::-1])
        times_m3.append(time.perf_counter() - t0)
    print(f"Method 3 (np.frombuffer + slice [::-1] + ascontiguousarray): {np.median(times_m3)*1000:.2f} ms (contiguous: {bgr3.flags['C_CONTIGUOUS']})")

    # Check OpenCV compatibility with non-contiguous array (Method 2)
    try:
        gray_test = cv2.cvtColor(bgr2, cv2.COLOR_BGR2GRAY)
        print("OpenCV cv2.cvtColor works on non-contiguous bgr2: YES")
    except Exception as e:
        print(f"OpenCV cv2.cvtColor works on non-contiguous bgr2: NO ({e})")

    # Verify equivalence
    diff = np.max(np.abs(bgr1.astype(int) - bgr3.astype(int)))
    print(f"Pixel difference between Method 1 and Method 3: {diff} (0 = identical)")

    # Check RGBA handling if alpha=True
    pix_rgba = page.get_pixmap(dpi=300, alpha=True)
    t0 = time.perf_counter()
    img_rgba = np.frombuffer(pix_rgba.samples, dtype=np.uint8).reshape((pix_rgba.height, pix_rgba.width, 4))
    bgr_rgba = cv2.cvtColor(img_rgba, cv2.COLOR_RGBA2BGR)
    t_rgba = time.perf_counter() - t0
    print(f"RGBA handling (alpha=True + COLOR_RGBA2BGR): {t_rgba*1000:.2f} ms | size: {bgr_rgba.shape}")

    # Check Grayscale handling
    pix_gray = page.get_pixmap(dpi=300, colorspace=fitz.csGRAY)
    t0 = time.perf_counter()
    img_gray = np.frombuffer(pix_gray.samples, dtype=np.uint8).reshape((pix_gray.height, pix_gray.width))
    bgr_from_gray = cv2.cvtColor(img_gray, cv2.COLOR_GRAY2BGR)
    t_gray = time.perf_counter() - t0
    print(f"Grayscale handling (csGRAY + COLOR_GRAY2BGR): {t_gray*1000:.2f} ms | size: {bgr_from_gray.shape}")

    doc.close()

if __name__ == "__main__":
    benchmark_conversions()
