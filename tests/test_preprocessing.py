"""Unit and adversarial tests for Milestone 2: Adaptive Preprocessing.

Covers:
- Feature 6: Orientation detection (OSD) and 90/180/270° normalization.
- Feature 7: Contour-based deskewing, angle clamping, white border filling, and PageTransform.
- Feature 8: Contrast enhancement with CLAHE on CIELAB L* luminance channel.
- Feature 9: Cyrillic-safe bilateral denoising, Otsu binarization, Bulgarian diacritic preservation,
  decimal comma preservation, and non-destructive preprocessing variants.
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
    PageImage,
    PageTransform,
    apply_deskew,
    apply_orientation,
    binarize_otsu,
    check_and_fix_orientation,
    denoise_bilateral,
    deskew_image,
    detect_deskew_angle,
    detect_orientation,
    enhance_contrast,
    enhance_contrast_clahe,
    generate_preprocessing_variants,
    morphological_cleanup,
    normalize_page_geometry,
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


def _create_synthetic_document_image(
    text_lines: list[str] | None = None,
    width: int = 1200,
    height: int = 1200,
    font_size: int = 24,
) -> np.ndarray:
    """Create a high-resolution synthetic document page with horizontal text lines."""
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = _get_font(font_size)

    if text_lines is None:
        text_lines = [
            f"ФАКТУРА ОРИГИНАЛ ДДС ДОСТАВЧИК ПОЛУЧАТЕЛ 110012458{i} СТОЙНОСТ 12,50 ЛВ ДАТА 28.04.2026"
            for i in range(25)
        ]

    for idx, line in enumerate(text_lines):
        y = 60 + idx * 42
        if y + font_size < height:
            draw.text((100, y), line, fill=(0, 0, 0), font=font)

    return np.array(img)


# ===========================================================================
# 1. Feature 6: Orientation Detection & Normalization Tests
# ===========================================================================

class TestOrientationDetection:
    """Tests for detect_orientation, apply_orientation, and check_and_fix_orientation."""

    def test_detect_orientation_upright(self):
        """Upright text page should return rotation 0."""
        doc = _create_synthetic_document_image()
        rot = detect_orientation(doc)
        assert rot == 0

    def test_apply_orientation_rotations(self):
        """apply_orientation should rotate images by 90, 180, and 270 degrees."""
        img = np.zeros((100, 200, 3), dtype=np.uint8)
        # Mark top-left pixel
        img[0, 0] = [10, 20, 30]

        # 90° Clockwise: shape becomes (200, 100, 3), top-left moves to top-right
        rot90 = apply_orientation(img, 90)
        assert rot90.shape == (200, 100, 3)
        assert np.array_equal(rot90[0, 99], [10, 20, 30])

        # 180°: shape stays (100, 200, 3), top-left moves to bottom-right
        rot180 = apply_orientation(img, 180)
        assert rot180.shape == (100, 200, 3)
        assert np.array_equal(rot180[99, 199], [10, 20, 30])

        # 270° (90° CCW): shape becomes (200, 100, 3), top-left moves to bottom-left
        rot270 = apply_orientation(img, 270)
        assert rot270.shape == (200, 100, 3)
        assert np.array_equal(rot270[199, 0], [10, 20, 30])

        # 0° or unsupported: unchanged
        noop = apply_orientation(img, 0)
        assert np.array_equal(noop, img)

    def test_detect_and_fix_orientation_end_to_end(self):
        """Tesseract OSD should detect rotated text and check_and_fix_orientation restore it."""
        doc = _create_synthetic_document_image()

        # Rotate 90° CW
        rot90 = apply_orientation(doc, 90)
        detected_rot = detect_orientation(rot90)
        # OSD returns the degrees to rotate clockwise to restore upright (270)
        assert detected_rot == 270

        # check_and_fix_orientation restores to upright
        restored = check_and_fix_orientation(rot90)
        assert detect_orientation(restored) == 0

    def test_detect_orientation_blank_and_noise_graceful(self):
        """Blank or pure noise image should return 0 without raising exceptions."""
        blank = np.full((500, 500, 3), 255, dtype=np.uint8)
        assert detect_orientation(blank) == 0

        noise = np.random.randint(0, 256, (300, 300, 3), dtype=np.uint8)
        assert detect_orientation(noise) == 0

        empty = np.array([])
        assert detect_orientation(empty) == 0

    def test_detect_orientation_min_conf_threshold(self):
        """Rotation should be rejected if confidence is below min_conf."""
        doc = _create_synthetic_document_image()
        rot90 = apply_orientation(doc, 90)
        # Setting min_conf unrealistically high (e.g. 1000.0) must reject the rotation
        assert detect_orientation(rot90, min_conf=1000.0) == 0


# ===========================================================================
# 2. Feature 7: Contour-Based Deskewing Tests
# ===========================================================================

class TestContourDeskewing:
    """Tests for detect_deskew_angle, apply_deskew, deskew_image, and normalize_page_geometry."""

    def test_detect_deskew_angle_upright(self):
        """Upright document should return 0.0 skew."""
        doc = _create_synthetic_document_image()
        angle = detect_deskew_angle(doc)
        assert abs(angle) < 0.2

    def test_detect_and_correct_positive_skew(self):
        """Detect and correct intentional counter-clockwise tilt (+4.0°)."""
        doc = _create_synthetic_document_image()
        h, w = doc.shape[:2]
        center = (w / 2.0, h / 2.0)

        # Warp by +4.0° CCW
        M = cv2.getRotationMatrix2D(center, 4.0, 1.0)
        skewed = cv2.warpAffine(doc, M, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))

        det_angle = detect_deskew_angle(skewed)
        # Should detect ~ -4.0° to restore upright
        assert abs(abs(det_angle) - 4.0) < 0.8

        deskewed = deskew_image(skewed)
        residual = detect_deskew_angle(deskewed)
        assert abs(residual) < 0.2

    def test_detect_and_correct_negative_skew(self):
        """Detect and correct intentional clockwise tilt (-5.0°)."""
        doc = _create_synthetic_document_image()
        h, w = doc.shape[:2]
        center = (w / 2.0, h / 2.0)

        # Warp by -5.0° CW
        M = cv2.getRotationMatrix2D(center, -5.0, 1.0)
        skewed = cv2.warpAffine(doc, M, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))

        det_angle = detect_deskew_angle(skewed)
        # Should detect ~ +5.0° to restore upright
        assert abs(abs(det_angle) - 5.0) < 0.8

        deskewed = deskew_image(skewed)
        residual = detect_deskew_angle(deskewed)
        assert abs(residual) < 0.2

    def test_deskew_angle_sub_threshold_ignored(self):
        """Angles below min_angle (0.2°) should return 0.0 to prevent interpolation blur."""
        doc = _create_synthetic_document_image()
        h, w = doc.shape[:2]
        center = (w / 2.0, h / 2.0)

        # Warp by tiny 0.1°
        M = cv2.getRotationMatrix2D(center, 0.1, 1.0)
        skewed = cv2.warpAffine(doc, M, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))

        angle = detect_deskew_angle(skewed, min_angle=0.2)
        assert angle == 0.0

    def test_deskew_angle_extreme_clamped(self):
        """Angles greater than max_angle (15.0°) should be rejected (returns 0.0)."""
        doc = _create_synthetic_document_image()
        h, w = doc.shape[:2]
        center = (w / 2.0, h / 2.0)

        # Warp by large 30.0°
        M = cv2.getRotationMatrix2D(center, 30.0, 1.0)
        skewed = cv2.warpAffine(doc, M, (w, h), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))

        angle = detect_deskew_angle(skewed, max_angle=15.0)
        assert angle == 0.0

    def test_deskew_image_white_border_constant(self):
        """Deskewing must use white border value (255, 255, 255) rather than border replication."""
        doc = np.full((400, 400, 3), 255, dtype=np.uint8)
        # Put black horizontal line
        doc[190:210, 50:350] = 0

        # Rotate by 5 degrees with white border
        deskewed, M, M_inv = apply_deskew(doc, 5.0)
        assert deskewed.shape == doc.shape
        # Corners should be pristine white
        assert np.array_equal(deskewed[0, 0], [255, 255, 255])
        assert np.array_equal(deskewed[0, 399], [255, 255, 255])
        assert np.array_equal(deskewed[399, 0], [255, 255, 255])
        assert np.array_equal(deskewed[399, 399], [255, 255, 255])

    def test_normalize_page_geometry(self):
        """normalize_page_geometry should perform 2-stage normalization and produce PageTransform."""
        doc = _create_synthetic_document_image()
        h, w = doc.shape[:2]

        # Rotate by 90° CW
        rot90 = apply_orientation(doc, 90)
        page = PageImage(page_number=1, image=rot90, width=rot90.shape[1], height=rot90.shape[0])

        norm_page, transform = normalize_page_geometry(page)
        assert isinstance(transform, PageTransform)
        assert transform.orientation_rotate_deg == 270
        assert norm_page.width == w
        assert norm_page.height == h
        assert norm_page.image.shape == doc.shape


# ===========================================================================
# 3. Feature 8: Contrast Enhancement with CLAHE Tests
# ===========================================================================

class TestContrastEnhancementCLAHE:
    """Tests for enhance_contrast_clahe and enhance_contrast."""

    def test_enhance_contrast_bgr_luminance_channel(self):
        """CLAHE on BGR image must operate on CIELAB L* channel and preserve BGR shape/dtype."""
        bgr = np.random.randint(100, 160, (300, 300, 3), dtype=np.uint8)
        enhanced = enhance_contrast_clahe(bgr, clip_limit=2.0, tile_grid_size=(8, 8))

        assert enhanced.shape == bgr.shape
        assert enhanced.dtype == np.uint8
        # Standard deviation should increase due to contrast stretching
        assert np.std(enhanced) > np.std(bgr)

    def test_enhance_contrast_grayscale(self):
        """CLAHE on 1-channel grayscale image."""
        gray = np.random.randint(100, 160, (300, 300), dtype=np.uint8)
        enhanced = enhance_contrast_clahe(gray, clip_limit=2.0)

        assert enhanced.shape == gray.shape
        assert enhanced.dtype == np.uint8
        assert np.std(enhanced) > np.std(gray)

    def test_enhance_contrast_bgra(self):
        """CLAHE on 4-channel BGRA image converts to BGR without crashing."""
        bgra = np.full((200, 200, 4), 128, dtype=np.uint8)
        enhanced = enhance_contrast_clahe(bgra)
        assert len(enhanced.shape) == 3
        assert enhanced.shape[2] == 3

    def test_enhance_contrast_wrapper(self):
        """enhance_contrast() backward compatible alias."""
        gray = np.random.randint(50, 200, (100, 100), dtype=np.uint8)
        res = enhance_contrast(gray)
        assert res.shape == gray.shape


# ===========================================================================
# 4. Feature 9: Cyrillic-Safe Denoising & Binarization Tests
# ===========================================================================

class TestDenoisingAndBinarization:
    """Tests for denoise_bilateral, binarize_otsu, and Cyrillic/currency preservation."""

    def test_denoise_bilateral_edge_preservation(self):
        """Bilateral filter should smooth flat texture while preserving sharp edges."""
        # Create an image with a sharp vertical step edge (left=50, right=200) + noise
        img = np.zeros((100, 100), dtype=np.uint8)
        img[:, :50] = 50
        img[:, 50:] = 200
        noise = np.random.normal(0, 5, (100, 100)).astype(np.int16)
        noisy = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)

        denoised = denoise_bilateral(noisy, d=5, sigma_color=50.0, sigma_space=50.0)
        assert denoised.shape == noisy.shape

        # The step across edge (column 48 to 52) must remain sharp
        diff_noisy = float(noisy[:, 55].mean() - noisy[:, 45].mean())
        diff_denoised = float(denoised[:, 55].mean() - denoised[:, 45].mean())
        assert abs(diff_denoised - diff_noisy) < 5.0

    def test_binarize_otsu_values(self):
        """Otsu binarization must produce strict binary values {0, 255}."""
        img = np.random.randint(0, 256, (100, 100), dtype=np.uint8)
        binary = binarize_otsu(img)
        unique_vals = set(np.unique(binary))
        assert unique_vals.issubset({0, 255})

    def test_diacritic_preservation_й_Й_i(self):
        """Bulgarian Cyrillic diacritics ('й', 'Й', 'i') must not be erased or corrupted."""
        # Render clean text patch with diacritics
        img = Image.new("L", (800, 150), color=255)
        draw = ImageDraw.Draw(img)
        font = _get_font(32)
        draw.text((20, 30), "Йордан, Ивайло и Йовка", fill=0, font=font)
        arr = np.array(img)

        # Preprocess with bilateral filter and Otsu
        denoised = denoise_bilateral(arr, d=5, sigma_color=50.0, sigma_space=50.0)
        binary = binarize_otsu(denoised)

        # Verify dark pixel connectivity: diacritic breve above 'Й' and 'й' must have black pixels
        # Breve sits above the main character body (y between 30 and 45)
        breve_region = binary[30:50, 20:60]
        assert np.any(breve_region == 0), "Breve diacritic above Й was erased!"

    def test_decimal_comma_preservation_12_50(self):
        """Decimal comma in '12,50' must NOT be mutated into period '.' or erased by morphology."""
        img = Image.new("L", (600, 100), color=255)
        draw = ImageDraw.Draw(img)
        font = _get_font(32)
        draw.text((20, 20), "Стойност: 12,50 лв.", fill=0, font=font)
        arr = np.array(img)

        # Baseline Otsu
        binary_base = binarize_otsu(arr)

        # Verify morphological_cleanup is a safe no-op that does NOT erode the comma
        morphed = morphological_cleanup(binary_base)
        assert np.array_equal(morphed, binary_base)

    def test_generate_preprocessing_variants(self):
        """generate_preprocessing_variants produces complementary variants without morphology bug."""
        doc = _create_synthetic_document_image(width=400, height=400)
        variants = generate_preprocessing_variants(doc)

        variant_names = [name for name, _ in variants]
        assert "minimal" in variant_names
        assert "clahe_gray" in variant_names or "standard" in variant_names
        assert "enhanced_otsu" in variant_names

        for name, var_img in variants:
            assert isinstance(var_img, np.ndarray)
            assert var_img.size > 0


# ===========================================================================
# 5. Adversarial Robustness Tests
# ===========================================================================

class TestAdversarialPreprocessingRobustness:
    """Robustness against extreme, corrupted, and degenerate inputs."""

    def test_empty_and_zero_size_images(self):
        """Empty array should not cause unhandled crashes."""
        empty = np.zeros((0, 0), dtype=np.uint8)
        assert detect_orientation(empty) == 0
        assert detect_deskew_angle(empty) == 0.0
        assert deskew_image(empty).size == 0
        assert enhance_contrast_clahe(empty).size == 0
        assert denoise_bilateral(empty).size == 0
        assert binarize_otsu(empty).size == 0

    def test_all_black_image(self):
        """All-zero (black) image should not trigger division-by-zero."""
        black = np.zeros((300, 300, 3), dtype=np.uint8)
        assert detect_orientation(black) == 0
        assert detect_deskew_angle(black) == 0.0
        deskewed = deskew_image(black)
        assert deskewed.shape == black.shape
        enhanced = enhance_contrast_clahe(black)
        assert enhanced.shape == black.shape
        binary = binarize_otsu(black)
        assert binary.shape == (300, 300)

    def test_all_white_image(self):
        """All-white image should not trigger exceptions."""
        white = np.full((300, 300, 3), 255, dtype=np.uint8)
        assert detect_orientation(white) == 0
        assert detect_deskew_angle(white) == 0.0
        deskewed = deskew_image(white)
        assert deskewed.shape == white.shape
        enhanced = enhance_contrast_clahe(white)
        assert enhanced.shape == white.shape

    def test_random_noise_image(self):
        """Random noise should gracefully produce 0 rotation/skew."""
        noise = np.random.randint(0, 256, (400, 400, 3), dtype=np.uint8)
        assert detect_orientation(noise) == 0
        assert detect_deskew_angle(noise) == 0.0
        enhanced = enhance_contrast_clahe(noise)
        assert enhanced.shape == noise.shape

    def test_extreme_aspect_ratios(self):
        """Extreme aspect ratios (very tall or very wide) should not crash."""
        tall = np.full((4000, 100, 3), 255, dtype=np.uint8)
        wide = np.full((100, 4000, 3), 255, dtype=np.uint8)

        assert detect_deskew_angle(tall) == 0.0
        assert detect_deskew_angle(wide) == 0.0
        assert deskew_image(tall).shape == tall.shape
        assert deskew_image(wide).shape == wide.shape
