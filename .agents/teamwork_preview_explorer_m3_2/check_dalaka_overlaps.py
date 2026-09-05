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

p1_tokens = invoice_ocr.execute_ocr_pass(target_img, psm=3, lang="bul")
p2_tokens = invoice_ocr.execute_ocr_pass(target_img, psm=11, lang="bul")

clean_p1 = [t for t in p1_tokens if not invoice_ocr.is_line_noise_token(t)]
clean_p2 = [t for t in p2_tokens if not invoice_ocr.is_line_noise_token(t)]

for t1 in clean_p1:
    if "далака" in t1.text:
        overlaps = []
        for idx2, t2 in enumerate(clean_p2):
            iou, iomin = invoice_ocr.compute_box_metrics(t1.bbox, t2.bbox)
            if iou >= 0.40 or iomin >= 0.65:
                overlaps.append((idx2, t2, iou, iomin))
        print(f"Tokens overlapping with '{t1.text}' bbox={t1.bbox}: count={len(overlaps)}")
        for idx2, t2, iou, iomin in overlaps:
            print(f"   idx2={idx2}: '{t2.text}' bbox={t2.bbox} iou={iou:.3f} iomin={iomin:.3f} score={invoice_ocr.score_token_quality(t2)}")
        best_idx2, best_t2, best_iou, _ = max(overlaps, key=lambda x: x[2])
        print(f"WINNER by max(x[2]): '{best_t2.text}' idx2={best_idx2} with iou={best_iou:.3f}")
