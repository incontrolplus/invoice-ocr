"""Unit tests for Bulgarian legal entity form party extraction, EIK Mod-11 validation,
and candidate scoring heuristics.
"""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pytest
from invoice_ocr import (
    OcrToken,
    LogicalLine,
    group_tokens_into_lines,
    extract_party,
    validate_eik,
    normalize_eik,
    normalize_vat_number,
    clean_party_name,
    score_party_candidate,
    fuse_visual_rows,
)


class TestBulgarianEikValidation:
    """Test standard Bulgarian Mod-11 UIC/BULSTAT checksum validation."""

    def test_valid_9_digit_eiks(self):
        valid_eiks = [
            "114500333",  # КАПИНА 71 ООД
            "121644736",  # МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД
            "207930830",  # ФАСТ ТОП ФУУДС ЕООД
            "104586266",  # ГРЕСТОКОМЕРС ЕООД
            "114681129",  # НЕНДВ ООД
            "114609731",  # ВАЛБОРГЕН ООД
            "824156453",  # ЕТ ТЕМЕНУЖКА КОЧЕВА - НАДЯ
            "114540185",  # ЕКСПРЕС СЕКЮРИТИ СОД ЕООД
            "114598690",  # ИНТЕРМЕС ООД
            "114598204",  # О СКАРИ ООД
            "202262252",  # АНДА 2012 АНКО ПЕТРОВ ЕООД
        ]
        for eik in valid_eiks:
            assert validate_eik(eik), f"Expected {eik} to be a valid Bulgarian EIK"

    def test_invalid_9_digit_checksums(self):
        invalid_eiks = [
            "121644734",  # Last digit OCR 4 instead of 6
            "114500330",  # Last digit 0 instead of 3
            "207930839",  # Last digit 9 instead of 0
            "104586260",  # Last digit 0 instead of 6
        ]
        for eik in invalid_eiks:
            assert not validate_eik(eik), f"Expected {eik} to fail Mod-11 checksum"

    def test_length_and_edge_cases(self):
        assert not validate_eik(None)
        assert not validate_eik("")
        assert not validate_eik("12345")
        assert validate_eik("1234567890")  # 10-digit (EGN / natural persons)
        assert validate_eik("1234567890123")  # 13-digit branch UIC


class TestPartyCleaningAndCandidateScoring:
    """Test party name cleaning, legal form filtering, and scoring heuristics."""

    def test_rejection_of_table_numbers_and_arithmetic(self):
        table_line = "1.0000 750.0.85000 * 750.00."
        assert score_party_candidate(table_line) < -100

    def test_rejection_of_invoice_metadata(self):
        meta_line1 = "Номер 0020028286 # Дата03.04.2026."
        meta_line2 = "актур"
        meta_line3 = "ОРИГИНАЛ"
        for line in [meta_line1, meta_line2, meta_line3]:
            assert score_party_candidate(line) < -100

    def test_rejection_of_address_lines(self):
        addr_line = "Р-ЦЕН 1798 СОФИЯ"
        assert score_party_candidate(addr_line) < -100

    def test_rejection_of_form_headers(self):
        header_line = "- Описание на сделката"
        assert score_party_candidate(header_line) < -100

    def test_rejection_of_bare_labels(self):
        assert score_party_candidate("Доставчик:") < -100
        assert score_party_candidate("Получател:") < -100

    def test_rejection_of_bare_legal_form(self):
        assert score_party_candidate("ООД") < 0
        assert score_party_candidate("ЕООД") < 0

    def test_valid_company_name_scoring(self):
        good_names = [
            "НендВ ООД",
            "Валборген ООД",
            "КАПИНА 71 ООД",
            "ФАСТ ТОП ФУУДС ЕООД",
            "Грестокомерс ЕООД",
            "ЕТ ТЕМЕНУЖКА КОЧЕВА- НАДЯ",
        ]
        for name in good_names:
            score = score_party_candidate(name)
            assert score > 100, f"Expected {name!r} to have high positive score, got {score}"

    def test_clean_party_name_deduplication(self):
        raw = "ЕТ ТЕМЕНУЖКА КОЧЕВА- НАДЯ НАДЯ"
        cleaned = clean_party_name(raw)
        assert cleaned == "ЕТ ТЕМЕНУЖКА КОЧЕВА- НАДЯ"

    def test_clean_party_name_strips_branch_code(self):
        raw = "01004078 ФАСТ ТОП ФУУДС ЕООД"
        cleaned = clean_party_name(raw)
        assert cleaned == "ФАСТ ТОП ФУУДС ЕООД"

    def test_clean_party_name_strips_trailing_noise(self):
        raw = "О СКАРИ ООД 7777"
        cleaned = clean_party_name(raw)
        assert cleaned == "О СКАРИ ООД"


class TestPartyExtractionPipelines:
    """Test extract_party with simulated token layouts from real-world edge cases."""

    def test_eik_vat_cross_derivation_from_vat(self):
        """When EIK is missing from text but VAT number is present, EIK is derived."""
        tokens = [
            OcrToken(text="Доставчик:", conf=90.0, bbox=(1400, 400, 150, 30)),
            OcrToken(text="КАПИНА", conf=90.0, bbox=(1400, 450, 100, 30)),
            OcrToken(text="71", conf=90.0, bbox=(1510, 450, 40, 30)),
            OcrToken(text="ООД", conf=90.0, bbox=(1560, 450, 60, 30)),
            OcrToken(text="ДДС", conf=90.0, bbox=(1400, 500, 60, 30)),
            OcrToken(text="номер", conf=90.0, bbox=(1470, 500, 80, 30)),
            OcrToken(text="BG114500333", conf=90.0, bbox=(1560, 500, 180, 30)),
        ]
        lines = group_tokens_into_lines(tokens)
        party = extract_party(lines, tokens, "supplier")
        assert party.vat_number == "BG114500333"
        assert party.eik == "114500333", f"Expected EIK 114500333 derived from VAT, got {party.eik}"
        assert "КАПИНА" in party.name and "ООД" in party.name

    def test_multi_line_visual_row_fusion(self):
        """Tokens separated horizontally at same Y must fuse into complete company name."""
        tokens = [
            OcrToken(text="Получател", conf=90.0, bbox=(100, 400, 140, 30)),
            OcrToken(text="ФАСТ", conf=90.0, bbox=(250, 400, 70, 30)),
            OcrToken(text="ТОП", conf=90.0, bbox=(330, 400, 60, 30)),
            OcrToken(text="ФУУДС", conf=90.0, bbox=(400, 400, 90, 30)),
            OcrToken(text="ЕООД", conf=90.0, bbox=(800, 402, 80, 30)),
            OcrToken(text="ЕИК", conf=90.0, bbox=(100, 450, 50, 30)),
            OcrToken(text="207930830", conf=90.0, bbox=(160, 450, 150, 30)),
        ]
        lines = group_tokens_into_lines(tokens)
        party = extract_party(lines, tokens, "recipient")
        assert party.name == "ФАСТ ТОП ФУУДС ЕООД"
        assert party.eik == "207930830"

    def test_table_numbers_not_extracted_as_party_name(self):
        """Table arithmetic row must be excluded even if present in party column."""
        tokens = [
            OcrToken(text="Доставчик:", conf=90.0, bbox=(1400, 400, 150, 30)),
            OcrToken(text="НендВ", conf=90.0, bbox=(1400, 450, 100, 30)),
            OcrToken(text="ООД", conf=90.0, bbox=(1510, 450, 60, 30)),
            OcrToken(text="ЕИК", conf=90.0, bbox=(1400, 500, 50, 30)),
            OcrToken(text="114681129", conf=90.0, bbox=(1460, 500, 150, 30)),
            OcrToken(text="1.0000", conf=90.0, bbox=(1400, 800, 90, 30)),
            OcrToken(text="750.00", conf=90.0, bbox=(1500, 800, 90, 30)),
            OcrToken(text="*", conf=90.0, bbox=(1600, 800, 20, 30)),
            OcrToken(text="750.00", conf=90.0, bbox=(1630, 800, 90, 30)),
        ]
        lines = group_tokens_into_lines(tokens)
        party = extract_party(lines, tokens, "supplier")
        assert party.name == "НендВ ООД"
        assert "750" not in party.name
        assert party.eik == "114681129"
