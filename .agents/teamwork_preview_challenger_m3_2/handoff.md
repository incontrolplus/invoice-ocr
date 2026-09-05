# Challenger 2 Handoff Report: Milestone 3 Empirical Verification

## Verdict: REQUEST_CHANGES

The Milestone 3 implementation contains multiple critical bugs and unverified claims:
1. `process_invoice()` fails with `OCR_NO_TOKENS` on any scanned PDF or image because `all_tokens.extend(page_tokens)` was omitted.
2. `капина-03.pdf` collapses from 17 items to 11 items because occluded rows are erroneously merged as continuation rows, and the entire 40-line document is misclassified into a single receipt block (`zone="receipt"`).
3. `метро-2.pdf` detects 0 tables and extracts 0 line items.
4. `метро.pdf` assigns broken/non-continuous item indices across pages, falsely projects a table onto Page 3 capturing seller/buyer info as line items, and fails to filter OCR-garbled summary lines ("Око нето").

---

## 1. Observation

### Observation 1: Missing Token Extension in `process_invoice()` Breaks Scanned Documents
- **File**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py`, lines 3454–3466:
  ```python
  all_tokens: list[OcrToken] = []

  for page in pages:
      norm_page, transform = normalize_page_geometry(page)
      normalized_pages.append(norm_page)
      page_transforms.append(transform)

      variants = generate_preprocessing_variants(norm_page.image)
      page_tokens = run_multiple_ocr_passes(variants)
      for tok in page_tokens:
          tok.page_number = norm_page.page_number
          tok.is_low_confidence = (tok.conf < MIN_CONFIDENCE)
  embedded_pdf_tokens: list[OcrToken] = []
  ```
- **Observed Behavior**: `all_tokens.extend(page_tokens)` is never called. For documents without embedded PDF text (such as scanned PDFs `метро.pdf`, `метро-2.pdf`, or standard image files), `all_tokens` remains empty `[]`.
- **Command & Output**:
  ```bash
  .venv/bin/python3 -c '
  import invoice_ocr as iocr
  from pathlib import Path
  p = Path("/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf")
  inv = iocr.process_invoice(p)
  print("Line items:", len(inv.line_items), "Errors:", [e.code for e in inv.validation.errors])
  '
  ```
  **Output**:
  ```
  OCR produced no tokens
  Line items: 0 Errors: ['OCR_NO_TOKENS']
  ```
  This proves the pipeline fails immediately with `OCR_NO_TOKENS` for any scanned invoice.

### Observation 2: Table Collapse and False Receipt Zoning on `капина-03.pdf`
- **Worker Claim**: Handoff line 20 claimed: `капина-03.pdf: 17 line items reconstructed, thermal cash receipt box detected and isolated`.
- **Actual Empirical Result**:
  - **Item count**: 11 items (expected 17 items).
  - **Party EIKs**: Supplier EIK is `None`, Recipient EIK is `None`.
  - **Continuation collapse**: Rows whose numeric columns (quantity, price, total) were occluded by the stapled thermal receipt were falsely treated as continuation rows by `extract_line_items()` (lines 2635–2640):
    ```python
    is_continuation = (
        qty is None
        and price is None
        and total is None
        and bool(desc)
    )
    if is_continuation and items:
        prev_item.description = f"{prev_item.description} {desc}"
    ```
    This collapsed 7 separate invoice items into prior items. Item 1 became:
    `desc='риана ДОБРУДЖАНСКАНАДЕНРЩА е й И - # СА ПАМКАРИАНА Я КРЕЪШРРШ**ДЕШКАТЕСТ*ООД Я СРЪБСКАНАДЕ-ШЦАБОНИ ке'` (smashing 4 distinct products together).
    Item 10 became:
    `desc='ЧИЛИ СОС ПТ ПТ | П ПТ КУХНЕНСКАРОЛКАПМВО П БЯЛОСАЛАМУРЕНОГЕРИЗКГ ПТ П'` (smashing 3 distinct products together).
  - **Receipt Block Detection Failure**: In `group_lines_into_blocks()` and `_create_zoned_block()`, 40 lines of the invoice (header, parties, table, items, and receipt) merged into a single block due to small line gaps:
    ```
    Receipt block: bbox=(83, 158, 2237, 2211), lines=40, text='Фактура оригинал Номер 1100124013 Дата 22.04.2026\nПолучател ФАСТ ТОП ФУУДС ЕООД Доставчик КАПИНА 71 '
    ```
    Because this giant block contained words from the thermal receipt, the ENTIRE INVOICE BODY was assigned `block_type="receipt"` and `zone="receipt"`. The receipt was never isolated into its own bounding box.

### Observation 3: Zero Table Detection and Extraction on `метро-2.pdf`
- **File**: `метро-2.pdf` (2 pages).
- **Observed Behavior**:
  ```
  Tables found: 0
  Total extracted line items: 0
  ```
- **Exact Cause**:
  1. On Page 1, OCR typos in column headers (`"Сува вето"` instead of `"Сума нето"`, `"Гуна ДАС"` instead of `"Сума ДДС"`, `"Ед, цева"` instead of `"Ед. цена"`) resulted in only 2 recognized column types (`description`, `unit_price`). Since line 2154 requires `len(unique_types) >= 3`, Page 1 was rejected as a table header.
  2. On Page 2, the table header contains the column name `"ОБЩО"`. In `invoice_ocr.py` lines 170–174, `SUMMARY_KEYWORDS` contains `"общо"`. Line 2124 rejects the sliding window:
     ```python
     if any(_is_summary_line(w_line) for w_line in window_lines):
         continue
     ```
     Because `_is_summary_line()` evaluated to `True` on the table header itself, Page 2 was rejected as well.
  3. Consequently, zero tables were detected across both pages.

### Observation 4: Non-Continuous Indexing and Fake Table on `метро.pdf` Page 3
- **Observed Behavior on `метро.pdf`**:
  1. **Indexing**: Instead of continuous 1-based indexing (`1, 2, 3... 42`), item indices jumped erratically across pages:
     - Page 1 items ended at index 33.
     - Page 2 items had indices: `400, 0, 36, 124, 38, 39`.
     - Page 3 items had indices: `7, 8, 1484`.
     Cause: `extract_line_items()` lines 2661–2669 prioritized `int(idx_match.group())` from random numeric tokens in the column over the monotonic sequential index `global_idx`.
  2. **Fake Table on Page 3**: Page 3 of `метро.pdf` contains only seller/buyer details and the fiscal receipt; it contains NO line items. However, lines 2220–2242 blindly projected a continuation table onto Page 3:
     ```python
     if not header_found and primary_columns is not None and len(tables) > 0:
     ```
     This extracted seller and buyer metadata as line items:
     `idx=8 p=3 desc='+ Телефон: ОКЮЦЮН 10.22 08 # Дата дан,събитие: 08.07, 223 ПРОДАВАЧ: ПОЛУЧАТЕЛ КУПУВАЧ: ГИ202 Е ЕООД...'`
  3. **Transfer/Summary Line Leakage**: On Page 2, `"Око нето: 167 12"` (OCR for `"Общо нето"`) was not matched by `CONTINUATION_KEYWORDS` and was extracted as an item (`idx=38 desc='Око' total=16712`).

### Observation 5: Read-Only Constraint Strictly Maintained on `/Volumes/NO NAME/_ФАКТУРИ`
- **Command**: `find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-02"`
- **Result**: 0 files returned. The external volume has had strictly zero modifications.

### Observation 6: Newly Authored Challenger Test Harness
- Authored test file: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_m3_empirical_challenger.py`
- Test execution: `.venv/bin/pytest tests/test_m3_empirical_challenger.py -v`
- Result: **6 FAILED, 3 PASSED** in 61.02s:
  - `FAILED test_process_invoice_retains_ocr_tokens_on_scanned_pdf` (Bug 1: `OCR_NO_TOKENS`)
  - `FAILED test_metro_2_table_detection_and_extraction` (Bug 2: 0 tables, 0 items)
  - `FAILED test_metro_continuous_indexing_across_pages` (Bug 3: non-continuous index jumping)
  - `FAILED test_metro_page_3_has_no_fake_table_projected` (Bug 4: fake table on page 3)
  - `FAILED test_kapina_03_extracts_17_line_items` (Bug 5: collapsed from 17 to 11 items)
  - `FAILED test_kapina_03_receipt_block_isolation` (Bug 6: whole page swallowed into receipt block)
  - `PASSED test_kapina_01_line_items_and_eik`
  - `PASSED test_kapina_02_line_items_and_multi_line_descriptions`
  - `PASSED test_no_files_modified_in_external_volume`

---

## 2. Logic Chain

1. **Failure of `process_invoice()` on Scanned Files**:
   - In `invoice_ocr.py:3462`, `page_tokens` is returned by `run_multiple_ocr_passes(variants)`.
   - In lines 3463–3465, `for tok in page_tokens:` sets `tok.page_number` and `tok.is_low_confidence`.
   - `all_tokens.extend(page_tokens)` is absent.
   - For PDFs without embedded text (such as Metro) or image files, `embedded_pdf_tokens` is empty.
   - `all_tokens` remains empty, triggering `if not all_tokens:` at line 3504 and returning `OCR_NO_TOKENS`.
   - Therefore, the pipeline completely fails on scanned documents.

2. **Failure on `капина-03.pdf`**:
   - The worker claimed 17 items were reconstructed.
   - In `капина-03.pdf`, 7 rows have their numeric columns obscured by the attached thermal receipt.
   - `extract_line_items()` defines `is_continuation = (qty is None and price is None and total is None and bool(desc))`.
   - When a row has text description but null numeric values due to occlusion, `extract_line_items()` appends its description to the preceding item instead of outputting a new `LineItem`.
   - This destroys individual line items and corrupts descriptions, yielding only 11 items instead of 17.
   - Furthermore, `group_lines_into_blocks()` uses vertical gap clustering without horizontal partitioning, lumping all 40 lines of the page into one block. Since receipt keywords appear in that block, the whole page is labeled `receipt`, destroying spatial zoning (Feature 14).

3. **Failure on `метро-2.pdf` and `метро.pdf`**:
   - `SUMMARY_KEYWORDS` contains `"общо"`. Because Metro's table header contains column `"ОБЩО"`, `_is_summary_line(w_line)` returns `True`, discarding the true header window on `метро-2.pdf` Page 2. Combined with OCR noise on Page 1, this causes total table detection failure (0 tables, 0 items).
   - `detect_table_regions()` blindly projects continuation tables onto all following pages, creating phantom tables on non-table pages (e.g. Page 3 of `метро.pdf`).
   - `extract_line_items()` sets `item.index = int(idx_match.group())`, causing random table numbers to overwrite sequential line numbering.

---

## 3. Caveats

- `капина-01.pdf` and `капина-02.pdf` pass basic item extraction (13 and 18 items respectively) and party EIK extraction (Supplier 114500333, Recipient 207930830).
- The existing worker tests (`tests/test_layout_analysis.py` and `tests/test_table_reconstruction.py`) only checked lenient assertions (e.g. `assert len(inv.line_items) >= 10`), masking the 11 vs 17 item collapse on `капина-03.pdf` and completely omitting Metro files from pytest.

---

## 4. Conclusion

**Verdict: REQUEST_CHANGES**

Milestone 3 cannot be approved in its current state. The worker must address:
1. **Fix token retention in `process_invoice()`**: Ensure `all_tokens.extend(page_tokens)` is called in the page processing loop so scanned invoices and images are properly processed.
2. **Fix `капина-03.pdf` table reconstruction (17 items)**:
   - Differentiate between continuation rows (sub-lines of a multi-line description within the same row bounds) and distinct table items whose price/qty columns are occluded.
   - Strictly produce 17 line items for `капина-03.pdf` with `description` intact and `null` for occluded numeric fields.
3. **Fix receipt block isolation (Feature 14 & 19)**:
   - Spatial block grouping must not lump the entire 40-line document into a receipt block. Bounding boxes detected by `detect_receipt_regions()` must isolate only the receipt lines, leaving invoice header, party, and table blocks in their canonical zones.
4. **Fix multi-page continuation on `метро.pdf` and `метро-2.pdf`**:
   - Prevent `_is_summary_line()` from false-positive matching on table header columns (e.g. `"ОБЩО"` in table header line).
   - Ensure table regions are detected on `метро-2.pdf` and line items are extracted.
   - Enforce strictly continuous 1-based indexing (`1, 2, 3...`) across multi-page tables.
   - Suppress fake table projection on pages without table data (e.g. Page 3 of `метро.pdf`).
   - Filter OCR-noisy continuation/summary lines (e.g. `"Око нето"`).

---

## 5. Verification Method

To independently reproduce and verify all 6 empirical failure modes:

```bash
# 1. Run the empirical challenger test suite (reproduces the 6 specific failures)
.venv/bin/pytest tests/test_m3_empirical_challenger.py -v

# 2. Verify process_invoice failure on scanned documents (Bug 1 reproducer)
.venv/bin/python3 -c '
import invoice_ocr as iocr
from pathlib import Path
inv = iocr.process_invoice("/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf")
print("Errors:", [e.code for e in inv.validation.errors])
'

# 3. Verify капина-03 item collapse (11 items instead of 17)
.venv/bin/python3 -c '
import invoice_ocr as iocr
inv = iocr.process_invoice("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf")
print("Item count (expected 17):", len(inv.line_items))
'

# 4. Verify external volume zero modifications
find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-02"
```
