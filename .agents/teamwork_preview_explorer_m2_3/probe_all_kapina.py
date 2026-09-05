import cv2
import numpy as np
import pymupdf
import pytesseract
from pytesseract import Output

FILES = [
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf",
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf",
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf"
]

def test_all():
    for fpath in FILES:
        print(f"\n==========================================")
        print(f"File: {fpath.split('/')[-1]}")
        doc = pymupdf.open(fpath)
        page = doc[0]
        pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csRGB, alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        doc.close()
        
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        
        for psm in [3, 11]:
            data = pytesseract.image_to_data(enhanced, lang='bul', config=f'--psm {psm}', output_type=Output.DICT)
            tokens = []
            for i in range(len(data['text'])):
                txt = str(data['text'][i]).strip()
                conf = float(data['conf'][i])
                if conf != -1 and txt:
                    tokens.append((txt, conf))
            
            confs = [t[1] for t in tokens]
            mean_c = np.mean(confs) if confs else 0
            
            # Check key terms
            terms = ["фактура", "доставчик", "получател", "еик", "ддс", "капина"]
            found = [t for t in terms if any(t in tok[0].lower() for tok in tokens)]
            print(f"PSM {psm:2d}: {len(tokens):3d} tokens, mean_conf: {mean_c:4.1f}%, found terms ({len(found)}/{len(terms)}): {found}")

if __name__ == "__main__":
    test_all()
