import fitz

doc = fitz.open("/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf")
page = doc[0]
blocks = page.get_text("blocks")

print("ALL BLOCKS IN КАПИНА-03:")
for b in sorted(blocks, key=lambda x: (x[1], x[0])):
    text = b[4].strip().replace('\n', ' // ')
    print(f"y0={b[1]:5.1f}, x0={b[0]:5.1f}, y1={b[3]:5.1f}, x1={b[2]:5.1f} | {text}")
