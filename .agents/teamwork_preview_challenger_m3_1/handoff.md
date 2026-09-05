# Milestone 3: Spatial Layout Analysis & Table Reconstruction — Adversarial Challenge Report & Handoff

**Agent**: Challenger 1 (`teamwork_preview_challenger_m3_1`)  
**Archetype**: EMPIRICAL CHALLENGER  
**Roles**: critic, specialist  
**Target Milestone**: Milestone 3: Spatial Layout Analysis & Table Reconstruction (Features 13–19)  
**Target Specifications**: `ORIGINAL_REQUEST.md` (R3, R5) & `PROJECT.md` (Features 13–19)  
**Worker Handoff**: `.agents/teamwork_preview_worker_m3_rep/handoff.md`  
**Date**: 2026-09-05T03:25:00Z  
**Verdict**: **REQUEST_CHANGES**  
**Overall Risk Assessment**: **HIGH**  

---

## Challenge Summary

The worker has made substantial progress on Milestone 3:
- Vertical overlap chaining correctly prevents line shattering on 51% overlap, extreme staggered words, subscripts ($H_2O$), superscripts ($m^2$), and baseline punctuation (`.`, `,`, `-`).
- Residual tilt line chaining succeeds across 2000px widths at $\pm 0.5^\circ, \pm 1.0^\circ, \pm 1.5^\circ$.
- Clean continuation rows (1, 2, 3, 4 lines) with numeric strings ("Картофи 2.5 кг", "Олио 3л", "Спирт 48брХ26") merge into line items without generating spurious rows.
- Table cells containing placeholders (`"Item"`, `"Unknown"`, `"Placeholder"`, `"Артикул"`, `""`, `"  "`) within table regions are converted to `description = None` and flag `ValidationIssue(code="MISSING_DESCRIPTION")`.
- Acceptance dataset immutability is strictly maintained (0 files modified in `/Volumes/NO NAME/_ФАКТУРИ`).

However, adversarial stress testing using `tests/test_adversarial_m3.py` revealed **three concrete vulnerabilities** that fail required architectural and data integrity constraints:

1. **Multi-Column Party Isolation Failure in `group_tokens_into_lines` (Feature 13 & 14)**:  
   Tokens across Left and Right party columns at identical Y coordinates (e.g., `ПОЛУЧАТЕЛ:` at $x=100$ and `ДОСТАВЧИК:` at $x=1400$) are unconditionally concatenated into a single full-width line (`ПОЛУЧАТЕЛ: ДОСТАВЧИК:`). This occurs because condition 2 in `group_tokens_into_lines` enforces zero horizontal distance or column gap checks. While the worker patched `extract_party` with an ad-hoc midpoint filter (`in_col(t)`), spatial lines and blocks (`LogicalBlock` in Feature 14) remain fused across columns, violating multi-column isolation in spatial layout analysis.
2. **Silent Description Data Loss via Naive Substring Matching in `is_transfer_or_header_line` (Feature 17 & 18)**:  
   `CONTINUATION_KEYWORDS` contains `"стр."` and `"продължение"`, and `is_transfer_or_header_line` uses substring matching (`kw in txt`). Legitimate item descriptions or continuation lines containing common Bulgarian business text (such as `"стр. обект Люлин"`, `"стр. материали"`, `"договор - продължение"`) are falsely classified as multi-page carry-forward transfer lines and silently dropped.
3. **Incomplete Placeholder Banning and Null Sanitization in `_validate_line_items` (Feature 19)**:  
   In `_validate_line_items` (line 3062), `banned_desc = {"item", "unknown", "placeholder", "n/a", "none"}` is missing `"артикул"`, `""`, and `"  "` (which were included in `extract_line_items`). Direct validation of line items with description `"Артикул"`, `""`, or `"  "` leaves `item.description` intact and emits zero `MISSING_DESCRIPTION` issues.

---

## 1. Observation

### 1.1 Verbatim Failures in Adversarial Stress Harness (`tests/test_adversarial_m3.py`)

Command executed:
```bash
/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.venv/bin/pytest tests/test_adversarial_m3.py -v
```

Execution output:
```
=========================== short test summary info ============================
FAILED tests/test_adversarial_m3.py::TestAdversarialMultiColumnPartyIsolation::test_spatial_line_grouping_multi_column_isolation
FAILED tests/test_adversarial_m3.py::TestAdversarialMultiLineContinuation::test_continuation_line_keyword_collision_vulnerability
FAILED tests/test_adversarial_m3.py::TestAdversarialSyntheticPlaceholderRejection::test_direct_line_item_validation_placeholder_sanitization[Артикул]
FAILED tests/test_adversarial_m3.py::TestAdversarialSyntheticPlaceholderRejection::test_direct_line_item_validation_placeholder_sanitization[]
FAILED tests/test_adversarial_m3.py::TestAdversarialSyntheticPlaceholderRejection::test_direct_line_item_validation_placeholder_sanitization[  ]
=================== 5 failed, 23 passed, 5 warnings in 0.35s ===================
```

### 1.2 Failure 1: Multi-Column Party Line Merging
```
_ TestAdversarialMultiColumnPartyIsolation.test_spatial_line_grouping_multi_column_isolation _
>       assert not merged, (
            "VULNERABILITY: group_tokens_into_lines merged Left column ('ПОЛУЧАТЕЛ:') "
            "and Right column ('ДОСТАВЧИК:') across a 1150px gap into a single line!"
        )
E       AssertionError: VULNERABILITY: group_tokens_into_lines merged Left column ('ПОЛУЧАТЕЛ:') and Right column ('ДОСТАВЧИК:') across a 1150px gap into a single line!
```

Code location in `invoice_ocr.py`, lines 1870–1883:
```python
# 2. Overlap with bounding box vertical span of line
l_top = min(x.top for x in l)
l_bot = max(x.bottom for x in l)
l_h = l_bot - l_top
if l_h <= int(2.5 * max(t.height, 20)):
    v_int = max(0, min(t.bottom, l_bot) - max(t.top, l_top))
    if t.height > 0:
        scores.append(v_int / t.height)

score = max(scores) if scores else 0.0
if score >= 0.50 and score > best_score:
    best_score = score
    best_line = l
```

In `tests/test_layout_analysis.py`, lines 113–122, the worker explicitly codified and asserted this concatenation bug:
```python
def test_multi_column_token_separation(self):
    """Tokens across left and right columns are partitioned appropriately."""
    tokens = [
        OcrToken(text="Получател:", conf=90.0, bbox=(100, 300, 150, 30)),
        OcrToken(text="Доставчик:", conf=90.0, bbox=(1400, 300, 150, 30)),
    ]
    lines = group_tokens_into_lines(tokens)
    assert len(lines) == 1
    line_300 = lines[0]
    assert line_300.tokens[0].text == "Получател:"
    assert line_300.tokens[1].text == "Доставчик:"
```

### 1.3 Failure 2: Substring Dropping of Legitimate Descriptions in `is_transfer_or_header_line`
```
_ TestAdversarialMultiLineContinuation.test_continuation_line_keyword_collision_vulnerability _
>       assert "стр. обект" in (items[0].description or ""), (
            "VULNERABILITY: Continuation line containing 'стр. обект' was dropped because "
            "is_transfer_or_header_line uses substring matching on CONTINUATION_KEYWORDS ('стр.')!"
        )
E       AssertionError: VULNERABILITY: Continuation line containing 'стр. обект' was dropped because is_transfer_or_header_line uses substring matching on CONTINUATION_KEYWORDS ('стр.')!
E       assert 'стр. обект' in (('Доставка на материали'))
```

Code location in `invoice_ocr.py`, lines 164–167 and 2011–2014:
```python
CONTINUATION_KEYWORDS: list[str] = [
    "пренос", "от пренос", "посл. стр. общо", "посл. стр.", "стр. общо",
    "продължение", "пренесен остатък", "стр.", "страница",
]

def is_transfer_or_header_line(line: LogicalLine) -> bool:
    """Return True if line is a multi-page subtotal transfer line or repeated header."""
    txt = line.text_lower
    return any(kw in txt for kw in CONTINUATION_KEYWORDS)
```

In `extract_line_items` (line 2612) and `detect_table_regions` (lines 2202, 2225):
```python
if is_transfer_or_header_line(row_line):
    continue
```

### 1.4 Failure 3: Missing Placeholder Rejection in `_validate_line_items`
```
_ TestAdversarialSyntheticPlaceholderRejection.test_direct_line_item_validation_placeholder_sanitization[Артикул] _
>       assert item.description is None, (
            f"VULNERABILITY: LineItem description={placeholder!r} was not sanitized to None by _validate_line_items! "
            f"Got: {item.description!r}"
        )
E       AssertionError: VULNERABILITY: LineItem description='Артикул' was not sanitized to None by _validate_line_items! Got: 'Артикул'
```

Code location in `invoice_ocr.py`, lines 3062–3073:
```python
def _validate_line_items(invoice: Invoice) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    for i, item in enumerate(invoice.line_items):
        field_prefix = f"line_items[{i}]"

        # Check description (Feature 19: Strict Null Fallback)
        banned_desc = {"item", "unknown", "placeholder", "n/a", "none"}
        if item.description is None or item.description.strip().lower() in banned_desc:
            item.description = None
            issues.append(ValidationIssue(
                code="MISSING_DESCRIPTION",
                ...
```
`"артикул"`, `""`, and `"  "` are not present in `banned_desc`, and empty strings bypass `item.description is None or item.description.strip().lower() in banned_desc`.

---

## 2. Logic Chain

1. **Multi-Column Isolation Failure (Feature 13 & 14)**:
   - Observation 1.2 shows that `group_tokens_into_lines` evaluates candidate lines using condition 2: `v_int = max(0, min(t.bottom, l_bot) - max(t.top, l_top))`.
   - When token `t` is 1400px horizontally away from line `l`, condition 2 yields vertical overlap 1.0 because their heights align.
   - Therefore, `group_tokens_into_lines` clusters left and right column headers together.
   - When `group_lines_into_blocks` runs, these merged lines are grouped into single `LogicalBlock`s containing both Supplier and Recipient content.
   - The worker bypassed this defect in `extract_party` by re-grouping raw tokens filtered by `center_x < mid_x`, but left the core spatial layout engine broken for any downstream module consuming `LogicalLine` or `LogicalBlock`.
2. **Continuation Keyword Substring Collisions (Feature 17 & 18)**:
   - Observation 1.3 shows that `CONTINUATION_KEYWORDS` includes `"стр."` and `"продължение"`.
   - `is_transfer_or_header_line` performs simple substring checks: `any(kw in txt for kw in CONTINUATION_KEYWORDS)`.
   - Any valid Bulgarian invoice line containing `"стр."` (standard abbreviation for "строителен" / "строителство" or catalog pages) or `"продължение"` (continuation contracts) matches `is_transfer_or_header_line = True`.
   - `extract_line_items` immediately skips these lines, causing silent data omission of valid products.
3. **Strict Null Fallback Discrepancy (Feature 19)**:
   - Observation 1.4 demonstrates that `extract_line_items` filters `"артикул"`, but `_validate_line_items` omits `"артикул"`, `""`, and `"  "` from its banned set.
   - When line items with `description="Артикул"` or `description="   "` are passed into validation, `_validate_line_items` fails to sanitize them to `None` and fails to record `MISSING_DESCRIPTION`.
   - This violates the Integrity Mandate: ZERO synthetic dummy strings admitted.

---

## 3. Stress Test Results

| # | Stress Scenario | Expected Behavior | Actual Behavior | Result |
|---|----------------|-------------------|-----------------|--------|
| 1 | Overlap boundary 49% vs 51% | 51% merges, 49% splits | 51% in 1 line, 49% in 2 lines | **PASS** |
| 2 | Extreme vertical staggering (5 tokens) | 5 tokens chain into 1 line | Unified into 1 line | **PASS** |
| 3 | Subscript formula $H_2O$ | Formula preserved in 1 line | `['H 2 O']` | **PASS** |
| 4 | Superscript exponent $m^2$ | Exponent preserved in 1 line | `['100 m 2']` | **PASS** |
| 5 | Baseline punctuation (`.`, `,`, `-`) | Punctuation chains along baseline | `['гр . , - София']` | **PASS** |
| 6 | Residual tilt across 2000px ($\pm 0.5^\circ, \pm 1.0^\circ, \pm 1.5^\circ$) | 12 words form 1 continuous line | 1 continuous line across all 6 angles | **PASS** |
| 7 | Multi-column party isolation in `extract_party` | Supplier & Recipient EIKs extracted without collision | Supplier EIK 114500333, Recip EIK 207930830 | **PASS** |
| 8 | Multi-column isolation in `group_tokens_into_lines` | Left and Right columns form separate lines | Merged into `ПОЛУЧАТЕЛ: ДОСТАВЧИК:` | **FAIL** |
| 9 | 1, 2, 3, 4 continuation lines with numeric strings | Merged into item description without new rows | 4 clean items, all continuation lines merged | **PASS** |
| 10 | Continuation lines with `"стр. обект"` | Preserved as description continuation | Dropped by `is_transfer_or_header_line` | **FAIL** |
| 11 | Placeholder rejection in table extraction (`Item`, `Unknown`, `Placeholder`, `Артикул`, `""`, `"  "`) | Description set to `None`, warning issue raised | All 6 set to `None`, `MISSING_DESCRIPTION` raised | **PASS** |
| 12 | Direct LineItem validation with `"Артикул"`, `""`, `"  "` | Description set to `None`, warning issue raised | Unsanitized, 0 issues raised | **FAIL** |
| 13 | Source dataset immutability | 0 files modified in `/Volumes/NO NAME/_ФАКТУРИ` | 0 files modified | **PASS** |

---

## 4. Caveats

- In `extract_party`, the worker's midpoint pre-filter (`center_x < mid_x`) prevents EIK collision on standard 2-column invoices (such as Kapina). However, if an invoice has unequal column widths or non-party content in the party band, the global line grouping failure will propagate errors.
- Real invoice testing on `капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf` passes existing acceptance tests because those specific files do not contain `"стр."` in their item descriptions.

---

## 5. Challenges & Recommended Mitigations

### Challenge 1 (High): Multi-Column Boundary Isolation in `group_tokens_into_lines`
- **Root Cause**: Condition 2 in `group_tokens_into_lines` lacks a horizontal proximity limit. Any token matching vertical overlap is appended, even across an 1100px column gap.
- **Recommended Mitigation**:
  In `group_tokens_into_lines`, condition 2 must enforce a maximum horizontal gap between the new token `t` and the candidate line's horizontal extent (e.g. `t.left - max(x.right for x in l) <= 350` or `min(x.left for x in l) - t.right <= 350`), or require column zoning before full-line aggregation.

### Challenge 2 (High): Transfer Line False Positives in `is_transfer_or_header_line`
- **Root Cause**: Unanchored substring matching on generic words like `"стр."` and `"продължение"`.
- **Recommended Mitigation**:
  Upgrade `is_transfer_or_header_line` to use regex word boundaries and context phrases:
  ```python
  CONTINUATION_PATTERNS = [
      r'(?i)\bпренос(?:\s+на|\s+от|\s+към|\s+следваща|\s+предишна)?\b',
      r'(?i)\b(?:посл\.\s*стр\.\s*общо|стр\.\s*общо|пренесен\s+остатък)\b',
      r'(?i)^\s*(?:продължение|страница|\bстр\.\s*\d+)\s*$',
  ]
  ```
  Only classify as transfer line if it matches these structured patterns or spans multiple table columns.

### Challenge 3 (Medium): Unify Strict Null Fallback in `_validate_line_items`
- **Root Cause**: `banned_desc` in `_validate_line_items` does not match `banned_desc` in `extract_line_items`, and does not handle empty or whitespace-only descriptions.
- **Recommended Mitigation**:
  Update `_validate_line_items`:
  ```python
  banned_desc = {"item", "unknown", "placeholder", "n/a", "none", "артикул", "null", ""}
  desc_clean = (item.description or "").strip().lower()
  if not desc_clean or desc_clean in banned_desc:
      item.description = None
      issues.append(ValidationIssue(
          code="MISSING_DESCRIPTION",
          message=f"Line {i+1} has missing or unresolvable description due to occlusion or illegibility",
          severity="warning",
          field=f"{field_prefix}.description",
          detected_value=None,
          expected_value="Valid item description",
      ))
  ```

---

## 6. Conclusion & Verdict

**Verdict**: **REQUEST_CHANGES**  
The implementation exhibits high quality in geometric angle tolerance and numeric column parsing, but suffers from three defects that break multi-column isolation, silently drop legitimate descriptions containing `"стр."`, and permit bypass of synthetic placeholder validation. 

---

## 7. Verification Method

Run the following commands to independently verify all findings and test suites:

```bash
# 1. Run adversarial test suite authored by Challenger 1 (23 passed, 5 failed demonstrating vulnerabilities)
.venv/bin/pytest tests/test_adversarial_m3.py -v

# 2. Run existing Milestone 3 layout and table reconstruction unit tests (30 passed)
.venv/bin/pytest tests/test_layout_analysis.py tests/test_table_reconstruction.py -v

# 3. Run full regression suite across all prior milestones (128 passed)
.venv/bin/pytest tests/test_layout_analysis.py tests/test_table_reconstruction.py tests/test_preprocessing.py tests/test_ocr_engine.py tests/test_adversarial_ingestion.py tests/test_ingestion.py -q

# 4. Verify read-only dataset immutability (0 files modified)
find "/Volumes/NO NAME/_ФАКТУРИ" -newerct "2026-09-04"
```
