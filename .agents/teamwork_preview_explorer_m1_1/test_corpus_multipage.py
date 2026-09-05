#!/usr/bin/env python3
"""Verify multi-page iteration and memory behavior across multi-page corpus PDFs."""
import os
import fitz
import numpy as np
import cv2
import tracemalloc

METRO_2 = "/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро-2.pdf"
METRO_3 = "/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf"

def test_multipage_file(filepath: str, dpi: int = 300):
    filename = os.path.basename(filepath)
    print(f"\nTesting Multi-Page PDF: {filename} (Path: {filepath})")
    
    tracemalloc.start()
    doc = fitz.open(filepath)
    total_pages = len(doc)
    print(f"Total Pages: {total_pages}")
    
    pages_data = []
    for idx, page in enumerate(doc):
        page_num = idx + 1
        rect = page.rect
        
        # Pixmap generation
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
        w, h = pix.width, pix.height
        
        # NumPy BGR conversion
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((h, w, 3))
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        
        pages_data.append({
            "page_number": page_num,
            "rect_pt": (rect.width, rect.height),
            "width_px": w,
            "height_px": h,
            "bgr_shape": bgr.shape,
            "bgr_nbytes": bgr.nbytes,
        })
        
        current, peak = tracemalloc.get_traced_memory()
        print(f"  Page {page_num}/{total_pages}: Rect=({rect.width:.1f}x{rect.height:.1f} pt) -> "
              f"Pixels=({w}x{h}) | Shape={bgr.shape} | Current RAM: {current/(1024*1024):.1f} MB | Peak: {peak/(1024*1024):.1f} MB")
        
        del pix
        del arr
        # Note: keep bgr if simulating pipeline list[PageImage]
        
    doc.close()
    tracemalloc.stop()
    return pages_data

if __name__ == "__main__":
    test_multipage_file(METRO_2, dpi=300)
    test_multipage_file(METRO_3, dpi=300)
