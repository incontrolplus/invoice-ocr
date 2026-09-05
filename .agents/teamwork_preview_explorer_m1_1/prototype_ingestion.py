#!/usr/bin/env python3
"""Prototype and verify unified ingestion module (PDF + Images) -> list[PageImage]."""
from dataclasses import dataclass
from pathlib import Path
import cv2
import fitz
import numpy as np

# Supported extensions
PDF_EXTENSIONS: set[str] = {".pdf"}
IMAGE_EXTENSIONS: set[str] = {".png", ".jpg", ".jpeg"}
SUPPORTED_EXTENSIONS: set[str] = PDF_EXTENSIONS | IMAGE_EXTENSIONS

@dataclass
class PageImage:
    page_number: int
    image: np.ndarray  # BGR image (OpenCV format)
    width: int
    height: int

def pixmap_to_bgr(pix: fitz.Pixmap) -> np.ndarray:
    """Convert a PyMuPDF Pixmap to a contiguous OpenCV BGR uint8 numpy array.
    
    Handles Grayscale (n=1), RGB (n=3), RGBA (n=4), and fallback CMYK.
    """
    if pix.n == 1:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width))
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
    elif pix.n == 3:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
    elif pix.n == 4:
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 4))
        return cv2.cvtColor(arr, cv2.COLOR_RGBA2BGR)
    else:
        # e.g., CMYK or non-standard
        rgb_pix = fitz.Pixmap(fitz.csRGB, pix)
        arr = np.frombuffer(rgb_pix.samples, dtype=np.uint8).reshape((rgb_pix.height, rgb_pix.width, 3))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

def rasterize_pdf(path: Path, dpi: int = 300) -> list[PageImage]:
    """Rasterize all pages of a PDF document at specified DPI using PyMuPDF."""
    try:
        doc = fitz.open(str(path))
    except (fitz.EmptyFileError, fitz.FileDataError) as e:
        raise ValueError(f"Corrupted or invalid PDF file: {path} ({e})") from e

    try:
        if doc.is_encrypted:
            if doc.needs_pass:
                raise ValueError(f"Password-protected PDF cannot be processed: {path}")

        total_pages = len(doc)
        if total_pages == 0:
            raise ValueError(f"PDF document contains zero pages: {path}")

        pages: list[PageImage] = []
        for idx in range(total_pages):
            page_num = idx + 1
            page = doc[idx]
            pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
            bgr = pixmap_to_bgr(pix)
            pages.append(PageImage(
                page_number=page_num,
                image=bgr,
                width=pix.width,
                height=pix.height,
            ))
            del pix
        return pages
    finally:
        doc.close()

def load_image_file(path: Path) -> PageImage:
    """Load a single image file (.png, .jpg, .jpeg) into a PageImage."""
    try:
        data = path.read_bytes()
    except Exception as e:
        raise ValueError(f"Failed to read image file: {path} ({e})") from e

    if len(data) == 0:
        raise ValueError(f"Empty image file: {path}")

    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError(f"Failed to decode image file: {path}")

    h, w = img.shape[:2]
    return PageImage(
        page_number=1,
        image=img,
        width=w,
        height=h,
    )

def load_document(path: Path | str, dpi: int = 300) -> list[PageImage]:
    """Unified entry point for loading any supported document format."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type: {path.suffix} (supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))})"
        )

    if suffix in PDF_EXTENSIONS:
        return rasterize_pdf(path, dpi=dpi)
    elif suffix in IMAGE_EXTENSIONS:
        return [load_image_file(path)]
    else:
        raise ValueError(f"Unsupported file format: {suffix}")

def run_verification():
    print("=== Verifying Prototype Ingestion on Primary Acceptance Files ===")
    kapina_files = [
        "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf",
        "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf",
        "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf",
    ]
    for kf in kapina_files:
        pages = load_document(kf, dpi=300)
        assert len(pages) == 1
        p = pages[0]
        assert p.page_number == 1
        assert p.width == 2481
        assert p.height == 3508
        assert p.image.shape == (3508, 2481, 3)
        assert p.image.flags["C_CONTIGUOUS"]
        print(f"PASS: {Path(kf).name} -> {len(pages)} page, shape={p.image.shape}, C_CONTIGUOUS={p.image.flags['C_CONTIGUOUS']}")

    print("\n=== Verifying Multi-Page Files ===")
    metro_file = "/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf"
    pages = load_document(metro_file, dpi=300)
    assert len(pages) == 3
    for i, p in enumerate(pages, 1):
        assert p.page_number == i
        assert p.width == 2481
        assert p.height == 3508
        print(f"PASS: Metro page {p.page_number}/3 -> shape={p.image.shape}")

    print("\n=== Verifying Image Ingestion ===")
    tmp_png = Path("/tmp/prototype_test.png")
    test_img = np.full((600, 800, 3), 200, dtype=np.uint8)
    cv2.imwrite(str(tmp_png), test_img)
    img_pages = load_document(tmp_png)
    assert len(img_pages) == 1
    assert img_pages[0].page_number == 1
    assert img_pages[0].width == 800
    assert img_pages[0].height == 600
    assert img_pages[0].image.shape == (600, 800, 3)
    print(f"PASS: Image -> {len(img_pages)} page, {img_pages[0].width}x{img_pages[0].height}")
    tmp_png.unlink()

    print("\n=== Verifying Error Handling ===")
    # Missing file
    try:
        load_document("/path/does/not/exist.pdf")
        assert False, "Should fail on missing file"
    except FileNotFoundError as e:
        print(f"PASS: Missing file -> {type(e).__name__}")

    # Unsupported format
    fake_docx = Path("/tmp/fake.docx")
    fake_docx.write_text("dummy")
    try:
        load_document(fake_docx)
        assert False, "Should fail on unsupported extension"
    except ValueError as e:
        print(f"PASS: Unsupported extension -> {e}")
    finally:
        fake_docx.unlink(missing_ok=True)


    # Corrupted PDF
    corrupt_pdf = Path("/tmp/corrupt.pdf")
    corrupt_pdf.write_bytes(b"%PDF-corrupt garbage header and no xref table")
    try:
        load_document(corrupt_pdf)
        assert False, "Should fail on corrupt PDF"
    except ValueError as e:
        print(f"PASS: Corrupted PDF -> {e}")
    corrupt_pdf.unlink()

    # Empty PDF
    empty_pdf = Path("/tmp/empty.pdf")
    empty_pdf.write_bytes(b"")
    try:
        load_document(empty_pdf)
        assert False, "Should fail on empty PDF"
    except ValueError as e:
        print(f"PASS: Empty PDF -> {e}")
    empty_pdf.unlink()

    # Password-protected PDF
    pw_pdf = Path("/tmp/protected.pdf")
    doc = fitz.open()
    doc.new_page()
    doc.save(str(pw_pdf), encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="pwd123", owner_pw="adm123")
    doc.close()
    try:
        load_document(pw_pdf)
        assert False, "Should fail on protected PDF"
    except ValueError as e:
        print(f"PASS: Password-protected PDF -> {e}")
    pw_pdf.unlink()

    print("\nALL INGESTION PROTOTYPE TESTS PASSED!")

if __name__ == "__main__":
    run_verification()
