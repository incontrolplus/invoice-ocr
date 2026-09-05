import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
"""Tests for Milestone 3: Spatial Layout Analysis (Features 13–14).

Covers:
- Coordinate-based token grouping (Feature 13):
  - 2D vertical overlap calculation (superscript, subscript, punctuation chaining)
  - Residual tilt/skew tolerance (+0.86 deg across 2000px)
  - Horizontal reading order & multi-column isolation
  - Multi-page line separation
- LogicalBlock & Spatial Zoning (Feature 14):
  - LogicalBlock sequence protocol (__iter__, __len__, __getitem__, bbox, zone)
  - Canonical 7-zone spatial assignment
  - Dynamic party orientation resolution (resolve_party_orientation)
  - Party EIK collision prevention between Supplier and Recipient
  - Thermal receipt region isolation (detect_receipt_regions)
  - Multi-page block isolation
"""

import math
from pathlib import Path
import pytest

from invoice_ocr import (
    OcrToken,
    LogicalLine,
    LogicalBlock,
    group_tokens_into_lines,
    group_lines_into_blocks,
    resolve_party_orientation,
    detect_receipt_regions,
    extract_party,
    Party,
)

KAPINA_DIR = Path("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026")


# ===================================================================
# 1. Feature 13: Coordinate-based Token Grouping Tests
# ===================================================================

class TestTokenGroupingAndOverlap:
    """Tests for coordinate-based token grouping into logical lines."""

    def test_vertical_overlap_standard_line(self):
        """Tokens aligned along the same vertical baseline group into a single line."""
        tokens = [
            OcrToken(text="ФАКТУРА", conf=95.0, bbox=(100, 200, 150, 30)),
            OcrToken(text="№", conf=90.0, bbox=(260, 200, 25, 30)),
            OcrToken(text="1100124585", conf=96.0, bbox=(295, 200, 180, 30)),
        ]
        lines = group_tokens_into_lines(tokens)
        assert len(lines) == 1
        assert lines[0].text == "ФАКТУРА № 1100124585"
        assert lines[0].left == 100
        assert lines[0].right == 475

    def test_superscript_chaining(self):
        """Superscript token (e.g. m² exponent) groups with base word."""
        tokens = [
            OcrToken(text="150", conf=95.0, bbox=(100, 200, 50, 30)),
            OcrToken(text="m", conf=90.0, bbox=(160, 200, 25, 30)),
            OcrToken(text="2", conf=85.0, bbox=(188, 192, 15, 20)),
        ]
        lines = group_tokens_into_lines(tokens)
        assert len(lines) == 1
        assert "m" in lines[0].text
        assert "2" in lines[0].text

    def test_subscript_chaining(self):
        """Subscript token (e.g. CO₂ formula) groups with base word."""
        tokens = [
            OcrToken(text="CO", conf=92.0, bbox=(100, 200, 40, 30)),
            OcrToken(text="2", conf=88.0, bbox=(145, 215, 15, 20)),
        ]
        lines = group_tokens_into_lines(tokens)
        assert len(lines) == 1
        assert "CO 2" in lines[0].text or "CO2" in lines[0].text

    def test_baseline_punctuation_chaining(self):
        """Small baseline period "." chaining to preceding word."""
        tokens = [
            OcrToken(text="гр", conf=90.0, bbox=(100, 200, 40, 30)),
            OcrToken(text=".", conf=95.0, bbox=(143, 222, 6, 8)),
            OcrToken(text="София", conf=92.0, bbox=(160, 200, 80, 30)),
        ]
        lines = group_tokens_into_lines(tokens)
        assert len(lines) == 1
        assert "гр" in lines[0].text
        assert "София" in lines[0].text

    def test_residual_tilt_skew_tolerance(self):
        """Synthetic line with +0.86 deg residual tilt across 2000px groups into a single line."""
        tokens = []
        angle_rad = math.radians(0.86)
        words = ["ДОСТАВЧИК", "КАПИНА", "71", "ООД", "ГР.", "ПЛЕВЕН", "УЛ.", "ГРЕНАДИРСКА", "40", "ЕИК", "114500333"]
        x = 100
        base_y = 300
        for w in words:
            y = int(base_y + x * math.tan(angle_rad))
            tokens.append(OcrToken(text=w, conf=90.0, bbox=(x, y, len(w) * 15, 28)))
            x += len(w) * 15 + 40

        lines = group_tokens_into_lines(tokens)
        assert len(lines) == 1, f"Tilted line fractured into {len(lines)} lines: {[l.text for l in lines]}"
        assert "ДОСТАВЧИК" in lines[0].text
        assert "114500333" in lines[0].text

    def test_multi_column_horizontal_separation(self):
        """Tokens in distinct columns (gap > 400px) are recognized in correct spatial order."""
        t_left1 = OcrToken(text="Получател:", conf=90.0, bbox=(100, 300, 150, 25))
        t_left2 = OcrToken(text="ФАСТ ТОП ФУУДС", conf=90.0, bbox=(100, 340, 200, 25))
        t_right1 = OcrToken(text="Доставчик:", conf=90.0, bbox=(1200, 300, 150, 25))
        t_right2 = OcrToken(text="КАПИНА 71 ООД", conf=90.0, bbox=(1200, 340, 180, 25))

        lines = group_tokens_into_lines([t_left1, t_right1, t_left2, t_right2])
        assert len(lines) == 4, f"Expected 4 separate lines for 2 columns x 2 rows, got {len(lines)}"
        left_lines = [l for l in lines if l.tokens[0].left < 600]
        right_lines = [l for l in lines if l.tokens[0].left >= 600]
        assert len(left_lines) == 2
        assert len(right_lines) == 2
        assert left_lines[0].tokens[0].text == "Получател:"
        assert right_lines[0].tokens[0].text == "Доставчик:"

    def test_multi_page_token_partitioning(self):
        """Tokens on distinct pages are strictly partitioned and lines maintain page_number."""
        tokens_p1 = [
            OcrToken(text="Страница", conf=90.0, bbox=(100, 100, 80, 20), page_number=1),
            OcrToken(text="1", conf=90.0, bbox=(190, 100, 20, 20), page_number=1),
        ]
        tokens_p2 = [
            OcrToken(text="Страница", conf=90.0, bbox=(100, 100, 80, 20), page_number=2),
            OcrToken(text="2", conf=90.0, bbox=(190, 100, 20, 20), page_number=2),
        ]
        lines = group_tokens_into_lines(tokens_p1 + tokens_p2)
        assert len(lines) == 2
        p1_lines = [l for l in lines if l.page_number == 1]
        p2_lines = [l for l in lines if l.page_number == 2]
        assert len(p1_lines) == 1
        assert len(p2_lines) == 1
        assert p1_lines[0].text == "Страница 1"
        assert p2_lines[0].text == "Страница 2"


# ===================================================================
# 2. Feature 14: Spatial Zoning & LogicalBlock Protocol Tests
# ===================================================================

class TestLogicalBlockAndZoning:
    """Tests for LogicalBlock sequence protocol and 7-zone spatial tagging."""

    def test_logical_block_sequence_protocol(self):
        """LogicalBlock supports __iter__, __len__, __getitem__, and bounding box properties."""
        l1 = LogicalLine(tokens=[OcrToken(text="Line1", conf=90.0, bbox=(100, 100, 100, 20))], page_number=1)
        l2 = LogicalLine(tokens=[OcrToken(text="Line2", conf=90.0, bbox=(100, 130, 120, 20))], page_number=1)

        block = LogicalBlock(lines=[l1, l2], page_number=1, zone="table_body", block_type="table")
        assert len(block) == 2
        assert block[0] == l1
        assert block[1] == l2
        assert list(iter(block)) == [l1, l2]
        assert block.bbox == (100, 100, 120, 50)
        assert block.zone == "table_body"
        assert block.block_type == "table"
        assert block.page_number == 1

    def test_spatial_zoning_canonical_zones(self):
        """Block grouping assigns lines into canonical spatial zones."""
        lines = [
            LogicalLine(tokens=[OcrToken(text="ОРИГИНАЛ ФАКТУРА № 1100124585", conf=90.0, bbox=(200, 50, 400, 30))], page_number=1),
            LogicalLine(tokens=[OcrToken(text="Сума за плащане: 193.35 лв.", conf=90.0, bbox=(200, 2800, 300, 30))], page_number=1),
            LogicalLine(tokens=[OcrToken(text="Плащане: по банков път", conf=90.0, bbox=(200, 3000, 250, 30))], page_number=1),
        ]
        blocks = group_lines_into_blocks(lines, page_width=2400, page_height=3500)
        assert len(blocks) >= 2
        assert any(b.zone in ("header", "unknown") for b in blocks)
        assert any(b.zone in ("financial_summary", "payment_details") for b in blocks)

    def test_dynamic_party_orientation_resolution(self):
        """resolve_party_orientation dynamically resolves recipient on left vs supplier on right."""
        pw, ph = 2400, 3500
        party_band_y = int(0.15 * ph)

        lines = [
            LogicalLine(tokens=[
                OcrToken(text="Получател:", conf=90.0, bbox=(100, party_band_y, 150, 30)),
                OcrToken(text="ФАСТ ТОП ФУУДС", conf=90.0, bbox=(300, party_band_y, 200, 30)),
                OcrToken(text="Доставчик:", conf=90.0, bbox=(1400, party_band_y, 150, 30)),
                OcrToken(text="КАПИНА 71 ООД", conf=90.0, bbox=(1600, party_band_y, 180, 30)),
            ], page_number=1),
            LogicalLine(tokens=[
                OcrToken(text="ЕИК: 207930830", conf=90.0, bbox=(100, party_band_y + 40, 200, 30)),
                OcrToken(text="ЕИК: 114500333", conf=90.0, bbox=(1400, party_band_y + 40, 200, 30)),
            ], page_number=1),
        ]

        left_role, right_role = resolve_party_orientation(lines, pw, ph)
        assert left_role == "recipient"
        assert right_role == "supplier"

    def test_dynamic_party_orientation_standard_supplier_left(self):
        """resolve_party_orientation correctly detects standard layout where supplier is left."""
        pw, ph = 2400, 3500
        party_band_y = int(0.15 * ph)

        lines = [
            LogicalLine(tokens=[
                OcrToken(text="Доставчик:", conf=90.0, bbox=(100, party_band_y, 150, 30)),
                OcrToken(text="МЕТРО КЕШ ЕНД КЕРИ", conf=90.0, bbox=(300, party_band_y, 250, 30)),
                OcrToken(text="Получател:", conf=90.0, bbox=(1400, party_band_y, 150, 30)),
                OcrToken(text="КЛИЕНТ ООД", conf=90.0, bbox=(1600, party_band_y, 180, 30)),
            ], page_number=1),
        ]

        left_role, right_role = resolve_party_orientation(lines, pw, ph)
        assert left_role == "supplier"
        assert right_role == "recipient"

    def test_party_eik_collision_prevention(self):
        """extract_party with column-half isolation extracts correct EIK for each party without collision."""
        tokens = [
            OcrToken(text="ФАКТУРА", conf=90.0, bbox=(500, 100, 200, 40), page_number=1),
            OcrToken(text="Получател:", conf=90.0, bbox=(100, 400, 150, 30), page_number=1),
            OcrToken(text="ФАСТ ТОП ФУУДС", conf=90.0, bbox=(100, 450, 250, 30), page_number=1),
            OcrToken(text="ЕИК 207930830", conf=90.0, bbox=(100, 500, 200, 30), page_number=1),
            OcrToken(text="Доставчик:", conf=90.0, bbox=(1400, 400, 150, 30), page_number=1),
            OcrToken(text="КАПИНА 71 ООД", conf=90.0, bbox=(1400, 450, 250, 30), page_number=1),
            OcrToken(text="ЕИК 114500333", conf=90.0, bbox=(1400, 500, 200, 30), page_number=1),
        ]
        lines = group_tokens_into_lines(tokens)

        supp = extract_party(lines, tokens, "supplier")
        recip = extract_party(lines, tokens, "recipient")

        assert supp.eik == "114500333", f"Expected supplier EIK 114500333, got {supp.eik}"
        assert recip.eik == "207930830", f"Expected recipient EIK 207930830, got {recip.eik}"
        assert supp.eik != recip.eik, "Supplier and Recipient EIKs collided!"

    def test_receipt_region_isolation(self):
        """detect_receipt_regions identifies thermal cash receipt while ignoring regular invoice items."""
        receipt_tokens = [
            OcrToken(text="ФИСКАЛЕН", conf=90.0, bbox=(1600, 2200, 120, 25)),
            OcrToken(text="БОН", conf=90.0, bbox=(1730, 2200, 60, 25)),
            OcrToken(text="ФИСКАЛНА", conf=90.0, bbox=(1600, 2250, 120, 25)),
            OcrToken(text="ПАМЕТ", conf=90.0, bbox=(1730, 2250, 80, 25)),
        ]
        invoice_tokens = [
            OcrToken(text="Сръбска", conf=90.0, bbox=(100, 1000, 100, 25)),
            OcrToken(text="наденица", conf=90.0, bbox=(210, 1000, 110, 25)),
            OcrToken(text="Бони", conf=90.0, bbox=(330, 1000, 60, 25)),
        ]

        regions = detect_receipt_regions(receipt_tokens + invoice_tokens)
        assert len(regions) == 1
        rx, ry, rw, rh = regions[0]
        assert rx <= 1600 and ry <= 2200
        for t in invoice_tokens:
            inside = (rx <= t.center_x <= rx + rw and ry <= t.center_y <= ry + rh)
            assert not inside, f"Invoice token {t.text} falsely captured inside receipt box!"

    @pytest.mark.skipif(not KAPINA_DIR.exists(), reason="Acceptance dataset not mounted")
    def test_live_kapina_03_receipt_box_isolated(self):
        """капина-03.pdf thermal cash receipt is detected and isolated."""
        import invoice_ocr as iocr
        pages = iocr.load_document(KAPINA_DIR / "капина-03.pdf")
        norm_page, _ = iocr.normalize_page_geometry(pages[0])
        variants = iocr.generate_preprocessing_variants(norm_page.image)
        tokens = iocr.run_multiple_ocr_passes(variants)
        receipts = detect_receipt_regions(tokens)
        assert len(receipts) >= 1
        rx, ry, rw, rh = receipts[0]
        assert rw > 100 and rh > 100
