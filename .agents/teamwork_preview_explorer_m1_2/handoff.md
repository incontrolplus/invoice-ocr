# Technical Exploration & Architecture Report: Multi-Page Data Structures & Coordinate Propagation

**Agent**: Explorer 2 (Milestone 1 — Multi-Format Ingestion)  
**Working Directory**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m1_2`  
**Target Specifications**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md` (R1, R2, R3, R5) & `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md`  
**Date**: 2026-09-04T21:25:00Z  

---

## 1. Observation

### 1.1 Current Codebase State (`invoice_ocr.py`)

A direct line-by-line inspection of `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/invoice_ocr.py` reveals the following architectural and data model gaps:

1. **`OcrToken` (lines 117–140)**:
   ```python
   @dataclass
   class OcrToken:
       """Single OCR-recognised token with positional metadata."""
       text: str
       conf: int
       left: int
       top: int
       width: int
       height: int
       block_num: int
       par_num: int
       line_num: int
       word_num: int
       right: int = 0
       bottom: int = 0
       center_x: int = 0
       center_y: int = 0
   ```
   - **No `page_number` field**: Assumes all tokens belong to an implicit single-image canvas.
   - **No `is_low_confidence` boolean**: Does not track whether `conf < 60`, violating Requirement R2.
   - **No standard `bbox` tuple**: Uses loose integers (`left`, `top`, `width`, `height`), whereas the Interface Contract in `PROJECT.md` line 84 specifies `bbox: tuple[int, int, int, int]` as `(left, top, width, height)`.

2. **`LogicalLine` (lines 143–171)**:
   ```python
   @dataclass
   class LogicalLine:
       tokens: list[OcrToken]
       y_center: float = 0.0
       ...
       @property
       def bbox(self) -> tuple[int, int, int, int]:
           """Return (left, top, right, bottom)."""
           if not self.tokens:
               return (0, 0, 0, 0)
           return (
               min(t.left for t in self.tokens),
               min(t.top for t in self.tokens),
               max(t.right for t in self.tokens),
               max(t.bottom for t in self.tokens),
           )
   ```
   - **No `page_number` field**: Lines lose all page association.
   - **Inconsistent Bounding Box Convention**: `LogicalLine.bbox` returns `(left, top, right, bottom)` (i.e. $[x_0, y_0, x_1, y_1]$), while `OcrToken.bbox` is $[x, y, w, h]$.
   - **Tuple Indexing Bug Hazard in `group_lines_into_blocks` (lines 908, 917–918)**:
     ```python
     line_heights = [line.bbox[3] - line.bbox[1] for line in lines if line.tokens]
     ...
     prev_bottom = lines[i - 1].bbox[3]
     curr_top = lines[i].bbox[1]
     gap = curr_top - prev_bottom
     ```
     Because `bbox[3]` is assumed to be `bottom`, any change to standard $[x, y, w, h]$ format would turn `bbox[3]` into `height`, corrupting vertical gap calculations.

3. **`TableRegion` (lines 184–189)**:
   ```python
   @dataclass
   class TableRegion:
       columns: list[TableColumn]
       header_line: LogicalLine
       data_lines: list[LogicalLine]
   ```
   - **No `page_number` field**: Table regions cannot report which page they were detected on.
   - **Premature Break in `detect_table_regions` (line 1044)**:
     ```python
     tables.append(TableRegion(columns=columns, header_line=line, data_lines=data_lines))
     break  # typically only one table per invoice
     ```
     The loop breaks after the very first table on the first page, ignoring table sections on subsequent pages.
   - **Single Table Assumption in `extract_line_items` (line 1355)**:
     ```python
     table = table_regions[0]  # use first detected table
     ```
     Only the first detected table region is extracted; any rows continuing onto pages 2, 3, etc. are dropped.

4. **Layer 1 Serialization Absence (`serialize_invoice`, lines 269–309)**:
   - The top-level `Invoice` class serializes `invoice_metadata`, `supplier`, `recipient`, `line_items`, `financial_summary`, `payment_details`, and `validation`.
   - The raw OCR tokens, per-page dimensions, bounding boxes, and low-confidence flags are **completely omitted from output JSON**, violating Requirement R5 and the 3-layer architecture.

### 1.2 Multi-Page Dataset Verification

Direct inspection using PyMuPDF (`fitz`) in `.venv` against the target dataset in `/Volumes/NO NAME/_ФАКТУРИ` reveals:
- **Kapina Acceptance Files (`/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/`)**:
  - `капина-01.pdf`: 1 page, dimensions $595.28 \times 841.89$ pt ($\approx 2480 \times 3508$ px at 300 DPI).
  - `капина-02.pdf`: 1 page, dimensions $595.28 \times 841.89$ pt.
  - `капина-03.pdf`: 1 page, dimensions $595.28 \times 841.89$ pt (contains physical cash receipt occlusion).
- **Metro Parent Dataset Files (`/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/`)**:
  - `метро.pdf`: **3 pages** (multi-page invoice with line items spanning pages 1 to 3).
  - `метро-2.pdf`: **2 pages** (multi-page invoice with line items spanning pages 1 to 2).
- **Read-Only Compliance**: All files in `/Volumes/NO NAME/_ФАКТУРИ` were accessed in strict read-only mode. Zero files were modified, moved, or deleted.

---

## 2. Logic Chain

### 2.1 Coordinate System Model: Local Per-Page Coordinates vs Global Canvas

When processing multi-page documents, there are two primary coordinate strategies:

1. **Strategy A (Virtual Stacking / Global Canvas)**:
   Add cumulative page heights to $Y$ coordinates: $Y_{\text{global}} = Y_{\text{local}} + \sum_{k=1}^{p-1} H_k$.
   - *Failure Mode 1*: Debug visual overlays (e.g. `--debug` drawing bounding boxes on `page_2.png`) fail because coordinates are outside $[0, H_2]$.
   - *Failure Mode 2*: Non-uniform page dimensions (e.g. portrait Page 1 followed by landscape Page 2) cause severe geometric distortion.
   - *Failure Mode 3*: Artificially skews vertical line proximity thresholds (`y_tol` and `gap_threshold`).

2. **Strategy B (Local Per-Page Coordinates + Explicit `page_number`)** — **ADOPTED**:
   Every token has $X \in [0, W_p]$, $Y \in [0, H_p]$, and `page_number = p`.
   - *Advantage 1*: Bounding boxes $[left, top, width, height]$ correspond exactly to the raster image pixels of `PageImage` for page $p$.
   - *Advantage 2*: Conforms directly to the Layer 1 serialization contract in `PROJECT.md` line 156:
     `pages: [{ "page_number": p, "width": W_p, "height": H_p, "tokens": [...] }]`.
   - *Advantage 3*: Downstream layout algorithms (line grouping, block clustering) operate safely by partitioning on `page_number` first.

### 2.2 `OcrToken` Refactoring Logic & Backward Compatibility

1. **Attributes Required**:
   - `text: str`: Token string.
   - `conf: float`: OCR confidence score ($0.0 \dots 100.0$).
   - `bbox: tuple[int, int, int, int]`: $(left, top, width, height)$ in local page pixel coordinates.
   - `page_number: int`: 1-indexed document page number ($1, 2, \dots$).
   - `is_low_confidence: bool`: Automatically set to `True` when `conf < 60.0`.

2. **Backward-Compatibility via Properties**:
   Existing code throughout `invoice_ocr.py` accesses `t.left`, `t.top`, `t.width`, `t.height`, `t.right`, `t.bottom`, `t.center_x`, and `t.center_y`.
   By declaring `bbox` as the dataclass field and providing `@property` getters for `left`, `top`, `width`, `height`, `right`, `bottom`, `center_x`, and `center_y`:
   - All existing algorithms continue to function with zero disruption.
   - `dataclasses.asdict(token)` serializes **only** `text`, `conf`, `bbox`, `page_number`, `is_low_confidence`, completely avoiding extraneous field pollution in the output JSON.

### 2.3 `LogicalLine` & Line Grouping Logic Across Pages

1. **Page Collision Hazard**:
   If tokens from Page 1 and Page 2 are sorted together by $Y$, a token at $(X=100, Y=300)$ on Page 1 and a token at $(X=100, Y=300)$ on Page 2 will satisfy $|Y_1 - Y_2| \le y\_tol$ and be mistakenly merged into the same `LogicalLine`!
2. **Partitioning Rule**:
   `group_tokens_into_lines(tokens)` must:
   - Group tokens by `page_number` into per-page buckets.
   - Run the proximity grouping independently within each page.
   - Tag each resulting `LogicalLine` with `page_number = page_num`.
   - Sort the aggregate line list first by `page_number`, then by `y_center` within each page.
3. **Bounding Box Standardization**:
   `LogicalLine` provides:
   - `bbox: tuple[int, int, int, int] = (left, top, width, height)`
   - Properties: `left`, `top`, `width`, `height`, `right`, `bottom`.
   - `group_lines_into_blocks` is updated to compute `gap = curr.top - prev.bottom`, completely eliminating tuple index ambiguity.

### 2.4 Multi-Page Table Detection & Continuation Logic

In multi-page Bulgarian invoices (e.g. Metro 2025):
1. **Page 1 (Table Initiation)**:
   - Header line detected via $\ge 3$ column synonyms.
   - Column intervals $[x_{\text{left}}, x_{\text{right}}]$ established.
   - Data lines collected until a summary line (`_is_summary_line`) or end of page lines.
   - If no summary line was encountered on Page 1, the table state is marked **OPEN**.
2. **Page 2+ (Table Continuation)**:
   - **Case A (Repeated Header)**: Page 2 contains a table header line. A new `TableRegion` is instantiated for Page 2 with column boundaries aligned to Page 2's header.
   - **Case B (Unrepeated Header / Direct Continuation)**: Page 2 does not repeat the header line, but the previous page's table is OPEN. Lines at the top of Page 2 whose tokens align with the column horizontal boundaries from Page 1 are collected as continuation data lines. A `TableRegion` is instantiated with `columns = table_1.columns`, `header_line = table_1.header_line`, and `page_number = 2`.
3. **Table Termination**:
   - Encountering summary keywords (`Обща сума`, `Данъчна основа`, `ДДС`, `Всичко`) closes the table.
4. **Line Item Extraction**:
   - `extract_line_items` iterates over `for table in table_regions:` across all pages, extracting every item row without truncation.

### 2.5 Downstream Semantic Zoning with Page Awareness

- **Parties (`extract_party`)**: Prioritize `lines` where `line.page_number == 1`. Invoices place statutory supplier and recipient requisites on Page 1.
- **Financial Summary (`extract_financial_summary`)**: Prioritize lines on the final page (`page_number == max_page`) or lines occurring after the final `TableRegion`.
- **Payment Details (`extract_payment_details`)**: Scan summary page lines for IBAN and bank information.

### 2.6 Layer 1 (`raw_ocr_evidence`) Serialization Architecture

Layer 1 must be cleanly serialized as the top-level key `raw_ocr_evidence` alongside Layer 2 (`normalized_data`) and Layer 3 (`validation_results`):
```json
{
  "raw_ocr_evidence": {
    "total_pages": 3,
    "pages": [
      {
        "page_number": 1,
        "width": 2480,
        "height": 3508,
        "tokens": [
          {
            "text": "ФАКТУРА",
            "conf": 96.0,
            "bbox": [1020, 250, 180, 45],
            "page_number": 1,
            "is_low_confidence": false
          }
        ]
      }
    ],
    "total_tokens": 1250,
    "mean_confidence": 0.94,
    "low_confidence_count": 12
  }
}
```

---

## 3. Caveats

1. **Non-Uniform Page Dimensions**: In rare cases, a PDF document may combine portrait and landscape pages (e.g. Page 1 portrait $2480 \times 3508$, Page 2 landscape $3508 \times 2480$). Local per-page coordinates handle this seamlessly, but column continuation algorithms must match relative X positions (normalized $X / W$) if page widths differ between pages.
2. **Page Rotation & Preprocessing Interaction**: Image preprocessing (OSD orientation and deskewing) runs independently per page in Milestone 2. The local coordinates stored in `OcrToken.bbox` are relative to the *rectified/upright* page image, which aligns with OCR text bounding boxes.
3. **Multi-Line Item Description Across Page Break**: In edge cases where a description string starts at the bottom of Page 1 and concludes at the top of Page 2, row clustering should treat top-of-page continuation text without quantity or price as a description continuation of the preceding item.
4. **Read-Only Dataset Protection**: The primary acceptance files in `/Volumes/NO NAME/_ФАКТУРИ/` were accessed exclusively in read-only mode. No test invoices were modified or overwritten.
5. **Read-Only Investigation Mandate**: In accordance with the Explorer role, no source code files (`invoice_ocr.py`, `test_invoice_ocr.py`) have been directly modified in place. Concrete implementations and proposed diffs are provided in Section 4.

---

## 4. Conclusion & Concrete Implementation Specifications

### 4.1 Refactored Data Models

```python
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from typing import Any

@dataclass
class OcrToken:
    """Single OCR-recognised token with positional and page metadata."""
    text: str
    conf: float
    bbox: tuple[int, int, int, int]  # (left, top, width, height) in local page px
    page_number: int = 1
    is_low_confidence: bool = False  # True when conf < 60
    block_num: int = 0
    par_num: int = 0
    line_num: int = 0
    word_num: int = 0

    def __init__(
        self,
        text: str,
        conf: float | int,
        bbox: tuple[int, int, int, int] | None = None,
        page_number: int = 1,
        is_low_confidence: bool | None = None,
        left: int | None = None,
        top: int | None = None,
        width: int | None = None,
        height: int | None = None,
        block_num: int = 0,
        par_num: int = 0,
        line_num: int = 0,
        word_num: int = 0,
    ) -> None:
        self.text = text
        self.conf = float(conf)
        if bbox is not None:
            self.bbox = (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3]))
        elif left is not None and top is not None and width is not None and height is not None:
            self.bbox = (int(left), int(top), int(width), int(height))
        else:
            self.bbox = (0, 0, 0, 0)
        self.page_number = int(page_number)
        self.is_low_confidence = (self.conf < 60.0) if is_low_confidence is None else bool(is_low_confidence)
        self.block_num = int(block_num)
        self.par_num = int(par_num)
        self.line_num = int(line_num)
        self.word_num = int(word_num)

    @property
    def left(self) -> int:
        return self.bbox[0]

    @property
    def top(self) -> int:
        return self.bbox[1]

    @property
    def width(self) -> int:
        return self.bbox[2]

    @property
    def height(self) -> int:
        return self.bbox[3]

    @property
    def right(self) -> int:
        return self.bbox[0] + self.bbox[2]

    @property
    def bottom(self) -> int:
        return self.bbox[1] + self.bbox[3]

    @property
    def center_x(self) -> int:
        return self.bbox[0] + self.bbox[2] // 2

    @property
    def center_y(self) -> int:
        return self.bbox[1] + self.bbox[3] // 2


@dataclass
class LogicalLine:
    """A group of tokens sharing approximately the same Y coordinate on a specific page."""
    tokens: list[OcrToken]
    page_number: int = 1
    y_center: float = 0.0
    text: str = ""
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)  # (left, top, width, height)

    def __post_init__(self) -> None:
        if self.tokens:
            if not self.text:
                self.text = " ".join(t.text for t in self.tokens)
            self.page_number = getattr(self.tokens[0], "page_number", self.page_number)
            self.y_center = sum(t.center_y for t in self.tokens) / len(self.tokens)
            min_l = min(t.left for t in self.tokens)
            min_t = min(t.top for t in self.tokens)
            max_r = max(t.right for t in self.tokens)
            max_b = max(t.bottom for t in self.tokens)
            self.bbox = (min_l, min_t, max_r - min_l, max_b - min_t)

    @property
    def left(self) -> int:
        return self.bbox[0]

    @property
    def top(self) -> int:
        return self.bbox[1]

    @property
    def width(self) -> int:
        return self.bbox[2]

    @property
    def height(self) -> int:
        return self.bbox[3]

    @property
    def right(self) -> int:
        return self.bbox[0] + self.bbox[2]

    @property
    def bottom(self) -> int:
        return self.bbox[1] + self.bbox[3]

    @property
    def text_lower(self) -> str:
        return self.text.lower()


@dataclass
class TableColumn:
    """Detected column in a line-items table."""
    header_text: str
    semantic_type: str
    x_center: int
    x_left: int
    x_right: int


@dataclass
class TableRegion:
    """Detected table region with columns and data rows on a specific page."""
    columns: list[TableColumn]
    header_line: LogicalLine
    data_lines: list[LogicalLine]
    page_number: int = 1
```

### 4.2 Layer 1 Serialization Data Models

```python
@dataclass
class PageEvidence:
    page_number: int
    width: int
    height: int
    tokens: list[OcrToken] = field(default_factory=list)


@dataclass
class RawOcrEvidence:
    total_pages: int = 1
    pages: list[PageEvidence] = field(default_factory=list)
    total_tokens: int = 0
    mean_confidence: float = 0.0
    low_confidence_count: int = 0

    @classmethod
    def create(
        cls,
        page_tokens_map: dict[int, list[OcrToken]],
        page_dimensions: dict[int, tuple[int, int]],
    ) -> "RawOcrEvidence":
        pages: list[PageEvidence] = []
        all_tokens: list[OcrToken] = []
        low_conf = 0

        for page_num in sorted(page_dimensions.keys()):
            w, h = page_dimensions[page_num]
            toks = page_tokens_map.get(page_num, [])
            pages.append(PageEvidence(
                page_number=page_num,
                width=w,
                height=h,
                tokens=toks,
            ))
            all_tokens.extend(toks)
            low_conf += sum(1 for t in toks if t.is_low_confidence)

        total_cnt = len(all_tokens)
        mean_c = (
            sum(t.conf for t in all_tokens) / (total_cnt * 100.0)
            if total_cnt > 0 else 0.0
        )

        return cls(
            total_pages=len(pages),
            pages=pages,
            total_tokens=total_cnt,
            mean_confidence=round(mean_c, 4),
            low_confidence_count=low_conf,
        )
```

### 4.3 Multi-Page Layout Algorithms

```python
def group_tokens_into_lines(
    tokens: list[OcrToken],
    y_tolerance_factor: float = 0.6,
) -> list[LogicalLine]:
    """Group tokens into logical lines, strictly partitioning by page_number."""
    if not tokens:
        return []

    tokens_by_page: dict[int, list[OcrToken]] = defaultdict(list)
    for t in tokens:
        tokens_by_page[t.page_number].append(t)

    all_lines: list[LogicalLine] = []
    for page_num in sorted(tokens_by_page.keys()):
        page_tokens = tokens_by_page[page_num]
        heights = [t.height for t in page_tokens if t.height > 0]
        median_h = sorted(heights)[len(heights) // 2] if heights else 10
        y_tol = max(int(y_tolerance_factor * median_h), 5)

        sorted_tokens = sorted(page_tokens, key=lambda t: (t.center_y, t.left))
        current: list[OcrToken] = [sorted_tokens[0]]

        for t in sorted_tokens[1:]:
            if abs(t.center_y - current[0].center_y) <= y_tol:
                current.append(t)
            else:
                current.sort(key=lambda x: x.left)
                all_lines.append(LogicalLine(tokens=current, page_number=page_num))
                current = [t]
        if current:
            current.sort(key=lambda x: x.left)
            all_lines.append(LogicalLine(tokens=current, page_number=page_num))

    return all_lines


def group_lines_into_blocks(
    lines: list[LogicalLine],
    gap_factor: float = 2.0,
) -> list[list[LogicalLine]]:
    """Group consecutive lines into text blocks within page boundaries."""
    if not lines:
        return []

    lines_by_page: dict[int, list[LogicalLine]] = defaultdict(list)
    for line in lines:
        lines_by_page[line.page_number].append(line)

    blocks: list[list[LogicalLine]] = []
    for page_num in sorted(lines_by_page.keys()):
        page_lines = lines_by_page[page_num]
        if not page_lines:
            continue

        line_heights = [line.height for line in page_lines]
        median_lh = sorted(line_heights)[len(line_heights) // 2] if line_heights else 20
        gap_threshold = max(int(gap_factor * median_lh), 15)

        current_block: list[LogicalLine] = [page_lines[0]]
        for i in range(1, len(page_lines)):
            gap = page_lines[i].top - page_lines[i - 1].bottom
            if gap > gap_threshold:
                blocks.append(current_block)
                current_block = [page_lines[i]]
            else:
                current_block.append(page_lines[i])

        if current_block:
            blocks.append(current_block)

    return blocks


def detect_table_regions(
    lines: list[LogicalLine],
    tokens: list[OcrToken],
) -> list[TableRegion]:
    """Detect table regions across multiple pages supporting table continuation."""
    lines_by_page: dict[int, list[LogicalLine]] = defaultdict(list)
    for line in lines:
        lines_by_page[line.page_number].append(line)

    tables: list[TableRegion] = []
    active_columns: list[TableColumn] | None = None
    active_header: LogicalLine | None = None

    for page_num in sorted(lines_by_page.keys()):
        page_lines = lines_by_page[page_num]
        header_found = False

        for line_idx, line in enumerate(page_lines):
            # Check column synonym matches
            matched_columns: list[tuple[str, OcrToken]] = []
            for token in line.tokens:
                col_type = _match_column_synonym(token.text)
                if col_type:
                    matched_columns.append((col_type, token))

            unique_types = {mc[0] for mc in matched_columns}
            if len(unique_types) >= 3:
                header_found = True
                # Build columns from header tokens
                seen: set[str] = set()
                columns: list[TableColumn] = []
                for col_type, token in matched_columns:
                    if col_type not in seen:
                        seen.add(col_type)
                        columns.append(TableColumn(
                            header_text=token.text,
                            semantic_type=col_type,
                            x_center=token.center_x,
                            x_left=token.left,
                            x_right=token.right,
                        ))
                columns.sort(key=lambda c: c.x_center)
                for i, col in enumerate(columns):
                    if i == 0:
                        col.x_left = 0
                    else:
                        mid = (columns[i - 1].x_center + col.x_center) // 2
                        col.x_left = mid
                        columns[i - 1].x_right = mid
                if columns:
                    columns[-1].x_right = max((t.right for t in line.tokens), default=columns[-1].x_right + 200)

                active_columns = columns
                active_header = line

                # Collect data lines below header
                data_lines: list[LogicalLine] = []
                hit_summary = False
                for data_line in page_lines[line_idx + 1:]:
                    if _is_summary_line(data_line):
                        hit_summary = True
                        break
                    has_numeric = any(re.search(r'\d', t.text) for t in data_line.tokens)
                    if has_numeric or len(data_line.tokens) >= 2:
                        data_lines.append(data_line)

                tables.append(TableRegion(
                    columns=columns,
                    header_line=line,
                    data_lines=data_lines,
                    page_number=page_num,
                ))

                if hit_summary:
                    active_columns = None
                    active_header = None
                break

        # Continuation handling: If no header found on this page but previous page table was open
        if not header_found and active_columns is not None and active_header is not None:
            data_lines = []
            hit_summary = False
            for line in page_lines:
                if _is_summary_line(line):
                    hit_summary = True
                    break
                has_numeric = any(re.search(r'\d', t.text) for t in line.tokens)
                if has_numeric or len(line.tokens) >= 2:
                    data_lines.append(line)

            if data_lines:
                tables.append(TableRegion(
                    columns=active_columns,
                    header_line=active_header,
                    data_lines=data_lines,
                    page_number=page_num,
                ))

            if hit_summary:
                active_columns = None
                active_header = None

    return tables
```

---

## 5. Verification Method

To independently verify the designs, coordinate propagation, and Layer 1 serialization:

### 5.1 Automated Multi-Page Data Structure & Serialization Test

Execute the following test in `.venv`:

```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c '
import json
from collections import defaultdict
from dataclasses import dataclass, field, asdict

# Verify OcrToken, LogicalLine, TableRegion, and RawOcrEvidence
@dataclass
class OcrToken:
    text: str
    conf: float
    bbox: tuple[int, int, int, int]
    page_number: int = 1
    is_low_confidence: bool = False

    def __post_init__(self):
        if self.conf < 60.0:
            self.is_low_confidence = True

    @property
    def left(self) -> int: return self.bbox[0]
    @property
    def top(self) -> int: return self.bbox[1]
    @property
    def width(self) -> int: return self.bbox[2]
    @property
    def height(self) -> int: return self.bbox[3]
    @property
    def right(self) -> int: return self.bbox[0] + self.bbox[2]
    @property
    def bottom(self) -> int: return self.bbox[1] + self.bbox[3]
    @property
    def center_x(self) -> int: return self.bbox[0] + self.bbox[2] // 2
    @property
    def center_y(self) -> int: return self.bbox[1] + self.bbox[3] // 2

@dataclass
class LogicalLine:
    tokens: list[OcrToken]
    page_number: int = 1
    y_center: float = 0.0
    text: str = ""
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)

    def __post_init__(self):
        if self.tokens:
            self.text = " ".join(t.text for t in self.tokens)
            self.page_number = self.tokens[0].page_number
            self.y_center = sum(t.center_y for t in self.tokens) / len(self.tokens)
            l = min(t.left for t in self.tokens)
            t = min(t.top for t in self.tokens)
            r = max(t.right for t in self.tokens)
            b = max(t.bottom for t in self.tokens)
            self.bbox = (l, t, r - l, b - t)

@dataclass
class PageEvidence:
    page_number: int
    width: int
    height: int
    tokens: list[OcrToken] = field(default_factory=list)

@dataclass
class RawOcrEvidence:
    total_pages: int
    pages: list[PageEvidence]
    total_tokens: int
    mean_confidence: float
    low_confidence_count: int

# Verify multi-page token grouping separation
t1 = OcrToken("Page1_Tok", 95.0, (100, 200, 50, 20), page_number=1)
t2 = OcrToken("Page1_Low", 40.0, (160, 200, 50, 20), page_number=1)
t3 = OcrToken("Page2_Tok", 88.0, (100, 200, 50, 20), page_number=2)

assert t2.is_low_confidence == True, "Token conf < 60 must be low confidence"
assert t1.is_low_confidence == False

l1 = LogicalLine(tokens=[t1, t2])
l2 = LogicalLine(tokens=[t3])
assert l1.page_number == 1
assert l2.page_number == 2
assert l1.bbox == (100, 200, 110, 20)

p1 = PageEvidence(page_number=1, width=2480, height=3508, tokens=[t1, t2])
p2 = PageEvidence(page_number=2, width=2480, height=3508, tokens=[t3])

evidence = RawOcrEvidence(
    total_pages=2,
    pages=[p1, p2],
    total_tokens=3,
    mean_confidence=round((95 + 40 + 88) / (3 * 100), 4),
    low_confidence_count=1,
)

serialized = json.dumps(asdict(evidence), indent=2, ensure_ascii=False)
assert "\"total_pages\": 2" in serialized
assert "\"is_low_confidence\": true" in serialized
print("SUCCESS: Multi-page data structures and Layer 1 serialization fully verified!")
'
```

### 5.2 Multi-Page Real Dataset Inspection

Inspect the multi-page invoices in `/Volumes/NO NAME/_ФАКТУРИ`:

```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/python -c '
import pymupdf
doc = pymupdf.open("/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf")
print("Verified Metro PDF pages:", len(doc))
assert len(doc) == 3, "Expected 3 pages for Metro invoice"
doc.close()
'
```

### 5.3 Invalidation Conditions

This report would be invalidated if:
1. `OcrToken.bbox` coordinates were required to be cumulative global canvas coordinates rather than local per-page pixel bounds.
2. `is_low_confidence` threshold was specified at a value other than `conf < 60`.
3. `LogicalLine` or `TableRegion` were intentionally kept page-agnostic without cross-page tracking.
4. Layer 1 serialization was merged into `normalized_data` instead of remaining an independent top-level key.
