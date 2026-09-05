#!/usr/bin/env python3
"""PyMuPDF capability and API inspection script."""
import sys
import fitz

print(f"PyMuPDF Version: {fitz.__version__}")
print(f"PyMuPDF Version tuple: {fitz.version}")
print(f"MuPDF Version: {fitz.mupdf_version}")

# Check get_pixmap parameters
doc = fitz.open()
page = doc.new_page(width=595.276, height=841.890)

# Check if dpi parameter is supported directly in get_pixmap
try:
    pix_dpi = page.get_pixmap(dpi=300)
    print(f"page.get_pixmap(dpi=300) SUPPORTED! Pixmap size: {pix_dpi.width}x{pix_dpi.height}")
except Exception as e:
    print(f"page.get_pixmap(dpi=300) failed: {e}")

# Check Matrix zoom factor
zoom_300 = 300 / 72.0
mat_300 = fitz.Matrix(zoom_300, zoom_300)
pix_mat = page.get_pixmap(matrix=mat_300)
print(f"page.get_pixmap(matrix=300/72) size: {pix_mat.width}x{pix_mat.height}")

# Check colorspaces and alpha options
pix_no_alpha = page.get_pixmap(dpi=300, alpha=False)
print(f"alpha=False pixmap: n={pix_no_alpha.n}, alpha={pix_no_alpha.alpha}, stride={pix_no_alpha.stride}")

pix_alpha = page.get_pixmap(dpi=300, alpha=True)
print(f"alpha=True pixmap: n={pix_alpha.n}, alpha={pix_alpha.alpha}, stride={pix_alpha.stride}")

pix_gray = page.get_pixmap(dpi=300, colorspace=fitz.csGRAY)
print(f"csGRAY pixmap: n={pix_gray.n}, alpha={pix_gray.alpha}, stride={pix_gray.stride}")

doc.close()
