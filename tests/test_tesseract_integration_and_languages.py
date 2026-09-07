"""Tests for Tesseract OCR integration, language pack verification, and bul+eng standard.

Verifies:
1. Local tessdata directory and bul.traineddata / eng.traineddata discovery
2. Verification of installed languages via pytesseract.get_languages()
3. Error handling and user-friendly instructions when traineddata is missing
4. Combined bul+eng model for Bulgarian invoices containing Latin elements (IBAN, BIC, EUR, VAT)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import pytest

import invoice_ocr as iocr
from invoice_ocr import (
    DEFAULT_OCR_LANG,
    TesseractLanguageMissingError,
    ensure_tesseract_ready,
    execute_ocr_pass,
    format_language_install_instructions,
    get_installed_ocr_languages,
    resolve_effective_ocr_lang,
    run_multiple_ocr_passes,
    setup_tessdata_prefix,
    verify_tesseract_languages,
)


@pytest.fixture(autouse=True)
def reset_tessdata_environment():
    """Ensure every test begins and ends with project-local tessdata configured."""
    project_root = Path(iocr.__file__).resolve().parent
    project_tessdata = project_root / "tessdata"
    os.environ["TESSDATA_PREFIX"] = str(project_tessdata)
    yield
    os.environ["TESSDATA_PREFIX"] = str(project_tessdata)


def _get_font(size: int = 24) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Retrieve an available system font or default bitmap font."""
    font_candidates = [
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in font_candidates:
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size=size)
            except Exception:
                pass
    return ImageFont.load_default()


# ===========================================================================
# Point A: Local tessdata & Traineddata Bundling / Discovery
# ===========================================================================

class TestTessdataBundlingAndDiscovery:
    """Tests for local tessdata directory and discovery."""

    def test_local_tessdata_directory_exists(self):
        """Project root must provide a local tessdata directory with required models."""
        project_root = Path(iocr.__file__).resolve().parent
        tessdata_dir = project_root / "tessdata"
        assert tessdata_dir.is_dir(), f"Expected local tessdata directory at {tessdata_dir}"

        bul_file = tessdata_dir / "bul.traineddata"
        eng_file = tessdata_dir / "eng.traineddata"

        assert bul_file.is_file(), "bul.traineddata must exist in project tessdata/"
        assert bul_file.stat().st_size > 500_000, "bul.traineddata must not be empty or truncated"

        assert eng_file.is_file(), "eng.traineddata must exist in project tessdata/"
        assert eng_file.stat().st_size > 500_000, "eng.traineddata must not be empty or truncated"

    def test_setup_tessdata_prefix_discovers_local_dir(self):
        """setup_tessdata_prefix discovers project-local tessdata when env is unset."""
        with patch.dict(os.environ, {}, clear=True):
            resolved = setup_tessdata_prefix()
            assert resolved is not None
            assert (resolved / "bul.traineddata").exists()
            assert os.environ.get("TESSDATA_PREFIX") == str(resolved)

    def test_setup_tessdata_prefix_with_custom_valid_dir(self, tmp_path):
        """setup_tessdata_prefix sets custom valid directory."""
        dummy_dir = tmp_path / "custom_tessdata"
        dummy_dir.mkdir()
        (dummy_dir / "bul.traineddata").write_text("dummy")

        with patch.dict(os.environ, {}, clear=True):
            resolved = setup_tessdata_prefix(custom_dir=dummy_dir)
            assert resolved == dummy_dir.resolve()
            assert os.environ.get("TESSDATA_PREFIX") == str(dummy_dir.resolve())

    def test_setup_tessdata_prefix_with_invalid_custom_dir(self, tmp_path):
        """setup_tessdata_prefix falls back gracefully if custom_dir does not exist."""
        nonexistent = tmp_path / "does_not_exist"
        with patch.dict(os.environ, {}, clear=True):
            resolved = setup_tessdata_prefix(custom_dir=nonexistent)
            if resolved is not None:
                assert (resolved / "bul.traineddata").exists()


# ===========================================================================
# Point B: Language Verification via get_languages
# ===========================================================================

class TestLanguageVerification:
    """Tests for get_installed_ocr_languages and verify_tesseract_languages."""

    def test_get_installed_ocr_languages(self):
        """get_installed_ocr_languages returns a list containing at least bul and eng."""
        langs = get_installed_ocr_languages()
        assert isinstance(langs, list)
        assert "bul" in langs, f"'bul' must be installed; found: {langs}"
        assert "eng" in langs, f"'eng' must be installed; found: {langs}"

    def test_verify_tesseract_languages_success(self):
        """verify_tesseract_languages succeeds when required languages are present."""
        is_ready, available, missing = verify_tesseract_languages(["bul", "eng"])
        assert is_ready is True
        assert "bul" in available
        assert "eng" in available
        assert missing == []

    def test_verify_tesseract_languages_detects_missing(self):
        """verify_tesseract_languages detects missing language pack."""
        is_ready, available, missing = verify_tesseract_languages(["bul", "xyz_fake_lang"])
        assert is_ready is False
        assert "xyz_fake_lang" in missing

    def test_ensure_tesseract_ready_passes_on_valid_system(self):
        """ensure_tesseract_ready completes without error when environment is valid."""
        ensure_tesseract_ready(required_langs=["bul", "eng"])


# ===========================================================================
# Point C: Missing Language Error Handling & Actionable Instructions
# ===========================================================================

class TestMissingLanguageErrorHandling:
    """Tests for TesseractLanguageMissingError and user instructions."""

    def test_format_language_install_instructions_content(self):
        """format_language_install_instructions includes instructions for all major OS platforms."""
        instructions = format_language_install_instructions(["bul", "eng"])
        assert "brew install tesseract-lang" in instructions
        assert "apt-get install" in instructions
        assert "tesseract-ocr-bul" in instructions
        assert "tesseract-ocr-eng" in instructions
        assert "dnf install" in instructions
        assert "pacman -S" in instructions
        assert "https://github.com/tesseract-ocr/tessdata_fast" in instructions

    def test_ensure_tesseract_ready_raises_missing_error(self):
        """ensure_tesseract_ready raises TesseractLanguageMissingError when language is missing."""
        with pytest.raises(TesseractLanguageMissingError) as exc_info:
            ensure_tesseract_ready(required_langs=["bul", "nonexistent_lang_123"])

        err_msg = str(exc_info.value)
        assert "nonexistent_lang_123" in err_msg
        assert "brew install" in err_msg
        assert "apt-get install" in err_msg

    def test_execute_ocr_pass_missing_language_raises_custom_error(self):
        """execute_ocr_pass with missing language raises TesseractLanguageMissingError with guide."""
        dummy_img = np.zeros((100, 100), dtype=np.uint8)
        with pytest.raises(TesseractLanguageMissingError) as exc_info:
            execute_ocr_pass(dummy_img, lang="nonexistent_lang_xyz")

        err_msg = str(exc_info.value)
        assert "nonexistent_lang_xyz" in err_msg
        assert "brew install" in err_msg or "apt-get install" in err_msg

    def test_resolve_effective_ocr_lang_fallback_warning(self):
        """resolve_effective_ocr_lang warns and falls back if only one language is available."""
        with patch("invoice_ocr.get_installed_ocr_languages", return_value=["bul"]):
            resolved = resolve_effective_ocr_lang("bul+eng")
            assert resolved == "bul"

        with patch("invoice_ocr.get_installed_ocr_languages", return_value=["eng"]):
            resolved = resolve_effective_ocr_lang("bul+eng")
            assert resolved == "eng"

        with patch("invoice_ocr.get_installed_ocr_languages", return_value=[]):
            with pytest.raises(TesseractLanguageMissingError):
                resolve_effective_ocr_lang("bul+eng")


# ===========================================================================
# Point D: Combined bul+eng Standard & Latin Invoice Elements Recognition
# ===========================================================================

class TestCombinedBulEngModel:
    """Tests for DEFAULT_OCR_LANG and combined Cyrillic/Latin invoice recognition."""

    def test_default_ocr_lang_is_bul_plus_eng(self):
        """Default OCR language must be bul+eng."""
        assert DEFAULT_OCR_LANG == "bul+eng"

    def test_execute_ocr_pass_defaults_to_bul_plus_eng(self):
        """execute_ocr_pass without explicit lang uses DEFAULT_OCR_LANG ('bul+eng')."""
        img = Image.new("L", (400, 100), color=255)
        draw = ImageDraw.Draw(img)
        font = _get_font(24)
        draw.text((10, 30), "EUR 1250.00", fill=0, font=font)
        arr = np.array(img)

        tokens = execute_ocr_pass(arr)
        assert len(tokens) > 0
        texts = [t.text.upper() for t in tokens]
        assert any("EUR" in t or "1250" in t for t in texts)

    def test_bul_plus_eng_accurately_recognizes_latin_identifiers(self):
        """bul+eng accurately recognizes IBAN BG prefix, BIC, and EUR without Cyrillic corruption."""
        img = Image.new("L", (900, 250), color=255)
        draw = ImageDraw.Draw(img)
        font = _get_font(28)

        draw.text((20, 30), "ФАКТУРА № 0100000001", fill=0, font=font)
        draw.text((20, 80), "IBAN: BG80BNBG96611020345678", fill=0, font=font)
        draw.text((20, 130), "BIC: BNBGBGSF", fill=0, font=font)
        draw.text((20, 180), "Сума: 1250.00 EUR  ДДС: 250.00 EUR", fill=0, font=font)

        arr = np.array(img)

        tokens_bul_eng = execute_ocr_pass(arr, psm=3, lang="bul+eng")
        full_text = " ".join(t.text for t in tokens_bul_eng).upper()

        assert "BG" in full_text, f"Expected Latin 'BG' in tokens: {full_text}"
        assert "EUR" in full_text, f"Expected Latin 'EUR' in tokens: {full_text}"
        assert any(num in full_text for num in ("1250", "250", "0100000001")), f"Expected numeric totals in: {full_text}"

    def test_run_multiple_ocr_passes_with_default_lang(self):
        """run_multiple_ocr_passes operates with bul+eng by default."""
        img = Image.new("L", (600, 150), color=255)
        draw = ImageDraw.Draw(img)
        font = _get_font(24)
        draw.text((20, 30), "ДДС № BG207930830", fill=0, font=font)
        draw.text((20, 80), "ОБЩО: 100.00 BGN", fill=0, font=font)
        arr = np.array(img)

        variants = [("standard", arr)]
        tokens = run_multiple_ocr_passes(variants)
        assert len(tokens) > 0
        all_text = " ".join(t.text for t in tokens).upper()
        assert "BG" in all_text or "207930830" in all_text
