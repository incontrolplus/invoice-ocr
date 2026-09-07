"""Document ingestion and rasterization for PDF and image formats."""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Generator, Iterator

import cv2
import numpy as np
from PIL import Image

try:
    import pymupdf
    fitz = pymupdf
except ImportError:
    import fitz as pymupdf
    fitz = pymupdf

from .constants import (
    DEFAULT_RASTER_DPI,
    IMAGE_EXTENSIONS,
    PDF_EXTENSIONS,
    SUPPORTED_EXTENSIONS,
)
from .models import PageImage

logger = logging.getLogger("invoice_ocr")

def pixmap_to_bgr(pix: pymupdf.Pixmap) -> np.ndarray:
    """Convert a PyMuPDF Pixmap to a contiguous OpenCV BGR uint8 array.

    Handles Grayscale (n=1), RGB (n=3), RGBA (n=4), and CMYK fallbacks.
    """
    if pix.colorspace and pix.colorspace.name == "DeviceCMYK":
        rgb_pix = fitz.Pixmap(fitz.csRGB, pix)
        arr = np.frombuffer(rgb_pix.samples, dtype=np.uint8).reshape((rgb_pix.height, rgb_pix.width, 3))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    elif pix.n == 1:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width))
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    elif pix.n == 3:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    elif pix.n == 4:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 4))
        return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
    else:
        rgb_pix = fitz.Pixmap(fitz.csRGB, pix)
        arr = np.frombuffer(rgb_pix.samples, dtype=np.uint8).reshape((rgb_pix.height, rgb_pix.width, 3))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)


def _stream_pdf_pages(
    doc: Any,
    path: Path,
    dpi: int,
    total_pages: int,
) -> Generator[PageImage, None, None]:
    """Generator yielding PageImage instances one-by-one from an open PyMuPDF document."""
    try:
        logger.info("Streaming rasterization of PDF: %s (%d page(s) at %d DPI)", path.name, total_pages, dpi)
        for idx in range(total_pages):
            try:
                page = doc[idx]
                pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
                bgr = pixmap_to_bgr(pix)
                w, h = pix.width, pix.height
                del pix
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(f"Failed to rasterize PDF document: {path} ({exc})") from exc

            yield PageImage(
                page_number=idx + 1,
                image=bgr,
                width=w,
                height=h,
            )
    finally:
        doc.close()


def iter_rasterize_pdf(
    path: Path | str,
    dpi: int = DEFAULT_RASTER_DPI,
) -> Generator[PageImage, None, None]:
    """Rasterize pages of a PDF one by one as a generator to prevent OOM on large documents.

    Yields:
        PageImage: One page at a time with image: np.ndarray, width, height.

    Raises:
        ValueError: If DPI <= 0, or PDF is empty, corrupted, or encrypted.
    """
    path = Path(path)
    if dpi <= 0:
        raise ValueError(f"Invalid rasterization DPI: {dpi}. Must be a positive integer.")

    try:
        doc = pymupdf.open(str(path))
    except (pymupdf.EmptyFileError, pymupdf.FileDataError) as exc:
        raise ValueError(f"Failed to open PDF document: {path} ({exc})") from exc
    except Exception as exc:
        raise ValueError(f"Failed to open PDF document: {path} ({exc})") from exc

    if doc.is_encrypted and doc.needs_pass:
        doc.close()
        raise ValueError(f"Encrypted or password-protected PDF is not supported: {path}")

    total_pages = len(doc)
    if total_pages == 0:
        doc.close()
        raise ValueError(f"PDF document contains 0 pages: {path}")

    return _stream_pdf_pages(doc, path, dpi, total_pages)


def rasterize_pdf(path: Path | str, dpi: int = DEFAULT_RASTER_DPI) -> list[PageImage]:
    """Rasterize all pages of a PDF into a list of PageImage objects.

    Raises:
        ValueError: If PDF is corrupted, empty, or password-protected.
    """
    return list(iter_rasterize_pdf(path, dpi=dpi))


def load_image_page(path: Path | str) -> PageImage:
    """Load a single image file (.png, .jpg, .jpeg) into a PageImage using imdecode for Cyrillic path safety."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except Exception as exc:
        raise ValueError(f"Failed to read image file: {path} ({exc})") from exc

    if len(data) == 0:
        raise ValueError(f"Failed to decode image file (empty): {path}")

    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Failed to decode image file: {path}")

    h, w = img.shape[:2]
    logger.info("Loaded image: %s (%dx%d)", path.name, w, h)
    return PageImage(
        page_number=1,
        image=img,
        width=w,
        height=h,
    )


def iter_document(
    path: Path | str,
    dpi: int = DEFAULT_RASTER_DPI,
) -> Generator[PageImage, None, None]:
    """Unified document streaming generator supporting PDF, PNG, JPG, and JPEG.

    Yields PageImage objects one-by-one to ensure O(1) memory overhead.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file format is unsupported or corrupted.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Document file not found: {path}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file format '{suffix}' for file: {path}. "
            f"Supported formats: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    if suffix in PDF_EXTENSIONS:
        return iter_rasterize_pdf(path, dpi=dpi)
    elif suffix in IMAGE_EXTENSIONS:
        def _single_page():
            yield load_image_page(path)
        return _single_page()
    else:
        raise ValueError(f"Unsupported file format: {suffix}")


def load_document(path: Path | str, dpi: int = DEFAULT_RASTER_DPI) -> list[PageImage]:
    """Unified document loader supporting PDF, PNG, JPG, and JPEG.

    Returns a list of PageImage objects with uniform BGR arrays and page numbers.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file format is unsupported or corrupted.
    """
    return list(iter_document(path, dpi=dpi))


def load_image(path: Path | str) -> np.ndarray:
    """Backwards-compatible wrapper returning the first page image as np.ndarray."""
    for page in iter_document(path):
        if page.image is not None:
            return page.image
    raise ValueError(f"No pages found in document: {path}")

