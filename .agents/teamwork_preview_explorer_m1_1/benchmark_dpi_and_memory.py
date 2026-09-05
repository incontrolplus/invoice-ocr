#!/usr/bin/env python3
"""Comprehensive benchmark for 300 DPI vs 400 DPI resolution, memory, and performance."""
import gc
import os
import resource
import time
import tracemalloc
import fitz
import numpy as np
import cv2

KAPINA_DIR = "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026"
METRO_DIR = "/Volumes/NO NAME/_ФАКТУРИ/01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025"

FILES = [
    os.path.join(KAPINA_DIR, "капина-01.pdf"),
    os.path.join(KAPINA_DIR, "капина-02.pdf"),
    os.path.join(KAPINA_DIR, "капина-03.pdf"),
    os.path.join(METRO_DIR, "метро-2.pdf"),
]

def get_peak_rss_mb() -> float:
    """Get peak resident memory usage in MB for macOS."""
    # On macOS, ru_maxrss is in bytes; on Linux, in KB.
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024)

def benchmark_file(filepath: str, dpi: int):
    filename = os.path.basename(filepath)
    file_size_mb = os.path.getsize(filepath) / (1024 * 1024)
    
    gc.collect()
    tracemalloc.start()
    t_start = time.perf_counter()
    
    doc = fitz.open(filepath)
    page_count = len(doc)
    page_metrics = []
    
    total_raster_time = 0.0
    total_convert_time = 0.0
    total_uncompressed_bytes = 0
    
    for page_num in range(page_count):
        page = doc[page_num]
        rect = page.rect
        
        t0 = time.perf_counter()
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
        t_raster = time.perf_counter() - t0
        total_raster_time += t_raster
        
        t1 = time.perf_counter()
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape((pix.height, pix.width, 3))
        bgr = cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
        t_convert = time.perf_counter() - t1
        total_convert_time += t_convert
        
        uncompressed_bytes = bgr.nbytes
        total_uncompressed_bytes += uncompressed_bytes
        
        page_metrics.append({
            "page": page_num + 1,
            "rect_pt": (rect.width, rect.height),
            "pixel_dims": (pix.width, pix.height),
            "uncompressed_mb": uncompressed_bytes / (1024 * 1024),
            "raster_ms": t_raster * 1000,
            "convert_ms": t_convert * 1000,
        })
        
        # Explicit cleanup per page
        del pix
        del arr
        del bgr
    
    doc.close()
    t_total = time.perf_counter() - t_start
    current_mem, peak_traced_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    gc.collect()
    
    return {
        "file": filename,
        "size_mb": file_size_mb,
        "pages": page_count,
        "dpi": dpi,
        "total_raster_ms": total_raster_time * 1000,
        "total_convert_ms": total_convert_time * 1000,
        "total_time_ms": t_total * 1000,
        "total_uncompressed_mb": total_uncompressed_bytes / (1024 * 1024),
        "peak_traced_mb": peak_traced_mem / (1024 * 1024),
        "page_metrics": page_metrics,
    }

def main():
    print("=" * 80)
    print("PyMuPDF Rasterization Benchmark: 300 DPI vs 400 DPI")
    print("=" * 80)
    
    for filepath in FILES:
        print(f"\n--- Testing File: {os.path.basename(filepath)} ---")
        for dpi in [300, 400]:
            res = benchmark_file(filepath, dpi)
            p = res["page_metrics"][0]
            print(f"[DPI {dpi}] Pages: {res['pages']} | "
                  f"Dims: {p['pixel_dims'][0]}x{p['pixel_dims'][1]} | "
                  f"Raster: {res['total_raster_ms']:.1f} ms | "
                  f"Convert: {res['total_convert_ms']:.1f} ms | "
                  f"Total: {res['total_time_ms']:.1f} ms | "
                  f"Raw Image: {res['total_uncompressed_mb']:.1f} MB | "
                  f"Peak Traced RAM: {res['peak_traced_mb']:.1f} MB")

if __name__ == "__main__":
    main()
