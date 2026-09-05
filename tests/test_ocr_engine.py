"""Unit and integration tests for Milestone 2: Multi-Pass OCR Engine & Token Fusion.

Covers:
- Feature 10: Multi-pass Tesseract OCR (PSM 3 and PSM 11) with lang='bul'.
- Feature 11: Multi-factor scoring, bounding box metrics (IoU, IoMin), line noise suppression,
  split-word resolution, and token fusion.
- Feature 12: Low-confidence tagging (conf < 60.0) and the ZERO-DISCARD contract in raw_ocr_evidence.
"""

from __future__ import annotations

from pathlib import Path
import sys

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pytest
import pymupdf
from PIL import Image, ImageDraw, ImageFont

from invoice_ocr import (
    BULGARIAN_KEYWORDS,
    OcrToken,
    PageImage,
    _parse_ocr_dict_to_tokens,
    _score_ocr_result,
    build_raw_ocr_evidence,
    compute_box_metrics,
    execute_ocr_pass,
    fuse_ocr_passes,
    generate_preprocessing_variants,
    is_line_noise_token,
    run_multiple_ocr_passes,
    score_token_quality,
)


def _get_font(size: int = 28) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Helper to load a font supporting Cyrillic."""
    font_candidates = [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in font_candidates:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def _make_token(
    text: str,
    conf: float,
    bbox: tuple[int, int, int, int],
    page_number: int = 1,
) -> OcrToken:
    """Helper to create an OcrToken."""
    return OcrToken(
        text=text,
        conf=conf,
        bbox=bbox,
        page_number=page_number,
    )


# ===========================================================================
# 1. Feature 10: Multi-Pass OCR Execution Tests
# ===========================================================================

class TestMultiPassExecution:
    """Tests for execute_ocr_pass and run_multiple_ocr_passes."""

    def test_execute_ocr_pass_psm3_and_psm11(self):
        """execute_ocr_pass runs Tesseract with PSM 3 and PSM 11 on synthetic text."""
        img = Image.new("L", (800, 200), color=255)
        draw = ImageDraw.Draw(img)
        font = _get_font(32)
        draw.text((30, 40), "ФАКТУРА 1100124585", fill=0, font=font)
        draw.text((30, 110), "ДОСТАВЧИК КАПИНА", fill=0, font=font)
        arr = np.array(img)

        # Run Pass 1: PSM 3
        tokens_p1 = execute_ocr_pass(arr, psm=3, lang="bul")
        assert len(tokens_p1) > 0
        texts_p1 = [t.text for t in tokens_p1]
        assert any("1100124585" in t for t in texts_p1)

        # Run Pass 2: PSM 11
        tokens_p2 = execute_ocr_pass(arr, psm=11, lang="bul")
        assert len(tokens_p2) > 0
        texts_p2 = [t.text for t in tokens_p2]
        assert any("1100124585" in t for t in texts_p2)

    def test_execute_ocr_pass_blank_image(self):
        """execute_ocr_pass on blank image returns empty list without error."""
        blank = np.full((300, 300), 255, dtype=np.uint8)
        tokens = execute_ocr_pass(blank, psm=3, lang="bul")
        assert tokens == []

    def test_run_multiple_ocr_passes_with_variants(self):
        """run_multiple_ocr_passes fuses results from multiple variants."""
        img = Image.new("L", (800, 200), color=255)
        draw = ImageDraw.Draw(img)
        font = _get_font(32)
        draw.text((30, 40), "ОРИГИНАЛ ФАКТУРА 1100124585", fill=0, font=font)
        arr = np.array(img)

        variants = [("minimal", arr), ("clahe_gray", arr)]
        tokens = run_multiple_ocr_passes(variants)
        assert len(tokens) > 0
        all_text = " ".join(t.text for t in tokens)
        assert "1100124585" in all_text


# ===========================================================================
# 2. Feature 11: Token Scoring Engine Tests
# ===========================================================================

class TestTokenQualityScoring:
    """Tests for score_token_quality and _score_ocr_result."""

    def test_score_base_confidence(self):
        """Higher confidence tokens receive higher scores."""
        t_high = _make_token("текст", 95.0, (10, 10, 50, 20))
        t_low = _make_token("текст", 50.0, (10, 10, 50, 20))
        assert score_token_quality(t_high) > score_token_quality(t_low)

    def test_score_keyword_bonus(self):
        """Statutory Bulgarian invoice keywords receive significant bonuses."""
        t_kw = _make_token("ФАКТУРА", 85.0, (10, 10, 80, 20))
        t_plain = _make_token("ТЕКСТОВ", 85.0, (10, 10, 80, 20))
        assert score_token_quality(t_kw) - score_token_quality(t_plain) >= 30.0

    def test_score_date_pattern_bonus(self):
        """Valid Bulgarian date patterns receive pattern bonus."""
        t_date = _make_token("28.04.2026", 80.0, (10, 10, 80, 20))
        t_frag = _make_token("28.04.", 80.0, (10, 10, 60, 20))
        assert score_token_quality(t_date) > score_token_quality(t_frag)

    def test_score_monetary_amount_bonus(self):
        """Valid monetary amounts receive pattern bonus."""
        t_money = _make_token("1234.56", 80.0, (10, 10, 60, 20))
        t_money_comma = _make_token("12,50", 80.0, (10, 10, 50, 20))
        t_word = _make_token("абвгде", 80.0, (10, 10, 60, 20))
        assert score_token_quality(t_money) > score_token_quality(t_word)
        assert score_token_quality(t_money_comma) > score_token_quality(t_word)

    def test_score_eik_and_vat_pattern_bonus(self):
        """9-digit EIK and BG+9-digit VAT candidates receive pattern bonus."""
        t_eik = _make_token("123456789", 80.0, (10, 10, 80, 20))
        t_vat = _make_token("BG123456789", 80.0, (10, 10, 90, 20))
        t_num = _make_token("1234", 80.0, (10, 10, 40, 20))
        assert score_token_quality(t_eik) > score_token_quality(t_num)
        assert score_token_quality(t_vat) > score_token_quality(t_num)

    def test_score_iban_pattern_bonus(self):
        """Bulgarian IBAN candidate receives pattern bonus."""
        t_iban = _make_token("BG80BNBG96611020345678", 85.0, (10, 10, 180, 20))
        t_random = _make_token("BG80BNBG96611020345XYZ", 85.0, (10, 10, 180, 20))
        assert score_token_quality(t_iban) > score_token_quality(t_random)

    def test_score_spurious_quotes_penalty(self):
        """Spurious quote characters (e.g. „ДДС) receive a penalty relative to clean text."""
        t_clean = _make_token("ДДС", 70.0, (10, 10, 30, 20))
        t_junk = _make_token("„ДДС", 70.0, (10, 10, 35, 20))
        assert score_token_quality(t_clean) > score_token_quality(t_junk)

    def test_score_repetitive_noise_penalty(self):
        """Repetitive noise strings receive severe negative penalties."""
        t_rep = _make_token("ООООООООООО", 60.0, (10, 10, 100, 10))
        assert score_token_quality(t_rep) < 30.0

    def test_legacy_score_ocr_result(self):
        """Backward-compatible _score_ocr_result scores Bulgarian invoice tokens higher than noise."""
        valid_tokens = [
            _make_token("ФАКТУРА", 95.0, (0, 0, 50, 20)),
            _make_token("Доставчик", 92.0, (0, 30, 80, 20)),
            _make_token("Получател", 90.0, (0, 60, 80, 20)),
            _make_token("28.04.2026", 88.0, (0, 90, 80, 20)),
        ]
        noisy_tokens = [
            _make_token("???", 40.0, (0, 0, 50, 20)),
            _make_token("###", 30.0, (0, 30, 80, 20)),
        ]
        score_v = _score_ocr_result(valid_tokens)
        score_n = _score_ocr_result(noisy_tokens)
        assert score_v > score_n


# ===========================================================================
# 3. Feature 11: Box Metrics & Line Noise Gating
# ===========================================================================

class TestBoxMetricsAndNoiseGating:
    """Tests for compute_box_metrics and is_line_noise_token."""

    def test_compute_box_metrics_disjoint(self):
        """Disjoint boxes return 0.0 IoU and IoMin."""
        b1 = (0, 0, 10, 10)
        b2 = (20, 20, 10, 10)
        iou, iomin = compute_box_metrics(b1, b2)
        assert iou == 0.0
        assert iomin == 0.0

    def test_compute_box_metrics_identical(self):
        """Identical boxes return 1.0 IoU and IoMin."""
        b1 = (10, 10, 50, 20)
        iou, iomin = compute_box_metrics(b1, b1)
        assert iou == 1.0
        assert iomin == 1.0

    def test_compute_box_metrics_contained(self):
        """Smaller box inside larger box returns IoMin=1.0 even if IoU is small."""
        b_large = (10, 10, 200, 30)
        b_small = (10, 10, 30, 30)
        iou, iomin = compute_box_metrics(b_large, b_small)
        assert iomin == 1.0
        assert iou < 0.3

    def test_is_line_noise_token_detects_horizontal_lines(self):
        """Detect horizontal table divider border lines."""
        t_hline = _make_token("—————", 20.0, (50, 200, 600, 3))
        assert is_line_noise_token(t_hline) is True

    def test_is_line_noise_token_detects_vertical_lines(self):
        """Detect vertical table divider border pipes."""
        t_vline = _make_token("|", 85.0, (200, 50, 3, 250))
        assert is_line_noise_token(t_vline) is True

    def test_is_line_noise_token_preserves_valid_text(self):
        """Legitimate words must not be tagged as line noise."""
        t_word = _make_token("ФАКТУРА", 92.0, (100, 50, 120, 25))
        assert is_line_noise_token(t_word) is False


# ===========================================================================
# 4. Feature 11: Bounding Box Fusion Tests
# ===========================================================================

class TestBoundingBoxFusion:
    """Tests for fuse_ocr_passes."""

    def test_fuse_exact_duplicates_deduplicated(self):
        """Identical tokens in both passes deduplicate to exactly one token."""
        t1 = _make_token("ФАКТУРА", 90.0, (100, 50, 80, 20))
        t2 = _make_token("ФАКТУРА", 90.0, (100, 50, 80, 20))

        fused = fuse_ocr_passes([t1], [t2])
        assert len(fused) == 1
        assert fused[0].text == "ФАКТУРА"

    def test_fuse_competing_tokens_higher_score_wins(self):
        """Pass 2 higher-quality token replaces low-confidence Pass 1 misread."""
        t1_misread = _make_token("„БИК", 41.0, (100, 50, 50, 20))
        t2_clean = _make_token("ЕИК", 91.0, (100, 50, 40, 20))

        fused = fuse_ocr_passes([t1_misread], [t2_clean])
        assert len(fused) == 1
        assert fused[0].text == "ЕИК"
        assert fused[0].conf == 91.0

    def test_fuse_split_word_replaced_by_unified_token(self):
        """When P1 has split fragments and P2 has unified word, unified word wins without duplication."""
        # P1 has 'Фак' and 'тура'
        t1_frag1 = _make_token("Фак", 88.0, (100, 50, 40, 20))
        t1_frag2 = _make_token("тура", 88.0, (142, 50, 45, 20))
        # P2 has full 'Фактура'
        t2_unified = _make_token("Фактура", 92.0, (100, 50, 88, 20))

        fused = fuse_ocr_passes([t1_frag1, t1_frag2], [t2_unified])
        assert len(fused) == 1
        assert fused[0].text == "Фактура"

    def test_fuse_filters_out_table_line_noise(self):
        """Table divider noise tokens in Pass 2 are excluded from fused tokens."""
        t1 = _make_token("СТОЙНОСТ", 90.0, (100, 50, 80, 20))
        t2_noise = _make_token("ООООООООООО", 10.0, (100, 75, 500, 3))

        fused = fuse_ocr_passes([t1], [t2_noise])
        texts = [t.text for t in fused]
        assert "СТОЙНОСТ" in texts
        assert "ООООООООООО" not in texts

    def test_fuse_admits_valid_pass2_orphans(self):
        """Sparse tokens found only in Pass 2 (e.g. isolated company name) are preserved."""
        t1 = _make_token("ФАКТУРА", 90.0, (100, 50, 80, 20))
        t2_orphan = _make_token("КАПИНА", 93.0, (300, 50, 90, 20))

        fused = fuse_ocr_passes([t1], [t2_orphan])
        texts = [t.text for t in fused]
        assert "ФАКТУРА" in texts
        assert "КАПИНА" in texts

    def test_fuse_geometric_sorting(self):
        """Fused tokens are sorted top-to-bottom, left-to-right."""
        t_bottom_right = _make_token("BottomRight", 90.0, (500, 600, 50, 20))
        t_top_left = _make_token("TopLeft", 90.0, (50, 100, 50, 20))
        t_top_right = _make_token("TopRight", 90.0, (500, 100, 50, 20))

        fused = fuse_ocr_passes([t_bottom_right, t_top_right], [t_top_left])
        assert [t.text for t in fused] == ["TopLeft", "TopRight", "BottomRight"]


# ===========================================================================
# 5. Feature 12: Low-Confidence Tagging & ZERO-DISCARD Contract Tests
# ===========================================================================

class TestLowConfidenceTaggingAndZeroDiscard:
    """Tests for Feature 12: conf < 60 threshold and zero-discard policy in raw_ocr_evidence."""

    def test_low_confidence_boundary_59_vs_60(self):
        """conf=59.9 must have is_low_confidence=True, conf=60.0 must have False."""
        t_59 = _make_token("тест", 59.9, (10, 10, 30, 20))
        t_60 = _make_token("тест", 60.0, (50, 10, 30, 20))

        fused = fuse_ocr_passes([t_59, t_60], [])
        token_map = {t.left: t for t in fused}

        assert token_map[10].is_low_confidence is True
        assert token_map[50].is_low_confidence is False

    def test_zero_discard_contract_preserves_low_conf_tokens(self):
        """ZERO-DISCARD CONTRACT: tokens with conf=15.0 must NOT be dropped from raw_ocr_evidence."""
        tokens = [
            _make_token("СЛАБ", 15.0, (10, 10, 40, 20)),
            _make_token("СИЛЕН", 90.0, (60, 10, 40, 20)),
        ]
        pages = [PageImage(page_number=1, image=np.zeros((100, 100, 3), dtype=np.uint8), width=100, height=100)]

        evidence = build_raw_ocr_evidence(pages, tokens)

        assert evidence["total_tokens"] == 2
        assert evidence["low_confidence_count"] == 1
        page_tokens = evidence["pages"][0]["tokens"]
        assert len(page_tokens) == 2
        assert page_tokens[0]["text"] == "СЛАБ"
        assert page_tokens[0]["is_low_confidence"] is True
        assert page_tokens[0]["conf"] == 15.0

    def test_raw_ocr_evidence_schema_structure(self):
        """build_raw_ocr_evidence produces compliant Layer 1 schema structure."""
        tokens = [
            _make_token("ФАКТУРА", 92.0, (100, 50, 120, 25), page_number=1),
            _make_token("1100124585", 88.0, (230, 50, 100, 25), page_number=1),
        ]
        pages = [PageImage(page_number=1, image=np.zeros((1000, 800, 3), dtype=np.uint8), width=800, height=1000)]

        ev = build_raw_ocr_evidence(pages, tokens)
        assert "total_pages" in ev
        assert "total_tokens" in ev
        assert "mean_confidence" in ev
        assert "low_confidence_count" in ev
        assert "pages" in ev
        assert ev["total_tokens"] == 2
        assert ev["mean_confidence"] == 90.0
        assert ev["low_confidence_count"] == 0

        p0 = ev["pages"][0]
        assert p0["page_number"] == 1
        assert p0["width"] == 800
        assert p0["height"] == 1000
        assert p0["token_count"] == 2
        assert p0["tokens"][0]["bbox"] == [100, 50, 120, 25]


# ===========================================================================
# 6. Real Acceptance Dataset Verification (Kapina Invoices)
# ===========================================================================

class TestLiveKapinaIntegration:
    """Read-only verification on the mandatory Kapina invoice dataset."""

    def test_kapina_01_fused_yield_and_terms(self):
        """капина-01.pdf multi-pass fused yield, confidence, and keyword recovery."""
        fpath = Path("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf")
        if not fpath.exists():
            pytest.skip("Acceptance dataset not mounted")

        doc = pymupdf.open(fpath)
        page = doc[0]
        pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csRGB, alpha=False)
        bgr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
        doc.close()

        # Run multi-pass OCR via preprocessing variants
        variants = generate_preprocessing_variants(bgr)
        tokens = run_multiple_ocr_passes(variants)

        # Fused token yield assertion
        assert len(tokens) >= 220, f"Expected >= 220 tokens, got {len(tokens)}"

        # Mean confidence assertion
        mean_conf = np.mean([t.conf for t in tokens])
        assert mean_conf >= 75.0, f"Expected mean confidence >= 75.0, got {mean_conf:.1f}"

        # Statutory keywords assertion
        terms = ["фактура", "доставчик", "получател", "еик", "ддс", "капина"]
        found = [term for term in terms if any(term in tok.text.lower() for tok in tokens)]
        assert len(found) >= 5, f"Expected at least 5 statutory terms, found {found}"
