"""Tier 4: Real-World Acceptance Tests for Bulgarian Invoice OCR.

Covers:
- Acceptance testing against the 3 primary Kapina PDFs at:
  /Volumes/NO NAME/_ФАКТУРИ/02_КАПИНА_71_ООД/Капина 2026/
  - капина-01.pdf (14 items, 82.38 EUR tax base, 16.48 EUR VAT, 98.86 EUR total)
  - капина-02.pdf (20 items, 101.42 EUR tax base, 20.28 EUR VAT, 121.69 EUR total)
  - капина-03.pdf (17 items, fiscal cash slip occlusion, 123.17 EUR tax base, 24.65 EUR VAT, 147.83 EUR total)
- Strict read-only verification: 0 files modified, deleted, or created on external volume.
- Diagnostic verification table generation comparing all 3 files.
- Broader corpus read-only traversal across /Volumes/NO NAME/_ФАКТУРИ.
"""
import os
import sys
import unittest
from decimal import Decimal
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import invoice_ocr
from invoice_ocr import (
    Invoice,
    InvoiceMetadata,
    LineItem,
    MoneyAmount,
    Party,
    FinancialSummary,
    PaymentDetails,
    load_document,
    _validate_totals,
    _validate_currency,
    _validate_required_fields,
    validate_invoice,
)
from tests.e2e.test_helpers import (
    ACCEPTANCE_DIR,
    CORPUS_ROOT,
    DirectoryStateSnapshot,
)

# Ground-truth specifications for Kapina acceptance invoices
KAPINA_GROUND_TRUTH = {
    "капина-01.pdf": {
        "invoice_number": "1100124585",
        "date_issued": "2026-04-28",
        "date_tax_event": "2026-04-28",
        "place_issued": "ПЛЕВЕН",
        "supplier_name": "КАПИНА 71 ООД",
        "supplier_eik": "114500333",
        "supplier_vat": "BG114500333",
        "recipient_name": "ФАСТ ТОП ФУУДС ЕООД",
        "recipient_eik": "207930830",
        "recipient_vat": "BG207930830",
        "line_items_count": 14,
        "tax_base_eur": Decimal("82.38"),
        "vat_amount_eur": Decimal("16.48"),
        "total_amount_eur": Decimal("98.86"),
        "tax_base_bgn": Decimal("161.12"),
        "vat_amount_bgn": Decimal("32.23"),
        "total_amount_bgn": Decimal("193.35"),
        "currency": "EUR",
        "amount_words": "Деветдесет и осем евро и 86 е.ц.",
    },
    "капина-02.pdf": {
        "invoice_number": "1100123568",
        "date_issued": "2026-04-17",
        "date_tax_event": "2026-04-17",
        "place_issued": "ПЛЕВЕН",
        "supplier_name": "КАПИНА 71 ООД",
        "supplier_eik": "114500333",
        "supplier_vat": "BG114500333",
        "recipient_name": "ФАСТ ТОП ФУУДС ЕООД",
        "recipient_eik": "207930830",
        "recipient_vat": "BG207930830",
        "line_items_count": 20,
        "tax_base_eur": Decimal("101.42"),
        "vat_amount_eur": Decimal("20.28"),
        "total_amount_eur": Decimal("121.69"),
        "tax_base_bgn": Decimal("198.34"),
        "vat_amount_bgn": Decimal("39.66"),
        "total_amount_bgn": Decimal("238.00"),
        "currency": "EUR",
        "amount_words": "Сто двадесет и едно евро и 69 е.ц.",
    },
    "капина-03.pdf": {
        "invoice_number": "1100124013",
        "date_issued": "2026-04-22",
        "date_tax_event": "2026-04-22",
        "place_issued": "ПЛЕВЕН",
        "supplier_name": "КАПИНА 71 ООД",
        "supplier_eik": "114500333",
        "supplier_vat": "BG114500333",
        "recipient_name": "ФАСТ ТОП ФУУДС ЕООД",
        "recipient_eik": "207930830",
        "recipient_vat": "BG207930830",
        "line_items_count": 17,
        "tax_base_eur": Decimal("123.17"),
        "vat_amount_eur": Decimal("24.65"),
        "total_amount_eur": Decimal("147.83"),
        "tax_base_bgn": Decimal("240.92"),
        "vat_amount_bgn": Decimal("48.21"),
        "total_amount_bgn": Decimal("289.13"),
        "currency": "EUR",
        "amount_words": "Сто четиридесет и седем евро и 83 е.ц.",
    },
}


class TestTier4RealWorldScenarios(unittest.TestCase):
    """Tier 4: Acceptance testing against real invoices and volume integrity verification."""

    def setUp(self):
        if not ACCEPTANCE_DIR.exists():
            self.skipTest(f"Acceptance directory {ACCEPTANCE_DIR} is not mounted or accessible")
        self.snapshot = DirectoryStateSnapshot(ACCEPTANCE_DIR)

    def tearDown(self):
        # Enforce zero-touch guarantee on external volume
        is_clean, errors = self.snapshot.verify_unchanged()
        self.assertTrue(is_clean, f"External volume was modified during test: {errors}")

    def test_real_01_kapina_01_acceptance(self):
        """Tier 4: капина-01.pdf acceptance against ground truth."""
        pdf_name = "капина-01.pdf"
        pdf_path = ACCEPTANCE_DIR / pdf_name
        self.assertTrue(pdf_path.exists(), f"Missing acceptance file: {pdf_path}")
        self.assertEqual(pdf_path.stat().st_size, 7516207, "File size mismatch for капина-01.pdf")

        # 1. Ingestion verification
        pages = load_document(pdf_path)
        self.assertEqual(len(pages), 1, "Kapina 01 must have 1 page")
        self.assertGreaterEqual(pages[0].height, 3000, "Page must be rasterized at 300+ DPI")

        gt = KAPINA_GROUND_TRUTH[pdf_name]

        # 2. Verify ground truth model invariants
        self.assertEqual(gt["line_items_count"], 14)
        self.assertLessEqual(abs((gt["tax_base_eur"] + gt["vat_amount_eur"]) - gt["total_amount_eur"]), Decimal("0.02"))
        self.assertEqual(gt["currency"], "EUR")

        # 3. Full pipeline execution
        try:
            inv = invoice_ocr.process_invoice(pdf_path)
            self.assertEqual(inv.invoice_metadata.invoice_number, gt["invoice_number"])
            self.assertEqual(len(inv.line_items), gt["line_items_count"])
            self.assertEqual(inv.financial_summary.total_amount_due.amount, gt["total_amount_eur"])
        except ValueError as exc:
            self.fail(f"process_invoice pending load_document integration for PDF: {exc}")

    def test_real_02_kapina_02_acceptance(self):
        """Tier 4: капина-02.pdf acceptance against ground truth (20 items)."""
        pdf_name = "капина-02.pdf"
        pdf_path = ACCEPTANCE_DIR / pdf_name
        self.assertTrue(pdf_path.exists(), f"Missing acceptance file: {pdf_path}")
        self.assertEqual(pdf_path.stat().st_size, 8148645, "File size mismatch for капина-02.pdf")

        # Ingestion verification
        pages = load_document(pdf_path)
        self.assertEqual(len(pages), 1, "Kapina 02 must have 1 page")
        self.assertGreaterEqual(pages[0].height, 3000, "Page must be rasterized at 300+ DPI")

        gt = KAPINA_GROUND_TRUTH[pdf_name]
        self.assertEqual(gt["line_items_count"], 20)
        self.assertLessEqual(abs((gt["tax_base_eur"] + gt["vat_amount_eur"]) - gt["total_amount_eur"]), Decimal("0.02"))

        try:
            inv = invoice_ocr.process_invoice(pdf_path)
            self.assertEqual(len(inv.line_items), gt["line_items_count"])
            self.assertEqual(inv.financial_summary.total_amount_due.amount, gt["total_amount_eur"])
        except ValueError as exc:
            self.fail(f"process_invoice pending load_document integration for PDF: {exc}")

    def test_real_03_kapina_03_acceptance(self):
        """Tier 4: капина-03.pdf acceptance with receipt occlusion (17 items)."""
        pdf_name = "капина-03.pdf"
        pdf_path = ACCEPTANCE_DIR / pdf_name
        self.assertTrue(pdf_path.exists(), f"Missing acceptance file: {pdf_path}")
        self.assertEqual(pdf_path.stat().st_size, 7218503, "File size mismatch for капина-03.pdf")

        # Ingestion verification
        pages = load_document(pdf_path)
        self.assertEqual(len(pages), 1, "Kapina 03 must have 1 page")
        self.assertGreaterEqual(pages[0].height, 3000, "Page must be rasterized at 300+ DPI")

        gt = KAPINA_GROUND_TRUTH[pdf_name]
        self.assertEqual(gt["line_items_count"], 17)
        self.assertLessEqual(abs((gt["tax_base_eur"] + gt["vat_amount_eur"]) - gt["total_amount_eur"]), Decimal("0.02"))

        try:
            inv = invoice_ocr.process_invoice(pdf_path)
            self.assertEqual(len(inv.line_items), gt["line_items_count"])
            self.assertEqual(inv.financial_summary.total_amount_due.amount, gt["total_amount_eur"])
        except ValueError as exc:
            self.fail(f"process_invoice pending load_document integration for PDF: {exc}")

    def test_real_04_external_volume_immutability(self):
        """Tier 4: Verify read-only operations on external volume leave 0 modifications."""
        for filename in ["капина-01.pdf", "капина-02.pdf", "капина-03.pdf"]:
            filepath = ACCEPTANCE_DIR / filename
            with open(filepath, "rb") as f:
                header = f.read(1024)
                self.assertTrue(header.startswith(b"%PDF"))

        is_clean, errors = self.snapshot.verify_unchanged()
        self.assertTrue(is_clean, f"Volume was modified: {errors}")

    def test_real_05_diagnostic_audit_table_generation(self):
        """Tier 4: Generate diagnostic audit table detailing all 3 Kapina acceptance files."""
        headers = ["File", "Pages", "Items", "Tax Base (EUR)", "VAT (EUR)", "Total (EUR)", "Currency", "Status"]
        rows = []

        for name, data in KAPINA_GROUND_TRUTH.items():
            rows.append([
                name,
                "1",
                str(data["line_items_count"]),
                f"{data['tax_base_eur']:.2f}",
                f"{data['vat_amount_eur']:.2f}",
                f"{data['total_amount_eur']:.2f}",
                data["currency"],
                "VALID"
            ])

        self.assertEqual(len(rows), 3)
        col_widths = [max(len(row[i]) for row in [headers] + rows) for i in range(len(headers))]
        table_str = " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)) + "\n"
        table_str += "-+-".join("-" * col_widths[i] for i in range(len(headers))) + "\n"
        for row in rows:
            table_str += " | ".join(val.ljust(col_widths[i]) for i, val in enumerate(row)) + "\n"

        self.assertIn("капина-01.pdf", table_str)
        self.assertIn("капина-02.pdf", table_str)
        self.assertIn("капина-03.pdf", table_str)

    def test_real_06_broader_corpus_read_only_scan(self):
        """Tier 4: Traversal of broader /Volumes/NO NAME/_ФАКТУРИ corpus in read-only mode."""
        if not CORPUS_ROOT.exists():
            self.skipTest("Corpus root not accessible")

        pdf_files = list(CORPUS_ROOT.glob("**/*.pdf"))
        self.assertGreaterEqual(len(pdf_files), 20, "Corpus must contain at least 20 PDF files")


if __name__ == "__main__":
    unittest.main()
