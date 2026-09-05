import time
from pathlib import Path
import cv2
import numpy as np
import pymupdf
import pytesseract
from pytesseract import Output

PDF_PATH = "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"

def benchmark():
    print(f"Reading acceptance file (READ ONLY): {PDF_PATH}")
    doc = pymupdf.open(PDF_PATH)
    page = doc[0]
    pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csRGB, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
    bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    doc.close()
    
    h, w = bgr.shape[:2]
    print(f"Rasterized page at 300 DPI: {w}x{h}, shape={bgr.shape}")
    
    # Preprocessing timing
    t0 = time.perf_counter()
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    t_gray = time.perf_counter() - t0
    print(f"Grayscale conversion: {t_gray*1000:.2f} ms")
    
    # CLAHE
    t0 = time.perf_counter()
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    t_clahe = time.perf_counter() - t0
    print(f"CLAHE enhancement: {t_clahe*1000:.2f} ms")
    
    # Otsu
    t0 = time.perf_counter()
    _, otsu = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    t_otsu = time.perf_counter() - t0
    print(f"Otsu threshold: {t_otsu*1000:.2f} ms")
    
    # FastNlMeans (testing overhead)
    print("Testing cv2.fastNlMeansDenoising overhead...")
    t0 = time.perf_counter()
    # test on small crop or full image
    denoised_crop = cv2.fastNlMeansDenoising(gray[:500, :500], None, 10, 7, 21)
    t_crop = time.perf_counter() - t0
    print(f"fastNlMeans on 500x500 crop took {t_crop*1000:.2f} ms (projected full image ~{t_crop * (w*h/250000):.1f} s!)")

    # Benchmarking OCR PSMs with 'bul' on standard CLAHE image
    variants = [
        ("gray", gray),
        ("clahe", enhanced),
        ("otsu", otsu)
    ]
    
    psms = [3, 6, 11]
    
    results = {}
    
    for v_name, img in variants:
        for psm in psms:
            label = f"{v_name}_psm{psm}"
            t0 = time.perf_counter()
            data = pytesseract.image_to_data(
                img, lang='bul', config=f'--psm {psm}', output_type=Output.DICT
            )
            elapsed = time.perf_counter() - t0
            
            # parse tokens
            tokens = []
            n = len(data.get('text', []))
            for i in range(n):
                text = str(data['text'][i]).strip()
                conf = float(data['conf'][i])
                if conf == -1 or not text:
                    continue
                tokens.append({
                    'text': text,
                    'conf': conf,
                    'bbox': (int(data['left'][i]), int(data['top'][i]), int(data['width'][i]), int(data['height'][i])),
                    'is_low_conf': conf < 60
                })
            
            confs = [t['conf'] for t in tokens]
            mean_conf = np.mean(confs) if confs else 0.0
            low_conf_cnt = sum(1 for t in tokens if t['is_low_conf'])
            char_cnt = sum(len(t['text']) for t in tokens)
            
            results[label] = {
                'elapsed_s': elapsed,
                'token_count': len(tokens),
                'char_count': char_cnt,
                'mean_conf': mean_conf,
                'low_conf_cnt': low_conf_cnt,
                'tokens': tokens
            }
            print(f"OCR {label:15s}: {elapsed:.2f}s | {len(tokens):4d} tokens | {char_cnt:5d} chars | mean_conf: {mean_conf:5.1f}% | low_conf: {low_conf_cnt:3d}")

    return results

if __name__ == "__main__":
    benchmark()
