import fitz

kapina_paths = [
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf",
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf",
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf",
]

for p in kapina_paths:
    print("=" * 80)
    print("PYMUPDF WORDS DUMP:", p)
    print("=" * 80)
    doc = fitz.open(p)
    page = doc[0]
    words = page.get_text("words")  # (x0, y0, x1, y1, word, block_no, line_no, word_no)
    # Filter words around the table area (y between 100 and 450 in PDF points)
    # Let's inspect page rect
    print("Page rect:", page.rect)
    for w in sorted(words, key=lambda x: (x[1], x[0])):
        if 100 <= w[1] <= 450:
            print(f"y0={w[1]:6.1f}, x0={w[0]:6.1f}, x1={w[2]:6.1f}, y1={w[3]:6.1f} | '{w[4]}'")
