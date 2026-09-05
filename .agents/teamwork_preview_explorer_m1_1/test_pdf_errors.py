#!/usr/bin/env python3
"""Investigate error handling for corrupted, password-protected, or invalid PDFs."""
import io
import fitz
import pytest

def test_missing_file():
    try:
        doc = fitz.open("/nonexistent/path/file.pdf")
        assert False, "Should have failed"
    except Exception as e:
        print(f"Missing file exception: {type(e).__name__}: {e}")

def test_empty_file(tmp_path="/tmp"):
    empty_pdf = f"{tmp_path}/empty_test.pdf"
    with open(empty_pdf, "wb") as f:
        pass
    try:
        doc = fitz.open(empty_pdf)
        assert False, "Should have failed"
    except Exception as e:
        print(f"Empty file exception: {type(e).__name__}: {e}")

def test_corrupted_file(tmp_path="/tmp"):
    corrupt_pdf = f"{tmp_path}/corrupt_test.pdf"
    with open(corrupt_pdf, "wb") as f:
        f.write(b"%PDF-1.4\ncorrupted content that is not a valid pdf at all\n%%EOF")
    try:
        doc = fitz.open(corrupt_pdf)
        # Some corrupted files open but fail on page access
        print(f"Corrupted file opened with pages: {len(doc)}")
        pix = doc[0].get_pixmap()
    except Exception as e:
        print(f"Corrupted file exception: {type(e).__name__}: {e}")

def test_encrypted_pdf(tmp_path="/tmp"):
    # Create an encrypted PDF in-memory / on-disk using PyMuPDF
    doc = fitz.open()
    doc.new_page()
    pw_pdf = f"{tmp_path}/password_protected.pdf"
    perm = fitz.PDF_PERM_ACCESSIBILITY  # some permission
    doc.save(pw_pdf, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret123", owner_pw="admin123")
    doc.close()

    # Now attempt to open and read without password
    doc2 = fitz.open(pw_pdf)
    print(f"Encrypted PDF opened. doc.is_encrypted = {doc2.is_encrypted}, doc.needs_pass = {doc2.needs_pass}")
    try:
        # Try accessing page without authenticating
        page = doc2[0]
        pix = page.get_pixmap()
        print(f"Accessed page without password? pix: {pix}")
    except Exception as e:
        print(f"Accessing encrypted page exception: {type(e).__name__}: {e}")

    # Try authentication with wrong password
    rc_wrong = doc2.authenticate("wrong_pass")
    print(f"Authentication with wrong password returned: {rc_wrong} (0 means failed)")

    # Try authentication with correct password
    rc_correct = doc2.authenticate("secret123")
    print(f"Authentication with correct password returned: {rc_correct} (>0 means success)")
    pix_ok = doc2[0].get_pixmap(dpi=150)
    print(f"Authenticated page rasterized successfully: {pix_ok.width}x{pix_ok.height}")
    doc2.close()

if __name__ == "__main__":
    print("--- Testing PDF Error Conditions ---")
    test_missing_file()
    test_empty_file()
    test_corrupted_file()
    test_encrypted_pdf()
