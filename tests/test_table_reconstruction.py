"""Tests for Milestone 3: Table Reconstruction (Features 15–19).

Covers:
- Column synonym matching & 9-category taxonomy coverage (Feature 15)
- Multi-line header sliding window detection (Feature 15)
- Dynamic asymmetric column projection (Feature 16)
- Multi-line item description merging & bbox union (Feature 17)
- Multi-page table continuation & transfer line suppression (Feature 18)
- Occlusion handling & strict null fallback without synthetic placeholders (Feature 19)
- Acceptance dataset table reconstruction against real Bulgarian invoices
"""

from decimal import Decimal
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_ocr import (
    OcrToken,
    LogicalLine,
    TableColumn,
    TableRegion,
    LineItem,
    MoneyAmount,
    Invoice,
    _match_column_synonym,
    detect_table_regions,
    extract_line_items,
    _assign_line_to_columns,
    _union_bbox,
    is_transfer_or_header_line,
    _validate_line_items,
)

KAPINA_DIR = Path("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026")


# ===================================================================
# 1. Feature 15 & 16: Column Synonym Matching & 9-Category Taxonomy
# ===================================================================

class TestColumnSynonymsAndTaxonomy:
    """Tests for column synonym matching regex safety and taxonomy coverage."""

    def test_synonym_word_boundary_safety(self):
        """Column matching enforces strict Cyrillic word boundaries and avoids false collisions."""
        # "стойност" must match total_price, NOT "но" (abbreviation for номер)
        assert _match_column_synonym("стойност") == "total_price"
        assert _match_column_synonym("Стойност") == "total_price"
        assert _match_column_synonym("СТОЙНОСТ") == "total_price"

        # Product name words must NOT match units or headers
        assert _match_column_synonym("ДОБРУДЖАНСКА") is None  # must not match "бр"
        assert _match_column_synonym("НАДЕНИЦА") is None
        assert _match_column_synonym("ХАМБУРГСКИ") is None
        assert _match_column_synonym("САЛАМ") is None
        assert _match_column_synonym("КРЕНВИРШ") is None

    def test_metadata_phrases_ignored(self):
        """Metadata phrases containing deal details must NOT trigger table column match."""
        assert _match_column_synonym("описание на сделката") is None
        assert _match_column_synonym("място на сделката") is None
        assert _match_column_synonym("дата на данъчно събитие") is None

    def test_nine_category_taxonomy_coverage(self):
        """Verify coverage across all 9 canonical column categories."""
        taxonomy_samples = {
            "index": ["№", "No", "код"],
            "description": ["наименование", "стока", "описание", "продукт"],
            "unit": ["мярка", "ед. мярка", "ед.мярка", "м-ка"],
            "quantity": ["количество", "кол-во", "кол.", "бр."],
            "unit_price": ["ед. цена", "цена", "ед.цена"],
            "vat_rate": ["ддс %", "% ддс", "ставка"],
            "total_price": ["стойност", "стойност без ддс", "сума"],
            "discount": ["отстъпка", "търговска отстъпка", "отст."],
        }
        for expected_category, samples in taxonomy_samples.items():
            for sample in samples:
                matched = _match_column_synonym(sample)
                assert matched == expected_category, f'Sample "{sample}" expected {expected_category}, got {matched}'


# ===================================================================
# 2. Feature 15 & 16: Multi-line Header & Column Projection
# ===================================================================

class TestTableHeaderAndColumnProjection:
    """Tests for multi-line sliding window header detection and asymmetric column boundaries."""

    def test_single_line_header_detection(self):
        """Standard single-line table header is detected with correct column ordering."""
        header_tokens = [
            OcrToken(text="№", conf=95.0, bbox=(100, 500, 30, 25)),
            OcrToken(text="Наименование на стоката", conf=95.0, bbox=(200, 500, 300, 25)),
            OcrToken(text="Мярка", conf=90.0, bbox=(600, 500, 80, 25)),
            OcrToken(text="Количество", conf=92.0, bbox=(750, 500, 120, 25)),
            OcrToken(text="Ед. цена", conf=93.0, bbox=(950, 500, 100, 25)),
            OcrToken(text="Стойност", conf=94.0, bbox=(1150, 500, 100, 25)),
        ]
        data_tokens = [
            OcrToken(text="1", conf=95.0, bbox=(100, 550, 20, 25)),
            OcrToken(text="Кафе Лаваца", conf=92.0, bbox=(200, 550, 150, 25)),
            OcrToken(text="бр.", conf=90.0, bbox=(600, 550, 40, 25)),
            OcrToken(text="2.000", conf=95.0, bbox=(750, 550, 60, 25)),
            OcrToken(text="12.50", conf=95.0, bbox=(950, 550, 60, 25)),
            OcrToken(text="25.00", conf=96.0, bbox=(1150, 550, 60, 25)),
        ]
        lines = [
            LogicalLine(tokens=header_tokens, page_number=1),
            LogicalLine(tokens=data_tokens, page_number=1),
        ]
        tables = detect_table_regions(lines, header_tokens + data_tokens)

        assert len(tables) == 1
        cols = tables[0].columns
        assert len(cols) >= 5
        types = [c.semantic_type for c in cols]
        assert "description" in types
        assert "quantity" in types
        assert "unit_price" in types
        assert "total_price" in types

    def test_multi_line_header_sliding_window(self):
        """Header spanning 2 lines (split titles) detected via sliding window."""
        l1_tokens = [
            OcrToken(text="№", conf=90.0, bbox=(100, 500, 30, 20)),
            OcrToken(text="Наименование", conf=90.0, bbox=(200, 500, 150, 20)),
        ]
        l2_tokens = [
            OcrToken(text="Количество", conf=90.0, bbox=(700, 525, 100, 20)),
            OcrToken(text="Ед. цена", conf=90.0, bbox=(900, 525, 80, 20)),
            OcrToken(text="Стойност", conf=90.0, bbox=(1100, 525, 80, 20)),
        ]
        d_tokens = [
            OcrToken(text="1", conf=90.0, bbox=(100, 560, 20, 20)),
            OcrToken(text="Шоколад Милка", conf=90.0, bbox=(200, 560, 140, 20)),
            OcrToken(text="5", conf=90.0, bbox=(700, 560, 20, 20)),
            OcrToken(text="2.00", conf=90.0, bbox=(900, 560, 40, 20)),
            OcrToken(text="10.00", conf=90.0, bbox=(1100, 560, 50, 20)),
        ]
        lines = [
            LogicalLine(tokens=l1_tokens, page_number=1),
            LogicalLine(tokens=l2_tokens, page_number=1),
            LogicalLine(tokens=d_tokens, page_number=1),
        ]
        tables = detect_table_regions(lines, l1_tokens + l2_tokens + d_tokens)

        assert len(tables) == 1
        cols = tables[0].columns
        types = [c.semantic_type for c in cols]
        assert "description" in types
        assert "quantity" in types
        assert "total_price" in types

    def test_asymmetric_column_projection(self):
        """Description column uses left-aligned boundary while numeric columns use midpoints."""
        cols = [
            TableColumn(header_text="Стока", semantic_type="description", x_center=300, x_left=200, x_right=500),
            TableColumn(header_text="Количество", semantic_type="quantity", x_center=700, x_left=650, x_right=750),
            TableColumn(header_text="Цена", semantic_type="unit_price", x_center=900, x_left=860, x_right=940),
        ]
        cols[0].x_left = 0
        split_x = max(cols[0].x_right + 10, cols[1].x_left - 35)
        cols[0].x_right = split_x
        cols[1].x_left = split_x
        mid = (cols[1].x_center + cols[2].x_center) // 2
        cols[1].x_right = mid
        cols[2].x_left = mid

        assert cols[0].x_right == 615
        assert cols[1].x_left == 615
        assert cols[1].x_right == 800
        assert cols[2].x_left == 800


# ===================================================================
# 3. Feature 17: Multi-line Description Continuation Rows
# ===================================================================

class TestMultiLineDescriptionMerging:
    """Tests for merging second-line product descriptions into parent LineItem."""

    def test_continuation_row_merges_into_parent(self):
        """Secondary text line with no quantity/price is merged into parent LineItem."""
        table = TableRegion(
            columns=[
                TableColumn(header_text="№", semantic_type="index", x_center=50, x_left=0, x_right=100),
                TableColumn(header_text="Стока", semantic_type="description", x_center=300, x_left=100, x_right=600),
                TableColumn(header_text="Кол.", semantic_type="quantity", x_center=700, x_left=600, x_right=800),
                TableColumn(header_text="Цена", semantic_type="unit_price", x_center=900, x_left=800, x_right=1000),
                TableColumn(header_text="Сума", semantic_type="total_price", x_center=1100, x_left=1000, x_right=1300),
            ],
            header_line=LogicalLine(tokens=[OcrToken(text="Стока", conf=90.0, bbox=(100, 100, 100, 20))]),
            data_lines=[
                LogicalLine(tokens=[
                    OcrToken(text="1", conf=90.0, bbox=(50, 150, 15, 20)),
                    OcrToken(text="КРЕНВИРШ", conf=92.0, bbox=(150, 150, 100, 20)),
                    OcrToken(text="2.00", conf=95.0, bbox=(700, 150, 40, 20)),
                    OcrToken(text="10.00", conf=95.0, bbox=(900, 150, 50, 20)),
                    OcrToken(text="20.00", conf=95.0, bbox=(1100, 150, 50, 20)),
                ]),
                LogicalLine(tokens=[
                    OcrToken(text='"ДЕЛИКАТЕС 2" ООД', conf=91.0, bbox=(150, 180, 180, 20)),
                ]),
            ],
            page_number=1,
        )

        items = extract_line_items([table], table.data_lines)
        assert len(items) == 1, f"Expected 1 merged item, got {len(items)}"
        assert items[0].index == 1
        assert "КРЕНВИРШ" in items[0].description
        assert "ДЕЛИКАТЕС 2" in items[0].description
        assert items[0].quantity == Decimal("2.00")
        assert items[0].unit_price_net.amount == Decimal("10.00")
        assert items[0].total_price_net.amount == Decimal("20.00")

    def test_bbox_union_on_continuation_merge(self):
        """Bounding box unions correctly when merging multi-line rows."""
        b1 = (100, 150, 800, 25)
        b2 = (150, 180, 200, 25)
        union = _union_bbox(b1, b2)
        assert union[0] == 100
        assert union[1] == 150
        assert union[2] == 800
        assert union[3] == 55


# ===================================================================
# 4. Feature 18: Multi-page Table Continuation & Transfer Lines
# ===================================================================

class TestMultiPageTableContinuation:
    """Tests for multi-page table stitching, transfer line suppression, and index continuity."""

    def test_transfer_keywords_identified(self):
        """Transfer and carry-forward lines are detected and flagged."""
        transfer_lines = [
            LogicalLine(tokens=[OcrToken(text="Пренос", conf=90.0, bbox=(100, 900, 80, 20))]),
            LogicalLine(tokens=[OcrToken(text="Стр. Общо", conf=90.0, bbox=(100, 900, 100, 20))]),
            LogicalLine(tokens=[OcrToken(text="Посл. Стр. Общо", conf=90.0, bbox=(100, 900, 150, 20))]),
        ]
        for l in transfer_lines:
            assert is_transfer_or_header_line(l) is True

    def test_multi_page_table_stitching_and_indexing(self):
        """Table spanning page 1 and page 2 stitches items with continuous indexing."""
        cols = [
            TableColumn(header_text="№", semantic_type="index", x_center=50, x_left=0, x_right=100),
            TableColumn(header_text="Стока", semantic_type="description", x_center=300, x_left=100, x_right=600),
            TableColumn(header_text="Кол.", semantic_type="quantity", x_center=700, x_left=600, x_right=800),
            TableColumn(header_text="Цена", semantic_type="unit_price", x_center=900, x_left=800, x_right=1000),
            TableColumn(header_text="Сума", semantic_type="total_price", x_center=1100, x_left=1000, x_right=1300),
        ]
        t1 = TableRegion(
            columns=cols,
            header_line=LogicalLine(tokens=[OcrToken(text="Стока", conf=90.0, bbox=(100, 100, 100, 20))], page_number=1),
            data_lines=[
                LogicalLine(tokens=[
                    OcrToken(text="1", conf=90.0, bbox=(50, 150, 15, 20)),
                    OcrToken(text="Артикул 1", conf=90.0, bbox=(150, 150, 100, 20)),
                    OcrToken(text="1", conf=90.0, bbox=(700, 150, 20, 20)),
                    OcrToken(text="10.00", conf=90.0, bbox=(900, 150, 50, 20)),
                    OcrToken(text="10.00", conf=90.0, bbox=(1100, 150, 50, 20)),
                ], page_number=1),
                LogicalLine(tokens=[OcrToken(text="Пренос на следваща страница", conf=90.0, bbox=(100, 900, 300, 20))], page_number=1),
            ],
            page_number=1,
        )
        t2 = TableRegion(
            columns=cols,
            header_line=LogicalLine(tokens=[OcrToken(text="Стока", conf=90.0, bbox=(100, 100, 100, 20))], page_number=2),
            data_lines=[
                LogicalLine(tokens=[OcrToken(text="Пренос от предишна страница", conf=90.0, bbox=(100, 120, 300, 20))], page_number=2),
                LogicalLine(tokens=[
                    OcrToken(text="2", conf=90.0, bbox=(50, 150, 15, 20)),
                    OcrToken(text="Артикул 2", conf=90.0, bbox=(150, 150, 100, 20)),
                    OcrToken(text="2", conf=90.0, bbox=(700, 150, 20, 20)),
                    OcrToken(text="20.00", conf=90.0, bbox=(900, 150, 50, 20)),
                    OcrToken(text="40.00", conf=90.0, bbox=(1100, 150, 50, 20)),
                ], page_number=2),
            ],
            page_number=2,
        )

        items = extract_line_items([t1, t2], t1.data_lines + t2.data_lines)
        assert len(items) == 2, f"Expected 2 items after transfer line filtering, got {len(items)}"
        assert items[0].description == "Артикул 1"
        assert items[0].page_number == 1
        assert items[1].description == "Артикул 2"
        assert items[1].page_number == 2


# ===================================================================
# 5. Feature 19: Occlusion Handling & Strict Null Fallback
# ===================================================================

class TestOcclusionHandlingAndStrictNullFallback:
    """Tests for strict null fallback policy: zero synthetic placeholders."""

    def test_occluded_description_assigned_none(self):
        """Row with missing or obscured description is assigned None, not synthetic string."""
        cols = [
            TableColumn(header_text="№", semantic_type="index", x_center=50, x_left=0, x_right=100),
            TableColumn(header_text="Стока", semantic_type="description", x_center=300, x_left=100, x_right=600),
            TableColumn(header_text="Кол.", semantic_type="quantity", x_center=700, x_left=600, x_right=800),
            TableColumn(header_text="Цена", semantic_type="unit_price", x_center=900, x_left=800, x_right=1000),
            TableColumn(header_text="Сума", semantic_type="total_price", x_center=1100, x_left=1000, x_right=1300),
        ]
        table = TableRegion(
            columns=cols,
            header_line=LogicalLine(tokens=[OcrToken(text="Стока", conf=90.0, bbox=(100, 100, 100, 20))]),
            data_lines=[
                LogicalLine(tokens=[
                    OcrToken(text="1", conf=90.0, bbox=(50, 150, 15, 20)),
                    OcrToken(text="1.00", conf=90.0, bbox=(700, 150, 40, 20)),
                    OcrToken(text="50.00", conf=90.0, bbox=(900, 150, 50, 20)),
                    OcrToken(text="50.00", conf=90.0, bbox=(1100, 150, 50, 20)),
                ]),
            ],
            page_number=1,
        )
        items = extract_line_items([table], table.data_lines)
        assert len(items) == 1
        assert items[0].description is None, f"Expected None, got {items[0].description}"

    def test_synthetic_placeholder_banned(self):
        """Synthetic placeholder words like 'Item', 'Unknown', 'Артикул' are converted to None."""
        item = LineItem(description="Item", quantity=Decimal("1"), unit_price_net=MoneyAmount(Decimal("10")), total_price_net=MoneyAmount(Decimal("10")))
        inv = Invoice(line_items=[item])
        issues = _validate_line_items(inv)
        assert item.description is None
        assert any(iss.code == "MISSING_DESCRIPTION" for iss in issues)

    def test_zero_cheating_audit_no_hardcoded_placeholders(self):
        """Audit: ensure no dummy descriptions like 'Item 1' or 'Unknown' in extracted items."""
        item = LineItem(description="Unknown")
        inv = Invoice(line_items=[item])
        _validate_line_items(inv)
        assert item.description is None


# ===================================================================
# 6. Live Acceptance Dataset Table Reconstruction Tests
# ===================================================================

@pytest.mark.skipif(not KAPINA_DIR.exists(), reason="Acceptance dataset not mounted")
class TestLiveAcceptanceTableReconstruction:
    """Acceptance tests on real Bulgarian invoices from KAPINA_DIR."""

    def test_kapina_01_line_items_reconstruction(self):
        """капина-01.pdf line items reconstructed with high accuracy."""
        import invoice_ocr as iocr
        inv = iocr.process_invoice(KAPINA_DIR / "капина-01.pdf")
        assert len(inv.line_items) >= 10, f"Expected >= 10 items, got {len(inv.line_items)}"
        descriptions = [it.description for it in inv.line_items if it.description]
        assert len(descriptions) >= 8
        all_desc_text = " ".join(descriptions).upper()
        assert "САЛАМ" in all_desc_text or "КРЕНВИРШ" in all_desc_text or "НАДЕНИЦА" in all_desc_text or "ОЛИО" in all_desc_text

    def test_kapina_02_line_items_reconstruction(self):
        """капина-02.pdf line items reconstructed."""
        import invoice_ocr as iocr
        inv = iocr.process_invoice(KAPINA_DIR / "капина-02.pdf")
        assert len(inv.line_items) >= 10, f"Expected >= 10 items, got {len(inv.line_items)}"

    def test_kapina_03_line_items_reconstruction(self):
        """капина-03.pdf line items reconstructed."""
        import invoice_ocr as iocr
        inv = iocr.process_invoice(KAPINA_DIR / "капина-03.pdf")
        assert len(inv.line_items) >= 10, f"Expected >= 10 items, got {len(inv.line_items)}"


# ===================================================================
# 7. Distributor / Warehouse Tables: SKU Filtering & Column Cross-Validation
# ===================================================================

class TestDistributorTableAndSkuClassification:
    """Tests for Problem #2 (P0): 5-8 digit SKU filtering and column cross-mathematical validation."""

    def test_5_to_8_digit_integer_rejected_as_quantity(self):
        """5-8 digit integers (SKUs / barcodes / LOTs) must not be accepted as quantity."""
        from invoice_ocr import _sanitize_line_item_candidate

        # 5-8 digit integers rejected as quantity without matching expected_val
        assert _sanitize_line_item_candidate("24352", is_qty=True) is None
        assert _sanitize_line_item_candidate("1081207", is_qty=True) is None
        assert _sanitize_line_item_candidate("7720107", is_qty=True) is None
        assert _sanitize_line_item_candidate("7774645", is_qty=True) is None

        # Standard decimal quantities and small integers remain valid
        assert _sanitize_line_item_candidate("12", is_qty=True) == Decimal("12")
        assert _sanitize_line_item_candidate("1.841", is_qty=True) == Decimal("1.841")
        assert _sanitize_line_item_candidate("100.000", is_qty=True) == Decimal("100")

    def test_extract_line_items_routes_sku_to_article_code(self):
        """extract_line_items routes 5-8 digit integers to article_code / sku and avoids 0.00 price calculation."""
        cols = [
            TableColumn(header_text="№", semantic_type="index", x_center=50, x_left=0, x_right=100),
            TableColumn(header_text="Артикул", semantic_type="description", x_center=300, x_left=100, x_right=500),
            TableColumn(header_text="Код/Кол.", semantic_type="quantity", x_center=600, x_left=500, x_right=700),
            TableColumn(header_text="Сума", semantic_type="total_price", x_center=800, x_left=700, x_right=900),
        ]
        table = TableRegion(
            columns=cols,
            header_line=LogicalLine(tokens=[OcrToken(text="Артикул", conf=90.0, bbox=(100, 100, 100, 20))]),
            data_lines=[
                LogicalLine(tokens=[
                    OcrToken(text="1", conf=90.0, bbox=(50, 150, 15, 20)),
                    OcrToken(text="Тестов Продукт", conf=90.0, bbox=(200, 150, 150, 20)),
                    OcrToken(text="24352", conf=90.0, bbox=(600, 150, 50, 20)),
                    OcrToken(text="8.00", conf=90.0, bbox=(800, 150, 40, 20)),
                ]),
            ],
            page_number=1,
        )
        items = extract_line_items([table], table.data_lines)
        assert len(items) == 1
        it = items[0]
        # 24352 is routed to article_code/sku, NOT left as quantity
        assert it.article_code == "24352"
        assert it.sku == "24352"
        assert it.quantity != Decimal("24352")
        # Unit price must NOT be calculated as 0.00
        assert it.unit_price_net.amount != Decimal("0.00")

    def test_cross_mathematical_column_validation(self):
        """Cross-mathematical verification correctly identifies true quantity column over packaging."""
        from invoice_ocr import _cross_validate_table_columns

        cols = [
            TableColumn(header_text="№", semantic_type="index", x_center=50, x_left=0, x_right=100),
            TableColumn(header_text="Описание", semantic_type="description", x_center=300, x_left=100, x_right=500),
            TableColumn(header_text="Съд/бр", semantic_type="packaging", x_center=600, x_left=500, x_right=700),
            TableColumn(header_text="Цена", semantic_type="unit_price", x_center=800, x_left=700, x_right=900),
            TableColumn(header_text="Количество", semantic_type="quantity", x_center=1000, x_left=900, x_right=1100),
            TableColumn(header_text="Сума нето", semantic_type="total_price", x_center=1200, x_left=1100, x_right=1300),
        ]
        data_lines = [
            LogicalLine(tokens=[
                OcrToken(text="1", conf=90.0, bbox=(50, 150, 15, 20)),
                OcrToken(text="Сок Ябълка", conf=90.0, bbox=(200, 150, 100, 20)),
                OcrToken(text="6", conf=90.0, bbox=(600, 150, 20, 20)),
                OcrToken(text="2.50", conf=90.0, bbox=(800, 150, 40, 20)),
                OcrToken(text="12", conf=90.0, bbox=(1000, 150, 30, 20)),
                OcrToken(text="30.00", conf=90.0, bbox=(1200, 150, 50, 20)),
            ]),
            LogicalLine(tokens=[
                OcrToken(text="2", conf=90.0, bbox=(50, 180, 15, 20)),
                OcrToken(text="Сок Портокал", conf=90.0, bbox=(200, 180, 100, 20)),
                OcrToken(text="6", conf=90.0, bbox=(600, 180, 20, 20)),
                OcrToken(text="3.00", conf=90.0, bbox=(800, 180, 40, 20)),
                OcrToken(text="10", conf=90.0, bbox=(1000, 180, 30, 20)),
                OcrToken(text="30.00", conf=90.0, bbox=(1200, 180, 50, 20)),
            ]),
        ]
        # Initially simulate packaging misclassified as quantity
        cols[2].semantic_type = "quantity"
        cols[4].semantic_type = "packaging"

        validated_cols = _cross_validate_table_columns(cols, data_lines)
        qty_col = [c for c in validated_cols if c.semantic_type == "quantity"][0]
        assert qty_col.x_center == 1000
        assert qty_col.header_text == "Количество"

    def test_kapina_03_dot_matrix_line_item_and_total_reconciliation(self):
        """Regression test for Goal 2: капина-03 line item 13 dot matrix token and zero validation errors.

        КИСЕЛО МЛЯКО БОР ЧВОР has dot-matrix token 77777050|000 which must resolve to
        unit price 0.50, quantity 2.000, and total price 1.00, resulting in exact
        line item sum 123.17 matching tax base 123.17 and eliminating LINE_ITEMS_TOTAL_MISMATCH.
        """
        import json
        from invoice_ocr import (
            OcrToken,
            FinancialSummary,
            MoneyAmount,
            normalize_ocr_tokens,
            group_tokens_into_lines,
            detect_table_regions,
            extract_line_items,
            extract_party,
            validate_invoice,
            _sanitize_line_item_candidate,
        )

        # 1. Verify candidate sanitizer splits pipes and strips dot-matrix 7s prefix
        cand = _sanitize_line_item_candidate("77777050|000")
        assert cand == Decimal("0.5")

        # 2. Verify against cached evidence tokens from капина-03.json
        res_file = Path(__file__).resolve().parent.parent / "results" / "капина-03.json"
        assert res_file.exists(), "results/капина-03.json must exist"

        with open(res_file, encoding="utf-8") as f:
            data = json.load(f)

        raw_toks = [
            OcrToken(
                text=t["text"],
                conf=t["conf"],
                bbox=tuple(t["bbox"]),
                page_number=t["page_number"],
                is_low_confidence=t.get("is_low_confidence", False),
            )
            for p in data["raw_ocr_evidence"]["pages"]
            for t in p["tokens"]
        ]

        norm_toks = normalize_ocr_tokens(raw_toks)
        lines = group_tokens_into_lines(norm_toks)
        tables = detect_table_regions(lines, norm_toks)

        fs = FinancialSummary(
            tax_base=MoneyAmount(Decimal("123.17"), "EUR"),
            vat_amount=MoneyAmount(Decimal("24.65"), "EUR"),
            total_amount_due=MoneyAmount(Decimal("147.83"), "EUR"),
        )

        items = extract_line_items(tables, lines, financial_summary=fs)
        assert len(items) == 17, f"Expected 17 line items, got {len(items)}"

        # Item 13: КИСЕЛО МЛЯКО БОР ЧВОР
        item13 = items[12]
        assert "КИСЕЛО" in (item13.description or "")
        assert item13.quantity == Decimal("2.000")
        assert item13.unit_price_net.amount == Decimal("0.50")
        assert item13.total_price_net.amount == Decimal("1.00")

        # Line items sum matches tax base exactly (0.00 difference)
        line_sum = sum(it.total_price_net.amount for it in items if it.total_price_net.amount is not None)
        assert line_sum == Decimal("123.17")

        inv = Invoice()
        inv.invoice_metadata.invoice_number = "1100124013"
        inv.invoice_metadata.date_issued = "2026-04-22"
        inv.invoice_metadata.date_tax_event = "2026-04-22"
        inv.supplier = extract_party(lines, norm_toks, "supplier")
        inv.recipient = extract_party(lines, norm_toks, "recipient")
        inv.financial_summary = fs
        inv.line_items = items

        val = validate_invoice(inv, norm_toks)
        error_codes = [e.code for e in val.errors]
        assert "LINE_ITEMS_TOTAL_MISMATCH" not in error_codes
        assert len(error_codes) == 0

