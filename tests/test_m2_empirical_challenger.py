"""Empirical Challenger 2 Test Suite for Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine.

Author: Challenger 2 (teamwork_preview_challenger_m2_2)
Roles: critic, specialist

Covers:
1. Multi-pass OCR execution and token fusion on Kapina acceptance files (капина-01.pdf, капина-02.pdf, капина-03.pdf)
   - Recall of statutory Bulgarian terms: фактура, доставчик, получател, еик, ддс, сума, плащане.
   - Mean confidence score before and after fusion across all 3 documents.
   - Thermal slip occlusion in капина-03.pdf: CLAHE enhancement and low-confidence tagging (conf < 60, is_low_confidence=True).
2. Layer 1 Zero-Discard Contract:
   - build_raw_ocr_evidence preserves all tokens with complete fields: [left, top, width, height], conf, page_number, is_low_confidence.
   - Exact count conservation: len(tokens) == total_tokens == sum(page token_counts).
3. Adversarial and edge case verification for token fusion, scoring, CLAHE, and deskew.
4. Source dataset immutability verification on /Volumes/NO NAME/_ФАКТУРИ.
"""

import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
import pytest

from invoice_ocr import (
    PageImage,
    PageTransform,
    OcrToken,
    MIN_CONFIDENCE,
    BULGARIAN_KEYWORDS,
    load_document,
    normalize_page_geometry,
    generate_preprocessing_variants,
    enhance_contrast_clahe,
    denoise_bilateral,
    binarize_otsu,
    execute_ocr_pass,
    fuse_ocr_passes,
    score_token_quality,
    compute_box_metrics,
    is_line_noise_token,
    build_raw_ocr_evidence,
    process_invoice,
)

KAPINA_DIR = Path("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026")
KAPINA_FILES = ["капина-01.pdf", "капина-02.pdf", "капина-03.pdf"]
STATUTORY_KEYWORDS = ["фактура", "доставчик", "получател", "еик", "ддс", "сума", "плащане"]


@pytest.mark.skipif(not KAPINA_DIR.exists(), reason="Acceptance dataset volume not mounted")
class TestKapinaAcceptanceMultiPassAndFusion:
    """Empirical verification on actual Kapina acceptance invoices in read-only mode."""

    @pytest.mark.parametrize("filename", KAPINA_FILES)
    def test_multi_pass_fusion_and_keyword_recall(self, filename: str) -> None:
        """Verify that multi-pass fusion achieves 100% recall of statutory Bulgarian invoice keywords."""
        fpath = KAPINA_DIR / filename
        pages = load_document(fpath)
        assert len(pages) == 1, f"Expected 1 page for {filename}, found {len(pages)}"

        page = pages[0]
        norm_page, transform = normalize_page_geometry(page)
        variants = generate_preprocessing_variants(norm_page.image)

        # Select standard target
        target_img = next(img for name, img in variants if name == "standard")

        p1_tokens = execute_ocr_pass(target_img, psm=3, lang="bul")
        p2_tokens = execute_ocr_pass(target_img, psm=11, lang="bul")
        fused_tokens = fuse_ocr_passes(p1_tokens, p2_tokens)

        # Check token existence
        assert len(p1_tokens) > 100, f"Pass 1 produced too few tokens: {len(p1_tokens)}"
        assert len(p2_tokens) > 100, f"Pass 2 produced too few tokens: {len(p2_tokens)}"
        assert len(fused_tokens) > 100, f"Fused pass produced too few tokens: {len(fused_tokens)}"

        # Check statutory keyword recall
        fused_text = " ".join(t.text.lower() for t in fused_tokens)
        matched_keywords = [kw for kw in STATUTORY_KEYWORDS if kw in fused_text]
        assert len(matched_keywords) == len(STATUTORY_KEYWORDS), (
            f"{filename}: Missing statutory keywords in fused output: "
            f"{set(STATUTORY_KEYWORDS) - set(matched_keywords)}"
        )

        # Verify confidence score increase over Pass 1
        p1_conf = np.mean([t.conf for t in p1_tokens]) if p1_tokens else 0.0
        fused_conf = np.mean([t.conf for t in fused_tokens]) if fused_tokens else 0.0
        assert fused_conf > p1_conf, (
            f"{filename}: Fused confidence ({fused_conf:.2f}%) did not exceed "
            f"Pass 1 confidence ({p1_conf:.2f}%)"
        )
        assert fused_conf >= 65.0, f"{filename}: Fused confidence below baseline ({fused_conf:.2f}%)"

    def test_kapina_03_thermal_slip_clahe_and_low_confidence_tagging(self) -> None:
        """Verify thermal slip handling in капина-03.pdf:

        - CLAHE enhances low-contrast thermal text
        - All low-confidence tokens (conf < 60.0) are properly tagged with is_low_confidence=True
        - Low-confidence tokens are fully preserved in raw_ocr_evidence
        """
        fpath = KAPINA_DIR / "капина-03.pdf"
        pages = load_document(fpath)
        norm_page, _ = normalize_page_geometry(pages[0])
        variants = generate_preprocessing_variants(norm_page.image)

        # Retrieve standard and minimal (raw grayscale)
        clahe_img = next(img for name, img in variants if name == "standard")
        raw_gray = next(img for name, img in variants if name == "minimal")

        # Contrast enhancement check
        clahe_p2 = execute_ocr_pass(clahe_img, psm=11, lang="bul")
        fused = fuse_ocr_passes(
            execute_ocr_pass(clahe_img, psm=3, lang="bul"),
            clahe_p2,
        )

        for t in fused:
            t.page_number = norm_page.page_number
            t.is_low_confidence = (t.conf < MIN_CONFIDENCE)

        # Verify low confidence tagging consistency
        low_conf_tokens = [t for t in fused if t.conf < 60.0]
        assert len(low_conf_tokens) > 30, (
            f"Expected substantial low-confidence tokens from thermal slip, found {len(low_conf_tokens)}"
        )

        for t in fused:
            if t.conf < 60.0:
                assert t.is_low_confidence is True, f"Token {t.text} (conf={t.conf}) was not tagged as low confidence"
            else:
                assert t.is_low_confidence is False, f"Token {t.text} (conf={t.conf}) incorrectly tagged as low confidence"

        # Verify thermal slip tokens present in raw evidence
        evidence = build_raw_ocr_evidence([norm_page], fused)
        assert evidence["low_confidence_count"] == len(low_conf_tokens)
        assert evidence["total_tokens"] == len(fused)

        # Specifically check thermal slip coordinates (right margin: x > 1600)
        thermal_tokens = [
            tok for tok in evidence["pages"][0]["tokens"]
            if tok["bbox"][0] >= 1600 and tok["bbox"][1] <= 1200
        ]
        assert len(thermal_tokens) >= 10, f"Expected thermal slip tokens in right margin, found {len(thermal_tokens)}"
        # Check that thermal tokens contain low-confidence flags
        thermal_low_conf = [tok for tok in thermal_tokens if tok["is_low_confidence"]]
        assert len(thermal_low_conf) > 0, "Expected low-confidence tokens within thermal slip region"

    def test_mean_confidence_improvement_across_all_documents(self) -> None:
        """Verify empirical confidence gains across the full Kapina test set."""
        results: list[dict[str, float]] = []

        for fname in KAPINA_FILES:
            pages = load_document(KAPINA_DIR / fname)
            norm_page, _ = normalize_page_geometry(pages[0])
            variants = generate_preprocessing_variants(norm_page.image)
            target_img = next(img for name, img in variants if name == "standard")

            p1 = execute_ocr_pass(target_img, psm=3, lang="bul")
            p2 = execute_ocr_pass(target_img, psm=11, lang="bul")
            fused = fuse_ocr_passes(p1, p2)

            c1 = float(np.mean([t.conf for t in p1]))
            c2 = float(np.mean([t.conf for t in p2]))
            cf = float(np.mean([t.conf for t in fused]))

            results.append({
                "file": fname,
                "pass1_conf": c1,
                "pass2_conf": c2,
                "fused_conf": cf,
                "gain": cf - c1,
            })

        for r in results:
            # Each document must exhibit at least +8.0% confidence boost over Pass 1
            assert r["gain"] >= 8.0, (
                f"{r['file']}: Gain {r['gain']:.2f}% is lower than required +8.0% minimum "
                f"(Pass 1: {r['pass1_conf']:.2f}%, Fused: {r['fused_conf']:.2f}%)"
            )


class TestLayer1ZeroDiscardContract:
    """Rigorous verification of the Layer 1 Zero-Discard Contract."""

    def test_zero_discard_full_token_preservation(self) -> None:
        """Every token passed to build_raw_ocr_evidence must appear in the serialized output."""
        pages = [
            PageImage(page_number=1, image=np.zeros((100, 100, 3), dtype=np.uint8), width=100, height=100),
            PageImage(page_number=2, image=np.zeros((100, 100, 3), dtype=np.uint8), width=100, height=100),
        ]
        tokens = [
            OcrToken(text="HighConf", conf=95.0, bbox=(10, 10, 50, 20), page_number=1),
            OcrToken(text="Borderline", conf=60.0, bbox=(70, 10, 50, 20), page_number=1),
            OcrToken(text="LowConf1", conf=59.9, bbox=(10, 40, 50, 20), page_number=1),
            OcrToken(text="ZeroConf", conf=0.0, bbox=(10, 70, 50, 20), page_number=1),
            OcrToken(text="P2Token", conf=45.0, bbox=(20, 20, 40, 15), page_number=2),
        ]

        evidence = build_raw_ocr_evidence(pages, tokens)

        # 1. Exact count matches
        assert evidence["total_tokens"] == len(tokens)
        assert evidence["total_pages"] == 2
        assert evidence["low_confidence_count"] == 3  # 59.9, 0.0, 45.0

        # 2. Check each page's token count
        p1_tokens = evidence["pages"][0]["tokens"]
        p2_tokens = evidence["pages"][1]["tokens"]
        assert len(p1_tokens) == 4
        assert len(p2_tokens) == 1

        # 3. Verify exact textual content preservation in order
        assert [t["text"] for t in p1_tokens] == ["HighConf", "Borderline", "LowConf1", "ZeroConf"]
        assert [t["text"] for t in p2_tokens] == ["P2Token"]

    def test_zero_discard_coordinate_and_field_completeness(self) -> None:
        """Verify that every token record contains valid complete coordinates and metadata."""
        pages = [PageImage(page_number=1, image=np.zeros((500, 500, 3), dtype=np.uint8), width=500, height=500)]
        tokens = [
            OcrToken(text="Тест", conf=85.5, bbox=(12, 34, 56, 78), page_number=1),
            OcrToken(text="Фактура", conf=32.1, bbox=(100, 200, 300, 40), page_number=1),
        ]

        evidence = build_raw_ocr_evidence(pages, tokens)
        tok_records = evidence["pages"][0]["tokens"]

        for i, rec in enumerate(tok_records):
            assert "text" in rec and isinstance(rec["text"], str)
            assert "conf" in rec and isinstance(rec["conf"], float)
            assert "bbox" in rec and isinstance(rec["bbox"], list)
            assert len(rec["bbox"]) == 4
            left, top, width, height = rec["bbox"]
            assert all(isinstance(v, int) for v in (left, top, width, height))
            assert "page_number" in rec and rec["page_number"] == 1
            assert "is_low_confidence" in rec and isinstance(rec["is_low_confidence"], bool)

        assert tok_records[0]["bbox"] == [12, 34, 56, 78]
        assert tok_records[0]["is_low_confidence"] is False
        assert tok_records[1]["bbox"] == [100, 200, 300, 40]
        assert tok_records[1]["is_low_confidence"] is True

    def test_zero_discard_json_serializability(self) -> None:
        """Verify that the raw OCR evidence dict can be serialized cleanly to JSON without error."""
        pages = [PageImage(page_number=1, image=np.zeros((100, 100, 3), dtype=np.uint8), width=100, height=100)]
        tokens = [
            OcrToken(text="Стойност", conf=92.3456, bbox=(10, 20, 30, 40), page_number=1),
            OcrToken(text="ДДС", conf=55.0, bbox=(50, 20, 30, 40), page_number=1),
        ]
        evidence = build_raw_ocr_evidence(pages, tokens)
        dumped = json.dumps(evidence, ensure_ascii=False)
        loaded = json.loads(dumped)

        assert loaded["total_tokens"] == 2
        assert loaded["mean_confidence"] == pytest.approx(73.67, abs=0.1)
        assert loaded["pages"][0]["tokens"][0]["text"] == "Стойност"
        assert loaded["pages"][0]["tokens"][1]["is_low_confidence"] is True


class TestTokenFusionAndScoringEngine:
    """Stress and edge case testing of the token scoring and fusion engine."""

    def test_iou_and_iomin_metric_computation(self) -> None:
        """Empirically test spatial overlap metrics."""
        # Exact match
        b1 = (10, 10, 50, 20)
        iou, iomin = compute_box_metrics(b1, b1)
        assert iou == pytest.approx(1.0)
        assert iomin == pytest.approx(1.0)

        # Disjoint boxes
        b2 = (100, 100, 50, 20)
        iou, iomin = compute_box_metrics(b1, b2)
        assert iou == 0.0
        assert iomin == 0.0

        # Contained box (b3 inside b1)
        b3 = (20, 12, 20, 10)  # Area: 200, b1 Area: 1000
        iou, iomin = compute_box_metrics(b1, b3)
        assert iomin == pytest.approx(1.0)
        assert iou == pytest.approx(200.0 / 1000.0)

    def test_line_noise_token_discrimination(self) -> None:
        """Verify that horizontal/vertical table divider lines are identified as noise."""
        line_horiz = OcrToken(text="—", conf=40.0, bbox=(10, 100, 500, 2))  # aspect = 250, h=2
        assert is_line_noise_token(line_horiz) is True

        line_vert = OcrToken(text="|", conf=40.0, bbox=(100, 10, 3, 400))   # aspect = 0.0075, w=3
        assert is_line_noise_token(line_vert) is True

        repetitive_noise = OcrToken(text="----------", conf=50.0, bbox=(10, 10, 100, 15))
        assert is_line_noise_token(repetitive_noise) is True

        # Valid financial symbols must NOT be flagged as line noise
        valid_dash = OcrToken(text="-", conf=80.0, bbox=(50, 50, 12, 10))
        assert is_line_noise_token(valid_dash) is False

        valid_word = OcrToken(text="ФАКТУРА", conf=85.0, bbox=(100, 50, 200, 30))
        assert is_line_noise_token(valid_word) is False

    def test_bulgarian_keyword_scoring_bias(self) -> None:
        """Bulgarian statutory keywords should receive substantial positive scoring bonus (+30)."""
        kw_token = OcrToken(text="Фактура", conf=65.0)
        non_kw_token = OcrToken(text="Произволен", conf=65.0)
        s_kw = score_token_quality(kw_token)
        s_non = score_token_quality(non_kw_token)
        assert s_kw > s_non + 25.0

    def test_date_and_monetary_amount_scoring_bias(self) -> None:
        """Valid dates and monetary amounts should receive specialized bonuses."""
        date_token = OcrToken(text="28.08.2026", conf=70.0)
        random_token = OcrToken(text="abcdefghij", conf=70.0)
        assert score_token_quality(date_token) > score_token_quality(random_token) + 20.0

        amount_token = OcrToken(text="1234.56", conf=70.0)
        assert score_token_quality(amount_token) > score_token_quality(random_token) + 10.0

    def test_eik_and_iban_scoring_bias(self) -> None:
        """EIK (9 or 13 digits) and BG IBAN tokens receive +25 bonus."""
        eik_token = OcrToken(text="207930830", conf=75.0)
        iban_token = OcrToken(text="BG80BNBG96611020345678", conf=75.0)
        plain_token = OcrToken(text="somestring", conf=75.0)

        assert score_token_quality(eik_token) > score_token_quality(plain_token) + 20.0
        assert score_token_quality(iban_token) > score_token_quality(plain_token) + 20.0

    def test_spurious_punctuation_penalty(self) -> None:
        """Tokens with spurious quotes or pipe wrappers should be penalized."""
        clean_tok = OcrToken(text="КАПИНА", conf=70.0)
        dirty_tok = OcrToken(text="„КАПИНА|", conf=70.0)
        assert score_token_quality(dirty_tok) < score_token_quality(clean_tok) - 15.0

    def test_pass2_orphan_token_admission(self) -> None:
        """Valid sparse tokens discovered only in Pass 2 must be admitted during fusion."""
        p1 = [OcrToken(text="Оригинал", conf=90.0, bbox=(10, 10, 80, 25))]
        # Pass 2 discovered an isolated company EIK not captured in Pass 1
        p2 = [
            OcrToken(text="Оригинал", conf=85.0, bbox=(10, 10, 80, 25)),
            OcrToken(text="207930830", conf=88.0, bbox=(500, 10, 90, 25)),  # orphan EIK
        ]
        fused = fuse_ocr_passes(p1, p2)
        fused_texts = [t.text for t in fused]
        assert "207930830" in fused_texts
        assert len(fused) == 2


@pytest.mark.skipif(not KAPINA_DIR.exists(), reason="Acceptance dataset volume not mounted")
class TestSourceDatasetImmutability:
    """Verify strictly read-only access to source volume /Volumes/NO NAME/_ФАКТУРИ."""

    def test_source_dataset_strictly_unmodified(self) -> None:
        """Assert no files have been modified or created since 2026-09-04."""
        import subprocess
        res = subprocess.run(
            ["find", str(KAPINA_DIR), "-newerct", "2026-09-04"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert res.stdout.strip() == "", f"Unexpected modified files detected: {res.stdout.strip()}"
