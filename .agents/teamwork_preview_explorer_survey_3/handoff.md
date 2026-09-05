# Acceptance Test Dataset & Corpus Survey Report (Survey 3)

**Author**: Survey Agent 3 (`teamwork_preview_explorer`)  
**Target File**: `/Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_survey_3/handoff.md`  
**Date**: 2026-09-04T21:21:00Z  
**Subject**: In-depth analysis of 3 primary acceptance invoices (`капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`) and comprehensive survey of the broader corpus `/Volumes/NO NAME/_ФАКТУРИ`.

---

## 1. Observation

### 1.1 Technical Specifications of Primary Acceptance Files

All three primary acceptance files reside in `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/`:
1. `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-01.pdf` (Size: 7,516,207 bytes)
2. `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-02.pdf` (Size: 8,148,645 bytes)
3. `/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/капина-03.pdf` (Size: 7,218,503 bytes)

Inspection via PyMuPDF (`fitz`), macOS Spotlight metadata (`mdls`), and OpenCV revealed identical technical container structures across all three files:

```
MediaBox / Rect:  Rect(0.0, 0.0, 595.276, 841.890) -> Standard ISO A4 (210.00 x 297.00 mm)
Page count:       1 page
PDF Version:      PDF 1.4
Creator:          NAPS2 (Not Another PDF Scanner 2)
Producer:         PDFsharp 1.50.4000-netstandard (https://github.com/ststeiger/PdfSharpCore)
Creation Date:    D:20260831235501+03'00' (scanned on 2026-08-31)
Embedded Image:   Exactly 1 image per file (xref 7)
Image Format:     PNG, 8 bits per component (bpc=8), colorspace=3 (24-bit RGB)
Image Dimensions: 9920 x 14030 pixels
Calculated DPI:   1199.85 x 1199.87 DPI (~1200 DPI scan)
Rotation:         0 degrees
```

### 1.2 Digital Text Streams vs. Scanned Raster Reality

- **Underlying Source**: Purely scanned optical images of physical paper invoices printed by ERP software (*Microinvest Склад Pro*).
- **Embedded Text Stream**: Yes, `page.get_text()` returns text blocks in all three PDFs, linked to font `Times New Roman` (`Identity-H` encoding).
- **Verbatim Text Layer Extraction**:
  - `капина-01.pdf`: Text length 1,668 chars. Examples of raw text tokens extracted: `'Номер1100124585'`, `'Дата28.04.2026'`, `'КАПИНА п ООД'`, `'ва14500333'`, `'| .КЕТЧУП БУЛМЕД 0 900гр |'`.
  - `капина-02.pdf`: Text length 2,035 chars. Examples: `'.„*Ш'`, `'1432 0608217 *ХАМБУРГСКИ САЛАМ КАРИАНА'`, `'КА[Ш(АВАЛ БАЙ ВЪЛЧАН ТОСТЕВ'`.
  - `капина-03.pdf`: Text length 2,239 chars. Examples: `'В(3207930830'`, `'”КНП ;д ; яОВЦ'`, `'ВЧУ/ЕВ/23СВЕДВТААНЕЧВАООИАРОРВВАНА/ЕВ'`.
- **Finding**: This embedded text is **not** original born-digital vector text from the ERP system. It is a secondary, low-quality invisible OCR overlay injected by NAPS2 upon scanning. It suffers from OCR hallucinations, fused tokens, missing whitespace, and substituted Cyrillic glyphs.

### 1.3 Visual Layout, Skew, Quality, Contrast, and Scanner Artifacts

- **Skew Angle Detection**:
  Evaluated on rendered page previews using OpenCV Canny edge detection and Probabilistic Hough Line Transform (`cv2.HoughLinesP`) on all horizontal table rules:
  - `капина-01.pdf`: 195 lines detected, **median skew = 0.0000°**, mean = 0.0373°
  - `капина-02.pdf`: 225 lines detected, **median skew = 0.0000°**, mean = -0.0116°
  - `капина-03.pdf`: 235 lines detected, **median skew = 0.0000°**, mean = -0.0153°
  - *Observation*: The physical documents were aligned with precision against the scanner glass; physical skew is negligible (< 0.05°).
- **Contrast & Dynamic Range**:
  Evaluated on 8-bit grayscale rendered representations:
  - `капина-01.pdf`: Mean = 238.57, Std = 52.11; 5th percentile (ink) = 86, 95th percentile (paper) = 255; Contrast ratio = 169.
  - `капина-02.pdf`: Mean = 234.55, Std = 58.13; 5th percentile (ink) = 53, 95th percentile (paper) = 255; Contrast ratio = 202.
  - `капина-03.pdf`: Mean = 236.43, Std = 55.48; 5th percentile (ink) = 66, 95th percentile (paper) = 255; Contrast ratio = 189.
  - *Observation*: Sharp, high-contrast, laser-printed black text on white stock.
- **Physical Artifacts and Anomalies**:
  - `капина-01.pdf`: Tiny scanner dust dots in the top margin (`y < 50 px`); a single curved pen stroke artifact near the right border of the supplier box (`МОЛ: ВЕСЕЛИН ВЪРБАНОВ`).
  - `капина-02.pdf`: 20 table line items, creating high vertical density; minor scanner smudge/fingerprint near the right edge of the supplier box.
  - `капина-03.pdf` (**CRITICAL DEFECT/OBSTACLE**): A physical thermal paper fiscal cash slip (**ФИСКАЛЕН БОН**) is stapled or placed over the upper-right section of the invoice!
    - The fiscal slip directly occludes the right half of the `Доставчик` details box (`ДДС номер`, `Град`, `Адрес`, `МОЛ`, `Телефон`).
    - The fiscal slip completely covers columns 5, 6, and 7 (`Кол.`, `Цена`, `ДДС %`) and partially covers column 4 (`Мярка`) and column 8 (`Стойност`) for table rows 1 through 12.
    - Rows 13 through 17 are located below the bottom edge of the fiscal slip and remain 100% visible.
    - The fiscal slip contains its own text, 2D QR code, fiscal memory serials (`DT972726`, `02972726`), cryptographic signature, and redundant summary amounts (`СТОЙНОСТ ПО ФАКТУРА 1 х 147.83 = 147.83 Б`, `ОБЩА СУМА ЕВРО 147.83`, `ОБЩА СУМА ЛВ 289.13`, `ОБМЕНЕН КУРС 1 ЕВРО = 1.95583 ЛВ`).

### 1.4 Ground-Truth Document Content Audit

#### Common Parties & Banking Details Across All 3 Files:
- **Recipient (`Получател`)**: `ФАСТ ТОП ФУУДС ЕООД`
  - EIK: `207930830` (9 digits)
  - VAT Number: `BG207930830`
  - Address: `гр. ПЛЕВЕН, ул. "Чаталджа"4`
  - MOL (`МОЛ`): `НИКОЛАЙ ЕНЧЕВ`
- **Supplier (`Доставчик`)**: `КАПИНА 71 ООД`
  - EIK: `114500333` (9 digits)
  - VAT Number: `BG114500333`
  - Address: `гр. ПЛЕВЕН, ул.ГРЕНАДИРСКА 40` (Fiscal slip in file 03 also lists warehouse: `гр. ПЛЕВЕН, ул. "ГЕОРГИ КОЧЕВ" 171`)
  - MOL (`МОЛ`): `ВЕСЕЛИН ВЪРБАНОВ`
  - Phone: `064/804 218`
- **Banking / Payment Details**:
  - Bank: `ЮРОБАНК БЪЛГАРИЯ АД`
  - BIC: `BPBIBGSF`
  - IBAN: `BG13 BPBI 8170 1060 0913 01` (22 chars, valid Bulgarian IBAN format)
  - Payment Method: `Плащане в брой` (Cash)
  - ERP System: `Microinvest Склад Pro` (`http://www.microinvest.net`)
  - QR Code: Bottom left with label `QR код за автоматично разплащане и осчетоводяване`

---

#### Detailed Data Matrix for the 3 Primary Acceptance Files:

| Field | капина-01.pdf | капина-02.pdf | капина-03.pdf |
| :--- | :--- | :--- | :--- |
| **Invoice Number** | `1100124585` | `1100123568` | `1100124013` |
| **Issue Date** | `28.04.2026` | `17.04.2026` | `22.04.2026` |
| **Tax Event Date** | `28.04.2026` | `17.04.2026` | `22.04.2026` |
| **Place of Issue** | `ПЛЕВЕН` | `ПЛЕВЕН` | `ПЛЕВЕН` |
| **Line Items Count** | 14 items | 20 items | 17 items (12 occluded by slip, 5 full) |
| **Table Unit Prices / Values** | Printed in **EUR (€)** | Printed in **EUR (€)** | Printed in **EUR (€)** |
| **Sum of Line Item Values** | `82.37 €` | `101.43 €` | `123.18 €` (sum of 17 line values) |
| **Tax Base (BGN / EUR)** | `161.12 лв.` / `82.38 €` | `198.34 лв.` / `101.42 €` | `240.92 лв.` / `123.17 €` |
| **VAT Rate** | `20%` | `20%` | `20%` |
| **VAT Amount (BGN / EUR)** | `32.23 лв.` / `16.48 €` | `39.66 лв.` / `20.28 €` | `48.21 лв.` / `24.65 €` |
| **Total Due (BGN / EUR)** | `193.35 лв.` / `98.86 €` | `238.00 лв.` / `121.69 €` | `289.13 лв.` / `147.83 €` |
| **Amount in Words** | *Деветдесет и осем евро и 86 е.ц.* | *Сто двадесет и едно евро и 69 е.ц.* | *Сто четиридесет и седем евро и 83 е.ц.* |
| **Amount in Words Currency** | EUR (euro & euro-cents) | EUR (euro & euro-cents) | EUR (euro & euro-cents) |
| **Exchange Rate Applied** | `1 EUR = 1.95583 BGN` | `1 EUR = 1.95583 BGN` | `1 EUR = 1.95583 BGN` |
| **Special Features** | Clean single page | 20 items (max page capacity) | Physical fiscal slip occluding table |

---

### 1.5 Detailed Itemization of Line Items

#### капина-01.pdf (14 items):
1. Code `62206` | `ДОБРУДЖАНСКА НАДЕНИЦА` | Unit: `кг` | Qty: `5.000` | Price: `3.08` | VAT: `20.00` | Total: `15.42`
2. Code `060821` | `ХАМБУРГСКИ САЛАМ КАРИАНА` | Unit: `кб` | Qty: `1.615` | Price: `3.13` | VAT: `20.00` | Total: `5.05`
3. Code `040419` | `КРЕНВИРШ "ДЕЛИКАТЕС 2"ООД` | Unit: `кг` | Qty: `1.153` | Price: `4.00` | VAT: `20.00` | Total: `4.61`
4. Code `040450` | `БЛАНШ. КАРТОФИ Стекхаусевро 2.5` | Unit: `к9` | Qty: `17.500` | Price: `1.66` | VAT: `20.00` | Total: `29.02`
5. Code `65964` | `ОЛИО 3л` | Unit: `бр.` | Qty: `2.000` | Price: `4.17` | VAT: `20.00` | Total: `8.33`
6. Code `63811` | `ВЕДА КИСЕЛ ПРОДУКТ 0.700л` | Unit: `бр.` | Qty: `2.000` | Price: `0.37` | VAT: `20.00` | Total: `0.73`
7. Code `66165` | `АЙРЯН СИТОВО 250 мл` | Unit: `бр.` | Qty: `12.000` | Price: `0.34` | VAT: `20.00` | Total: `4.10`
8. Code `66166` | `АЙРЯН СИТОВО 500 мл` | Unit: `бр.` | Qty: `12.000` | Price: `0.48` | VAT: `20.00` | Total: `5.70`
9. Code `61371` | `КУХНЕНСКА РОЛКА JUMBO` | Unit: `бр.` | Qty: `2.000` | Price: `1.17` | VAT: `20.00` | Total: `2.33`
10. Code `010418` | `шарена сол ЕЛИС` | Unit: `бр.` | Qty: `10.000` | Price: `0.13` | VAT: `20.00` | Total: `1.33`
11. Code `62113` | `КЕТЧУП БУЛМЕД 0.900гр` | Unit: `бр.` | Qty: `1.000` | Price: `1.00` | VAT: `20.00` | Total: `1.00`
12. Code `62114` | `МАЙОНЕЗА БУЛМЕД 0.870гр` | Unit: `бр.` | Qty: `1.000` | Price: `1.42` | VAT: `20.00` | Total: `1.42`
13. Code `010503` | `ЧИЛИ СОС` | Unit: `бр.` | Qty: `1.000` | Price: `1.25` | VAT: `20.00` | Total: `1.25`
14. Code `64006` | `ЕНЕРГИЙНА НАПИТКА ЧЕРНА МЕЧКА` | Unit: `бр.` | Qty: `5.000` | Price: `0.42` | VAT: `20.00` | Total: `2.08`

#### капина-02.pdf (20 items):
1. Code `62206` | `ДОБРУДЖАНСКА НАДЕНИЦА` | Unit: `кг` | Qty: `4.645` | Price: `3.08` | VAT: `20.00` | Total: `14.32`
2. Code `060821` | `ХАМБУРГСКИ САЛАМ КАРИАНА` | Unit: `кб` | Qty: `3.200` | Price: `3.13` | VAT: `20.00` | Total: `10.00`
3. Code `040419` | `КРЕНВИРШ "ДЕЛИКАТЕС 2"ООД` | Unit: `кг` | Qty: `1.074` | Price: `4.00` | VAT: `20.00` | Total: `4.30`
4. Code `61922` | `СРЪБСКА НАДЕНИЦА БОНИ` | Unit: `кг` | Qty: `1.352` | Price: `6.83` | VAT: `20.00` | Total: `9.24`
5. Code `63618` | `КАШКАВАЛ БАЙ ВЪЛЧАН ТОСТЕР` | Unit: `кг` | Qty: `1.970` | Price: `7.79` | VAT: `20.00` | Total: `15.35`
6. Code `040450` | `БЛАНШ. КАРТОФИ Стекхаусевро 2.5` | Unit: `к9` | Qty: `5.000` | Price: `1.66` | VAT: `20.00` | Total: `8.29`
7. Code `63811` | `ВЕДА КИСЕЛ ПРОДУКТ 0.700л` | Unit: `бр.` | Qty: `1.000` | Price: `0.37` | VAT: `20.00` | Total: `0.37`
8. Code `65964` | `ОЛИО 3л` | Unit: `бр.` | Qty: `1.000` | Price: `4.00` | VAT: `20.00` | Total: `4.00`
9. Code `010178` | `МАЙОНЕЗА КРАСИ` | Unit: `бр.` | Qty: `1.000` | Price: `0.83` | VAT: `20.00` | Total: `0.83`
10. Code `64006` | `ЕНЕРГИЙНА НАПИТКА ЧЕРНА МЕЧКА` | Unit: `бр.` | Qty: `5.000` | Price: `0.42` | VAT: `20.00` | Total: `2.08`
11. Code `65923` | `ПС СУХ СПИРТ 48брХ26` | Unit: `бр.` | Qty: `4.000` | Price: `0.73` | VAT: `20.00` | Total: `2.90`
12. Code `61371` | `КУХНЕНСКА РОЛКА JUMBO` | Unit: `бр.` | Qty: `3.000` | Price: `1.17` | VAT: `20.00` | Total: `3.50`
13. Code `64005` | `ЕНЕРГИЙНА НАПИТКА ХЕЛЛ КЛАСИК` | Unit: `бр.` | Qty: `24.000` | Price: `0.43` | VAT: `20.00` | Total: `10.40`
14. Code `62086` | `ИЗОСПОРТ С КАПАЧКА 14 БР` | Unit: `кут` | Qty: `1.000` | Price: `5.42` | VAT: `20.00` | Total: `5.42`
15. Code `62113` | `КЕТЧУП БУЛМЕД 0.900гр` | Unit: `бр.` | Qty: `1.000` | Price: `1.00` | VAT: `20.00` | Total: `1.00`
16. Code `62114` | `МАЙОНЕЗА БУЛМЕД 0.870гр` | Unit: `бр.` | Qty: `1.000` | Price: `1.42` | VAT: `20.00` | Total: `1.42`
17. Code `010503` | `ЧИЛИ СОС` | Unit: `бр.` | Qty: `1.000` | Price: `1.25` | VAT: `20.00` | Total: `1.25`
18. Code `62282` | `САЛФЕТКИ БЕЛИ 4*500/4*4.5` | Unit: `ст` | Qty: `0.250` | Price: `10.67` | VAT: `20.00` | Total: `2.67`
19. Code `66165` | `АЙРЯН СИТОВО 250 мл` | Unit: `бр.` | Qty: `5.000` | Price: `0.34` | VAT: `20.00` | Total: `1.71`
20. Code `66166` | `АЙРЯН СИТОВО 500 мл` | Unit: `бр.` | Qty: `5.000` | Price: `0.48` | VAT: `20.00` | Total: `2.38`

#### капина-03.pdf (17 items):
- Rows 1-12: Description and Line Total Value visible; Qty, Unit Price, and VAT% occluded by fiscal slip:
  1. Code `62206` | `ДОБРУДЖАНСКА НАДЕНИЦА` | Total: `14.49`
  2. Code `060821` | `ХАМБУРГСКИ САЛАМ КАРИАНА` | Total: `9.98`
  3. Code `040419` | `КРЕНВИРШ "ДЕЛИКАТЕС 2"ООД` | Total: `4.24`
  4. Code `61922` | `СРЪБСКА НАДЕНИЦА БОНИ` | Total: `9.21`
  5. Code `63618` | `КАШКАВАЛ БАЙ ВЪЛЧАН ТОСТЕР` | Total: `15.65`
  6. Code `62086` | `ИЗОСПОРТ С КАПАЧКА 14 БР` | Total: `5.42`
  7. Code `64005` | `ЕНЕРГИЙНА НАПИТКА ХЕЛЛ КЛАСИК` | Total: `10.40`
  8. Code `040450` | `БЛАНШ. КАРТОФИ Стекхаусевро 2.5` | Total: `24.87`
  9. Code `64006` | `ЕНЕРГИЙНА НАПИТКА ЧЕРНА МЕЧКА` | Total: `2.08`
  10. Code `65964` | `ОЛИО 3л` | Total: `4.17`
  11. Code `63811` | `ВЕДА КИСЕЛ ПРОДУКТ 0.700л` | Total: `0.73`
  12. Code `010418` | `шарена сол ЕЛИС` | Total: `1.33`
- Rows 13-17: All columns fully visible:
  13. Code `060729` | `КИСЕЛО МЛЯКО БОР ЧВОР 4.5%` | Unit: `бр.` | Qty: `2.000` | Price: `0.50` | VAT: `20.00` | Total: `1.01`
  14. Code `010503` | `ЧИЛИ СОС` | Unit: `бр.` | Qty: `1.000` | Price: `1.25` | VAT: `20.00` | Total: `1.25`
  15. Code `66166` | `АЙРЯН СИТОВО 500 мл` | Unit: `бр.` | Qty: `6.000` | Price: `0.48` | VAT: `20.00` | Total: `2.85`
  16. Code `61371` | `КУХНЕНСКА РОЛКА JUMBO` | Unit: `бр.` | Qty: `1.000` | Price: `1.17` | VAT: `20.00` | Total: `1.17`
  17. Code `040166` | `БЯЛО САЛАМУРЕНО ГЕРИ 8кг` | Unit: `кг` | Qty: `8.000` | Price: `1.79` | VAT: `20.00` | Total: `14.33`

---

### 1.6 Survey of the Broader Dataset (`/Volumes/NO NAME/_ФАКТУРИ`)

A recursive scan of `/Volumes/NO NAME/_ФАКТУРИ` yielded the following structural catalog:
- **Total Files**: 23 files
- **File Format Breakdown**: 23 files are `.pdf` (100%). No loose `.png` or `.jpg` files exist in the directory.
- **Scanner Provenance**: 100% of all 23 files have PDF metadata `Creator: 'NAPS2'` and contain 1 scanned raster image per page at ~1200 DPI.
- **Page Distribution**:
  - 21 single-page documents (1 page each)
  - 1 two-page document: `01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро-2.pdf`
  - 1 three-page document: `01_МЕТРО_БЪЛГАРИЯ_ООД/Метро 2025/метро.pdf`

#### Supplier Directory Breakdown:
1. `01_МЕТРО_БЪЛГАРИЯ_ООД` (2 files, 5 pages total)
   - `Метро 2025/метро-2.pdf` (2 pages, 15.2 MB)
   - `Метро 2025/метро.pdf` (3 pages, 16.2 MB)
   - Multi-page Metro Cash & Carry invoices with extremely dense multi-item tables, excise duties, discounts, and custom table headers.
2. `02_КАПИНА_71_ООД` (4 files)
   - `Капина 2025/капина.pdf` (1 page, 7.4 MB) — BGN only, recipient `ГМ2025 ЕООД`.
   - `Капина 2026/капина-01.pdf` (1 page, 7.5 MB) — Primary acceptance file 1.
   - `Капина 2026/капина-02.pdf` (1 page, 8.1 MB) — Primary acceptance file 2.
   - `Капина 2026/капина-03.pdf` (1 page, 7.2 MB) — Primary acceptance file 3.
3. `03_ЕЛИКО_143_ЕООД` (1 file)
   - `Елико 2025/елико.pdf` (1 page, 5.0 MB)
4. `04_ВАЛБОРГЕН_ООД` (1 file)
   - `валборген.pdf` (1 page, 4.3 MB, directly in supplier folder without year subfolder)
5. `05_АРПАК_ЕООД` (2 files)
   - `Арпак 2025/теменужка-01.pdf` (1 page, 4.4 MB)
   - `Арпак 2025/теменужка-02.pdf` (1 page, 4.6 MB)
6. `06_ЕТ_ТЕМЕНУЖКА_КОЧЕВА_НАДЯ` (0 files)
   - Directory is empty; its invoices are stored under `05_АРПАК_ЕООД`.
7. `07_О_СКАРИ_ООД` (2 files)
   - `Оскари 2026/оскари-01.pdf` (1 page, 8.0 MB)
   - `Оскари 2026/оскари-02.pdf` (1 page, 5.9 MB)
8. `08_AНДА_2012_АНКО_ПЕТРОВ_ЕООД` (4 files)
   - `Анда 2026/анда-01.pdf` (1 page, 1.4 MB)
   - `Анда 2026/анда-02.pdf` (1 page, 5.4 MB)
   - `Анда 2026/анда-03.pdf` (1 page, 6.1 MB)
   - `Анда 2026/анда-04.pdf` (1 page, 4.4 MB)
9. `09_НЕНДВ_ООД` (1 file)
   - `НендВ 2026/нендв.pdf` (1 page, 4.1 MB)
10. `10_ГРЕСТОКОМЕРС_ЕООД` (3 files)
    - `Грестокомерс 2026/грестокомерс-01.pdf` (1 page, 6.3 MB)
    - `Грестокомерс 2026/грестокомерс-02.pdf` (1 page, 6.6 MB)
    - `Грестокомерс 2026/грестокомерс-03.pdf` (1 page, 6.7 MB)
11. `11_ЕКСПРЕС_СЕКЮРИТИ_СОД_ЕООД` (1 file)
    - `Експрес 2026/експрес.pdf` (1 page, 5.2 MB) — Service invoice from security company.
12. `12_ИНТЕРМЕС_ООД` (2 files)
    - `Интермес 2026/интермес-01.pdf` (1 page, 1.2 MB)
    - `Интермес 2026/интермес-02.pdf` (1 page, 3.7 MB)

---

## 2. Logic Chain

1. **Premise from Observation 1.1**: The acceptance files are 1200 DPI scanned images encapsulated in PDF 1.4 by NAPS2. An uncompressed 9920x14030 24-bit image requires ~417 MB of RAM. Directly rasterizing and executing OCR at 1200 DPI will cause significant memory pressure, long processing times, and potential OOM errors during batch execution.
2. **Inference 1**: In accordance with Requirement R1, rendering pages via PyMuPDF (`fitz`) at **300 DPI** (or 400 DPI) produces images of ~2480 x 3508 pixels (~26 MB uncompressed). This is standard for OCR and preserves 100% of the character stroke definition while running ~15x faster than 1200 DPI.
3. **Premise from Observation 1.2**: The embedded PDF text layer created by NAPS2 contains severe OCR noise, fused strings (`'Номер1100124585'`), and corrupted characters (`'ва14500333'`).
4. **Inference 2**: The pipeline cannot rely on `page.get_text()` as ground truth. Ingestion must follow Requirement R2: rasterize the PDF page to an image, apply adaptive preprocessing (contrast enhancement, binarization), and execute multi-pass Tesseract OCR with `bul` language pack under PSM 3 and PSM 11.
5. **Premise from Observation 1.3 & 1.4**: In `капина-01.pdf`, `капина-02.pdf`, and `капина-03.pdf`, the line item prices and line values in the table are printed in **EURO (€)**, whereas the financial totals summary prints **both BGN and EUR**. Furthermore, the amount in words (`Словом`) is expressed in Euro.
6. **Inference 3**:
   - The extraction layer must support **dual currency** and must associate each numeric amount with its explicit currency code (`{ "amount": ..., "currency": "BGN" | "EUR" }`).
   - Line items must be assigned `currency: "EUR"`.
   - The validation engine must verify:
     - `sum(line_items.total_price_net)` against `financial_summary.tax_base_eur` (within 0.02 tolerance).
     - `financial_summary.tax_base_bgn` against `tax_base_eur * 1.95583` (within 0.02 tolerance).
     - `tax_base + vat_amount == total_amount_due` in both currencies.
   - For 2026 invoices, Bulgarian euro transition rules (R5) apply: BGN amounts must not be automatically overwritten with EUR, but cross-currency consistency must be validated.
7. **Premise from Observation 1.3 (File 03)**: `капина-03.pdf` has an attached physical fiscal slip covering columns 5-7 for lines 1-12 and covering parts of the supplier box.
8. **Inference 4**:
   - The layout analysis engine (R3) must use bounding boxes to segment distinct document zones and detect overlapping receipt boundaries.
   - For lines 1-12, where quantity and unit price are physically occluded, the table parser must NOT invent synthetic placeholder numbers or names (per R3: "Never substitute synthetic fallback descriptions... assign null and record a validation warning").
   - Total line value (`Стойност`) is visible for all 17 lines, allowing the sum of line values (`123.18 €`) to be computed and verified against the tax base (`123.17 €`).
   - The fiscal slip text must be isolated from the main invoice table to prevent foreign fiscal receipt lines (e.g., `СТОЙНОСТ ПО ФАКТУРА 1 х 147.83`) from corrupting the invoice line item table.

---

## 3. Caveats

1. **Non-Kapina Invoice Formats**: Only the Kapina 2026 invoices and sample headers of other suppliers were visually inspected in detail. Other suppliers (e.g. Metro multi-page invoices with 5 pages across 2 files, Anda, Grestokomers) employ different ERP templates, fonts, column structures, and multi-page continuation layouts.
2. **PyMuPDF in `.venv`**: Currently, `fitz` is installed in `/opt/homebrew/bin/python3` but **missing in `.venv`**. `.venv` has `cv2`, `PIL`, `pytesseract`, `numpy`. Installing `pymupdf` in `.venv` is a prerequisite for downstream implementation.
3. **Strict Read-Only Verification**: All inspections performed in this survey were executed strictly read-only. No files or directories in `/Volumes/NO NAME/_ФАКТУРИ` were touched, altered, moved, or created.

---

## 4. Conclusion

1. **Acceptance Dataset Readiness**: The 3 primary acceptance files (`капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf`) represent real-world Bulgarian invoices exhibiting critical edge cases:
   - High-resolution 1200 DPI scans requiring downsampling to 300 DPI for practical execution.
   - Flawed scanner-injected OCR text layers requiring genuine optical raster re-processing.
   - Dual-currency Euro transition accounting (table in EUR, summary in BGN & EUR).
   - Physical document occlusion by an attached thermal cash slip in `капина-03.pdf`.
2. **Corpus Scope**: The broader corpus consists of **23 PDF documents across 12 supplier directories**, featuring both 2025 (BGN-only) and 2026 (Euro-transition) invoices, as well as multi-page documents (Metro 2025).
3. **Implementation Guidance**:
   - Upgrade `invoice_ocr.py` to support `.pdf` ingestion via PyMuPDF at 300 DPI.
   - Handle dual currency parsing (`amount`, `currency`) on all monetary fields.
   - Handle partial line item occlusion gracefully with `null` fields and validation warnings.
   - Maintain spatial zoning to prevent attached fiscal slips from contaminating invoice table rows.

---

## 5. Verification Method

To independently verify the observations, measurements, and findings in this report:

1. **Verify Integrity of Source Directory (Zero Modifications)**:
   ```bash
   ls -la "/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026"
   # Verify modification timestamps are untouched (Aug 31 23:5x)
   ```

2. **Verify PDF Container & Image Dimensions**:
   ```bash
   /opt/homebrew/bin/python3 -c "
   import fitz
   for f in ['капина-01.pdf', 'капина-02.pdf', 'капина-03.pdf']:
       doc = fitz.open(f'/Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/{f}')
       img = doc.extract_image(doc[0].get_images()[0][0])
       print(f, 'Pages:', len(doc), 'Rect:', doc[0].rect, 'Image:', img['width'], 'x', img['height'], 'DPI:', img['width']/(doc[0].rect.width/72))
   "
   ```

3. **Verify Table Line Items Sum & Dual Currency Matching**:
   ```bash
   /opt/homebrew/bin/python3 -c "
   from decimal import Decimal
   # Check Kapina-01: 14 items sum = 82.37 EUR, Tax Base EUR = 82.38 EUR, BGN = 161.12 лв
   # Check Kapina-02: 20 items sum = 101.43 EUR, Tax Base EUR = 101.42 EUR, BGN = 198.34 лв
   # Check Kapina-03: 17 visible items sum = 123.18 EUR, Tax Base EUR = 123.17 EUR, BGN = 240.92 лв
   rate = Decimal('1.95583')
   print('F1 BGN calc:', (Decimal('82.38') * rate).quantize(Decimal('0.01')), 'printed: 161.12')
   print('F2 BGN calc:', (Decimal('101.42') * rate).quantize(Decimal('0.01')), 'printed: 198.34')
   print('F3 BGN calc:', (Decimal('123.17') * rate).quantize(Decimal('0.01')), 'printed: 240.92')
   "
   ```

4. **Verify Entire 23-File Corpus Count and Formats**:
   ```bash
   find "/Volumes/NO NAME/_ФАКТУРИ" -type f -not -name ".*" | wc -l
   # Returns exactly 23
   ```
