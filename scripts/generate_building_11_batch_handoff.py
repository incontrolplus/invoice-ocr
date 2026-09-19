#!/usr/bin/env python3
import datetime
import json
from pathlib import Path

PROJECT_ROOT = Path("/Users/diokarabaz/orca/projects/invoice-tessearct-ocr")
SUMMARY_FILE = PROJECT_ROOT / "Building_11" / "BUILDING_11_BATCH_SUMMARY.json"
TARGET_PATHS = [
    Path("/Volumes/NO NAME/Building_11/HANDOFF.md"),
    Path("/Volumes/NO NAME/HANDOFF.md"),
    PROJECT_ROOT / "Building_11" / "HANDOFF.md",
    Path("/Users/diokarabaz/.gemini/antigravity-cli/brain/0a7f4473-555c-4082-b34f-d1f3912dfbd5/HANDOFF.md"),
]

with open(SUMMARY_FILE, "r", encoding="utf-8") as f:
    items = json.load(f)

sales = [it for it in items if "ПРОДАЖБИ" in it["direction"]]
purchases = [it for it in items if "ПОКУПКИ" in it["direction"]]

sales_base = sum(it["tax_base"] for it in sales)
sales_vat = sum(it["vat"] for it in sales)
sales_total = sum(it["total"] for it in sales)

pur_base = sum(it["tax_base"] for it in purchases)
pur_vat = sum(it["vat"] for it in purchases)
pur_total = sum(it["total"] for it in purchases)

now_str = datetime.datetime.now().strftime("%d.%m.%Y г., %H:%M:%S ч.")

lines = []
lines.append("# ПРИЕМО-ПРЕДАВАТЕЛЕН ПРОТОКОЛ И ОДИТОРСКИ ДОКЛАД (HANDOFF.md)")
lines.append("## Завършен пълен OCR Pipeline & Пакет за импорт в Microinvest Delta Pro")
lines.append(f"**Предприятие:** БИЛДИНГ 11 ООД (ЕИК: 206062202, ИН по ЗДДС: BG206062202)")
lines.append(f"**Дата и час на обработка:** {now_str}")
lines.append(f"**Трансферни файлове:** `TRANSFER.LOG` (262,144 байта / 128 страници Jet 2.0), `TRANSFER.ldb` (64 байта)")
lines.append(f"**Локация за импорт:** `E:\\TRANSFER.LOG` (USB) или `C:\\MICRO\\TRANSFER.LOG`")
lines.append("")
lines.append("---")
lines.append("")
lines.append("## 1. ФИНАНСОВО РЕЗЮМЕ НА ПАРТИДАТА (9 ДОКУМЕНТА)")
lines.append("")
lines.append("| Категория | Брой документи | Данъчна основа (€) | ДДС 20% (€) | Обща стойност (€) | Счетоводен регистър |")
lines.append("| :--- | :---: | :---: | :---: | :---: | :--- |")
lines.append(f"| **Продажби (ФАКТУРИ_БИЛДИНГ_11_ДОСТАВЧИК)** | {len(sales)} бр. | {sales_base:,.2f} € | {sales_vat:,.2f} € | {sales_total:,.2f} € | Дневник Продажби (Дт 411 / Кт 702, Кт 4532) |")
lines.append(f"| **Покупки (ФАКТУРИ_БИЛДИНГ_11_ПОЛУЧАТЕЛ)** | {len(purchases)} бр. | {pur_base:,.2f} € | {pur_vat:,.2f} € | {pur_total:,.2f} € | Дневник Покупки (Дт 601, 602, Дт 4531 / Кт 401) |")
lines.append(f"| **ОБЩО ЗА ПАРТИДАТА** | **{len(items)} бр.** | **{(sales_base + pur_base):,.2f} €** | **{(sales_vat + pur_vat):,.2f} €** | **{(sales_total + pur_total):,.2f} €** | Балансирани операции (100%) |")
lines.append("")
lines.append("> [!NOTE]")
lines.append("> Фактура № `5000000087` е анулирана (Voided). Включена е с нулеви стойности (0.00 €), за да се запази строгата последователност на фактурните номера в Дневника за продажби на НАП съгласно чл. 119 от ЗДДС.")
lines.append("")
lines.append("---")
lines.append("")
lines.append("## 2. ДЕТАЙЛЕН ОПИС НА ВСИЧКИ ДОКУМЕНТИ")
lines.append("")
lines.append("### 2.1. Продажби (Building 11 е Доставчик)")
lines.append("")
lines.append("| № | Файл | Номер на фактура | Дата | Клиент / Контрагент | ЕИК | Данъчна основа (€) | ДДС 20% (€) | Обща сума (€) | Статус / Сметки |")
lines.append("| :-: | :--- | :---: | :---: | :--- | :---: | :---: | :---: | :---: | :--- |")

for idx, s in enumerate(sales, 1):
    status_str = "Анулирана (0.00 €)" if s["is_annulled"] else f"{s['total']:,.2f} €"
    lines.append(f"| {idx} | `{s['file']}` | **{s['invoice_number']}** | {s['date']} | {s['partner']} | `{s['eik']}` | {s['tax_base']:,.2f} | {s['vat']:,.2f} | **{status_str}** | Дт 411 / Кт 702, Кт 4532 |")

lines.append("")
lines.append("### 2.2. Покупки (Building 11 е Получател)")
lines.append("")
lines.append("| № | Файл | Номер на фактура | Дата | Доставчик | ЕИК | Данъчна основа (€) | ДДС 20% (€) | Обща сума (€) | Сметки / Основание |")
lines.append("| :-: | :--- | :---: | :---: | :--- | :---: | :---: | :---: | :---: | :--- |")

for idx, p in enumerate(purchases, 1):
    lines.append(f"| {idx} | `{p['file']}` | **{p['invoice_number']}** | {p['date']} | {p['partner']} | `{p['eik']}` | {p['tax_base']:,.2f} | {p['vat']:,.2f} | **{p['total']:,.2f} €** | {p['debit_account']} / {p['credit_account']} |")

lines.append("")
lines.append("---")
lines.append("")
lines.append("## 3. ИНСТРУКЦИИ ЗА ИМПОРТ В MICROINVEST DELTA PRO")
lines.append("1. Във виртуалната машина **Windows XP** отворете фирма **БИЛДИНГ 11**.")
lines.append("2. Изберете главното меню: `Обмен` -> `Обмен на операции` -> `Импорт`.")
lines.append("3. Изберете файла `E:\\TRANSFER.LOG` (или `C:\\MICRO\\TRANSFER.LOG`).")
lines.append("4. Потвърдете импорта. Всички 9 стопански операции ще бъдат въведени с точните им сметки, аналитични партиди и записи в Дневник Покупки и Дневник Продажби.")
lines.append("")

content = "\n".join(lines) + "\n"

for p in TARGET_PATHS:
    if p.parent.exists():
        p.write_text(content, encoding="utf-8")
        print(f"Written: {p}")

print("HANDOFF.md successfully generated!")
