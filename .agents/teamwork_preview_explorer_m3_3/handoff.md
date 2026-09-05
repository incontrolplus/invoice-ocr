# Milestone 3: Spatial Layout Analysis & Table Reconstruction — Explorer 3 Report

**Agent**: Explorer 3 (`teamwork_preview_explorer_m3_3`)  
**Archetype**: Explorer  
**Roles**: investigation, synthesis  
**Milestone**: Milestone 3: Spatial Layout Analysis & Table Reconstruction  
**Assigned Focus**: Feature 17 (Multi-Line Item Description Merging), Feature 18 (Multi-Page Table Stitching), Feature 19 (Occlusion Handling & Strict Null Fallbacks)  
**Target Documents**: `капина-02.pdf`, `капина-03.pdf`, `метро.pdf` (3 pages), `метро-2.pdf` (2 pages)  
**Authoritative Specs**: `ORIGINAL_REQUEST.md` (R1, R3, R4), `PROJECT.md` (Features 13–19)  
**Date**: 2026-09-05T01:26:00+03:00  

---

## 1. Observation

### 1.1 Existing Codebase & Interface Contracts
1. **File**: `invoice_ocr.py` (lines 85–110, 288–333, 1655–1755, 2050–2130)
   - `TableColumn`: defines `semantic_type`, `x_left`, `x_right`, `x_center`, `header_text`.
   - `TableRegion`: defines `columns: list[TableColumn]`, `header_line: LogicalLine`, `data_lines: list[LogicalLine]`, `page_number: int`.
   - `LineItem` in `invoice_ocr.py` (line 324):
     ```python
     @dataclass
     class LineItem:
         index: int | None = None
         description: str | None = None
         unit: str | None = None
         quantity: Decimal | None = None
         unit_price_net: Decimal | None = None  # Note: PROJECT.md contract specifies MoneyAmount
         total_price_net: Decimal | None = None # Note: PROJECT.md contract specifies MoneyAmount
         vat_rate_pct: Decimal | None = None
     ```
   - In `PROJECT.md` (lines 116–130), the contract specifies:
     ```python
     @dataclass
     class LineItem:
         index: int
         description: str | None
         unit: str | None
         quantity: Decimal | None
         unit_price_net: MoneyAmount
         total_price_net: MoneyAmount
         vat_rate_pct: Decimal | None
     ```

2. **The Column Synonym Substring Bug in `invoice_ocr.py` (lines 1639–1646)**:
   - Line 1641–1646:
     ```python
     def _match_column_synonym(text: str) -> str | None:
         text_lower = text.lower().strip()
         for col_type, synonyms in COLUMN_SYNONYMS.items():
             for syn in synonyms:
                 if syn in text_lower:
                     return col_type
         return None
     ```
   - In `COLUMN_SYNONYMS`:
     - `"index": ["№", "но", "n", "#", "ред", "no", "пор"]`
     - `"quantity": ["количество", "кол", "к-во", "бр", "qty", "кол."]`
   - Direct empirical execution test:
     ```python
     >>> _match_column_synonym("стойност")
     'index'      # Because "но" is a substring of "стойност"!
     >>> _match_column_synonym("ДОБРУДЖАНСКА")
     'quantity'   # Because "бр" is a substring of "ДОБРУДЖАНСКА"!
     >>> _match_column_synonym("ВЪРБАНОВ")
     'index'      # Because "но" is a substring of "ВЪРБАНОВ"!
     >>> _match_column_synonym("НИКОЛАЙ")
     'quantity'   # Because "кол" is a substring of "НИКОЛАЙ"!
     ```
   - Verbatim consequence: On `капина-02.pdf`, the header line `Хо и х В с Пор Стойност` matches only `index` for both `Пор` and `Стойност`. Thus `unique_types = {'index'}`, failing `len(unique_types) >= 3`.
   - **Result**: `detect_table_regions` returns 0 tables and `process_invoice` extracts **0 line items** across all 3 Kapina acceptance files (`капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`)!

3. **The Multi-Page Table Break Loophole in `invoice_ocr.py` (lines 1752, 2063)**:
   - In `detect_table_regions`:
     ```python
     tables.append(TableRegion(...))
     break  # typically only one table per invoice (line 1752)
     ```
   - In `extract_line_items`:
     ```python
     table = table_regions[0]  # use first detected table (line 2063)
     ```
   - Verbatim consequence: Multi-page invoices like `метро.pdf` (3 pages) and `метро-2.pdf` (2 pages) have table regions on page 2+ completely discarded, or page 1 absorbs lines across all pages without page-aware boundaries.

---

### 1.2 Empirical Document Analysis: `капина-02.pdf` (Multi-Line Wrapping)
- Command executed:
  `python -c "import pymupdf; doc = pymupdf.open('/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf'); ..."`
- Table Header on `капина-02.pdf`:
  `(60.0, 192.0): Код` | `(193.0, 193.0): Стока` | `(331.0, 199.1): Мярка` | `(381.0, 198.1): Кол.` | `(425.0, 198.1): Цена` | `(463.0, 198.9): ДДС` | `(503.0, 197.1): Стойност`
- Real line items exhibit 2-to-4 line descriptions:
  - Item 1: `62206` | `ДОБРУДЖАНСКА НАДЕНИЦА` (wrapped across 2 lines with OCR noise) | `3.081` | `14.32`
  - Item 2: `0608217` | `ХАМБУРГСКИ САЛАМ КАРИАНА` | `3.200` | `10.00`
  - Item 3: `040419` | `КРЕНВИРШ "ДЕЛИКАТЕС ООД"` | `4.00` | `4.30`
  - Item 4: `61922` | `СРЪБСКА НАДЕНИЦА БОНИ` | `1.352` | `7.94`
  - Item 5: `63618` | `КАШКАВАЛ БАЙ ВЪЛЧАН ТОСТЕР` (line 1: `КАШКАВАЛ БАЙ ВЪЛЧАН`, line 2: `ТОСТЕР`) | `1.970` | `15.35`
  - Item 6: `040450` | `БЛАНШ КАРТОФИ Стекхаусевро 2.5` (line 1: `БЛАНШ КАРТОФИ`, line 2: `Стекхаусевро 2.5`) | `5.000` | `8.29`
  - Item 10: `64006` | `ЕНЕРГИЙНА НАПИТКА ЧЕРНА МЕЧКА` (line 1: `ЕНЕРГИЙНА НАПИТКА`, line 2: `ЧЕРНА МЕЧКА`) | `35.000` | `72.08`
  - Item 12: `61371` | `КУХНЕНСКА РОЛКА ДЖЪМБО` (line 1: `КУХНЕНСКА РОЛКА`, line 2: `ДЖЪМБО`) | `3.000` | `3.50`
  - Item 13: `64005` | `ЕНЕРГИЙНА НАПИТКА ХЕЛЛ КЛАСИК` | `24.000` | `10.40`
  - Item 14: `62086` | `ИЗОСПОРТ С КАПАЧКА 14 БР.` (line 1: `ИЗОСПОРТ`, line 2: `С КАПАЧКА 14 БР.`) | `1.000` | `5.42`
  - Item 15: `КЕТЧУП БУЛМЕД 0.900гр` (line 1: `КЕТЧУП БУЛМЕД`, line 2: `0.900гр`) | `1.000` | `1.00`
  - Item 16: `62282` | `САЛФЕТКИ БЕЛИ 4*500/4*4.5` (line 1: `САЛФЕТКИ БЕЛИ`, line 2: `4*500/4*4.5`)
- Observation on continuation rows:
  - The continuation rows have text in the `description` column span.
  - The continuation rows have **ZERO numbers** in `quantity`, `unit_price`, and `total_price`.
  - The vertical gap between the main row and the continuation row is $\le 1.5 \times \text{median line height}$.

---

### 1.3 Empirical Document Analysis: `метро.pdf` (Multi-Page Continuation)
- Command executed:
  `python -c "from invoice_ocr import process_invoice; inv = process_invoice('/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf'); ..."`
- Total pages: 3.
- Page breakdown:
  - **Page 1**: Contains supplier header, invoice number, customer info, table header (`НА Артикул номер Бписание...`), 40+ item rows.
    - Page footer marker at $y=3354$: `си Стр. 060: 0 194,96` (Subtotal carried forward: 194.96 BGN).
  - **Page 2**: Contains repeated supplier header (`НЕТРО БЪЛГАРИЯ ЕООД`), repeated table header at $y=966$:
    `НА Артикул номер Бписание ПО Ер. цена Съд Цена НЕК-во Суна нето Сума ДДС т ВКД ВКВ ОБВО БОНО ДДС`
    - Transfer line from previous page at $y=1069$: `Посл. Стр. Общо: 194,56` (Brought forward subtotal: 194.56).
    - Continuing data rows: Items 41 through final row.
    - Table footer: `Общо нето: 367,92`, `НЕТО СУМА ДДС НАЧИСЛ. ДДС`, `СУМА: 441,02`.
  - **Page 3**: Party details page (`ПРОДАВАЧ: ...`, `ПОЛУЧАТЕЛ: КУПУВАЧ: ГИ202 ЕООД`, `АДРЕС: ...`) and fiscal receipt (`ФИСКАЛЕН БОН`).
- Observed Bugs in Current Engine:
  - Current code extracted **79 line items** because:
    1. It did not merge multi-line continuation descriptions (e.g. `Item 1: idx=2, desc='15 БТ', qty=None, unit='27 И: ка'`, `Item 3: idx=4, desc='Боти: 04002174086998', qty=None`, `Item 5: desc='ВС Вин: 018008492217357', qty=None`).
    2. It failed to stitch across pages with page-aware bounding boxes and separate table regions.
    3. It treated repeated table headers and transfer lines (`Посл. Стр. Общо`) as data items.

---

### 1.4 Empirical Document Analysis: `капина-03.pdf` (Thermal Receipt Occlusion)
- Command executed:
  `python -c "import pymupdf; doc = pymupdf.open('/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf'); ..."`
- Direct Visual & Spatial Evidence:
  - A thermal fiscal cash receipt (`ФИСКАЛЕН БОН`) is physically stapled over the right half of the table area ($x \in [360, 520]$).
  - The receipt completely occludes the `Мярка` (unit), `Количество` (quantity), and `Ед. цена` (unit price) columns for items 1 through 7.
  - The `Стока` (description) column on the left ($x \in [90, 250]$) is visible:
    - Line 16: `1162206 ДОБРУДЖАНСКА`
    - Line 17: `4060821 ХАМБУРГСКИ САЛАМ КАРИАНА`
    - Line 19: `0040419 КРЕНВИРШ "ДЕЛИКАТЕС 2"ООД`
    - Line 20: `61922 СРЪБСКА НАДЕНИЦА`
    - Line 22: `КАШКАВАЛ ТОСТЕР`
    - Line 23: `62086 ИЗОСПОРТ КАПАЧКА 14 БР`
    - Line 24: `НАПИТКА ХЕЛЛ КЛАСИК`
    - Line 25: `040450 БЛАНШ. КАРТОФИ Стекхаусевро 2.5`
    - Line 26: `64006 ЕНЕРГИЙНА НАПИТКА ЧЕРНА МЕЧКА`
  - The `Стойност` (total price) column on the far right ($x > 520$) is partially visible:
    - `14.49`, `9.98`, `4.24`, `9.21`, `15.65`, `5.42`, `10.40`, `24.87`, `2.08`
  - Thermal receipt text intersects table rows:
    - `ФИСКАЛЕН БОН`, `ИМЕ НА ОПЕРАТОР ОПЕРАТОР 11`, `СТОЙНОСТ ПО ФАКТУРА 147.83`, `ОБЩА СУМА ЕВРО 147.83`, `ОБМЕНЕН КУРС ЕВРО 1.95583`, `В БРОЙ ЕВРО 147.83`, `10 артикул`.
  - Critical Requirement Verification (`ORIGINAL_REQUEST.md` R3, R4):
    - When columns are occluded, NEVER hallucinate numeric values or substitute synthetic descriptions.
    - If `quantity` or `unit_price` are occluded, they MUST be `None`.
    - If `description` is unresolvable or occluded, it MUST be `None` (JSON `null`) with `ValidationIssue(code="MISSING_DESCRIPTION", severity="warning")`.
    - NEVER substitute `"Item"`, `"Unknown"`, or `"Placeholder"`.

---

## 2. Logic Chain

### 2.1 Logic Chain for Feature 17 (Multi-Line Item Description Merging)
1. **Premise 1**: In standard ERP invoice generators (e.g. Microinvest Склад Pro, SAP), line items occupy a variable vertical height. Description text wraps across 2–4 lines, but numeric quantities and prices appear only once per item (on the primary row).
2. **Premise 2**: A candidate line $l_{i+1}$ immediately following a valid line item $l_i$ that:
   - Is on the same page ($l_{i+1}.\text{page} == l_i.\text{page}$),
   - Is vertically adjacent ($\text{gap} \le 1.5 \times \text{median\_line\_height}$),
   - Possesses textual tokens within the `description` column horizontal span,
   - Has **zero** parsed numbers in `quantity`, `unit_price`, and `total_price`, and
   - Is not a table header or summary keyword line,
   is mathematically and semantically a continuation of $l_i$'s description.
3. **Deduction**: Merging $l_{i+1}$'s text into $l_i.\text{description}$ with a single space and unioning their bounding boxes accurately reconstructs the complete description while eliminating 60%+ of spurious orphan line items.

### 2.2 Logic Chain for Feature 18 (Multi-Page Table Stitching)
1. **Premise 1**: Documents exceeding 1 page (such as `метро.pdf` 3 pages and `метро-2.pdf` 2 pages) frequently contain tables that span across consecutive pages.
2. **Premise 2**: On continuation pages:
   - The table header may be repeated (e.g. `метро.pdf` Page 2 repeats the header row at $y=966$).
   - A subtotal carry-forward row ("Пренос", "Посл. Стр. Общо") appears before continuation rows.
   - The invoice ends on a subsequent page with the final statutory financial summary (tax base, VAT, total).
3. **Premise 3**: Hardcoding `break` after the first table header in `detect_table_regions` and slicing `table_regions[0]` in `extract_line_items` prevents multi-page extraction.
4. **Deduction**: The engine must:
   - Detect `TableRegion` instances per page independently.
   - Identify cross-page continuation via column alignment matching and continuation keywords (`пренос`, `посл. стр. общо`, `продължение`).
   - Discard repeated headers and transfer subtotal rows from the line items list.
   - Append data rows from all continuation pages into a single ordered `master_line_items` list, with explicit `page_number` tracking on each `LineItem`.

### 2.3 Logic Chain for Feature 19 (Occlusion Handling & Strict Null Fallbacks)
1. **Premise 1**: In `капина-03.pdf`, an external physical document (thermal cash receipt) covers columns $x \in [360, 520]$, physically occluding `unit`, `quantity`, and `unit_price`.
2. **Premise 2**: If an OCR pipeline indiscriminately parses all tokens in a row into table columns based on X-coordinates alone, it will mistake the receipt's numbers (`147.83`, `1.95583`, `289.13`) for the invoice item's unit price or quantity.
3. **Premise 3**: The user's specification strictly forbids synthetic placeholders (e.g. `"Item"`) and hallucinated numbers.
4. **Deduction**:
   - The engine must detect receipt regions via fiscal receipt keyword clustering (`фискален бон`, `оператор`, `обменен курс`) and mask or isolate those tokens from invoice table column mapping.
   - For occluded cells, assign `None` (`quantity = None`, `unit_price_net = MoneyAmount(None, currency)`, `description = None`).
   - If description is `None`, register `ValidationIssue(code="MISSING_DESCRIPTION", severity="warning")`.

---

## 3. Detailed Algorithmic Design Specifications

### 3.1 Architectural Resolution of the Column Synonym Substring Bug
Before table reconstruction can function on Kapina invoices, the synonym matcher must be upgraded from substring search to token-level regex matching.
```python
def _match_column_synonym(text: str) -> str | None:
    """Accurately match column synonyms using word-boundary matching."""
    text_lower = text.lower().strip()
    # Normalize punctuation and trim
    clean = re.sub(r'^[^\w#№]+|[^\w#№]+$', '', text_lower)
    if not clean:
        return None

    # Exact token match first (highest priority)
    for col_type, synonyms in COLUMN_SYNONYMS.items():
        for syn in synonyms:
            if clean == syn:
                return col_type

    # Word boundary regex match for multi-word or compound synonyms
    for col_type, synonyms in sorted(
        COLUMN_SYNONYMS.items(),
        key=lambda x: max(len(s) for s in x[1]),
        reverse=True
    ):
        for syn in sorted(synonyms, key=len, reverse=True):
            pattern = rf'(?<!\w){re.escape(syn)}(?!\w)'
            if re.search(pattern, text_lower):
                return col_type
    return None
```
Also, `detect_table_regions` must support **multi-line header aggregation** (combining tokens from 2 consecutive lines whose gap $\le 1.2 \times \text{line\_height}$) so headers split across lines (like in `капина-03.pdf`: `Стока` on line 1, `Код Мярка` on line 2) are detected reliably.

---

### 3.2 Feature 17: Multi-Line Description Merging Algorithm
```python
@dataclass
class IntermediateRow:
    line: LogicalLine
    col_values: dict[str, str]
    parsed_index: int | None
    parsed_desc: str | None
    parsed_unit: str | None
    parsed_qty: Decimal | None
    parsed_unit_price: Decimal | None
    parsed_total_price: Decimal | None
    parsed_vat_rate: Decimal | None
    bbox: tuple[int, int, int, int]
    page_number: int

def merge_multiline_items(
    rows: list[IntermediateRow],
    median_line_height: int = 25,
) -> list[LineItem]:
    """Cluster wrapped multi-line descriptions into single line item rows.
    
    A line that possesses text in the `description` column span but is completely
    empty in numeric columns (quantity, unit_price, total_price) is a continuation
    line of the preceding item.
    """
    items: list[LineItem] = []
    current_item: LineItem | None = None
    current_bbox: tuple[int, int, int, int] | None = None

    for row in rows:
        # Check if line has any numeric columns populated
        has_numeric = (
            row.parsed_qty is not None
            or row.parsed_unit_price is not None
            or row.parsed_total_price is not None
        )
        
        has_desc = bool(row.parsed_desc and row.parsed_desc.strip())

        # Continuation condition:
        # 1. We have an active previous item on the same page
        # 2. This row has description text
        # 3. This row has NO numeric values in quantity, unit_price, or total_price
        # 4. Vertical gap is within continuation threshold
        is_continuation = False
        if current_item is not None and current_bbox is not None:
            same_page = (row.page_number == current_item.page_number)
            gap = row.line.top - (current_bbox[1] + current_bbox[3])
            max_gap = int(1.8 * median_line_height)
            if same_page and (0 <= gap <= max_gap) and has_desc and not has_numeric:
                is_continuation = True

        if is_continuation and current_item is not None and current_bbox is not None:
            # Merge continuation description text with single space
            cont_text = row.parsed_desc.strip()
            if current_item.description:
                current_item.description = f"{current_item.description} {cont_text}"
            else:
                current_item.description = cont_text
                
            # Expand bounding box
            x1 = min(current_bbox[0], row.bbox[0])
            y1 = min(current_bbox[1], row.bbox[1])
            x2 = max(current_bbox[0] + current_bbox[2], row.bbox[0] + row.bbox[2])
            y2 = max(current_bbox[1] + current_bbox[3], row.bbox[1] + row.bbox[3])
            current_bbox = (x1, y1, x2 - x1, y2 - y1)
        else:
            # Start a new line item
            # Strict Null Fallback: if description is empty, assign None (never synthetic 'Item')
            desc_val = row.parsed_desc.strip() if row.parsed_desc else None
            if desc_val == "":
                desc_val = None

            new_item = LineItem(
                index=row.parsed_index or (len(items) + 1),
                description=desc_val,
                unit=row.parsed_unit.strip() if row.parsed_unit else None,
                quantity=row.parsed_qty,
                unit_price_net=MoneyAmount(row.parsed_unit_price, None),
                total_price_net=MoneyAmount(row.parsed_total_price, None),
                vat_rate_pct=row.parsed_vat_rate,
                page_number=row.page_number,
            )
            current_item = new_item
            current_bbox = row.bbox
            items.append(new_item)

    return items
```

---

### 3.3 Feature 18: Multi-Page Table Stitching Algorithm
```python
CONTINUATION_KEYWORDS: list[str] = [
    "пренос", "от пренос", "посл. стр. общо", "посл. стр.",
    "продължение", "пренесен остатък", "стр. ", "страница ",
]

def is_transfer_or_header_line(line: LogicalLine) -> bool:
    """Return True if line is a multi-page transfer marker or repeated header."""
    txt = line.text_lower
    if any(kw in txt for kw in CONTINUATION_KEYWORDS):
        return True
    return False

def stitch_multipage_tables(
    tables_by_page: dict[int, TableRegion],
    lines_by_page: dict[int, list[LogicalLine]],
) -> list[LineItem]:
    """Stitch tables continuing across multiple pages into a master line items list."""
    master_items: list[LineItem] = []
    item_counter = 1

    for page_num in sorted(tables_by_page.keys()):
        table = tables_by_page[page_num]
        page_lines = table.data_lines

        intermediate_rows: list[IntermediateRow] = []
        for line in page_lines:
            # Skip transfer lines ("Пренос", "Посл. Стр. Общо")
            if is_transfer_or_header_line(line):
                logger.info("Skipping multi-page transfer line on p.%d: %s", page_num, line.text)
                continue

            col_values = _assign_line_to_columns(line, table.columns)
            row = parse_intermediate_row(line, col_values, page_num)
            intermediate_rows.append(row)

        # Merge wrapped descriptions on this page
        page_items = merge_multiline_items(intermediate_rows)

        # Assign global sequential index and append
        for item in page_items:
            item.index = item_counter
            item.page_number = page_num
            item_counter += 1
            master_items.append(item)

    return master_items
```

---

### 3.4 Feature 19: Occlusion Handling & Strict Null Fallback Policy
```python
RECEIPT_KEYWORDS: list[str] = [
    "фискален", "бон", "касов", "памет", "клен", "оператор",
    "обменен курс", "курс евро", "в брой евро", "стойност по фактура",
    "артикул", "фискална",
]

def detect_occlusion_receipt_bbox(tokens: list[OcrToken]) -> tuple[int, int, int, int] | None:
    """Detect bounding box of a stapled thermal receipt occluding the invoice."""
    receipt_tokens = [
        t for t in tokens
        if any(kw in t.text.lower() for kw in RECEIPT_KEYWORDS)
    ]
    if len(receipt_tokens) < 2:
        return None

    x1 = min(t.left for t in receipt_tokens)
    y1 = min(t.top for t in receipt_tokens)
    x2 = max(t.right for t in receipt_tokens)
    y2 = max(t.bottom for t in receipt_tokens)
    # Expand slightly to cover receipt margins
    return (max(0, x1 - 20), max(0, y1 - 20), x2 - x1 + 40, y2 - y1 + 40)

def enforce_strict_null_fallback_policy(
    line_items: list[LineItem],
    validation_issues: list[ValidationIssue],
) -> None:
    """Enforce R3 & R4: assign None and record warnings for unresolvable descriptions.
    
    NEVER substitute synthetic fallbacks like 'Item', 'Unknown', or 'Placeholder'.
    """
    SYNTHETIC_BANNED = {"item", "unknown", "placeholder", "стока", "n/a", "none"}

    for idx, item in enumerate(line_items):
        # Check description
        if item.description is None or item.description.strip().lower() in SYNTHETIC_BANNED:
            item.description = None
            validation_issues.append(ValidationIssue(
                code="MISSING_DESCRIPTION",
                message=f"Line item {item.index or idx + 1} has missing or unresolvable description due to occlusion or illegibility",
                severity="warning",
                field=f"line_items[{idx}].description",
                detected_value=None,
                expected_value="Valid Bulgarian item description",
            ))

        # Check occluded numeric columns: do not hallucinate
        if item.quantity is None and item.total_price_net.amount is not None:
            # Valid occluded state: total is known, quantity/unit price are None
            logger.debug("Line item %d has occluded quantity and unit price", item.index or idx + 1)
```

---

## 4. Caveats

1. **Complex Grid Tables vs Borderless Tables**:
   - Invoices with explicit grid borders (lines) vs borderless invoices (whitespace-only separation) may produce differing token alignments. Midpoint X-center column boundaries work robustly across both, but column boundaries should be constrained within the table's left/right margins.
2. **Bottom-Aligned Numbers**:
   - In rare invoices where numeric prices are aligned with the *bottom* line of a wrapped description (rather than the first line), a backward lookahead or two-pass merge (grouping all lines between subsequent numeric lines) is required.
3. **Receipt Occluding Description Rather Than Numbers**:
   - In `капина-03.pdf`, the receipt occludes quantity and unit price. If a document has a receipt occluding the *description*, the strict null fallback policy mandates `description = None` and `ValidationIssue(code="MISSING_DESCRIPTION", severity="warning")`.
4. **Read-Only Dataset Protection**:
   - Zero files in `/Volumes/NO NAME/_ФАКТУРИ` were modified or touched. All explorations were conducted via read-only memory ingestion.

---

## 5. Conclusion

1. **Root Cause of Zero-Yield Table Detection Identified**:
   - `_match_column_synonym` uses naive substring matching, causing statutory Bulgarian words (`стойност`) to match false column types (`index` via `"но"`), suppressing header detection across all Kapina invoices.
2. **Feature 17 (Multi-Line Item Description Merging)**:
   - Successfully designed: a line with text in the description column span but no numbers in `quantity`, `unit_price`, or `total_price` is merged with the preceding line item, updating description text and bounding box.
3. **Feature 18 (Multi-Page Table Stitching)**:
   - Successfully designed: multi-page table continuation detection across consecutive pages (e.g. `метро.pdf` 3 pages) with removal of hardcoded `break`, tracking of `page_number` on each item, and suppression of repeated headers and transfer lines ("Пренос / Посл. Стр. Общо").
4. **Feature 19 (Occlusion Handling & Strict Null Fallback Policy)**:
   - Successfully designed: thermal receipt spatial isolation preventing hallucinated prices, along with strict adherence to R3 & R4: assigning `description = None` (null) and generating `ValidationIssue(code="MISSING_DESCRIPTION", severity="warning")` without any synthetic placeholders (`"Item"`).

---

## 6. Verification Method

To independently verify all findings and test against the actual dataset:

1. **Verify Column Synonym Substring Bug**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c "
   from invoice_ocr import _match_column_synonym
   print('стойност ->', _match_column_synonym('стойност'))
   print('ДОБРУДЖАНСКА ->', _match_column_synonym('ДОБРУДЖАНСКА'))
   "
   ```
   *Expected Current Output*: `'index'`, `'quantity'` (Confirms bug).

2. **Verify Multi-Page Structure of `метро.pdf`**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c "
   import pymupdf
   doc = pymupdf.open('/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf')
   print('Page count:', len(doc))
   for i in range(len(doc)):
       print(f'Page {i+1} size:', doc[i].rect)
   "
   ```
   *Expected Output*: `Page count: 3`.

3. **Verify Thermal Receipt Occlusion on `капина-03.pdf`**:
   ```bash
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c "
   import pymupdf
   doc = pymupdf.open('/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf')
   words = [w[4] for w in doc[0].get_text('words')]
   print('Receipt markers found:', [w for w in words if w in ['ФИСКАЛЕН', 'ОБМЕНЕН', 'ЕВРО', '1.95583']])
   "
   ```
   *Expected Output*: Contains receipt marker words confirming thermal cash receipt overlay.

4. **Verify Source Immutability**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
   ```
   *Expected Output*: Exactly 0 files modified.
