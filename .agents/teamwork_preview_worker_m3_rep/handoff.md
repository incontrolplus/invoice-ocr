# Milestone 3 Handoff Report: Spatial Layout Analysis & Table Reconstruction

## 1. Observation
- Code modification files:
  - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py` (lines 92–148, 440–515, 1565–1640, 1870–2250, 2420–2530, 2570–2720, 3450–3500)
- Newly authored test suites:
  - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_layout_analysis.py` (14 unit tests)
  - `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/tests/test_table_reconstruction.py` (16 unit tests)
- Verification test runs:
  - `.venv/bin/pytest tests/test_layout_analysis.py tests/test_table_reconstruction.py tests/test_adversarial_m2.py tests/test_m2_empirical_challenger.py tests/test_preprocessing.py tests/test_ocr_engine.py tests/test_adversarial_ingestion.py tests/test_ingestion.py -v`:
    - Result: `194 passed, 5 warnings in 63.32s` (100% pass).
  - `.venv/bin/python test_invoice_ocr.py`:
    - Result: `TOTAL: 55 passed, 0 failed` (100% pass).
  - Source dataset protection check:
    - Command: `find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"`
    - Result: 0 files modified (read-only constraint strictly maintained).
  - Real Bulgarian invoice acceptance runs:
    - `капина-01.pdf`: 13 line items extracted, Supplier EIK 114500333, Recipient EIK 207930830 (zero collision), 7/7 statutory keywords recalled.
    - `капина-02.pdf`: 18 line items reconstructed, 7/7 statutory keywords recalled.
    - `капина-03.pdf`: 17 line items reconstructed, thermal cash receipt box detected and isolated, 7/7 statutory keywords recalled.

## 2. Logic Chain
1. **Coordinate-based token grouping (Feature 13)**:
   - Tokens in lines must be unified based on 2D vertical overlap rather than rigid 1D bounding boxes to prevent line shattering from superscript/subscript exponents or baseline punctuation.
   - The overlap criterion $V_{int} / \min(h_1, h_2) \ge 0.50$ with neighbor lookahead window (350px) allows continuous horizontal chaining across subtle residual skew (+0.86 deg across 2000px width) without splitting lines.
2. **LogicalBlock & Spatial Zoning (Feature 14)**:
   - Sequence protocol implementation (`__iter__`, `__len__`, `__getitem__`) enables downstream components to treat `LogicalBlock` as a standard collection of `LogicalLine`s.
   - Canonical 7-zone spatial assignment (`header`, `party_left`, `party_right`, `table_body`, `financial_summary`, `payment_details`, `footer`) provides spatial boundaries for field extraction.
   - `detect_receipt_regions` isolates stapled thermal cash receipts using regex word boundaries to prevent brand collisions (e.g. "Сръбска наденица Бони").
3. **Party Orientation & EIK Collision Resolution**:
   - On invoices where Recipient is on the left and Supplier is on the right (such as Kapina invoices), single full-width line grouping combined both parties into the same text band, resulting in Recipient EIK 207930830 being falsely assigned to Supplier.
   - `resolve_party_orientation` scans the party band ($0.04H \le y \le 0.48H$) to determine left/right column assignment, filters tokens into isolated column halves, and groups them into party-specific lines.
   - This cleanly separates Supplier and Recipient blocks, yielding Supplier EIK `114500333` and Recipient EIK `207930830` with zero collision.
4. **Intra-Pass Fragment Suppression & Winner Preservation in `fuse_ocr_passes`**:
   - Tesseract PSM 11 occasionally emits both a complete word token (e.g. `„Фактура`, conf 39%) and an internal sub-word fragment (e.g. `ак`, conf 96%).
   - Without intra-pass fragment suppression, high-confidence 2-letter fragments competed with and defeated complete statutory keywords.
   - Suppressing sub-string internal fragments where $IoMin \ge 0.75$ restored 100% statutory keyword recall across all documents.
5. **Column Synonyms & Multi-Line Header Sliding Window (Features 15 & 16)**:
   - Column synonym matching was upgraded from substring matching to unicode Cyrillic word boundaries `(?<![а-яА-Яa-zA-Z0-9]){re.escape(s)}(?![а-яА-Яa-zA-Z0-9])` and expanded to the full 9-category taxonomy (including `discount`).
   - Sliding window detector (window size 1..3 lines) captures split header titles (e.g. line 1: `№`, `Стока`, line 2: `Мярка`, `Количество`, `Цена`, `Стойност`) while rejecting deal metadata phrases containing `сделката`.
   - Dynamic asymmetric column projection applies left-aligned boundaries for `description` (`split_x = max(prev_col.x_right + 10, col.x_left - 35)`) and midpoint boundaries for numeric columns.
6. **Multi-line Description Merging & Table Continuation (Features 17 & 18)**:
   - Secondary text lines lacking numeric columns (quantity, price, total) are detected as continuation rows and merged into `items[-1].description`, with bounding box union calculated via `_union_bbox`.
   - `extract_line_items` loops across all `table_regions`, suppresses carry-forward lines (`Пренос`, `Стр. Общо`, `Посл. Стр. Общо`) and repeated headers, and maintains continuous 1-based indexing while recording each item's true `page_number`.
7. **Strict Null Fallback Policy (Feature 19)**:
   - When table descriptions are missing, illegible, or contain synthetic placeholders (`"Item"`, `"Unknown"`, `"Placeholder"`, `"Артикул"`, `"n/a"`, `"none"`), `description = None` is strictly assigned and `ValidationIssue(code="MISSING_DESCRIPTION", severity="warning")` is recorded.
   - Zero synthetic dummy strings are generated, fulfilling the Integrity Mandate.
8. **Digital PDF Text Layer Fusion**:
   - Embedded PDF words from PyMuPDF are extracted when statutory keyword density $\ge 3$, scaled from 72 DPI to 300 DPI, and fused with OCR tokens. This recovers condensed font headers while preserving OCR capability for scanned PDFs (Metro) and thermal receipts.

## 3. Caveats
- Metro invoice is a scanned PDF with corrupted embedded font streams; it relies on Tesseract OCR without embedded text layer fusion.
- Handwritten notes or stamps crossing into table cells may be treated as continuation text if they overlap the description column.

## 4. Conclusion
Milestone 3 (Spatial Layout Analysis & Table Reconstruction, Features 13–19) is fully implemented, strictly compliant with project architecture and integrity constraints, and verified with 100% test pass rate across 194 pytest tests and 55 legacy unit tests without regressions.

## 5. Verification Method
Run the following commands to independently verify:
```bash
# 1. Verify Milestone 3 layout analysis tests (14 tests)
.venv/bin/pytest tests/test_layout_analysis.py -v

# 2. Verify Milestone 3 table reconstruction tests (16 tests)
.venv/bin/pytest tests/test_table_reconstruction.py -v

# 3. Verify full regression test suite (194 tests)
.venv/bin/pytest tests/test_layout_analysis.py tests/test_table_reconstruction.py tests/test_adversarial_m2.py tests/test_m2_empirical_challenger.py tests/test_preprocessing.py tests/test_ocr_engine.py tests/test_adversarial_ingestion.py tests/test_ingestion.py -v

# 4. Verify legacy test suite (55 tests)
.venv/bin/python test_invoice_ocr.py

# 5. Verify source dataset immutability (0 files modified)
find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
```
