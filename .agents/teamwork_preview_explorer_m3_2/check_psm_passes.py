import os
import sys

repo_root = "/Users/diokarabaz/orca/projects/invoice-tessearct-ocr"
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

import invoice_ocr

p = "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf"
pages = invoice_ocr.load_document(p)
norm_page, transform = invoice_ocr.normalize_page_geometry(pages[0])
variants = invoice_ocr.generate_preprocessing_variants(norm_page.image)

target_img = variants[0][1]
for name, img in variants:
    if name in ("clahe_gray", "standard"):
        target_img = img
        break

print(f"Target image shape: {target_img.shape}")

p1_tokens = invoice_ocr.execute_ocr_pass(target_img, psm=3, lang="bul")
print(f"PSM 3 tokens count: {len(p1_tokens)}")
for t in p1_tokens:
    if 650 <= t.bbox[1] <= 1100:
        print(f"  PSM 3: '{t.text:<25}' bbox={t.bbox} conf={t.conf:.1f}")

p2_tokens = invoice_ocr.execute_ocr_pass(target_img, psm=11, lang="bul")
print(f"\nPSM 11 tokens count: {len(p2_tokens)}")
for t in p2_tokens:
    if 650 <= t.bbox[1] <= 1100:
        print(f"  PSM 11: '{t.text:<25}' bbox={t.bbox} conf={t.conf:.1f}")
