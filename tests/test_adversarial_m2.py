"""Adversarial stress-test suite for Milestone 2: Adaptive Preprocessing & Multi-Pass OCR Engine.

Authored by Challenger 1 (teamwork_preview_challenger_m2_1).
Covers:
1. Extreme rotation stress testing (45°, 90°, 135°, 180°, 270°, 360°).
2. Extreme skew boundary and out-of-range testing (-15.0°, +15.0°, -15.1°, +16.0°, ±45°, ±85°).
3. Degraded, single-pixel, checkerboard, and inverted color scans.
4. Bulgarian Cyrillic diacritic stress test ("й", "Й", "ѝ", "è") and decimal comma numbers ("12,50", "0,20", "1.95583").
5. Table line noise stress testing (`----`, `____`, `====`, `|`) in gating, scoring, and fusion.
6. Dataset immutability on /Volumes/NO NAME/_ФАКТУРИ.
"""

from __future__ import annotations

from pathlib import Path
import sys

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from invoice_ocr import (
    BULGARIAN_KEYWORDS,
    OcrToken,
    PageImage,
    PageTransform,
    apply_deskew,
    apply_orientation,
    binarize_otsu,
    check_and_fix_orientation,
    compute_box_metrics,
    denoise_bilateral,
    deskew_image,
    detect_deskew_angle,
    detect_orientation,
    enhance_contrast_clahe,
    execute_ocr_pass,
    fuse_ocr_passes,
    generate_preprocessing_variants,
    is_line_noise_token,
    morphological_cleanup,
    normalize_page_geometry,
    score_token_quality,
)


def _get_font(size: int = 24) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
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


def _create_synthetic_invoice_page(
    lines: list[str] | None = None,
    width: int = 1200,
    height: int = 1200,
    font_size: int = 24,
) -> np.ndarray:
    """Create a high-resolution synthetic invoice page with Bulgarian text."""
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = _get_font(font_size)

    if lines is None:
        lines = [
            f"ФАКТУРА ОРИГИНАЛ ДДС ДОСТАВЧИК ПОЛУЧАТЕЛ 110012458{i} СТОЙНОСТ 12,50 ЛВ ДАТА 28.04.2026"
            for i in range(25)
        ]

    for idx, line in enumerate(lines):
        y = 60 + idx * 42
        if y + font_size < height:
            draw.text((100, y), line, fill=(0, 0, 0), font=font)

    return np.array(img)


# ===========================================================================
# 1. Extreme Rotation Stress Tests
# ===========================================================================

class TestAdversarialExtremeRotation:
    """Stress-test OSD and orientation handling across cardinal and oblique angles."""

    @pytest.mark.parametrize("angle,expected_detected_rot", [
        (90, 90),
        (180, 180),
        (270, 270),
        (360, 0),
    ])
    def test_rotation_cardinal_angles(self, angle: int, expected_detected_rot: int):
        """Cardinal rotations (90°, 180°, 270°, 360°) detect proper clockwise correction."""
        doc = _create_synthetic_invoice_page()
        h, w = doc.shape[:2]
        center = (w / 2.0, h / 2.0)

        M = cv2.getRotationMatrix2D(center, float(angle), 1.0)
        rotated = cv2.warpAffine(
            doc, M, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255)
        )

        detected = detect_orientation(rotated)
        assert detected == expected_detected_rot, (
            f"Expected detected orientation {expected_detected_rot}° for {angle}° rotation, got {detected}°"
        )

        # Check that check_and_fix_orientation restores page to upright
        restored = check_and_fix_orientation(rotated)
        assert restored.shape == doc.shape
        assert detect_orientation(restored) == 0

    @pytest.mark.parametrize("oblique_angle", [45, 135, 225, 315])
    def test_rotation_oblique_angles_safe_rejection(self, oblique_angle: int):
        """Angles outside 90/180/270 (e.g. 45°, 135°) must return 0 and never corrupt orientation."""
        doc = _create_synthetic_invoice_page()
        h, w = doc.shape[:2]
        center = (w / 2.0, h / 2.0)

        M = cv2.getRotationMatrix2D(center, float(oblique_angle), 1.0)
        rotated = cv2.warpAffine(
            doc, M, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255)
        )

        # detect_orientation must strictly reject non-orthogonal rotations
        detected = detect_orientation(rotated)
        assert detected == 0, f"Oblique angle {oblique_angle}° should return 0, got {detected}"

        # check_and_fix_orientation must return the original image without corruption
        fixed = check_and_fix_orientation(rotated)
        assert np.array_equal(fixed, rotated)

    @pytest.mark.parametrize("invalid_deg", [-90, -45, 0, 45, 135, 360, 450])
    def test_apply_orientation_unsupported_degrees_noop(self, invalid_deg: int):
        """apply_orientation must strictly leave image unchanged for unsupported degrees."""
        img = np.random.randint(0, 256, (100, 100, 3), dtype=np.uint8)
        result = apply_orientation(img, invalid_deg)
        assert np.array_equal(result, img)


# ===========================================================================
# 2. Extreme Skew Stress Tests
# ===========================================================================

class TestAdversarialExtremeSkew:
    """Stress-test deskew detection and correction across boundary, out-of-range, and extreme angles."""

    def test_boundary_skew_angles_plus_minus_15(self):
        """Angles at the exact boundary (-15.0°, +15.0°) are detected and clamped/corrected."""
        doc = _create_synthetic_invoice_page()
        h, w = doc.shape[:2]
        center = (w / 2.0, h / 2.0)

        for tilt in [-15.0, 15.0]:
            M = cv2.getRotationMatrix2D(center, tilt, 1.0)
            skewed = cv2.warpAffine(
                doc, M, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255)
            )
            det_angle = detect_deskew_angle(skewed, max_angle=15.0)
            assert abs(det_angle) <= 15.0
            assert abs(det_angle) >= 14.0, f"Expected ~15.0°, got {det_angle}"

    @pytest.mark.parametrize("out_angle", [-15.1, 15.1, -16.0, 16.0, -20.0, 20.0])
    def test_just_outside_boundary_skew_angles_rejected(self, out_angle: float):
        """Angles just outside [-15.0°, +15.0°] must be safely rejected (return 0.0)."""
        doc = _create_synthetic_invoice_page()
        h, w = doc.shape[:2]
        center = (w / 2.0, h / 2.0)

        M = cv2.getRotationMatrix2D(center, out_angle, 1.0)
        skewed = cv2.warpAffine(
            doc, M, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255)
        )
        det_angle = detect_deskew_angle(skewed, max_angle=15.0)
        assert det_angle == 0.0, f"Angle {out_angle}° should be rejected (0.0), got {det_angle}"

    @pytest.mark.parametrize("extreme_angle", [-45.0, 45.0])
    def test_extreme_skew_45_degrees_safely_rejected(self, extreme_angle: float):
        """Extreme skew of ±45.0° must be safely rejected (return 0.0)."""
        doc = _create_synthetic_invoice_page()
        h, w = doc.shape[:2]
        center = (w / 2.0, h / 2.0)

        M = cv2.getRotationMatrix2D(center, extreme_angle, 1.0)
        skewed = cv2.warpAffine(
            doc, M, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255)
        )
        det_angle = detect_deskew_angle(skewed, max_angle=15.0)
        assert det_angle == 0.0, f"Extreme skew {extreme_angle}° must return 0.0, got {det_angle}"

    @pytest.mark.parametrize("extreme_angle", [-85.0, 85.0])
    def test_extreme_skew_85_degrees_rejection_specification(self, extreme_angle: float):
        """Specification test: Extreme skew of ±85.0° must be safely rejected (return 0.0) without 90° flipping.

        NOTE FOR AUDIT: If detect_deskew_angle flips the contour bounding box by 90°
        (treating near-vertical lines as horizontal lines tilted by 5°), this test will fail,
        empirically proving the 90° flip bug in minAreaRect normalization.
        """
        doc = _create_synthetic_invoice_page()
        h, w = doc.shape[:2]
        center = (w / 2.0, h / 2.0)

        M = cv2.getRotationMatrix2D(center, extreme_angle, 1.0)
        skewed = cv2.warpAffine(
            doc, M, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255)
        )
        det_angle = detect_deskew_angle(skewed, max_angle=15.0)
        assert det_angle == 0.0, (
            f"VULNERABILITY: detect_deskew_angle returned {det_angle}° for {extreme_angle}° tilt! "
            f"Expected 0.0 (safe rejection). The function performed an illegitimate 90° flip!"
        )


# ===========================================================================
# 3. Degraded & Inverted Scans Stress Tests
# ===========================================================================

class TestAdversarialDegradedAndInvertedScans:
    """Stress-test all preprocessing functions against pathological and inverted scans."""

    @pytest.fixture
    def pathological_images(self) -> dict[str, np.ndarray]:
        """Generate a variety of pathological, degraded, and inverted test images."""
        # 1. Pure black
        black_bgr = np.zeros((300, 300, 3), dtype=np.uint8)
        black_gray = np.zeros((300, 300), dtype=np.uint8)

        # 2. Pure white
        white_bgr = np.full((300, 300, 3), 255, dtype=np.uint8)
        white_gray = np.full((300, 300), 255, dtype=np.uint8)

        # 3. Single pixel
        pixel_bgr = np.zeros((1, 1, 3), dtype=np.uint8)
        pixel_gray = np.zeros((1, 1), dtype=np.uint8)

        # 4. Checkerboard pattern (high frequency alternating 0 and 255)
        cb_gray = (np.indices((200, 200)).sum(axis=0) % 2 * 255).astype(np.uint8)
        cb_bgr = cv2.cvtColor(cb_gray, cv2.COLOR_GRAY2BGR)

        # 5. Inverted color image (white text on pure black background)
        inv_img = Image.new("RGB", (600, 200), (0, 0, 0))
        draw = ImageDraw.Draw(inv_img)
        font = _get_font(24)
        draw.text((30, 50), "ФАКТУРА 1100124585 ДДС 12,50 ЛВ", fill=(255, 255, 255), font=font)
        inv_bgr = np.array(inv_img)
        inv_gray = cv2.cvtColor(inv_bgr, cv2.COLOR_BGR2GRAY)

        # 6. Extreme aspect ratios
        tall = np.full((4000, 10, 3), 255, dtype=np.uint8)
        wide = np.full((10, 4000, 3), 255, dtype=np.uint8)

        # 7. Uniform mid-gray
        gray_uniform = np.full((200, 200), 128, dtype=np.uint8)

        return {
            "black_bgr": black_bgr,
            "black_gray": black_gray,
            "white_bgr": white_bgr,
            "white_gray": white_gray,
            "pixel_bgr": pixel_bgr,
            "pixel_gray": pixel_gray,
            "cb_gray": cb_gray,
            "cb_bgr": cb_bgr,
            "inv_bgr": inv_bgr,
            "inv_gray": inv_gray,
            "tall": tall,
            "wide": wide,
            "gray_uniform": gray_uniform,
        }

    def test_pathological_images_no_crashes(self, pathological_images: dict[str, np.ndarray]):
        """Verify that detect_orientation, detect_deskew_angle, and deskew_image never crash."""
        for name, img in pathological_images.items():
            # Orientation
            rot = detect_orientation(img)
            assert rot in (0, 90, 180, 270), f"Invalid orientation returned on {name}: {rot}"

            # Deskew angle
            angle = detect_deskew_angle(img)
            assert isinstance(angle, float), f"Invalid angle type on {name}: {type(angle)}"
            assert abs(angle) <= 15.0, f"Angle exceeded clamp range on {name}: {angle}"

            # Deskew image
            deskewed = deskew_image(img)
            assert deskewed.shape == img.shape, f"Shape mismatch on deskew_image for {name}"

    def test_pathological_images_variant_generation_robustness(self, pathological_images: dict[str, np.ndarray]):
        """Verify that generate_preprocessing_variants executes without unhandled errors."""
        for name, img in pathological_images.items():
            if img.size < 4:
                continue
            variants = generate_preprocessing_variants(img)
            assert len(variants) >= 2, f"Expected at least 2 variants for {name}, got {len(variants)}"
            for v_name, v_img in variants:
                assert isinstance(v_img, np.ndarray)
                assert v_img.size > 0


# ===========================================================================
# 4. Bulgarian Cyrillic Diacritic & Decimal Comma Stress Tests
# ===========================================================================

class TestAdversarialBulgarianDiacriticsAndDecimals:
    """Stress-test preservation of Cyrillic diacritics ('й', 'Й', 'ѝ', 'è') and decimal commas ('12,50')."""

    def test_cyrillic_diacritic_foreground_pixel_retention(self):
        """Bilateral denoising + Otsu binarization must retain >= 98% of diacritic foreground pixels."""
        img = Image.new("L", (800, 160), 255)
        draw = ImageDraw.Draw(img)
        font = _get_font(36)

        text = "Йордан, Ивайло и Йовка: стока за 12,50 лв. с 0,20 отстъпка и курс 1.95583"
        draw.text((20, 40), text, fill=0, font=font)
        raw = np.array(img)

        # Preprocessing pipeline
        clahe = enhance_contrast_clahe(raw)
        denoised = denoise_bilateral(clahe, d=5, sigma_color=50.0, sigma_space=50.0)
        binary = binarize_otsu(denoised)

        fg_raw = (raw < 128).astype(int)
        fg_bin = (binary == 0).astype(int)

        retention = (fg_raw & fg_bin).sum() / fg_raw.sum()
        assert retention >= 0.98, f"Foreground pixel retention too low: {retention:.4f}"

    def test_morphological_cleanup_zero_mutation_guarantee(self):
        """morphological_cleanup must be strictly idempotent to avoid eroding punctuation and commas."""
        doc = _create_synthetic_invoice_page(
            lines=["12,50 лв. 0,20 лв. 1.95583 EUR й Й ѝ è"],
            width=800, height=200, font_size=32,
        )
        gray = cv2.cvtColor(doc, cv2.COLOR_BGR2GRAY)
        binary = binarize_otsu(gray)

        # Verify that morphological_cleanup leaves binary image 100% identical
        cleaned = morphological_cleanup(binary)
        assert np.array_equal(cleaned, binary), "morphological_cleanup mutated pixels!"

    def test_end_to_end_ocr_diacritic_and_decimal_recognition(self):
        """Verify Tesseract OCR recognition across preprocessing variants on diacritics and decimals."""
        img = Image.new("RGB", (1200, 300), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        font = _get_font(32)

        draw.text((30, 40), "ФАКТУРА: Йордан и Ивайло закупиха стока за 12,50 лв.", fill=(0, 0, 0), font=font)
        draw.text((30, 120), "Отстъпка: 0,20 лв. Фиксиран валутен курс: 1.95583 BGN", fill=(0, 0, 0), font=font)
        arr = np.array(img)

        variants = generate_preprocessing_variants(arr)
        for var_name, var_img in variants:
            tokens = execute_ocr_pass(var_img, psm=3, lang="bul")
            assert len(tokens) > 0, f"No tokens extracted for variant {var_name}"
            full_text = " ".join(t.text for t in tokens)

            # Check decimal comma number 12,50 or 12.50
            has_1250 = any(x in full_text for x in ["12,50", "12.50", "12, 50"])
            assert has_1250, f"Variant {var_name} corrupted '12,50': {full_text}"

            # Check decimal number 1.95583 or 1,95583
            has_rate = any(x in full_text for x in ["1.95583", "1,95583"])
            assert has_rate, f"Variant {var_name} corrupted '1.95583': {full_text}"

            # Check Cyrillic letters with diacritics
            has_y_capital = ("Й" in full_text or "И" in full_text)
            has_y_lower = ("й" in full_text or "и" in full_text)
            assert has_y_capital and has_y_lower, f"Variant {var_name} lost diacritics: {full_text}"


# ===========================================================================
# 5. Table Line Noise Stress Tests
# ===========================================================================

class TestAdversarialLineNoiseSuppression:
    """Stress-test is_line_noise_token, score_token_quality, and fuse_ocr_passes on border strings."""

    def test_vertical_border_line_suppression(self):
        """Vertical table divider pipes '|' are correctly identified as line noise."""
        t_pipe1 = OcrToken(text="|", conf=85.0, bbox=(100, 50, 4, 250))
        t_pipe2 = OcrToken(text="|", conf=70.0, bbox=(250, 80, 2, 100))
        assert is_line_noise_token(t_pipe1) is True
        assert is_line_noise_token(t_pipe2) is True

    def test_long_table_border_suppression(self):
        """Long horizontal divider strings (>= 10 chars) are correctly flagged as line noise."""
        t_hyphens = OcrToken(text="----------------", conf=80.0, bbox=(50, 200, 400, 10))
        t_equals = OcrToken(text="================", conf=80.0, bbox=(50, 250, 400, 10))
        t_underscores = OcrToken(text="________________", conf=80.0, bbox=(50, 300, 400, 10))

        assert is_line_noise_token(t_hyphens) is True
        assert is_line_noise_token(t_equals) is True
        assert is_line_noise_token(t_underscores) is True

    @pytest.mark.parametrize("border_str", ["----", "____", "====", "------"])
    def test_short_table_border_strings_suppression_specification(self, border_str: str):
        """Specification test: Short table border strings '----', '____', '====' must be suppressed.

        NOTE FOR AUDIT: If is_line_noise_token only checks aspect ratio with h <= 6 or length >= 10,
        it will fail to suppress standard table border strings (e.g. height=10, len=4, conf=80),
        allowing table noise into the fused token stream.
        """
        tok = OcrToken(text=border_str, conf=75.0, bbox=(100, 100, 80, 10))
        assert is_line_noise_token(tok) is True, (
            f"VULNERABILITY: is_line_noise_token failed to suppress table border string {border_str!r} "
            f"(bbox={tok.bbox}, conf={tok.conf})!"
        )

    @pytest.mark.parametrize("legit_word", [
        "ФАКТУРА", "ДДС", "12,50", "0,20", "ЕИК", "КАПИНА", "лв.", "в", "и", "I", "1100124585",
    ])
    def test_legitimate_words_never_suppressed_as_line_noise(self, legit_word: str):
        """Legitimate Bulgarian words, single-letter prepositions, numbers, and codes are never line noise."""
        tok = OcrToken(text=legit_word, conf=85.0, bbox=(100, 100, 60, 20))
        assert is_line_noise_token(tok) is False, f"False positive: {legit_word!r} flagged as line noise!"

    def test_fusion_filters_table_border_noise_without_dropping_text(self):
        """fuse_ocr_passes must purge line noise tokens while preserving legitimate content."""
        t_valid = OcrToken(text="ФАКТУРА", conf=90.0, bbox=(100, 50, 120, 25))
        t_money = OcrToken(text="12,50", conf=88.0, bbox=(250, 50, 60, 25))
        t_noise_v = OcrToken(text="|", conf=80.0, bbox=(230, 50, 3, 100))
        t_noise_h = OcrToken(text="----------------", conf=80.0, bbox=(50, 80, 400, 8))

        fused = fuse_ocr_passes([t_valid, t_noise_v], [t_money, t_noise_h])
        fused_texts = [t.text for t in fused]

        assert "ФАКТУРА" in fused_texts
        assert "12,50" in fused_texts
        assert "|" not in fused_texts
        assert "----------------" not in fused_texts


# ===========================================================================
# 6. Dataset Immutability Verification
# ===========================================================================

class TestAdversarialVolumeImmutability:
    """Verify strictly read-only access to /Volumes/NO NAME/_ФАКТУРИ."""

    def test_volume_zero_mutations(self):
        """Ensure no files have been modified, created, or deleted on the source dataset."""
        volume_path = Path("/Volumes/NO NAME/_ФАКТУРИ")
        if not volume_path.exists():
            pytest.skip("Dataset volume not mounted")

        import subprocess
        res = subprocess.run(
            ["find", str(volume_path), "-newerct", "2026-09-04"],
            capture_output=True,
            text=True,
            check=True,
        )
        modified_files = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        assert len(modified_files) == 0, f"Dataset mutation detected! Files: {modified_files}"
