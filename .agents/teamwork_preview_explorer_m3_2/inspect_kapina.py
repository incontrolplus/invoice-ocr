import fitz
import os
import sys

kapina_paths = [
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf",
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf",
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf",
]

for p in kapina_paths:
    print("=" * 60)
    print(f"Inspecting: {p}")
    if not os.path.exists(p):
        print("FILE DOES NOT EXIST!")
        continue
    doc = fitz.open(p)
    print(f"Page count: {len(doc)}")
    for i, page in enumerate(doc):
        text = page.get_text()
        print(f"--- Page {i+1} embedded text length: {len(text.strip())} chars ---")
        if text.strip():
            print("First 500 chars of embedded text:")
            print(text[:500])
        else:
            print("No embedded digital text (scanned image).")
            images = page.get_images()
            print(f"Embedded images count: {len(images)}")
