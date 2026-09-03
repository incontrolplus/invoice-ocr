from pathlib import Path
import pypdfium2 as pdfium
from rapidocr_onnxruntime import RapidOCR

root = Path(__file__).resolve().parent
pdf_path = root / "samples" / "intermes.pdf"
png_path = root / "samples" / "intermes.png"
out_path = root / "output" / "intermes.txt"
out_path.parent.mkdir(exist_ok=True)

page = pdfium.PdfDocument(str(pdf_path))[0]
img = page.render(scale=2).to_pil()
img.save(png_path)

result, elapse = RapidOCR()(str(png_path))
lines = [x[1] for x in (result or [])]
text = "\n".join(lines)
out_path.write_text(text, encoding="utf-8")
print("READY")
print(text[:800])