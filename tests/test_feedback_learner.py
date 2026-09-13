"""Test Suite for Continuous Learning Engine (Delta Pro Feedback Analyzer)."""
from pathlib import Path
from invoice_core.feedback_learner import (
    extract_corrections,
    match_operation,
    normalize_doc_number,
    persist_learned_vendor_rules,
)


def test_normalize_doc_number():
    assert normalize_doc_number("0000012345") == "12345"
    assert normalize_doc_number("№ 01000555-A") == "1000555"
    assert normalize_doc_number("000") == "0"


def test_extract_corrections_and_learning(tmp_path):
    # Simulated original OCR operations (with some misclassifications / numbering flaws)
    original_ops = [
        {
            "contractor_eik": "121644138",
            "contractor_name": "МЕТРО КЕШ ЕНД КЕРИ",
            "document_number": "0012345678",
            "total_amount": "120.00",
            "expense_account": "601",
            "reason": "м-ли",
        },
        {
            "contractor_eik": "831642181",
            "contractor_name": "ВИВАКОМ БЪЛГАРИЯ",
            "document_number": "0987654321",
            "total_amount": "55.50",
            "expense_account": "601",  # OCR defaulted to 601 (materials)
            "reason": "м-ли",
        },
    ]

    # Human accountant reviewed operations in Delta Pro
    reviewed_ops = [
        {
            "contractor_eik": "121644138",
            "contractor_name": "МЕТРО КЕШ ЕНД КЕРИ",
            "document_number": "0012345678",
            "total_amount": "120.00",
            "expense_account": "601",  # Unchanged
            "reason": "м-ли",
        },
        {
            "contractor_eik": "831642181",
            "contractor_name": "ВИВАКОМ БЪЛГАРИЯ",
            "document_number": "0987654321",
            "total_amount": "55.50",
            "expense_account": "602",  # Corrected to 602 (external services / telecom)
            "reason": "телекомуникации",  # Corrected reason
        },
    ]

    report = extract_corrections(original_ops, reviewed_ops)
    assert report["total_compared"] == 2
    assert report["exact_matches"] == 1
    assert report["corrected_count"] == 1

    corr = report["corrections"][0]
    assert corr["contractor_eik"] == "831642181"
    assert corr["differences"]["expense_account"] == {"before": "601", "after": "602"}
    assert corr["differences"]["reason"] == {"before": "м-ли", "after": "телекомуникации"}

    # Test persisting learned vendor rules to YAML
    vendors_dir = tmp_path / "vendors"
    updated_files = persist_learned_vendor_rules(report["contractor_rules_to_learn"], vendors_dir)
    assert len(updated_files) == 1

    learned_yaml = vendors_dir / "learned_831642181.yaml"
    assert learned_yaml.exists()
    content = learned_yaml.read_text("utf-8")
    assert "602" in content
    assert "телекомуникации" in content
    assert "delta_pro_human_review_feedback" in content
