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
fused = invoice_ocr.fuse_ocr_passes(p1_tokens, p2_tokens)

print(f"p1 count={len(p1_tokens)}, p2 count={len(p2_tokens)}, fused count={len(fused)}")

for idx, t in enumerate(p2_tokens):
    if t.text.strip() == "Стока":
        print(f"p2 'Стока' found at idx={idx}, bbox={t.bbox}")
        
found_in_fused = [t for t in fused if "стока" in t.text.lower()]
print(f"Found in fused: {found_in_fused}")

# Now let's trace why 'Стока' was or wasn't added
clean_p1 = [t for t in p1_tokens if not invoice_ocr.is_line_noise_token(t)]
clean_p2 = [t for t in p2_tokens if not invoice_ocr.is_line_noise_token(t)]

# Check if 'Стока' is in clean_p2
stoka_p2 = [t for t in clean_p2 if "стока" in t.text.lower()]
print(f"Стока in clean_p2: {len(stoka_p2)}")

# Check what overlaps with clean_p1 tokens
for t1 in clean_p1:
    for idx2, t2 in enumerate(clean_p2):
        if "стока" in t2.text.lower():
            iou, iomin = invoice_ocr.compute_box_metrics(t1.bbox, t2.bbox)
            if iou >= 0.40 or iomin >= 0.65:
                print(f"t1 '{t1.text}' overlaps with Стока: iou={iou:.3f}, iomin={iomin:.3f}")
                s1 = invoice_ocr.score_token_quality(t1)
                s2 = invoice_ocr.score_token_quality(t2)
                print(f"  s1 ({t1.text}) = {s1}, s2 (Стока) = {s2}")
