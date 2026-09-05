import re
from dataclasses import dataclass
from pathlib import Path
import cv2
import numpy as np
import pymupdf
import pytesseract
from pytesseract import Output

BULGARIAN_KEYWORDS = {
    "фактура", "доставчик", "получател", "еик", "ддс", "данъчна", "основа",
    "стойност", "сума", "общо", "бг", "банка", "ибан", "лева", "лв", "евро",
    "eur", "bgn", "място", "издаване", "дата", "оригинал", "клиент", "купувач",
    "продавач", "капина", "плевен", "софия", "телефон", "мол", "адрес", "стока"
}

@dataclass
class Token:
    text: str
    conf: float
    bbox: tuple[int, int, int, int]  # (l, t, w, h)
    pass_id: str
    page_number: int = 1
    is_low_confidence: bool = False

    @property
    def left(self): return self.bbox[0]
    @property
    def top(self): return self.bbox[1]
    @property
    def width(self): return self.bbox[2]
    @property
    def height(self): return self.bbox[3]
    @property
    def right(self): return self.bbox[0] + self.bbox[2]
    @property
    def bottom(self): return self.bbox[1] + self.bbox[3]

def compute_box_metrics(b1, b2):
    l1, t1, w1, h1 = b1
    r1, b1_ = l1 + w1, t1 + h1
    l2, t2, w2, h2 = b2
    r2, b2_ = l2 + w2, t2 + h2
    
    inter_l = max(l1, l2)
    inter_t = max(t1, t2)
    inter_r = min(r1, r2)
    inter_b = min(b1_, b2_)
    
    if inter_r <= inter_l or inter_b <= inter_t:
        return 0.0, 0.0
    
    inter_area = (inter_r - inter_l) * (inter_b - inter_t)
    area1 = w1 * h1
    area2 = w2 * h2
    union_area = area1 + area2 - inter_area
    
    iou = inter_area / union_area if union_area > 0 else 0.0
    iomin = inter_area / min(area1, area2) if min(area1, area2) > 0 else 0.0
    return iou, iomin

def is_line_noise(t: Token) -> bool:
    w, h = t.width, t.height
    if h <= 0 or w <= 0:
        return True
    aspect = w / h
    # Extreme aspect ratio horizontal or vertical
    if aspect > 12 and h <= 6:
        return True
    if aspect < 0.08 and w <= 6:
        return True
    # Pure non-alphanumeric noise of small size
    if not re.search(r'[0-9a-zA-Zа-яА-Я]', t.text):
        if len(t.text) <= 2 and (w <= 8 or h <= 8):
            return True
        if t.conf < 30:
            return True
    # Repetitive character string (e.g. OOOOOOOO)
    if len(t.text) > 10 and len(set(t.text.lower())) <= 3:
        return True
    return False

def score_token(t: Token) -> float:
    # Base confidence [0..100]
    score = float(t.conf)
    
    clean_txt = t.text.strip().lower()
    
    # Length bonus
    score += min(len(clean_txt), 12) * 1.5
    
    # Check valid characters ratio
    valid_chars = sum(1 for c in t.text if c.isalnum() or c in '.,-/%()')
    ratio = valid_chars / len(t.text) if t.text else 0
    if ratio < 0.8:
        score -= 25.0
    
    # Bulgarian statutory / domain keywords
    if any(kw in clean_txt for kw in BULGARIAN_KEYWORDS):
        score += 30.0
        
    # Valid date
    if re.search(r'^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$', t.text):
        score += 25.0
        
    # Valid monetary amount
    if re.search(r'^\d+([.,]\d{2})?$', t.text):
        score += 15.0
        
    # Valid EIK or VAT ID
    if re.search(r'^\d{9}$|^\d{13}$|^BG\d{9,13}$', t.text, re.IGNORECASE):
        score += 25.0
        
    # Spurious quote/edge junk penalty
    if t.text.startswith(('„', '“', '"', "'", '`', '|')):
        score -= 10.0
    if t.text.endswith(('|', '`')):
        score -= 10.0
        
    return score

def fuse_tokens(p1_tokens: list[Token], p2_tokens: list[Token]) -> list[Token]:
    # 1. Filter obvious line noise
    clean_p1 = [t for t in p1_tokens if not is_line_noise(t)]
    clean_p2 = [t for t in p2_tokens if not is_line_noise(t)]
    
    # 2. Build spatial graph / match clusters
    matched_p2_idx = set()
    fused: list[Token] = []
    
    for t1 in clean_p1:
        # Find all overlapping tokens in p2
        overlaps = []
        for idx2, t2 in enumerate(clean_p2):
            iou, iomin = compute_box_metrics(t1.bbox, t2.bbox)
            if iou >= 0.4 or iomin >= 0.65:
                overlaps.append((idx2, t2, iou, iomin))
                
        if not overlaps:
            # P1 orphan
            t1.is_low_confidence = (t1.conf < 60)
            fused.append(t1)
        else:
            # Competing tokens
            # Score t1 vs best candidate in overlaps
            best_idx2, best_t2, best_iou, _ = max(overlaps, key=lambda x: x[2])
            s1 = score_token(t1)
            s2 = score_token(best_t2)
            
            # If t2 is clearly better
            if s2 > s1:
                winner = best_t2
            else:
                winner = t1
                
            winner.is_low_confidence = (winner.conf < 60)
            fused.append(winner)
            for idx2, _, _, _ in overlaps:
                matched_p2_idx.add(idx2)
                
    # 3. Handle P2 orphans (tokens discovered only in P2)
    for idx2, t2 in enumerate(clean_p2):
        if idx2 not in matched_p2_idx:
            # Admission criteria for P2 orphan:
            s2 = score_token(t2)
            # Must have decent conf or match key patterns
            is_valid_pattern = (
                any(kw in t2.text.lower() for kw in BULGARIAN_KEYWORDS) or
                re.search(r'\d{2,}', t2.text) or
                (t2.conf >= 55 and len(t2.text) >= 2)
            )
            if is_valid_pattern and s2 > 40:
                t2.is_low_confidence = (t2.conf < 60)
                fused.append(t2)

    # Sort geometrically: top-to-bottom, left-to-right
    fused.sort(key=lambda t: (t.bbox[1] // 15, t.bbox[0]))
    return fused

def run_test():
    files = [
        "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf",
        "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf",
        "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf",
    ]
    for fpath in files:
        fname = fpath.split('/')[-1]
        print(f"\n==========================================")
        print(f"Testing Fusion on: {fname}")
        doc = pymupdf.open(fpath)
        page = doc[0]
        pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csRGB, alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        doc.close()
        
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        
        # P1: PSM 3
        d1 = pytesseract.image_to_data(enhanced, lang='bul', config='--psm 3', output_type=Output.DICT)
        p1_tokens = []
        for i in range(len(d1['text'])):
            txt = str(d1['text'][i]).strip()
            conf = float(d1['conf'][i])
            if conf != -1 and txt:
                p1_tokens.append(Token(txt, conf, (int(d1['left'][i]), int(d1['top'][i]), int(d1['width'][i]), int(d1['height'][i])), "p1"))
                
        # P2: PSM 11
        d2 = pytesseract.image_to_data(enhanced, lang='bul', config='--psm 11', output_type=Output.DICT)
        p2_tokens = []
        for i in range(len(d2['text'])):
            txt = str(d2['text'][i]).strip()
            conf = float(d2['conf'][i])
            if conf != -1 and txt:
                p2_tokens.append(Token(txt, conf, (int(d2['left'][i]), int(d2['top'][i]), int(d2['width'][i]), int(d2['height'][i])), "p2"))
                
        fused = fuse_tokens(p1_tokens, p2_tokens)
        
        print(f"P1 (PSM 3) tokens: {len(p1_tokens)}")
        print(f"P2 (PSM 11) tokens: {len(p2_tokens)}")
        print(f"Fused tokens: {len(fused)}")
        
        # Stats
        p1_winners = sum(1 for t in fused if t.pass_id == "p1")
        p2_winners = sum(1 for t in fused if t.pass_id == "p2")
        low_conf = sum(1 for t in fused if t.is_low_confidence)
        mean_conf = np.mean([t.conf for t in fused])
        print(f"Fused composition: {p1_winners} from P1, {p2_winners} from P2 | Mean Conf: {mean_conf:.1f}% | Low Conf: {low_conf}")
        
        # Key terms check
        terms = ["фактура", "доставчик", "получател", "еик", "ддс", "капина", "плевен"]
        found = [t for t in terms if any(t in tok.text.lower() for tok in fused)]
        print(f"Key terms found in FUSED output ({len(found)}/{len(terms)}): {found}")

if __name__ == "__main__":
    run_test()
