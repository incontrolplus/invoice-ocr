"""Tesseract OCR environment discovery, language validation, and readiness checks."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import pytesseract

from .constants import DEFAULT_OCR_LANG, KNOWN_TESSDATA_LOCATIONS

logger = logging.getLogger("invoice_ocr")

class TesseractLanguageMissingError(RuntimeError):
    """Raised when one or more required Tesseract language models are missing."""

    def __init__(self, missing_langs: Sequence[str], available_langs: Sequence[str]):
        self.missing_langs = list(missing_langs)
        self.available_langs = list(available_langs)
        instructions = format_language_install_instructions(self.missing_langs)
        msg = (
            f"Required Tesseract language model(s) missing: {', '.join(self.missing_langs)}.\n"
            f"Currently available languages: {', '.join(self.available_langs) if self.available_langs else 'none'}.\n"
            f"{instructions}"
        )
        super().__init__(msg)


def format_language_install_instructions(missing_langs: Sequence[str]) -> str:
    """Return user-friendly, multi-platform instructions for installing missing Tesseract languages."""
    langs_str = " ".join(f"tesseract-ocr-{lang}" for lang in missing_langs)
    fedora_str = " ".join(f"tesseract-langpack-{lang}" for lang in missing_langs)
    arch_str = " ".join(f"tesseract-data-{lang}" for lang in missing_langs)
    urls = [
        f"https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"
        for lang in missing_langs
    ]
    urls_str = "\n      ".join(urls)

    return (
        "To install the missing language pack(s):\n"
        "  • macOS (Homebrew):\n"
        "      brew install tesseract-lang\n"
        "  • Ubuntu / Debian:\n"
        f"      sudo apt-get update && sudo apt-get install -y {langs_str}\n"
        "  • Fedora / RHEL:\n"
        f"      sudo dnf install -y {fedora_str}\n"
        "  • Arch Linux:\n"
        f"      sudo pacman -S {arch_str}\n"
        "  • Project-local / Manual / Windows:\n"
        "      Download the traineddata file(s):\n"
        f"      {urls_str}\n"
        "      and place them into the project's 'tessdata/' folder, or point TESSDATA_PREFIX to their location."
    )


def setup_tessdata_prefix(custom_dir: Path | str | None = None) -> Path | None:
    """Discover and configure TESSDATA_PREFIX, prioritizing local project bundle and valid paths.

    Returns the Path configured in TESSDATA_PREFIX, or None if system defaults are used.
    """
    if custom_dir:
        p = Path(custom_dir).resolve()
        if p.is_dir():
            os.environ["TESSDATA_PREFIX"] = str(p)
            logger.debug("TESSDATA_PREFIX configured from custom_dir: %s", p)
            return p
        else:
            logger.warning("Specified custom tessdata dir does not exist: %s", custom_dir)

    # If TESSDATA_PREFIX is already set in environment and valid with bul.traineddata, keep it
    env_prefix = os.environ.get("TESSDATA_PREFIX")
    if env_prefix:
        env_p = Path(env_prefix).resolve()
        if env_p.is_dir() and (env_p / "bul.traineddata").exists():
            return env_p

    # Search known candidate directories
    for candidate in KNOWN_TESSDATA_LOCATIONS:
        if candidate.is_dir() and (candidate / "bul.traineddata").exists():
            os.environ["TESSDATA_PREFIX"] = str(candidate)
            logger.debug("TESSDATA_PREFIX automatically configured to: %s", candidate)
            return candidate

    return None


def get_installed_ocr_languages(config: str = "") -> list[str]:
    """Retrieve list of languages available to Tesseract."""
    try:
        return pytesseract.get_languages(config=config)
    except Exception as exc:
        logger.debug("pytesseract.get_languages() failed: %s", exc)
        return []


def verify_tesseract_languages(
    required_langs: Sequence[str] = ("bul", "eng"),
    config: str = "",
) -> tuple[bool, list[str], list[str]]:
    """Verify that required OCR language models are installed.

    Returns:
        (is_ready: bool, available_langs: list[str], missing_langs: list[str])
    """
    available = get_installed_ocr_languages(config=config)
    missing = [lang for lang in required_langs if lang not in available]
    return (len(missing) == 0, available, missing)


def resolve_effective_ocr_lang(
    requested_lang: str = DEFAULT_OCR_LANG,
) -> str:
    """Resolve the effective OCR language string based on installed language packs.

    If 'bul+eng' is requested:
      - Returns 'bul+eng' if both are available.
      - Falls back to 'bul' if 'eng' is missing (with a warning about Latin recognition).
      - Falls back to 'eng' if 'bul' is missing (with a warning about Cyrillic recognition).
      - Raises TesseractLanguageMissingError if neither is available.
    """
    available = get_installed_ocr_languages()
    components = [c.strip() for c in requested_lang.split("+") if c.strip()]
    missing = [c for c in components if c not in available]

    if not missing:
        return requested_lang

    if requested_lang == "bul+eng":
        if "bul" in available and "eng" not in available:
            logger.warning(
                "Language 'eng' is missing from Tesseract. Falling back to 'bul'. "
                "Warning: Latin identifiers (IBANs 'BG...', VAT 'BG...', BIC, EUR) "
                "may be misrecognized as Cyrillic. Run 'brew install tesseract-lang' "
                "or install eng.traineddata."
            )
            return "bul"
        elif "eng" in available and "bul" not in available:
            logger.warning(
                "Language 'bul' is missing from Tesseract. Falling back to 'eng'. "
                "Warning: Cyrillic Bulgarian text will not be recognized properly."
            )
            return "eng"

    if missing:
        raise TesseractLanguageMissingError(missing_langs=missing, available_langs=available)

    return requested_lang


def ensure_tesseract_ready(
    required_langs: Sequence[str] = ("bul", "eng"),
    custom_tessdata: Path | str | None = None,
) -> None:
    """Verify Tesseract binary and required language models are ready, raising clear errors on failure."""
    setup_tessdata_prefix(custom_tessdata)
    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError:
        raise RuntimeError(
            "Tesseract OCR executable is not installed or not on PATH.\n"
            "Please install Tesseract OCR:\n"
            "  • macOS: brew install tesseract tesseract-lang\n"
            "  • Ubuntu/Debian: sudo apt-get install -y tesseract-ocr tesseract-ocr-bul tesseract-ocr-eng\n"
            "  • Fedora: sudo dnf install -y tesseract tesseract-langpack-bul tesseract-langpack-eng\n"
            "  • Windows: Install from https://github.com/UB-Mannheim/tesseract/wiki"
        )
    ok, available, missing = verify_tesseract_languages(required_langs)
    if not ok:
        raise TesseractLanguageMissingError(missing_langs=missing, available_langs=available)


# Initialize TESSDATA_PREFIX discovery automatically on import
setup_tessdata_prefix()
