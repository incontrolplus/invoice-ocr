import os
import sys
import json

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
    print("\n" + "=" * 75)
    print(f"ANALYZING: {fname}")
    print("=" * 75)
    
    # Process invoice
    inv = invoice_ocr.process_invoice(p)
    
    # Get lines and tokens
    pages = invoice_ocr.load_document(p)
    norm_page, transform = invoice_ocr.normalize_page_geometry(pages[0])
    variants = invoice_ocr.generate_preprocessing_variants(norm_page.image)
    page_tokens = invoice_ocr.run_multiple_ocr_passes(variants)
    tokens = invoice_ocr.normalize_ocr_tokens(page_tokens)
    lines = invoice_ocr.group_tokens_into_lines(tokens)
    table_regions = invoice_ocr.detect_table_regions(lines, tokens)
    
    print(f"Total tokens: {len(tokens)}, Total logical lines: {len(lines)}")
    print(f"Detected table regions: {len(table_regions)}")
    
    if table_regions:
        tbl = table_regions[0]
        print(f"Table Header Line: '{tbl.header_line.text}'")
        print(f"Header tokens count: {len(tbl.header_line.tokens)}")
        for tok in tbl.header_line.tokens:
            print(f"   Token '{tok.text}' bbox={tok.bbox} conf={tok.conf:.1f}")
        print("\nColumns:")
        for col in tbl.columns:
            print(f"   Col '{col.semantic_type}': header='{col.header_text}' x_left={col.x_left} x_right={col.x_right} center={col.x_center}")
        print(f"\nData lines in table ({len(tbl.data_lines)}):")
        for i, dl in enumerate(tbl.data_lines):
            col_map = invoice_ocr._assign_line_to_columns(dl, tbl.columns)
            print(f"   DL {i+1:2d} (y={dl.bbox[1]:4d}, h={dl.bbox[3]:2d}): text='{dl.text}'")
            print(f"         col_map: {col_map}")
    else:
        print("NO TABLE REGION DETECTED!")
        print("Let's look for header-like lines across all lines:")
        for idx, line in enumerate(lines):
            for kw in ["стока", "мярка", "кол", "цена", "стойност", "ддс", "описание", "№", "код"]:
                if kw in line.text_lower:
                    print(f"   Candidate Line {idx:2d} (y={line.bbox[1]:4d}): '{line.text}'")
                    break

    print(f"\nExtracted Line Items ({len(inv.line_items)}):")
    for item in inv.line_items:
        print(f"   Item #{item.index}: desc='{item.description}', qty={item.quantity}, unit='{item.unit}', price={item.unit_price_net}, total={item.total_price_net}, vat={item.vat_rate_pct}")
        
    print(f"\nFinancial summary:")
    print(f"   Tax Base: {inv.financial_summary.tax_base.amount} {inv.financial_summary.tax_base.currency}")
    print(f"   VAT:      {inv.financial_summary.vat_amount.amount} {inv.financial_summary.vat_amount.currency}")
    print(f"   Total:    {inv.financial_summary.total_amount_due.amount} {inv.financial_summary.total_amount_due.currency}")
