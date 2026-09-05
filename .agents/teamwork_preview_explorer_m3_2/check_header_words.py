import fitz

kapina_paths = [
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf",
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf",
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf",
]

for p in kapina_paths:
    print("=" * 70)
    print("HEADER WORDS:", p)
    print("=" * 70)
    doc = fitz.open(p)
    page = doc[0]
    words = page.get_text("words")
    for w in sorted(words, key=lambda x: (x[1], x[0])):
        if 130 <= w[1] <= 220:
            print(f"y0={w[1]:5.1f}, x0={w[0]:5.1f}, x1={w[2]:5.1f}, y1={w[3]:5.1f} | '{w[4]}'")
