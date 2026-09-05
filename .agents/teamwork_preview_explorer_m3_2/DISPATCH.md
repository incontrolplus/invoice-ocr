## 2026-09-04T22:21:10Z

You are Explorer 2 for Milestone 3: Spatial Layout Analysis & Table Reconstruction.
Your working directory is: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m3_2
Authoritative Requirements: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/ORIGINAL_REQUEST.md
Project Plan: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/PROJECT.md
Worker M2 Handoff: /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_worker_m2_iter2/handoff.md

STRICT CONSTRAINT:
NEVER modify, move, or delete any source files in /Volumes/NO NAME/_ФАКТУРИ.

Task:
Explore and design the Bulgarian Table Header Detection and Column Grid Reconstruction:
1. Feature 15: Bulgarian Table Header Synonym Taxonomy:
   - Compile an exhaustive dictionary of Bulgarian table column headers and synonyms found across B2B invoices and real acceptance files:
     * Line Number: `№`, `No`, `Поз`, `Позиция`, `Код`
     * Description: `Описание`, `Наименование на стоката / услугата`, `Стока`, `Услуга`, `Артикул`, `Наименование`
     * Quantity: `Количество`, `К-во`, `Кол.`, `Брой`
     * Unit of Measure: `Мярка`, `М-ка`, `Ед. м.`, `М. ед.`
     * Unit Price: `Ед. цена`, `Цена без ДДС`, `Цена`, `Единична цена`
     * Total Price Net / Value: `Стойност`, `Сума`, `Стойност без ДДС`, `Сума без ДДС`, `Обща стойност`
     * VAT Rate / Amount: `ДДС %`, `ДДС`, `Ставка`, `ДДС стойност`
     * Total with VAT: `Стойност с ДДС`, `Сума с ДДС`, `Общо`
   - Design header line detection: a `LogicalLine` or multi-line header zone matching >= 3 statutory column synonyms.
2. Feature 16: Column Boundary Projection & Row Data Extraction:
   - Design dynamic column boundary projection: calculate X-spans `(x_min, x_max)` for each detected column from header token bounding boxes.
   - Handle column gaps and center alignment: assign tokens in subsequent data lines to the column whose X-span they intersect or are closest to.
   - Design table row parsing: map each line below the header into structured fields (`description`, `quantity`, `unit`, `unit_price`, `total_price`, `vat_rate`).
3. Empirical Check on Kapina acceptance invoices:
   - Inspect table layouts in `капина-01.pdf`, `капина-02.pdf`, `капина-03.pdf` in read-only mode to verify column positions, column headers, and number of line items.
4. Output:
   Write a comprehensive architectural and algorithmic exploration report to:
   /Users/diokarabaz/orca/projects/invoice-tessearct-ocr/.agents/teamwork_preview_explorer_m3_2/handoff.md
   Notify orchestrator when done.
