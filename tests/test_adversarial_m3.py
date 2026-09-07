"""Adversarial stress-test suite for Milestone 3: Spatial Layout & Table Reconstruction.

Authored by Challenger 1 (teamwork_preview_challenger_m3_1).
Roles: critic, specialist

Covers:
1. Vertical Overlap Edge Cases:
   - 49% vs 51% vertical overlap boundaries (threshold at 50%).
   - Extreme vertical staggering across 5+ words chaining without shattering.
   - Subscripts ($H_2O$) and superscripts ($m^2$).
   - Baseline punctuation ('.', ',', '-') chaining without fragmentation.
2. Residual Tilt Line Chaining:
   - 12-word line spanning 2000px under residual skew angles (+-0.5 deg, +-1.0 deg, +-1.5 deg).
   - Verifies continuous 1-line grouping without mid-line splitting.
3. Multi-Column Party Isolation:
   - 2-column party headers at identical Y coordinates.
   - Isolation between Left column and Right column party tokens in extract_party and layout analysis.
4. Multi-Line Continuation Stress:
   - Items with 1, 2, 3, 4 continuation lines.
   - Numeric-like tokens in descriptions ("Картофи 2.5 кг", "Олио 3л", "Спирт 48брХ26").
   - Verifies aggregation into item.description rather than spurious new item creation.
   - Adversarial continuation keyword collision test ("стр.", "продължение").
5. Synthetic Placeholder Rejection:
   - Table cells containing "Item", "Unknown", "Placeholder", "Артикул", "", "  ".
   - Verifies description is strictly set to None and ValidationIssue(code="MISSING_DESCRIPTION") is raised.
   - Direct LineItem validation audit.
6. Acceptance Dataset Immutability:
   - Zero modifications to /Volumes/NO NAME/_ФАКТУРИ.
"""

from decimal import Decimal
import math
from pathlib import Path
import subprocess
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from invoice_ocr import (
    OcrToken,
    LogicalLine,
    LogicalBlock,
    TableColumn,
    TableRegion,
    LineItem,
    Invoice,
    MoneyAmount,
    ValidationIssue,
    group_tokens_into_lines,
    group_lines_into_blocks,
    extract_party,
    resolve_party_orientation,
    extract_line_items,
    _assign_line_to_columns,
    _validate_line_items,
    _token_vertical_overlap_ratio,
    is_transfer_or_header_line,
)


# ===================================================================
# 1. Vertical Overlap Edge Cases
# ===================================================================

class TestAdversarialVerticalOverlap:
    """Stress tests for token vertical overlap thresholds and typography edge cases."""

    def test_vertical_overlap_boundary_49_vs_51(self):
        """Tokens with 51% overlap must group into one line; 49% overlap must not group."""
        t_base = OcrToken(text="BASE", conf=95.0, bbox=(100, 100, 80, 100))

        # 51% overlap token: top=149, bot=249, height=100 -> v_int = 51 px (51%)
        t_51 = OcrToken(text="PASS51", conf=95.0, bbox=(200, 149, 80, 100))
        lines_51 = group_tokens_into_lines([t_base, t_51])
        assert len(lines_51) == 1, f"Expected 1 line for 51% overlap, got {len(lines_51)}"
        assert lines_51[0].text == "BASE PASS51"

        # 49% overlap token: top=151, bot=251, height=100 -> v_int = 49 px (49%)
        t_49 = OcrToken(text="FAIL49", conf=95.0, bbox=(200, 151, 80, 100))
        lines_49 = group_tokens_into_lines([t_base, t_49])
        assert len(lines_49) == 2, f"Expected 2 lines for 49% overlap, got {len(lines_49)}"
        assert lines_49[0].text == "BASE"
        assert lines_49[1].text == "FAIL49"

    def test_extreme_vertical_staggering_chaining(self):
        """5 tokens progressively staggered so token 0 and token 4 have 0% overlap, but chain continuously."""
        tokens = [
            OcrToken(text=f"WORD{i}", conf=92.0, bbox=(100 + i * 80, 100 + i * 8, 60, 30))
            for i in range(5)
        ]
        # Token 0 is [100..130]; Token 4 is [132..162]. They have 0 vertical overlap directly.
        v_int_0_4 = max(0, min(tokens[0].bottom, tokens[4].bottom) - max(tokens[0].top, tokens[4].top))
        assert v_int_0_4 == 0, "Precondition failed: token 0 and token 4 must have 0 vertical overlap"

        lines = group_tokens_into_lines(tokens)
        assert len(lines) == 1, f"Expected continuous line chaining, got {len(lines)} lines"
        assert lines[0].text == "WORD0 WORD1 WORD2 WORD3 WORD4"

    def test_subscript_h2o_not_shattered(self):
        """Standard subscript formula H2O is grouped into a single line without shattering."""
        tokens = [
            OcrToken(text="H", conf=95.0, bbox=(100, 100, 25, 30)),
            OcrToken(text="2", conf=90.0, bbox=(128, 115, 15, 18)),
            OcrToken(text="O", conf=95.0, bbox=(145, 100, 25, 30)),
        ]
        lines = group_tokens_into_lines(tokens)
        assert len(lines) == 1, f"Expected 1 line for H2O formula, got {len(lines)}"
        assert "H" in lines[0].text and "2" in lines[0].text and "O" in lines[0].text

    def test_superscript_m2_not_shattered(self):
        """Standard superscript m^2 is grouped into a single line without shattering."""
        tokens = [
            OcrToken(text="100", conf=95.0, bbox=(100, 100, 50, 30)),
            OcrToken(text="m", conf=92.0, bbox=(155, 100, 25, 30)),
            OcrToken(text="2", conf=88.0, bbox=(183, 92, 15, 20)),
        ]
        lines = group_tokens_into_lines(tokens)
        assert len(lines) == 1, f"Expected 1 line for 100 m^2, got {len(lines)}"
        assert "100" in lines[0].text and "m" in lines[0].text and "2" in lines[0].text

    def test_baseline_punctuation_chaining(self):
        """Small baseline punctuation tokens ('.', ',', '-') chain without forming isolated lines."""
        tokens = [
            OcrToken(text="гр", conf=92.0, bbox=(100, 100, 40, 30)),
            OcrToken(text=".", conf=95.0, bbox=(142, 125, 5, 5)),
            OcrToken(text=",", conf=95.0, bbox=(150, 126, 5, 8)),
            OcrToken(text="-", conf=95.0, bbox=(158, 114, 10, 4)),
            OcrToken(text="София", conf=92.0, bbox=(172, 100, 80, 30)),
        ]
        lines = group_tokens_into_lines(tokens)
        assert len(lines) == 1, f"Expected 1 line with baseline punctuation, got {len(lines)}"
        assert lines[0].text == "гр . , - София"


# ===================================================================
# 2. Residual Tilt Line Chaining
# ===================================================================

class TestAdversarialResidualTilt:
    """Stress tests for chaining lines across 2000px width under residual tilt angles."""

    @pytest.mark.parametrize("angle_deg", [0.5, -0.5, 1.0, -1.0, 1.5, -1.5])
    def test_residual_tilt_chaining_across_2000px(self, angle_deg: float):
        """Line of 12 words spanning 2000px at +-0.5, +-1.0, +-1.5 deg forms 1 continuous line."""
        words = [f"TOKEN_{i:02d}" for i in range(12)]
        rad = math.radians(angle_deg)
        base_y = 600
        tokens = []

        for i, w in enumerate(words):
            x = 100 + i * 160
            y = int(round(base_y + x * math.tan(rad)))
            tokens.append(OcrToken(text=w, conf=95.0, bbox=(x, y, 90, 30)))

        lines = group_tokens_into_lines(tokens)
        assert len(lines) == 1, (
            f"Residual tilt {angle_deg:+.1f} deg caused line to shatter into {len(lines)} lines! "
            f"Texts: {[l.text for l in lines]}"
        )
        assert lines[0].text == " ".join(words)


# ===================================================================
# 3. Multi-Column Party Isolation
# ===================================================================

class TestAdversarialMultiColumnPartyIsolation:
    """Stress tests for multi-column party headers at identical Y coordinates."""

    def test_party_extraction_left_right_isolation(self):
        """extract_party strictly isolates Left column and Right column party tokens at identical Y."""
        left_tokens = [
            OcrToken(text="ПОЛУЧАТЕЛ:", conf=95.0, bbox=(200, 500, 180, 30)),
            OcrToken(text="АЛФА", conf=95.0, bbox=(390, 500, 80, 30)),
            OcrToken(text="ЕООД", conf=95.0, bbox=(480, 500, 80, 30)),
            OcrToken(text="ЕИК", conf=95.0, bbox=(200, 550, 60, 30)),
            OcrToken(text="207930830", conf=95.0, bbox=(270, 550, 160, 30)),
        ]
        right_tokens = [
            OcrToken(text="ДОСТАВЧИК:", conf=95.0, bbox=(1400, 500, 200, 30)),
            OcrToken(text="БЕТА", conf=95.0, bbox=(1610, 500, 80, 30)),
            OcrToken(text="ООД", conf=95.0, bbox=(1700, 500, 80, 30)),
            OcrToken(text="ЕИК", conf=95.0, bbox=(1400, 550, 60, 30)),
            OcrToken(text="114500333", conf=95.0, bbox=(1470, 550, 160, 30)),
        ]

        all_tokens = left_tokens + right_tokens
        lines = group_tokens_into_lines(all_tokens)

        supp = extract_party(lines, all_tokens, "supplier")
        recip = extract_party(lines, all_tokens, "recipient")

        assert supp.eik == "114500333", f"Expected supplier EIK 114500333, got {supp.eik}"
        assert recip.eik == "207930830", f"Expected recipient EIK 207930830, got {recip.eik}"
        assert supp.eik != recip.eik, "Supplier and Recipient EIKs collided!"
        assert supp.name == "БЕТА ООД", f"Expected supplier name 'БЕТА ООД', got {supp.name!r}"
        assert recip.name == "АЛФА ЕООД", f"Expected recipient name 'АЛФА ЕООД', got {recip.name!r}"

    def test_spatial_line_grouping_multi_column_isolation(self):
        """Adversarial check: group_tokens_into_lines must isolate columns separated by large horizontal gap."""
        tokens = [
            OcrToken(text="ПОЛУЧАТЕЛ:", conf=95.0, bbox=(100, 400, 150, 30)),
            OcrToken(text="ДОСТАВЧИК:", conf=95.0, bbox=(1400, 400, 150, 30)),
        ]
        lines = group_tokens_into_lines(tokens)
        left_tok = tokens[0]
        right_tok = tokens[1]
        merged = any(left_tok in l.tokens and right_tok in l.tokens for l in lines)
        assert not merged, (
            "VULNERABILITY: group_tokens_into_lines merged Left column ('ПОЛУЧАТЕЛ:') "
            "and Right column ('ДОСТАВЧИК:') across a 1150px gap into a single line!"
        )


# ===================================================================
# 4. Multi-Line Continuation Stress
# ===================================================================

class TestAdversarialMultiLineContinuation:
    """Stress tests for items with 1, 2, 3, 4 continuation lines and numeric strings."""

    def test_continuation_lines_with_numeric_strings(self):
        """Items with 1, 2, 3, 4 continuation lines and numeric strings merge into description."""
        cols = [
            TableColumn(header_text="№", semantic_type="index", x_center=50, x_left=0, x_right=100),
            TableColumn(header_text="Стока", semantic_type="description", x_center=300, x_left=100, x_right=600),
            TableColumn(header_text="Кол.", semantic_type="quantity", x_center=700, x_left=600, x_right=800),
            TableColumn(header_text="Цена", semantic_type="unit_price", x_center=900, x_left=800, x_right=1000),
            TableColumn(header_text="Сума", semantic_type="total_price", x_center=1100, x_left=1000, x_right=1300),
        ]

        data_lines = [
            # Item 1: 1 continuation line
            LogicalLine(tokens=[
                OcrToken(text="1", conf=90.0, bbox=(50, 150, 20, 20)),
                OcrToken(text="Шоколад Милка", conf=90.0, bbox=(120, 150, 100, 20)),
                OcrToken(text="1", conf=90.0, bbox=(700, 150, 20, 20)),
                OcrToken(text="10.00", conf=90.0, bbox=(900, 150, 50, 20)),
                OcrToken(text="10.00", conf=90.0, bbox=(1100, 150, 50, 20)),
            ]),
            LogicalLine(tokens=[
                OcrToken(text="с цели лешници", conf=90.0, bbox=(120, 175, 120, 20)),
            ]),

            # Item 2: 2 continuation lines with numeric strings
            LogicalLine(tokens=[
                OcrToken(text="2", conf=90.0, bbox=(50, 200, 20, 20)),
                OcrToken(text="Зеленчуци пресни", conf=90.0, bbox=(120, 200, 120, 20)),
                OcrToken(text="5", conf=90.0, bbox=(700, 200, 20, 20)),
                OcrToken(text="3.00", conf=90.0, bbox=(900, 200, 50, 20)),
                OcrToken(text="15.00", conf=90.0, bbox=(1100, 200, 50, 20)),
            ]),
            LogicalLine(tokens=[
                OcrToken(text="Картофи 2.5 кг", conf=90.0, bbox=(120, 225, 150, 20)),
            ]),
            LogicalLine(tokens=[
                OcrToken(text="Олио 3л", conf=90.0, bbox=(120, 250, 100, 20)),
            ]),

            # Item 3: 3 continuation lines with mixed alphanumeric
            LogicalLine(tokens=[
                OcrToken(text="3", conf=90.0, bbox=(50, 275, 20, 20)),
                OcrToken(text="Медицински спирт", conf=90.0, bbox=(120, 275, 120, 20)),
                OcrToken(text="10", conf=90.0, bbox=(700, 275, 20, 20)),
                OcrToken(text="2.00", conf=90.0, bbox=(900, 275, 50, 20)),
                OcrToken(text="20.00", conf=90.0, bbox=(1100, 275, 50, 20)),
            ]),
            LogicalLine(tokens=[
                OcrToken(text="Спирт 48брХ26", conf=90.0, bbox=(120, 300, 120, 20)),
            ]),
            LogicalLine(tokens=[
                OcrToken(text="в стъклени шишета", conf=90.0, bbox=(120, 325, 140, 20)),
            ]),
            LogicalLine(tokens=[
                OcrToken(text="партида B-2026", conf=90.0, bbox=(120, 350, 120, 20)),
            ]),

            # Item 4: 4 continuation lines
            LogicalLine(tokens=[
                OcrToken(text="4", conf=90.0, bbox=(50, 375, 20, 20)),
                OcrToken(text="Строителни материали", conf=90.0, bbox=(120, 375, 150, 20)),
                OcrToken(text="2", conf=90.0, bbox=(700, 375, 20, 20)),
                OcrToken(text="25.00", conf=90.0, bbox=(900, 375, 50, 20)),
                OcrToken(text="50.00", conf=90.0, bbox=(1100, 375, 50, 20)),
            ]),
            LogicalLine(tokens=[
                OcrToken(text="Цимент клас А 25 кг", conf=90.0, bbox=(120, 400, 150, 20)),
            ]),
            LogicalLine(tokens=[
                OcrToken(text="Пясък фин 50 кг", conf=90.0, bbox=(120, 425, 140, 20)),
            ]),
            LogicalLine(tokens=[
                OcrToken(text="Пластификатор 5л", conf=90.0, bbox=(120, 450, 140, 20)),
            ]),
            LogicalLine(tokens=[
                OcrToken(text="Сертификат ISO", conf=90.0, bbox=(120, 475, 120, 20)),
            ]),
        ]

        t = TableRegion(
            columns=cols,
            header_line=LogicalLine(tokens=[OcrToken(text="Стока", conf=90.0, bbox=(100, 100, 100, 20))]),
            data_lines=data_lines,
            page_number=1,
        )

        items = extract_line_items([t], data_lines)
        assert len(items) == 4, f"Expected exactly 4 items, got {len(items)}"

        # Verify Item 1 (1 continuation line)
        assert items[0].index == 1
        assert items[0].description == "Шоколад Милка с цели лешници"
        assert items[0].quantity == Decimal("1")
        assert items[0].total_price_net.amount == Decimal("10.00")

        # Verify Item 2 (2 continuation lines with numeric strings)
        assert items[1].index == 2
        assert items[1].description == "Зеленчуци пресни Картофи 2.5 кг Олио 3л"
        assert items[1].quantity == Decimal("5")
        assert items[1].total_price_net.amount == Decimal("15.00")

        # Verify Item 3 (3 continuation lines with mixed alphanumeric)
        assert items[2].index == 3
        assert items[2].description == "Медицински спирт Спирт 48брХ26 в стъклени шишета партида B-2026"
        assert items[2].quantity == Decimal("10")
        assert items[2].total_price_net.amount == Decimal("20.00")

        # Verify Item 4 (4 continuation lines)
        assert items[3].index == 4
        assert items[3].description == "Строителни материали Цимент клас А 25 кг Пясък фин 50 кг Пластификатор 5л Сертификат ISO"
        assert items[3].quantity == Decimal("2")
        assert items[3].total_price_net.amount == Decimal("50.00")

    def test_continuation_line_keyword_collision_vulnerability(self):
        """Adversarial check: continuation lines containing 'стр.' or 'продължение' must not be dropped."""
        cols = [
            TableColumn(header_text="№", semantic_type="index", x_center=50, x_left=0, x_right=100),
            TableColumn(header_text="Стока", semantic_type="description", x_center=300, x_left=100, x_right=600),
            TableColumn(header_text="Кол.", semantic_type="quantity", x_center=700, x_left=600, x_right=800),
            TableColumn(header_text="Цена", semantic_type="unit_price", x_center=900, x_left=800, x_right=1000),
            TableColumn(header_text="Сума", semantic_type="total_price", x_center=1100, x_left=1000, x_right=1300),
        ]
        data_lines = [
            LogicalLine(tokens=[
                OcrToken(text="1", conf=90.0, bbox=(50, 150, 20, 20)),
                OcrToken(text="Доставка на материали", conf=90.0, bbox=(120, 150, 150, 20)),
                OcrToken(text="1", conf=90.0, bbox=(700, 150, 20, 20)),
                OcrToken(text="100.00", conf=90.0, bbox=(900, 150, 50, 20)),
                OcrToken(text="100.00", conf=90.0, bbox=(1100, 150, 50, 20)),
            ]),
            LogicalLine(tokens=[
                OcrToken(text="стр. обект Люлин бл. 100", conf=90.0, bbox=(120, 175, 200, 20)),
            ]),
        ]
        t = TableRegion(
            columns=cols,
            header_line=LogicalLine(tokens=[OcrToken(text="Стока", conf=90.0, bbox=(100, 100, 100, 20))]),
            data_lines=data_lines,
            page_number=1,
        )
        items = extract_line_items([t], data_lines)
        assert len(items) == 1
        assert "стр. обект" in (items[0].description or ""), (
            "VULNERABILITY: Continuation line containing 'стр. обект' was dropped because "
            "is_transfer_or_header_line uses substring matching on CONTINUATION_KEYWORDS ('стр.')!"
        )


# ===================================================================
# 5. Synthetic Placeholder Rejection
# ===================================================================

class TestAdversarialSyntheticPlaceholderRejection:
    """Stress tests for strict null fallback policy: zero synthetic dummy strings."""

    @pytest.mark.parametrize("placeholder", ["Item", "Unknown", "Placeholder", "Артикул", "", "  "])
    def test_table_cells_placeholder_rejection_in_table_extraction(self, placeholder: str):
        """Table cells containing placeholders or empty strings strictly yield description=None and MISSING_DESCRIPTION issue."""
        cols = [
            TableColumn(header_text="№", semantic_type="index", x_center=50, x_left=0, x_right=100),
            TableColumn(header_text="Стока", semantic_type="description", x_center=300, x_left=100, x_right=600),
            TableColumn(header_text="Кол.", semantic_type="quantity", x_center=700, x_left=600, x_right=800),
            TableColumn(header_text="Цена", semantic_type="unit_price", x_center=900, x_left=800, x_right=1000),
            TableColumn(header_text="Сума", semantic_type="total_price", x_center=1100, x_left=1000, x_right=1300),
        ]
        tokens = [
            OcrToken(text="1", conf=90.0, bbox=(50, 150, 15, 20)),
            OcrToken(text="1.00", conf=90.0, bbox=(700, 150, 40, 20)),
            OcrToken(text="50.00", conf=90.0, bbox=(900, 150, 50, 20)),
            OcrToken(text="50.00", conf=90.0, bbox=(1100, 150, 50, 20)),
        ]
        if placeholder:
            tokens.append(OcrToken(text=placeholder, conf=90.0, bbox=(200, 150, 100, 20)))

        t = TableRegion(
            columns=cols,
            header_line=LogicalLine(tokens=[OcrToken(text="Стока", conf=90.0, bbox=(100, 100, 100, 20))]),
            data_lines=[LogicalLine(tokens=tokens)],
            page_number=1,
        )

        items = extract_line_items([t], t.data_lines)
        assert len(items) == 1, f"Expected 1 item for placeholder {placeholder!r}, got {len(items)}"
        assert items[0].description is None, (
            f"Expected description=None for placeholder {placeholder!r}, got {items[0].description!r}"
        )

        inv = Invoice(line_items=items)
        issues = _validate_line_items(inv)
        missing_issues = [iss for iss in issues if iss.code == "MISSING_DESCRIPTION"]
        assert len(missing_issues) == 1, (
            f"Expected 1 MISSING_DESCRIPTION issue for placeholder {placeholder!r}, got {len(missing_issues)}"
        )
        assert missing_issues[0].field == "line_items[0].description"

    @pytest.mark.parametrize("placeholder", ["Item", "Unknown", "Placeholder", "Артикул", "", "  "])
    def test_direct_line_item_validation_placeholder_sanitization(self, placeholder: str):
        """Direct LineItem validation: placeholders ('Артикул', '', '  ') must be sanitized to None with MISSING_DESCRIPTION."""
        item = LineItem(
            description=placeholder,
            quantity=Decimal("1"),
            unit_price_net=MoneyAmount(Decimal("10.00")),
            total_price_net=MoneyAmount(Decimal("10.00")),
        )
        inv = Invoice(line_items=[item])
        issues = _validate_line_items(inv)

        assert item.description is None, (
            f"VULNERABILITY: LineItem description={placeholder!r} was not sanitized to None by _validate_line_items! "
            f"Got: {item.description!r}"
        )
        missing_issues = [iss for iss in issues if iss.code == "MISSING_DESCRIPTION"]
        assert len(missing_issues) >= 1, (
            f"VULNERABILITY: Missing description validation issue not raised for description={placeholder!r}!"
        )


# ===================================================================
# 6. Source Dataset Immutability Check
# ===================================================================

@pytest.mark.skipif(not Path("/Volumes/NO NAME/_ФАКТУРИ").exists(), reason="Acceptance dataset not mounted")
def test_source_dataset_read_only_protection():
    """Verify zero source files in /Volumes/NO NAME/_ФАКТУРИ have been modified."""
    cmd = ["find", "/Volumes/NO NAME/_ФАКТУРИ", "!", "-path", "*/00_РМ_КАСКАДА_2026_ЕООД*", "-newerct", "2026-09-04"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, f"find command failed: {result.stderr}"
    modified_files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert len(modified_files) == 0, (
        f"CRITICAL VIOLATION: Source files were modified in acceptance dataset: {modified_files}"
    )
