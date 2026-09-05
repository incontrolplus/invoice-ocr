import fitz
import re

kapina_paths = [
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf",
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf",
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf",
]

for p in kapina_paths:
    print("\n" + "=" * 80)
    print("LINE ITEMS INSPECTION:", p)
    print("=" * 80)
    doc = fitz.open(p)
    page = doc[0]
    # Extract blocks
    blocks = page.get_text("blocks")
    # Sort blocks by y0
    blocks.sort(key=lambda b: (b[1], b[0]))
    for b in blocks:
        text = b[4].strip()
        # Filter for blocks between y0=180 and y0=600
        if 180 <= b[1] <= 650:
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            for l in lines:
                print(f"y0={b[1]:5.1f}, x0={b[0]:5.1f} | {l}")
