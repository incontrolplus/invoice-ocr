import fitz
import re
from decimal import Decimal

def inspect_kapina(pdf_path):
    doc = fitz.open(pdf_path)
    page = doc[0]
    blocks = page.get_text("blocks")
    blocks.sort(key=lambda b: (b[1], b[0]))
    
    print(f"\n{'='*70}\nFILE: {pdf_path}\n{'='*70}")
    
    # Let's inspect words in the table region (y between 180 and 520)
    words = page.get_text("words")
    # Group words by line_no or y proximity
    # In PyMuPDF: word is (x0, y0, x1, y1, text, block_no, line_no, word_no)
    table_words = [w for w in words if 190 <= w[1] <= 510]
    
    # Let's group words into lines by y-coord
    lines = []
    curr_line = []
    curr_y = None
    for w in sorted(table_words, key=lambda x: (x[1], x[0])):
        if curr_y is None or abs(w[1] - curr_y) < 7:
            curr_line.append(w)
            if curr_y is None:
                curr_y = w[1]
        else:
            curr_line.sort(key=lambda x: x[0])
            lines.append(curr_line)
            curr_line = [w]
            curr_y = w[1]
    if curr_line:
        curr_line.sort(key=lambda x: x[0])
        lines.append(curr_line)
        
    print(f"Constructed {len(lines)} visual lines in table area:")
    for idx, l in enumerate(lines):
        txt = " ".join(w[4] for w in l)
        y_avg = sum(w[1] for w in l) / len(l)
        print(f"Line {idx:2d} (y={y_avg:5.1f}): {txt}")

for p in [
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf",
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf",
    "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf",
]:
    inspect_kapina(p)
