import os
import sys

repo_root = "/Users/diokarabaz/orca/projects/invoice-tessearct-ocr"
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

import invoice_ocr

kapina_files = [
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf",
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf",
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf",
]

for p in kapina_files:
    fname = os.path.basename(p)
    print("\n" + "=" * 80)
    print(f"DETAILED TOKEN & LINE DUMP: {fname}")
    print("=" * 80)
    
    pages = invoice_ocr.load_document(p)
    norm_page, transform = invoice_ocr.normalize_page_geometry(pages[0])
    variants = invoice_ocr.generate_preprocessing_variants(norm_page.image)
    page_tokens = invoice_ocr.run_multiple_ocr_passes(variants)
    tokens = invoice_ocr.normalize_ocr_tokens(page_tokens)
    
    # Filter tokens in y between 600 and 1500
    table_tokens = [t for t in tokens if 650 <= t.bbox[1] <= 1400]
    # Sort by y, then x
    table_tokens.sort(key=lambda t: (t.bbox[1], t.bbox[0]))
    
    print(f"--- Tokens between y=650 and y=1400 (count: {len(table_tokens)}) ---")
    for t in table_tokens:
        print(f"Token: '{t.text:<25}' bbox={t.bbox} conf={t.conf:.1f}")
        
    lines = invoice_ocr.group_tokens_into_lines(tokens)
    print(f"\n--- Logical Lines between y=650 and y=1400 ---")
    for idx, l in enumerate(lines):
        if 650 <= l.bbox[1] <= 1400:
            print(f"Line {idx:2d} (y={l.bbox[1]:4d}, h={l.bbox[3]:2d}, x={l.bbox[0]:4d}, w={l.bbox[2]:4d}):")
            print(f"   Text: '{l.text}'")
            tok_details = " | ".join([f"'{t.text}'(x={t.bbox[0]}, w={t.bbox[2]})" for t in l.tokens])
            print(f"   Tokens: {tok_details}")
