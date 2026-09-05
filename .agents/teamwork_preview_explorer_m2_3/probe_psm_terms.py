import cv2
import numpy as np
import pymupdf
import pytesseract
from pytesseract import Output

PDF_PATH = "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"

def test_psm6():
    doc = pymupdf.open(PDF_PATH)
    page = doc[0]
    pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csRGB, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    doc.close()
    
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    
    for psm in [3, 6, 11]:
        data = pytesseract.image_to_data(enhanced, lang='bul', config=f'--psm {psm}', output_type=Output.DICT)
        tokens = []
        for i in range(len(data['text'])):
            txt = str(data['text'][i]).strip()
            conf = float(data['conf'][i])
            if conf != -1 and txt:
                tokens.append((txt, conf))
        text = " ".join(t[0] for t in tokens)
        print(f"\n=== PSM {psm} (total tokens: {len(tokens)}) ===")
        # check key Bulgarian invoice terms
        for term in ["фактура", "доставчик", "получател", "еик", "ддс", "данъчна", "основа", "капина", "плевен", "софия", "сума"]:
            found = any(term in t[0].lower() for t in tokens)
            print(f"  term '{term}': {'FOUND' if found else 'MISSING'}")

if __name__ == "__main__":
    test_psm6()
