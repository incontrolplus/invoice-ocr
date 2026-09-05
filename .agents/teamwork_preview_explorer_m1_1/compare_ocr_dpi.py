#!/usr/bin/env python3
"""Compare OCR output, character count, and confidence between 300 DPI and 400 DPI."""
import time
import cv2
import fitz
import numpy as np
import pytesseract

KAPINA_FILE = "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"

def test_ocr_at_dpi(dpi: int):
    doc = fitz.open(KAPINA_FILE)
    page = doc[0]
    
    # Rasterize
    t0 = time.perf_counter()
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    t_raster = time.perf_counter() - t0
    
    # Run Tesseract OCR with lang="bul" PSM 3
    t1 = time.perf_counter()
    data = pytesseract.image_to_data(bgr, lang="bul", config="--psm 3", output_type=pytesseract.Output.DICT)
    t_ocr = time.perf_counter() - t1
    
    # Analyze tokens
    tokens = []
    confs = []
    text_chars = 0
    for i in range(len(data["text"])):
        text = data["text"][i].strip()
        conf = float(data["conf"][i])
        if text and conf >= 0:
            tokens.append(text)
            confs.append(conf)
            text_chars += len(text)
            
    doc.close()
    
    mean_conf = np.mean(confs) if confs else 0.0
    high_conf_pct = np.mean([c >= 60 for c in confs]) * 100 if confs else 0.0
    
    return {
        "dpi": dpi,
        "raster_ms": t_raster * 1000,
        "ocr_ms": t_ocr * 1000,
        "total_ms": (t_raster + t_ocr) * 1000,
        "token_count": len(tokens),
        "char_count": text_chars,
        "mean_conf": mean_conf,
        "high_conf_pct": high_conf_pct,
        "sample_tokens": tokens[:15],
    }

if __name__ == "__main__":
    print("Testing OCR at 300 DPI vs 400 DPI on капина-01.pdf...")
    res300 = test_ocr_at_dpi(300)
    print(f"[300 DPI] Raster: {res300['raster_ms']:.1f} ms | OCR: {res300['ocr_ms']:.1f} ms | Total: {res300['total_ms']:.1f} ms | "
          f"Tokens: {res300['token_count']} | Chars: {res300['char_count']} | Mean Conf: {res300['mean_conf']:.1f}% | High Conf %: {res300['high_conf_pct']:.1f}%")
    
    res400 = test_ocr_at_dpi(400)
    print(f"[400 DPI] Raster: {res400['raster_ms']:.1f} ms | OCR: {res400['ocr_ms']:.1f} ms | Total: {res400['total_ms']:.1f} ms | "
          f"Tokens: {res400['token_count']} | Chars: {res400['char_count']} | Mean Conf: {res400['mean_conf']:.1f}% | High Conf %: {res400['high_conf_pct']:.1f}%")
