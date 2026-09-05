import fitz

for fname in ["капина-01.pdf", "капина-02.pdf"]:
    p = f"/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/{fname}"
    print("\n" + "=" * 80)
    print("LINE ITEMS INSPECTION:", fname)
    print("=" * 80)
    doc = fitz.open(p)
    page = doc[0]
    blocks = page.get_text("blocks")
    blocks.sort(key=lambda b: (b[1], b[0]))
    for b in blocks:
        text = b[4].strip()
        if 180 <= b[1] <= 550:
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            for l in lines:
                print(f"y0={b[1]:5.1f}, x0={b[0]:5.1f} | {l}")
