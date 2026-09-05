# Test Infrastructure Specification: Bulgarian Invoice OCR Pipeline

## 1. Test Philosophy

The Bulgarian Invoice OCR & Document Understanding Pipeline operates under an **opaque-box, requirement-driven testing philosophy**. The test suite treats the document processing pipeline as a black box whose behavior is strictly governed by:
1. The authoritative specification in `ORIGINAL_REQUEST.md`.
2. The architectural contract and interface specifications in `PROJECT.md`.
3. Bulgarian statutory legislation:
   - **Закон за данък върху добавената стойност (ЗДДС), чл. 114**: Mandatory invoice requisites, tax event dates, and VAT rate tiers (20%, 9%, 0%).
   - **Закон за счетоводството (ЗСч), чл. 6 & 7**: Accounting document integrity and party requisites.
   - **Закон за търговския регистър и регистър БУЛСТАТ**: 9-digit and 13-digit EIK/BULSTAT Modulo-11 checksums.
   - **Закон за въвеждане на еврото в Република България (ЗВЕ)**: Fixed exchange parity (1.95583 BGN = 1 EUR), post-2026-01-01 warnings, post-2026-08-08 dual display deadline, and zero auto-conversion rule.
   - **Българска народна банка (БНБ) Наредба № 3**: 22-character Bulgarian IBAN format (`BGkk BBBB SSSS TT CCCCCCCC`) and ISO 7064 Modulo 97-10 validation.

### Core Testing Principles:
- **Requirement-Driven & Opaque-Box**: Tests are derived from observable external contracts (file inputs, CLI arguments, structured 3-layer JSON output, exit codes, and mathematical invariants) rather than internal implementation details.
- **Strict Read-Only Protection**: The acceptance dataset at `/Volumes/NO NAME/_ФАКТУРИ` is strictly read-only. Test cases must assert that zero files are modified, created, or deleted on the external volume.
- **Deterministic Financial Arithmetic**: Financial values are evaluated using Python `Decimal` objects with standard `ROUND_HALF_UP` semantics and an allowed statutory tolerance of $\le 0.02$ currency units.
- **No Facade Tests**: Every test assertion validates genuine mathematical or business logic. Tests cannot be satisfied by hardcoded constant stubs.
- **Independence & Isolation**: All test cases are independent, idempotent, and self-cleaning. Temporary test artifacts are generated in sandboxed temporary directories and removed upon test completion.

---

## 2. Feature Inventory Mapping to Test Tiers

The 47 features identified in `PROJECT.md` are systematically partitioned across four testing tiers:

| Tier | Name | Target Features | Scope & Focus | Minimum Test Count |
| :--- | :--- | :--- | :--- | :--- |
| **Tier 1** | **Feature Coverage** | Features 1–44 (grouped under R1–R6) | Happy-path and standard functionality for ingestion, preprocessing, layout, extraction, validation, and CLI. | $\ge 30$ tests ($\ge 5$ per R1–R6) |
| **Tier 2** | **Boundary & Corner Cases** | Features 20–39, 41–44 | Extreme inputs, empty files, corrupted images, Mod-11/Mod-97 checksum variations, date boundaries, Euro 2026 milestones, and precision rounding. | $\ge 35$ tests ($\ge 5$ per boundary group) |
| **Tier 3** | **Cross-Feature Combinations** | Pairwise interactions | Multi-page tables with dual currency, OCR noise with Mod-11 verification, CLI batch execution with debug visual exports, and occluded receipts. | $\ge 10$ combinatorial tests |
| **Tier 4** | **Real-World Scenarios** | Features 45–46 | Acceptance testing against the 3 real Kapina PDFs (`капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`) and corpus files under zero-touch volume constraints. | $\ge 5$ comprehensive acceptance tests |

### Detailed Feature-to-Tier Mapping Table:

| Feature # | Feature Description | Requirement | Primary Tier | Secondary Tier |
| :--- | :--- | :--- | :--- | :--- |
| 1 | Dependency Management (`pymupdf`, `pytest`) | R2, AC | Tier 1 (R1/R2) | Tier 4 |
| 2 | PDF Multi-Page Rasterization (300–400 DPI) | R1 | Tier 1 (R1) | Tier 3, Tier 4 |
| 3 | Image Multi-Format Ingestion (`.png`, `.jpg`, `.jpeg`) | R1 | Tier 1 (R1) | Tier 2 |
| 4 | Multi-Page Token Coordinate Tracking (`page_number`, bbox) | R1 | Tier 1 (R1) | Tier 3 |
| 5 | Strict Read-Only Source Protection | R1, AC | Tier 1 (R1) | Tier 4 |
| 6 | OSD Orientation Correction (0, 90, 180, 270) | R2 | Tier 1 (R2) | Tier 2 |
| 7 | Contour-Based Deskewing (< 15°) | R2 | Tier 1 (R2) | Tier 3 |
| 8 | Contrast Enhancement (CLAHE) | R2 | Tier 1 (R2) | Tier 2 |
| 9 | Adaptive & Otsu Binarization Variants | R2 | Tier 1 (R2) | Tier 2 |
| 10 | Multi-Pass Tesseract OCR Engine (`bul`, PSM 3 & 11) | R2 | Tier 1 (R2) | Tier 3 |
| 11 | OCR Pass Scoring Engine | R2 | Tier 1 (R2) | Tier 3 |
| 12 | Low-Confidence Token Tagging (`conf < 60`) | R2 | Tier 1 (R2) | Tier 3 |
| 13 | Coordinate Line Grouping (spatial proximity) | R3 | Tier 1 (R3) | Tier 2 |
| 14 | Spatial Block Grouping | R3 | Tier 1 (R3) | Tier 3 |
| 15 | Table Header Recognition (Bulgarian synonyms) | R3 | Tier 1 (R3) | Tier 2 |
| 16 | Multi-Line Description Aggregation | R3 | Tier 1 (R3) | Tier 3 |
| 17 | Multi-Page Table Continuation | R3 | Tier 1 (R3) | Tier 3 |
| 18 | No Synthetic Fallback Descriptions (`null` + warning) | R3 | Tier 1 (R3) | Tier 2 |
| 19 | Document Occlusion & Receipt Isolation | R3 | Tier 1 (R3) | Tier 4 |
| 20 | Invoice Number Extraction (10 digits) | R4 | Tier 1 (R4) | Tier 2 |
| 21 | Date Issued & Tax Event Date Normalization | R4 | Tier 1 (R4) | Tier 2 |
| 22 | Place of Issue Extraction | R4 | Tier 1 (R4) | Tier 2 |
| 23 | Supplier & Recipient Zoning | R4 | Tier 1 (R4) | Tier 3 |
| 24 | Bulgarian 9-Digit EIK Modulo-11 Checksum | R4 | Tier 1 (R4) | Tier 2 |
| 25 | Bulgarian 13-Digit EIK Modulo-11 Checksum | R4 | Tier 1 (R4) | Tier 2 |
| 26 | VAT ID Normalization (`BG` + digits) | R4 | Tier 1 (R4) | Tier 2 |
| 27 | Bulgarian IBAN Modulo-97 Checksum | R4 | Tier 1 (R4) | Tier 2 |
| 28 | BIC/SWIFT Extraction & Validation | R4 | Tier 1 (R4) | Tier 2 |
| 29 | Monetary Decimal Parsing (European/Bulgarian) | R4 | Tier 1 (R4) | Tier 2 |
| 30 | Explicit Currency Objects (`{ "amount": ..., "currency": ... }`) | R4 | Tier 1 (R4) | Tier 3 |
| 31 | Verbatim Amount-in-Words Extraction | R4 | Tier 1 (R4) | Tier 2 |
| 32 | Line Items Sum Validation ($\pm 0.02$) | R5 | Tier 1 (R5) | Tier 2 |
| 33 | Statutory VAT Rate & Amount Validation ($\pm 0.02$) | R5 | Tier 1 (R5) | Tier 2 |
| 34 | Total Due Sum Validation ($\pm 0.02$) | R5 | Tier 1 (R5) | Tier 2 |
| 35 | Dual Currency Parity Verification (1.95583) | R5 | Tier 1 (R5) | Tier 3 |
| 36 | Euro Transition Date $\ge$ 2026-01-01 (No auto-convert) | R5 | Tier 1 (R5) | Tier 2 |
| 37 | Euro Transition Date $\ge$ 2026-08-08 (Dual display deadline) | R5 | Tier 1 (R5) | Tier 2 |
| 38 | Dual Currency Informational Warning | R5 | Tier 1 (R5) | Tier 3 |
| 39 | Amount Words vs Numeric Currency Check | R5 | Tier 1 (R5) | Tier 3 |
| 40 | Strict 3-Layer Output Serialization | R5 | Tier 1 (R5) | Tier 3 |
| 41 | CLI Single-File Mode (Stdout JSON / Stderr Logs) | R6 | Tier 1 (R6) | Tier 2 |
| 42 | CLI Batch Processing Mode (`--input-dir`) | R6 | Tier 1 (R6) | Tier 3 |
| 43 | Batch Summary Generation (`batch_summary.json`) | R6 | Tier 1 (R6) | Tier 3 |
| 44 | Debug Mode & Visual Artifacts Export (`--debug`) | R6 | Tier 1 (R6) | Tier 3 |
| 45 | Comprehensive E2E Testing Suite | M_E2E | All Tiers | All Tiers |
| 46 | Acceptance Dataset Validation & Audit Table | M7 | Tier 4 | Tier 4 |
| 47 | Adversarial Coverage Hardening | M7 | Tier 2 | Tier 3 |

---

## 3. Test Architecture & Runner Invocation

### 3.1 Test Suite Directory Structure
```
tests/
└── e2e/
    ├── __init__.py
    ├── conftest.py                   # Pytest fixtures and helpers
    ├── test_tier1_features.py       # Tier 1: Feature coverage across R1-R6
    ├── test_tier2_boundaries.py     # Tier 2: Edge cases, boundaries, checksums
    ├── test_tier3_combinations.py   # Tier 3: Pairwise & cross-feature integration
    └── test_tier4_realworld.py      # Tier 4: Real-world Kapina acceptance tests
run_e2e_tests.py                     # Unified test runner script
```

### 3.2 Test Runner Invocation Options

The test suite can be executed using either the dedicated standalone runner script `run_e2e_tests.py` or `pytest`:

```bash
# Execute entire E2E test suite across all 4 tiers
python run_e2e_tests.py

# Execute specific tier
python run_e2e_tests.py --tier 1
python run_e2e_tests.py --tier 2
python run_e2e_tests.py --tier 3
python run_e2e_tests.py --tier 4

# Execute via pytest with detailed verbosity
pytest tests/e2e/ -v

# Execute specific tier via pytest
pytest tests/e2e/test_tier1_features.py -v
```

### 3.3 Test Runner Behavior & Exit Codes
- **Exit Code 0**: All executed tests pass successfully.
- **Exit Code 1**: One or more tests failed or encountered an unhandled error.
- **Diagnostic Progress**: Real-time progress indicators per test tier, elapsed execution time, and an ASCII summary table detailing pass/fail/skip counts.

---

## 4. Real-World Application Scenarios (Acceptance Datasets)

The acceptance dataset is located on the external volume at `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/`.

### 4.1 Specification Matrix of Primary Acceptance Files

| Metric | `капина-01.pdf` | `капина-02.pdf` | `капина-03.pdf` |
| :--- | :--- | :--- | :--- |
| **Invoice Number** | `1100124585` | `1100123568` | `1100124013` |
| **Issue Date** | `2026-04-28` (`28.04.2026`) | `2026-04-17` (`17.04.2026`) | `2026-04-22` (`22.04.2026`) |
| **Tax Event Date** | `2026-04-28` (`28.04.2026`) | `2026-04-17` (`17.04.2026`) | `2026-04-22` (`22.04.2026`) |
| **Place Issued** | `ПЛЕВЕН` | `ПЛЕВЕН` | `ПЛЕВЕН` |
| **Supplier Name** | `КАПИНА 71 ООД` | `КАПИНА 71 ООД` | `КАПИНА 71 ООД` |
| **Supplier EIK** | `114500333` | `114500333` | `114500333` |
| **Supplier VAT** | `BG114500333` | `BG114500333` | `BG114500333` |
| **Recipient Name** | `ФАСТ ТОП ФУУДС ЕООД` | `ФАСТ ТОП ФУУДС ЕООД` | `ФАСТ ТОП ФУУДС ЕООД` |
| **Recipient EIK** | `207930830` | `207930830` | `207930830` |
| **Recipient VAT** | `BG207930830` | `BG207930830` | `BG207930830` |
| **Line Items Count** | **14** | **20** | **17** |
| **Tax Base (EUR / BGN)** | `82.38 EUR` / `161.12 BGN` | `101.42 EUR` / `198.34 BGN` | `123.17 EUR` / `240.92 BGN` |
| **VAT Amount (EUR / BGN)**| `16.48 EUR` / `32.23 BGN` | `20.28 EUR` / `39.66 BGN` | `24.65 EUR` / `48.21 BGN` |
| **Total Due (EUR / BGN)** | `98.86 EUR` / `193.35 BGN` | `121.69 EUR` / `238.00 BGN` | `147.83 EUR` / `289.13 BGN` |
| **Currency** | EUR (primary), BGN (dual) | EUR (primary), BGN (dual) | EUR (primary), BGN (dual) |
| **Special Challenge** | Baseline clean scan | High density (20 items) | Physical cash slip occlusion |

### 4.2 Strict Volume Immutability Guarantee
All Tier 4 tests record directory state before execution and assert:
1. No files were created inside `/Volumes/NO NAME/_ФАКТУРИ/`.
2. No files were modified (content hash and modification timestamp identical).
3. No files were deleted.
4. Total byte count of the acceptance directory remains constant.

---

## 5. Coverage Thresholds & Quality Gates

To achieve production readiness, the test suite must satisfy the following thresholds:

| Quality Gate | Metric Threshold | Rationale |
| :--- | :--- | :--- |
| **Feature Coverage (Tier 1)** | 100% of R1–R6 requirements covered | Every statutory and functional requisite must have positive verification. |
| **Boundary Coverage (Tier 2)** | 100% of defined edge cases covered | Checksums, transition dates, and decimal precision must be verified against false positives/negatives. |
| **Combinations (Tier 3)** | $\ge 10$ distinct pairwise interactions | Document features interact non-linearly (e.g. multi-page with occlusions). |
| **Real-World Acceptance (Tier 4)**| 3 of 3 Kapina files pass validation | Golden ground truth dataset must be extracted with zero unhandled exceptions. |
| **External Volume Safety** | Exactly 0 byte changes to source volume | Production safety constraint. |
| **Test Execution Reliability** | 0 flaky tests over 5 consecutive runs | Deterministic pipeline behavior. |
