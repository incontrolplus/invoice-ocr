# BRIEFING — 2026-09-04T21:26:00Z

## Mission
Explore and design the concrete implementation strategy for PyMuPDF PDF rasterization, image ingestion unification, and benchmark memory/speed on real invoice samples.

## 🔒 My Identity
- Archetype: Explorer
- Roles: Investigation, Synthesis
- Working directory: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m1_1
- Original parent: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Milestone: Milestone 1 (Multi-Format Ingestion)

## 🔒 Key Constraints
- Read-only investigation — do NOT implement project source code directly
- NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ
- Write only to /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m1_1/
- Produce a self-contained 5-component handoff report

## Current Parent
- Conversation ID: 4667ebd3-e061-4b8e-b0a1-58dfb11adbcf
- Updated: not yet

## Investigation State
- **Explored paths**:
  - `ORIGINAL_REQUEST.md`, `PROJECT.md`, survey reports 2 & 3
  - PyMuPDF API, `fitz` 1.27.2, OpenCV 5.0.0, NumPy 2.5.2
  - Kapina acceptance invoices: `капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`
  - Multi-page corpus files: `метро-2.pdf` (2 pages), `метро.pdf` (3 pages)
  - Synthetic and corrupted test documents for error scenarios
- **Key findings**:
  - **Resolution selection**: 300 DPI decisively outperforms 400 DPI (237 vs 229 tokens, 1092 vs 999 chars, 69.4% vs 65.2% mean confidence, 1.35s vs 1.95s OCR time, 24.9 MB vs 44.3 MB RAM).
  - **Color conversion**: `cv2.cvtColor(np.frombuffer(...), cv2.COLOR_RGB2BGR)` runs in 1.47 ms and produces contiguous C-arrays. Slicing with `np.ascontiguousarray` is >12x slower (18.43 ms).
  - **Alpha channel**: Always use `alpha=False` in `page.get_pixmap()` to avoid 25% memory overhead.
  - **Multi-page & memory**: Immediate release of pixmap objects (`del pix`) keeps memory bounded (~50 MB active per page).
  - **Error handling**: Documented exact exception paths for missing files, empty files (`fitz.EmptyFileError`), corrupt files (`fitz.FileDataError`), and password-protected files (`doc.is_encrypted` & `doc.needs_pass`).
  - **Image ingestion**: `Path.read_bytes()` + `cv2.imdecode()` avoids Cyrillic path bugs and provides clean empty/corrupt image detection.
  - **Zero touch**: Verified zero modifications to `/Volumes/NO NAME/_ФАКТУРИ`.
- **Unexplored areas**: None for M1 rasterization & ingestion scope.

## Key Decisions Made
- Recommended 300 DPI as standard default resolution.
- Recommended `PageImage(page_number, image, width, height)` and `load_document(path, dpi=300) -> list[PageImage]`.
- Recommended `Path.read_bytes()` + `cv2.imdecode()` for image reading.
- Completed and verified prototype implementation in `prototype_ingestion.py`.

## Artifact Index
- handoff.md — Final 5-component technical exploration handoff report
- progress.md — Liveness heartbeat and progress log
- DISPATCH.md — Incoming task log
- inspect_pymupdf.py — PyMuPDF API and version inspection script
- test_conversions.py — Pixmap to NumPy BGR conversion benchmark script
- benchmark_dpi_and_memory.py — 300 DPI vs 400 DPI resolution, memory, and performance benchmark
- compare_ocr_dpi.py — OCR accuracy and confidence comparison between 300 and 400 DPI
- test_pdf_errors.py — Error handling exploration script for edge cases
- test_image_ingestion.py — Image ingestion and Cyrillic path safety script
- test_corpus_multipage.py — Multi-page iteration and memory benchmark script
- prototype_ingestion.py — Verified prototype of unified ingestion module
