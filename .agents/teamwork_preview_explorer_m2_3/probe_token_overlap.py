import json
from pathlib import Path
import cv2
import numpy as np
import pymupdf
import pytesseract
from pytesseract import Output

PDF_PATH = "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"

def compute_iou(box1, box2):
    # box: (left, top, width, height)
    l1, t1, w1, h1 = box1
    r1, b1 = l1 + w1, t1 + h1
    
    l2, t2, w2, h2 = box2
    r2, b2 = l2 + w2, t2 + h2
    
    inter_l = max(l1, l2)
    inter_t = max(t1, t2)
    inter_r = min(r1, r2)
    inter_b = min(b1, b2)
    
    if inter_r <= inter_l or inter_b <= inter_t:
        return 0.0, 0.0, 0.0
    
    inter_area = (inter_r - inter_l) * (inter_b - inter_t)
    area1 = w1 * h1
    area2 = w2 * h2
    union_area = area1 + area2 - inter_area
    
    iou = inter_area / union_area if union_area > 0 else 0.0
    iomin = inter_area / min(area1, area2) if min(area1, area2) > 0 else 0.0
    io1 = inter_area / area1 if area1 > 0 else 0.0
    return iou, iomin, io1

def analyze_overlap():
    doc = pymupdf.open(PDF_PATH)
    page = doc[0]
    pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csRGB, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    doc.close()
    
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    
    # Run Pass 1: PSM 3
    data_psm3 = pytesseract.image_to_data(enhanced, lang='bul', config='--psm 3', output_type=Output.DICT)
    tokens_psm3 = []
    for i in range(len(data_psm3['text'])):
        txt = str(data_psm3['text'][i]).strip()
        conf = float(data_psm3['conf'][i])
        if conf != -1 and txt:
            tokens_psm3.append({
                'text': txt,
                'conf': conf,
                'bbox': (int(data_psm3['left'][i]), int(data_psm3['top'][i]), int(data_psm3['width'][i]), int(data_psm3['height'][i]))
            })
            
    # Run Pass 2: PSM 11
    data_psm11 = pytesseract.image_to_data(enhanced, lang='bul', config='--psm 11', output_type=Output.DICT)
    tokens_psm11 = []
    for i in range(len(data_psm11['text'])):
        txt = str(data_psm11['text'][i]).strip()
        conf = float(data_psm11['conf'][i])
        if conf != -1 and txt:
            tokens_psm11.append({
                'text': txt,
                'conf': conf,
                'bbox': (int(data_psm11['left'][i]), int(data_psm11['top'][i]), int(data_psm11['width'][i]), int(data_psm11['height'][i]))
            })

    print(f"Pass 1 (PSM 3) tokens: {len(tokens_psm3)}")
    print(f"Pass 2 (PSM 11) tokens: {len(tokens_psm11)}")
    
    # Match Pass 1 tokens to Pass 2 tokens
    matched_p2_indices = set()
    overlapping_pairs = []
    
    for idx1, t1 in enumerate(tokens_psm3):
        best_match = None
        best_iou = 0.0
        best_iomin = 0.0
        for idx2, t2 in enumerate(tokens_psm11):
            iou, iomin, _ = compute_iou(t1['bbox'], t2['bbox'])
            if iou > best_iou:
                best_iou = iou
                best_iomin = iomin
                best_match = idx2
        if best_match is not None and (best_iou > 0.3 or best_iomin > 0.5):
            matched_p2_indices.add(best_match)
            t2 = tokens_psm11[best_match]
            overlapping_pairs.append((t1, t2, best_iou, best_iomin))
    
    print(f"Overlapping token pairs found: {len(overlapping_pairs)}")
    
    # Show examples where texts differ between Pass 1 and Pass 2
    diff_text_pairs = [p for p in overlapping_pairs if p[0]['text'] != p[1]['text']]
    print(f"Pairs where text differs: {len(diff_text_pairs)}")
    print("\n--- Sample Diff Text Pairs (Pass 1 vs Pass 2) ---")
    for t1, t2, iou, iomin in diff_text_pairs[:15]:
        print(f"P1 (PSM3, conf={t1['conf']:4.1f}%): '{t1['text']:20s}' vs P2 (PSM11, conf={t2['conf']:4.1f}%): '{t2['text']:20s}' [IoU={iou:.2f}, IoMin={iomin:.2f}]")
        
    # Unmatched tokens in Pass 2 (found ONLY by PSM 11)
    unmatched_p2 = [t for idx, t in enumerate(tokens_psm11) if idx not in matched_p2_indices]
    print(f"\nTokens found ONLY by Pass 2 (PSM 11): {len(unmatched_p2)}")
    print("Sample tokens found ONLY by Pass 2:")
    for t in unmatched_p2[:20]:
        print(f"  P2 only (conf={t['conf']:4.1f}%): '{t['text']}' at bbox={t['bbox']}")

if __name__ == "__main__":
    analyze_overlap()
