import sys
repo_root = "/Users/diokarabaz/orca/projects/invoice-tessearct-ocr"
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

import invoice_ocr

t1 = invoice_ocr.OcrToken(text='далака', conf=2.0, bbox=(381, 810, 1236, 118), page_number=1, is_low_confidence=True)
t2 = invoice_ocr.OcrToken(text='Стока', conf=92.0, bbox=(822, 811, 104, 29), page_number=1, is_low_confidence=False)

s1 = invoice_ocr.score_token_quality(t1)
s2 = invoice_ocr.score_token_quality(t2)

iou, iomin = invoice_ocr.compute_box_metrics(t1.bbox, t2.bbox)

print(f"t1 (далака): score={s1}, conf={t1.conf}")
print(f"t2 (Стока):  score={s2}, conf={t2.conf}")
print(f"iou={iou:.4f}, iomin={iomin:.4f}")

fused = invoice_ocr.fuse_ocr_passes([t1], [t2])
print("Fused tokens:")
for f in fused:
    print(f"  '{f.text}' bbox={f.bbox} conf={f.conf}")
