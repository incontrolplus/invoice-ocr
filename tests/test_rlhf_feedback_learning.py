"""Comprehensive test suite for Reinforcement Learning from Human Feedback (RLHF)

Verifies:
1. Coordinate normalization and spatial bounding box priors.
2. Invoice number series deduction (leading zeros, 10-digit series, regex patterns).
3. Dot-matrix homoglyph deduction (matrix font character mapping).
4. Dynamic vendor YAML generation and hot cache reloading.
5. End-to-end extraction boost using learned vendor profiles.
6. Database audit persistence of human feedback exemplars.
"""
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from database import Base, DocumentRecord, InvoiceFeedbackRecord, update_document_corrections
from invoice_core.feedback_learning import (
    deduce_invoice_number_rules,
    extract_company_keywords,
    get_feedback_statistics,
    normalize_bbox,
    process_human_feedback,
    update_or_create_vendor_profile,
)
from invoice_core.models import LogicalLine, OcrToken
from invoice_core.extraction import extract_invoice_number
from invoice_core.vendor_profiles import (
    CONFIG_VENDORS_DIR,
    get_vendor_profile,
    get_vendor_profile_loader,
    reset_vendor_profiles_cache,
)


class TestRLHFFeedbackLearning(unittest.TestCase):
    """Unit and integration tests for RLHF and adaptive vendor layout learning."""

    def setUp(self):
        # Create an in-memory SQLite database
        self.engine = create_engine("sqlite:///:memory:", echo=False)
        Base.metadata.create_all(bind=self.engine)
        self.SessionLocal = sessionmaker(bind=self.engine)
        self.db = self.SessionLocal()

        # Temporary vendor config dir
        self.test_vendor_dir = Path("/tmp/test_rlhf_vendors")
        if self.test_vendor_dir.exists():
            shutil.rmtree(self.test_vendor_dir)
        self.test_vendor_dir.mkdir(parents=True, exist_ok=True)

        # Track files created to clean up
        self.created_files = []

    def tearDown(self):
        self.db.close()
        for f in self.created_files:
            if f.exists():
                f.unlink()
        if self.test_vendor_dir.exists():
            shutil.rmtree(self.test_vendor_dir)
        reset_vendor_profiles_cache()

    def test_normalize_bbox(self):
        """Verify pixel to normalized relative coordinate transformation."""
        # 1. Standard pixel bbox on 2000x3000 image
        pixel_bbox = [200, 300, 400, 150]
        norm = normalize_bbox(pixel_bbox, page_width=2000, page_height=3000)
        self.assertEqual(norm, [0.1, 0.1, 0.2, 0.05])

        # 2. Already normalized coordinates
        already_norm = [0.45, 0.12, 0.35, 0.08]
        res = normalize_bbox(already_norm)
        self.assertEqual(res, [0.45, 0.12, 0.35, 0.08])

        # 3. Invalid bbox returns None
        self.assertIsNone(normalize_bbox(None))
        self.assertIsNone(normalize_bbox([1, 2]))

    def test_deduce_invoice_number_rules_leading_zeros(self):
        """Verify series prefix deduction for invoices starting with leading zeros (e.g. 0000006960)."""
        rules = deduce_invoice_number_rules("0000006960")
        self.assertEqual(rules["length"], 10)
        self.assertEqual(rules["prefix"], "000000")
        self.assertEqual(rules["regex"], "^000000\\d{4}$")
        self.assertEqual(rules["sample"], "0000006960")

    def test_deduce_invoice_number_rules_standard_series(self):
        """Verify series prefix deduction for standard 10-digit series (e.g. 1000993561)."""
        rules = deduce_invoice_number_rules("1000993561")
        self.assertEqual(rules["length"], 10)
        self.assertEqual(rules["prefix"], "1000")
        self.assertEqual(rules["sample"], "1000993561")

    def test_deduce_homoglyphs_from_raw_token(self):
        """Verify matrix font character substitution mapping deduction."""
        raw_token = "nnnnnneacn"
        corrected = "0000006960"
        rules = deduce_invoice_number_rules(corrected, raw_token_text=raw_token)
        self.assertTrue(rules.get("dot_matrix"))
        homo = rules.get("homoglyphs", {})
        self.assertEqual(homo.get("n"), "0")
        self.assertEqual(homo.get("e"), "6")
        self.assertEqual(homo.get("a"), "9")
        self.assertEqual(homo.get("c"), "6")

    def test_extract_company_keywords(self):
        """Verify legal suffix stripping and clean vendor keyword generation."""
        kws = extract_company_keywords("СИКРЕТ ЛЕДЖЪНД ЕООД")
        self.assertIn("сикрет", kws)
        self.assertIn("леджънд", kws)
        self.assertNotIn("еоод", kws)

    def test_update_or_create_vendor_profile_lifecycle(self):
        """Verify YAML generation, schema validation, and cache reloading."""
        test_eik = "202262252"
        vendor_id, file_path, is_new = update_or_create_vendor_profile(
            supplier_eik=test_eik,
            supplier_name="ТЕСТОВО ПРЕДПРИЯТИЕ ООД",
            supplier_vat=f"BG{test_eik}",
            series_rules={"prefix": "000000", "length": 10, "regex": "^000000\\d{4}$"},
            spatial_prior={"field": "invoice_number", "bbox": [0.5, 0.1, 0.3, 0.05]},
            homoglyphs={"n": "0", "e": "6"},
            dot_matrix=True,
        )
        self.created_files.append(file_path)
        self.assertTrue(file_path.exists())

        # Verify profile is discoverable via loader
        prof = get_vendor_profile(test_eik)
        self.assertIsNotNone(prof)
        self.assertEqual(prof["eik"], test_eik)
        self.assertEqual(prof["invoice_number_series"]["prefix"], "000000")
        self.assertTrue(prof["dot_matrix"])
        self.assertEqual(prof["spatial_priors"]["invoice_number"]["bbox"], [0.5, 0.1, 0.3, 0.05])

    def test_end_to_end_extraction_boost_with_learned_profile(self):
        """Verify that learned vendor rules allow extracting invoice numbers that previously were ambiguous."""
        test_eik = "999888777"
        # 1. Create a learned profile for this vendor specifying prefix '000000' and dot_matrix homoglyphs
        vendor_id, file_path, _ = update_or_create_vendor_profile(
            supplier_eik=test_eik,
            supplier_name="АВТОМАТИЧНО ОБУЧЕН ДОСТАВЧИК ЕООД",
            series_rules={"prefix": "000000", "length": 10},
            homoglyphs={"n": "0", "e": "6", "a": "9", "c": "6"},
            dot_matrix=True,
        )
        self.created_files.append(file_path)

        # 2. Simulate raw OCR lines with matrix artifact 'nnnnnneacn' and a distractor number
        tokens = [
            OcrToken(text="nnnnnneacn", conf=80, bbox=(500, 200, 200, 40), page_number=1),
            OcrToken(text="999888777", conf=95, bbox=(100, 100, 150, 30), page_number=1),
            OcrToken(text="0888123456", conf=90, bbox=(100, 600, 150, 30), page_number=1),
        ]
        lines = [
            LogicalLine(text="Доставчик: АВТОМАТИЧНО ОБУЧЕН ДОСТАВЧИК ЕООД ЕИК: 999888777", tokens=[]),
            LogicalLine(text="Документ: nnnnnneacn", tokens=[tokens[0]]),
            LogicalLine(text="Тел за контакти: 0888123456", tokens=[tokens[2]]),
        ]

        # 3. Extract invoice number passing supplier EIK
        extracted = extract_invoice_number(lines, tokens, supplier_eik=test_eik)
        self.assertEqual(extracted, "0000006960")

    def test_database_process_human_feedback_audit(self):
        """Verify that update_document_corrections records human feedback and updates vendor profile."""
        # 1. Create document record in DB
        doc_id = str(uuid.uuid4())
        record = DocumentRecord(
            id=doc_id,
            file_name="invoice_test.pdf",
            supplier_eik="114609507",
            supplier_name="ДЕТЕЛИНА-ДП ЕООД",
            invoice_number="0888979000",  # Incorrect phone mistakenly extracted
            tax_base=100.0,
            vat_amount=20.0,
            total_amount=120.0,
            status="needs_review",
            is_valid=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.db.add(record)
        self.db.commit()

        # 2. Human accountant applies 1-click correction to invoice_number
        corrections = {
            "invoice_number": "0000006960",
            "tax_base": 100.0,
            "vat_amount": 20.0,
            "total_amount": 120.0,
        }
        feedback_meta = {
            "field_name": "invoice_number",
            "old_value": "0888979000",
            "new_value": "0000006960",
            "raw_token_text": "nnnnnneacn",
            "token_bbox": [550, 180, 220, 45],
            "page_width": 2480,
            "page_height": 3508,
            "source": "canvas_click",
        }

        updated = update_document_corrections(
            db=self.db,
            doc_id=doc_id,
            corrections=corrections,
            actor="accountant",
            feedback_metadata=feedback_meta,
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated.invoice_number, "0000006960")

        # 3. Verify feedback learning record in database
        feedbacks = self.db.query(InvoiceFeedbackRecord).filter(
            InvoiceFeedbackRecord.document_id == doc_id
        ).all()
        self.assertGreaterEqual(len(feedbacks), 1)

        inv_feedback = [f for f in feedbacks if f.field_name == "invoice_number"][0]
        self.assertEqual(inv_feedback.original_value, "0888979000")
        self.assertEqual(inv_feedback.corrected_value, "0000006960")
        self.assertEqual(inv_feedback.raw_token_text, "nnnnnneacn")
        self.assertIsNotNone(inv_feedback.token_bbox_json)
        self.assertEqual(inv_feedback.reward_score, 1.0)

        # 4. Verify statistics endpoint helper
        stats = get_feedback_statistics(self.db)
        self.assertGreaterEqual(stats["total_corrections_learned"], 1)
        self.assertIn("invoice_number", stats["corrections_by_field"])


if __name__ == "__main__":
    unittest.main()
