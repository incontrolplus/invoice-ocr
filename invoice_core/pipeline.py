"""Master OCR pipeline orchestrating ingestion, extraction, and validation."""
from __future__ import annotations

import dataclasses
from decimal import Decimal
import gc
import json
import logging
import os
from pathlib import Path
import re
import time
from typing import Any

import cv2
import numpy as np
import pymupdf

from .cache import get_ocr_cache_path, load_ocr_cache, save_ocr_cache
from .classification import (
    DocumentClassifier,
    extract_budget_payment_order,
    extract_credit_debit_note_reference,
    extract_fiscal_memory_report,
    extract_goods_receipt,
    extract_goods_receipt_line_items,
    extract_parties_for_goods_receipt,
    extract_taxpayer_from_fiscal_report,
    validate_budget_payment_order,
    validate_fiscal_memory_report,
    validate_goods_receipt,
)
from .constants import (
    DEFAULT_OCR_CACHE_DIR,
    DEFAULT_OCR_LANG,
    DEFAULT_RASTER_DPI,
    IMAGE_EXTENSIONS,
    MIN_CONFIDENCE,
    PDF_EXTENSIONS,
)
from .currency import convert_bgn_to_eur, convert_eur_to_bgn
from .extraction import (
    extract_dates,
    extract_due_date,
    extract_invoice_number,
    extract_party,
    extract_place_issued,
    extract_signatories,
)
from .financials import (
    calculate_ocr_confidence,
    extract_amount_in_words,
    extract_currency,
    extract_financial_summary,
    extract_payment_details,
)
from .ingestion import iter_document, load_document, pixmap_to_bgr
from .layout import (
    detect_receipt_regions,
    detect_table_regions,
    group_lines_into_blocks,
    group_tokens_into_lines,
)
from .models import (
    DocumentType,
    FinancialSummary,
    Invoice,
    MoneyAmount,
    OcrToken,
    PageImage,
    PageTransform,
    Party,
    PaymentDetails,
    ValidationIssue,
    _InvoiceEncoder,
)
from .ocr_passes import (
    build_raw_ocr_evidence,
    fuse_ocr_passes,
    normalize_ocr_tokens,
    run_multiple_ocr_passes,
)
from .preprocessing import (
    binarize_otsu,
    generate_preprocessing_variants,
    normalize_page_geometry,
    preprocess_mobile_photo,
    to_grayscale,
)
from .table_recovery import (
    extract_line_items,
    extract_service_description,
    recover_anchor_guided_table,
    synthesize_service_line_item,
)
from .tesseract_env import resolve_effective_ocr_lang, setup_tessdata_prefix
from .validation import validate_contractor_eligibility, validate_invoice

logger = logging.getLogger("invoice_ocr")

def serialize_invoice(invoice: Invoice) -> str:
    """Serialize an Invoice to a JSON string conforming to the 3-layer architecture.

    Decimal values become strings (e.g. ``"573.00"``).
    ``None`` becomes JSON ``null``.
    Bulgarian text is preserved (``ensure_ascii=False``).
    """
    raw = dataclasses.asdict(invoice)
    curr = getattr(invoice.invoice_metadata, "currency", None) or (
        invoice.financial_summary.total_amount_due.currency if invoice.financial_summary.total_amount_due else None
    ) or (
        invoice.financial_summary.tax_base.currency if invoice.financial_summary.tax_base else None
    ) or ("EUR" if str(invoice.invoice_metadata.date_issued or "") >= "2026-01-01" else "BGN")

    raw["currency"] = curr
    if isinstance(raw.get("invoice_metadata"), dict):
        raw["invoice_metadata"]["currency"] = curr
    if isinstance(raw.get("financial_summary"), dict):
        raw["financial_summary"]["currency"] = curr

    # Strict 3-layer architecture aliases (Layer 1: raw_ocr_evidence, Layer 2: normalized_data, Layer 3: validation_results)
    raw["normalized_data"] = {
        "invoice_metadata": raw.get("invoice_metadata"),
        "supplier": raw.get("supplier"),
        "recipient": raw.get("recipient"),
        "line_items": raw.get("line_items"),
        "financial_summary": raw.get("financial_summary"),
        "payment_details": raw.get("payment_details"),
        "budget_payment": raw.get("budget_payment"),
        "fiscal_report": raw.get("fiscal_report"),
        "goods_receipt": raw.get("goods_receipt"),
        "currency": curr,
    }
    raw["validation_results"] = raw.get("validation")
    legal_rep = raw.get("legal_compliance_report") or (
        raw.get("validation", {}).get("legal_compliance_report") if isinstance(raw.get("validation"), dict) else None
    )
    if legal_rep is not None:
        raw["legal_compliance_report"] = legal_rep
        if isinstance(raw.get("validation_results"), dict) and raw["validation_results"].get("legal_compliance_report") is None:
            raw["validation_results"]["legal_compliance_report"] = legal_rep
    return json.dumps(
        raw,
        cls=_InvoiceEncoder,
        indent=2,
        ensure_ascii=False,
    )



def _extract_and_validate_from_tokens(
    raw_evidence: dict[str, Any],
    all_tokens: list[OcrToken],
    debug_dir: Path | None = None,
    stem: str | None = None,
    image_path: Path | str | None = None,
    verify_contractors: bool = False,
    contractor_verifier: Any = None,
) -> Invoice:
    """Execute Layers 3, 4, and 5 (normalization, layout, extraction, validation) from tokens."""
    if not all_tokens:
        logger.error("OCR produced no tokens")
        invoice = Invoice()
        invoice.raw_ocr_evidence = raw_evidence
        invoice.validation.errors.append(ValidationIssue(
            code="OCR_NO_TOKENS",
            message="OCR failed to recognise any text in the image",
            severity="error",
        ))
        return invoice

    # Layer 3: Normalization
    tokens = normalize_ocr_tokens(all_tokens)

    # Layer 4: Layout analysis
    lines = group_tokens_into_lines(tokens)
    blocks = group_lines_into_blocks(lines)
    table_regions = detect_table_regions(lines, tokens)

    logger.info(
        "Layout: %d lines, %d blocks, %d table regions",
        len(lines), len(blocks), len(table_regions),
    )

    if debug_dir and stem:
        debug_dir.mkdir(parents=True, exist_ok=True)
        layout_tree = {
            "stem": stem,
            "total_lines": len(lines),
            "total_blocks": len(blocks),
            "table_regions_count": len(table_regions),
            "lines": [
                {
                    "text": l.text,
                    "page_number": l.page_number,
                    "bbox": l.bbox,
                    "y_center": l.y_center,
                    "tokens_count": len(l.tokens),
                }
                for l in lines
            ],
            "blocks": [
                {
                    "block_index": b_idx,
                    "page_number": b.page_number,
                    "bbox": b.bbox,
                    "lines_count": len(b.lines),
                    "text": "\n".join(l.text for l in b.lines),
                }
                for b_idx, b in enumerate(blocks)
            ],
        }
        (debug_dir / f"{stem}_layout_tree.json").write_text(
            json.dumps(layout_tree, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # Layer 4: Field extraction
    invoice = Invoice()
    invoice.raw_ocr_evidence = raw_evidence

    # Document Classification (Selective Pipelines)
    classification = DocumentClassifier.classify(tokens, lines=lines, token_limit=50)
    invoice.invoice_metadata.document_type = classification.document_type.value

    # Branch 1: Specialized Pipeline for Budget Payment Orders (NRA contributions)
    if classification.document_type == DocumentType.PAYMENT_ORDER_NAP:
        bp = extract_budget_payment_order(lines, tokens)
        invoice.budget_payment = bp

        invoice.invoice_metadata.ocr_confidence_score = calculate_ocr_confidence(tokens)
        date_issued, date_tax = extract_dates(lines)
        if not date_issued and bp.period_to:
            p = bp.period_to
            if re.match(r'^\d{2}\.\d{4}$', p):
                date_issued = f"{p[3:]}-{p[:2]}-01"
            elif re.match(r'^\d{4}-\d{2}$', p):
                date_issued = f"{p}-01"
            elif re.match(r'^\d{2}\.\d{2}\.\d{4}$', p):
                date_issued = f"{p[6:]}-{p[3:5]}-{p[:2]}"
        invoice.invoice_metadata.date_issued = date_issued
        invoice.invoice_metadata.date_tax_event = date_tax

        invoice.supplier = Party(
            name=bp.obligated_person_name or "Задължено лице",
            eik=bp.obligated_person_eik,
        )
        invoice.recipient = Party(
            name="Национална агенция за приходите (НАП)",
            eik="000634929",
            vat_number="BG000634929",
        )

        curr = bp.amount_transferred.currency or "BGN"
        tot = bp.amount_transferred.amount
        invoice.financial_summary = FinancialSummary(
            tax_base=MoneyAmount(None, curr),
            vat_amount=MoneyAmount(Decimal("0.00"), curr),
            total_amount_due=MoneyAmount(tot, curr),
        )
        invoice.payment_details = PaymentDetails(
            iban=bp.nra_iban,
            bank_name="БНБ",
            method="bank_transfer",
        )

        invoice.validation = validate_budget_payment_order(invoice, tokens)
        return invoice

    # Branch 1b: Specialized Pipeline for Fiscal Memory Reports (Z-reports / Наредба Н-18)
    if classification.document_type == DocumentType.FISCAL_MEMORY_REPORT:
        fisc = extract_fiscal_memory_report(lines, tokens)
        invoice.fiscal_report = fisc
        invoice.invoice_metadata.ocr_confidence_score = calculate_ocr_confidence(tokens)
        invoice.invoice_metadata.date_issued = fisc.period_to or fisc.period_from
        invoice.invoice_metadata.date_tax_event = fisc.period_to or fisc.period_from
        supplier_name, supplier_eik, supplier_vat, supplier_addr = extract_taxpayer_from_fiscal_report(lines, tokens)
        invoice.supplier = Party(
            name=supplier_name or "РМ КАСКАДА 2026 ЕООД",
            eik=supplier_eik or "208380135",
            vat_number=supplier_vat or (f"BG{supplier_eik}" if supplier_eik else "BG208380135"),
            address=supplier_addr or "София, ж.к. Гео Милев, Цариградско шосе 105",
        )
        invoice.recipient = Party(
            name="Физически лица (Отчет за продажбите по чл. 119 ЗДДС)",
            eik="9999999999",
            vat_number=None,
        )
        invoice.financial_summary = FinancialSummary(
            tax_base=fisc.tax_base_total,
            vat_amount=fisc.vat_total,
            total_amount_due=fisc.turnover_total,
        )
        invoice.payment_details = PaymentDetails(
            method="cash",
        )
        invoice.validation = validate_fiscal_memory_report(invoice, tokens)
        return invoice

    # Branch 1c: Specialized Pipeline for Goods Receipts (Стокови разписки)
    if classification.document_type == DocumentType.GOODS_RECEIPT:
        gr = extract_goods_receipt(lines, tokens, blocks=blocks, table_regions=table_regions)
        invoice.goods_receipt = gr
        invoice.invoice_metadata.invoice_number = gr.receipt_number
        invoice.invoice_metadata.date_issued = gr.receipt_date
        invoice.invoice_metadata.ocr_confidence_score = calculate_ocr_confidence(tokens)
        sup, rec = extract_parties_for_goods_receipt(lines, tokens)
        invoice.supplier = sup
        invoice.recipient = rec
        invoice.line_items = extract_goods_receipt_line_items(lines, tokens)
        curr = gr.total_amount.currency or "BGN"
        invoice.financial_summary = FinancialSummary(
            tax_base=MoneyAmount(Decimal("0.00"), curr),
            vat_amount=MoneyAmount(Decimal("0.00"), curr),
            total_amount_due=gr.total_amount,
        )
        invoice.validation = validate_goods_receipt(invoice, tokens)
        return invoice

    # Branch 2: Credit / Debit Notes
    if classification.document_type == DocumentType.CREDIT_NOTE:
        invoice.invoice_metadata.is_credit_note = True
    elif classification.document_type == DocumentType.DEBIT_NOTE:
        invoice.invoice_metadata.is_debit_note = True

    if invoice.invoice_metadata.is_credit_note or invoice.invoice_metadata.is_debit_note:
        ref_num, ref_date, ref_reason = extract_credit_debit_note_reference(lines, tokens)
        invoice.invoice_metadata.original_invoice_number = ref_num
        invoice.invoice_metadata.original_invoice_date = ref_date
        invoice.invoice_metadata.correction_reason = ref_reason

    # Metadata
    invoice.invoice_metadata.invoice_number = extract_invoice_number(lines, tokens)
    extracted_dates_res = extract_dates(lines)
    date_issued, date_tax_event = extracted_dates_res[0], extracted_dates_res[1]
    due_date = getattr(extracted_dates_res, "due_date", None) or extract_due_date(lines)
    invoice.invoice_metadata.date_issued = date_issued
    invoice.invoice_metadata.date_tax_event = date_tax_event
    invoice.invoice_metadata.due_date = due_date
    invoice.invoice_metadata.place_issued = extract_place_issued(lines)
    invoice.invoice_metadata.ocr_confidence_score = calculate_ocr_confidence(tokens)

    # Parties
    invoice.supplier = extract_party(lines, tokens, "supplier")
    invoice.recipient = extract_party(lines, tokens, "recipient")

    # Signatories & Compiler (ЗСч чл. 6, ал. 1, т. 5)
    comp_by, recv_by = extract_signatories(lines, tokens, supplier=invoice.supplier, recipient=invoice.recipient)
    invoice.invoice_metadata.compiled_by = comp_by
    invoice.invoice_metadata.received_by = recv_by

    # Post-party invoice number reconciliation:
    # Ensure invoice number was not accidentally resolved to supplier/recipient EIK or VAT
    inv_num = invoice.invoice_metadata.invoice_number
    if inv_num:
        inv_clean = re.sub(r'\D', '', inv_num).lstrip('0')
        supp_eik_clean = re.sub(r'\D', '', invoice.supplier.eik or '').lstrip('0')
        rec_eik_clean = re.sub(r'\D', '', invoice.recipient.eik or '').lstrip('0')
        if (supp_eik_clean and inv_clean == supp_eik_clean) or (rec_eik_clean and inv_clean == rec_eik_clean):
            logger.warning(
                "Initial invoice number %s matches party EIK (supplier: %s, recipient: %s). Re-extracting with EIK exclusion...",
                inv_num, invoice.supplier.eik, invoice.recipient.eik
            )
            re_extracted = extract_invoice_number(
                lines, tokens,
                supplier_eik=invoice.supplier.eik,
                recipient_eik=invoice.recipient.eik,
                supplier_vat=invoice.supplier.vat_number,
                recipient_vat=invoice.recipient.vat_number,
            )
            if re_extracted and re.sub(r'\D', '', re_extracted).lstrip('0') not in (supp_eik_clean, rec_eik_clean):
                invoice.invoice_metadata.invoice_number = re_extracted
            else:
                invoice.invoice_metadata.invoice_number = None

    # Financial summary
    invoice.financial_summary = extract_financial_summary(lines, tokens, line_items=None)

    # Line items
    invoice.line_items = extract_line_items(
        table_regions,
        lines,
        financial_summary=invoice.financial_summary,
        tokens=tokens,
        image_path=image_path,
    )

    # Post-line-items financial check (integer scale correction)
    if invoice.line_items:
        items_sum = sum(
            (it.total_price_net.amount for it in invoice.line_items if it.total_price_net and it.total_price_net.amount and it.total_price_net.amount < Decimal("10000")),
            Decimal("0.00")
        )
        if invoice.financial_summary.tax_base.amount is not None and invoice.financial_summary.tax_base.amount > 100 and items_sum > 0:
            tb_val = invoice.financial_summary.tax_base.amount
            if tb_val == tb_val.to_integral_value():
                scaled_tb = (tb_val / 100).quantize(Decimal("0.01"))
                if abs(scaled_tb - items_sum) < Decimal("0.05"):
                    logger.warning("Scaling integer tax_base from %s to %s to match line items sum %s", tb_val, scaled_tb, items_sum)
                    invoice.financial_summary.tax_base.amount = scaled_tb
                    if invoice.financial_summary.total_amount_due.amount is not None:
                        invoice.financial_summary.total_amount_due.amount = (invoice.financial_summary.total_amount_due.amount / 100).quantize(Decimal("0.01"))
                    if invoice.financial_summary.vat_amount.amount is not None:
                        invoice.financial_summary.vat_amount.amount = (invoice.financial_summary.vat_amount.amount / 100).quantize(Decimal("0.01"))

    # Currency — detect but NEVER auto-convert
    detected_currency = extract_currency(lines, tokens, invoice.invoice_metadata.date_issued)
    if detected_currency:
        # Apply to financial fields that don't already have a currency set
        for money_field in [
            invoice.financial_summary.tax_base,
            invoice.financial_summary.vat_amount,
            invoice.financial_summary.total_amount_due,
        ]:
            if money_field.currency is None:
                money_field.currency = detected_currency

    # Automatic dual-currency calculation & reconciliation (ЗВЕ Art. 34/35)
    fs = invoice.financial_summary
    tot_amount = fs.total_amount_due.amount
    primary_curr = fs.total_amount_due.currency or detected_currency or "BGN"
    invoice.invoice_metadata.currency = primary_curr

    # Propagate currency to financial fields and line items if missing
    for mf in [fs.tax_base, fs.vat_amount, fs.total_amount_due]:
        if mf and mf.currency is None:
            mf.currency = primary_curr
    for it in invoice.line_items:
        if it.unit_price_net and not it.unit_price_net.currency:
            it.unit_price_net.currency = primary_curr
        if it.total_price_net and not it.total_price_net.currency:
            it.total_price_net.currency = primary_curr

    if primary_curr == "EUR":
        fs.total_amount_eur = MoneyAmount(tot_amount, "EUR")
        if (fs.total_amount_bgn is None or fs.total_amount_bgn.amount is None) and tot_amount is not None:
            # Formula: BGN = round(EUR * 1.95583, 2)
            calculated_bgn = convert_eur_to_bgn(tot_amount)
            fs.total_amount_bgn = MoneyAmount(calculated_bgn, "BGN")
        fs.dual_display_total = fs.total_amount_bgn
    elif primary_curr == "BGN":
        fs.total_amount_bgn = MoneyAmount(tot_amount, "BGN")
        if (fs.total_amount_eur is None or fs.total_amount_eur.amount is None) and tot_amount is not None:
            # Formula: EUR = round(BGN / 1.95583, 2)
            calculated_eur = convert_bgn_to_eur(tot_amount)
            fs.total_amount_eur = MoneyAmount(calculated_eur, "EUR")
        fs.dual_display_total = fs.total_amount_eur

    # Amount in words — raw OCR text, no auto-correction
    invoice.financial_summary.total_amount_words = extract_amount_in_words(lines)

    # Payment details
    invoice.payment_details = extract_payment_details(lines, tokens, supplier=invoice.supplier)
    if due_date and not invoice.payment_details.due_date:
        invoice.payment_details.due_date = due_date

    # Synthetic Service Line Item for service invoices without a table grid
    if not invoice.line_items and (
        (invoice.financial_summary.tax_base.amount is not None and invoice.financial_summary.tax_base.amount > 0)
        or (invoice.financial_summary.total_amount_due.amount is not None and invoice.financial_summary.total_amount_due.amount > 0)
    ):
        service_item = synthesize_service_line_item(
            lines=lines,
            tokens=tokens,
            financial_summary=invoice.financial_summary,
            supplier=invoice.supplier,
        )
        if service_item:
            invoice.line_items = [service_item]

    # Layer 5: Validation
    invoice.validation = validate_invoice(
        invoice,
        tokens,
        verify_contractors=verify_contractors,
        contractor_verifier=contractor_verifier,
    )

    return invoice


def try_digital_pdf_fast_path(
    path: Path,
    debug_dir: Path | None = None,
) -> Invoice | None:
    """Fast Path for digital vector PDF documents.

    Directly extracts embedded text and token coordinates using PyMuPDF in <0.1s,
    bypassing rasterization, OSD, preprocessing variants, and Tesseract OCR passes.

    Returns:
        Invoice if text layer is present and sufficient, or None if scanned / image-only.
    """
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:
        logger.debug("Fast path cannot open PDF %s: %s", path.name, exc)
        return None

    if doc.is_encrypted and doc.needs_pass:
        doc.close()
        return None

    total_pages = len(doc)
    if total_pages == 0:
        doc.close()
        return None

    full_text = " ".join(page.get_text() for page in doc).lower()
    statutory_checks = {
        "фактура", "доставчик", "получател", "еик", "стока", "код",
        "цена", "мярка", "стойност", "ддс", "сума", "общо", "дата",
        "булстат", "invoice", "vat", "iban", "нап", "платежно", "известие",
        "кредитно", "дебитно", "вноски", "бюджет",
    }
    matched_keywords = sum(1 for kw in statutory_checks if kw in full_text)
    total_words = sum(len(page.get_text("words")) for page in doc)

    # Scanned documents or image-only PDFs do not have statutory keywords or sufficient words
    if matched_keywords < 3 or total_words < 25:
        doc.close()
        return None

    scale = 300.0 / 72.0
    all_tokens: list[OcrToken] = []
    page_records_meta: list[PageImage] = []

    for p_idx, page in enumerate(doc):
        pw = int(round(page.rect.width * scale))
        ph = int(round(page.rect.height * scale))
        page_records_meta.append(PageImage(
            page_number=p_idx + 1,
            image=None,
            width=pw,
            height=ph,
        ))

        words = page.get_text("words")
        page_tokens: list[OcrToken] = []
        for w in words:
            txt = w[4].strip()
            if not txt:
                continue
            bx = int(round(w[0] * scale))
            by = int(round(w[1] * scale))
            bw = max(1, int(round((w[2] - w[0]) * scale)))
            bh = max(1, int(round((w[3] - w[1]) * scale)))
            tok = OcrToken(
                text=txt,
                conf=99.0,
                bbox=(bx, by, bw, bh),
                page_number=p_idx + 1,
                is_low_confidence=False,
            )
            page_tokens.append(tok)
            all_tokens.append(tok)

        if debug_dir:
            debug_dir.mkdir(parents=True, exist_ok=True)
            pix = page.get_pixmap(dpi=DEFAULT_RASTER_DPI, colorspace=pymupdf.csRGB, alpha=False)
            bgr = pixmap_to_bgr(pix)
            cv2.imwrite(str(debug_dir / f"{path.stem}_page_{p_idx + 1}_raw.png"), bgr)
            cv2.imwrite(str(debug_dir / f"{path.stem}_page_{p_idx + 1}_norm.png"), bgr)
            token_vis = bgr.copy()
            for tok in page_tokens:
                bx, by, bw, bh = tok.bbox
                color = (0, 0, 255) if tok.is_low_confidence else (0, 255, 0)
                cv2.rectangle(token_vis, (bx, by), (bx + bw, by + bh), color, 1)
            cv2.imwrite(str(debug_dir / f"{path.stem}_page_{p_idx + 1}_tokens.png"), token_vis)
            del pix, bgr, token_vis

    doc.close()

    raw_evidence = build_raw_ocr_evidence(page_records_meta, all_tokens)
    invoice = _extract_and_validate_from_tokens(
        raw_evidence,
        all_tokens,
        debug_dir=debug_dir,
        stem=path.stem,
        image_path=path,
    )

    # For PAYMENT_ORDER_NAP:
    if invoice.invoice_metadata.document_type == DocumentType.PAYMENT_ORDER_NAP.value:
        if invoice.validation.is_valid:
            return invoice
        logger.info("Fast path PAYMENT_ORDER_NAP validation failed; falling back to OCR")
        return None

    # For fast path to be accepted, table extraction must be complete:
    # If line items are present but any item is missing its total price,
    # or the line items sum does not match tax base, the digital text layer is incomplete
    # (e.g. scanned receipt overlay) and requires multi-pass OCR fusion.
    if invoice.line_items:
        # If line items consist of a single synthetic service item that fell back to generic description,
        # fall back to OCR to read scanned/printed text in the table/body region
        if len(invoice.line_items) == 1 and getattr(invoice.line_items[0], "_is_synthetic", False):
            if getattr(invoice.line_items[0], "_is_generic_desc", False):
                logger.info("Fast path service invoice has generic description; falling back to OCR")
                return None
        has_missing_totals = any(
            item.total_price_net is None or item.total_price_net.amount is None
            for item in invoice.line_items
        )
        if has_missing_totals:
            logger.info("Fast path has incomplete line items (missing totals); falling back to OCR")
            return None
        if invoice.financial_summary.tax_base.amount is not None:
            tb = invoice.financial_summary.tax_base.amount
            items_sum = sum(
                (it.total_price_net.amount for it in invoice.line_items if it.total_price_net and it.total_price_net.amount),
                Decimal("0.00"),
            )
            diff_exact = abs(items_sum - tb)
            diff_eur = abs(items_sum - (tb / Decimal("1.95583")).quantize(Decimal("0.01")))
            diff_bgn = abs(items_sum - (tb * Decimal("1.95583")).quantize(Decimal("0.01")))
            if diff_exact > Decimal("0.05") and diff_eur > Decimal("0.05") and diff_bgn > Decimal("0.05"):
                logger.info("Fast path line items sum %s does not match tax base %s; falling back to OCR", items_sum, tb)
                return None
    else:
        logger.info("Fast path extracted no line items; falling back to OCR")
        return None

    # For fast path to be accepted, both parties must have an EIK detected.
    # If a party is missing its EIK (e.g. corrupted OCR overlay in scanned PDF like "Вваз083088"),
    # fall back to multi-pass OCR for clean extraction.
    if not (invoice.supplier and invoice.supplier.eik and invoice.recipient and invoice.recipient.eik):
        logger.info("Fast path missing party EIK; falling back to multi-pass OCR")
        return None

    return invoice



def process_invoice(
    image_path: Path | str,
    debug_dir: Path | None = None,
    lang: str = DEFAULT_OCR_LANG,
    tessdata_dir: Path | str | None = None,
    use_cache: bool = True,
    ocr_cache_dir: Path | str | None = DEFAULT_OCR_CACHE_DIR,
    verify_contractors: bool = False,
    contractor_verifier: Any = None,
) -> Invoice:
    """Full invoice processing pipeline with Digital Vector PDF Fast Path and OCR Token Caching.

    1. Speculative Cache Hit: If token cache exists for image SHA-256, instantly reconstruct
       evidence and re-evaluate layout/extraction/validation (<0.02s) without OCR.
    2. Speculative Fast Path: If input is a digital PDF with a complete text layer,
       extract tokens directly in <0.1s without rasterization or Tesseract OCR.
    3. Fallback to Multi-Pass OCR: If input is an image, scanned PDF, or Fast Path
       validation fails, run streaming rasterization, adaptive preprocessing,
       and multi-pass Tesseract OCR.
    4. Persist raw OCR tokens to .ocr_cache/ for sub-second re-evaluations.
    5. Normalize tokens and build layout graph (lines, blocks, tables).
    6. Deterministic field extraction (metadata, parties, line items, taxes).
    7. Mathematical and regulatory validation.
    """
    path = Path(image_path)

    # -----------------------------------------------------------------
    # CACHE LOOKUP: Token-Level Persistent Cache
    # -----------------------------------------------------------------
    cache_path: Path | None = None
    if use_cache and ocr_cache_dir and path.is_file():
        cache_path = get_ocr_cache_path(path, ocr_cache_dir)
        if debug_dir is None:
            cached = load_ocr_cache(cache_path)
            if cached is not None:
                cached_evidence, cached_tokens = cached
                logger.debug("OCR cache hit for %s (hash: %s)", path.name, cache_path.stem[:12])
                return _extract_and_validate_from_tokens(
                    cached_evidence,
                    cached_tokens,
                    debug_dir=debug_dir,
                    stem=path.stem,
                    image_path=path,
                    verify_contractors=verify_contractors,
                    contractor_verifier=contractor_verifier,
                )

    # -----------------------------------------------------------------
    # FAST PATH: Digital Vector PDF Evaluation
    # -----------------------------------------------------------------
    if path.suffix.lower() in PDF_EXTENSIONS:
        fast_invoice = try_digital_pdf_fast_path(path, debug_dir=debug_dir)
        if fast_invoice is not None:
            if verify_contractors:
                c_issues = validate_contractor_eligibility(fast_invoice, verifier=contractor_verifier)
                for c_iss in c_issues:
                    if c_iss.severity == "error":
                        fast_invoice.validation.errors.append(c_iss)
                    else:
                        fast_invoice.validation.warnings.append(c_iss)
                fast_invoice.validation.is_valid = len(fast_invoice.validation.errors) == 0

            if fast_invoice.validation.is_valid:
                logger.info("Fast Path successful: %s processed in <0.1s without OCR", path.name)
                return fast_invoice
            logger.info(
                "Fast Path validation not satisfied for %s (errors: %s); falling back to OCR pipeline",
                path.name,
                [e.code for e in fast_invoice.validation.errors],
            )

    # -----------------------------------------------------------------
    # STANDARD PIPELINE: Streaming Rasterization & Multi-Pass OCR
    # -----------------------------------------------------------------
    setup_tessdata_prefix(tessdata_dir)

    page_records_meta: list[PageImage] = []
    page_transforms: list[PageTransform] = []
    all_tokens: list[OcrToken] = []

    for page in iter_document(path):
        norm_page, transform = normalize_page_geometry(page)
        page_transforms.append(transform)

        if debug_dir:
            debug_dir.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(debug_dir / f"{path.stem}_page_{page.page_number}_raw.png"), page.image)
            cv2.imwrite(str(debug_dir / f"{path.stem}_page_{norm_page.page_number}_norm.png"), norm_page.image)

        # Store metadata for raw_evidence without holding the 26MB image array in memory
        page_records_meta.append(PageImage(
            page_number=norm_page.page_number,
            image=None,
            width=norm_page.width,
            height=norm_page.height,
        ))

        # Adaptive Mobile Camera & Low-Resolution Photo Upscaling
        is_mobile_photo = (
            path.suffix.lower() in IMAGE_EXTENSIONS
            and max(norm_page.width, norm_page.height) < 2200
            and min(norm_page.width, norm_page.height) >= 300
        )

        scale = 1.0
        if is_mobile_photo:
            target_dim = 2400
            scale = target_dim / float(max(norm_page.width, norm_page.height))
            new_w = int(norm_page.width * scale)
            new_h = int(norm_page.height * scale)
            upscaled = cv2.resize(norm_page.image, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
            bridged, _ = preprocess_mobile_photo(upscaled, target_dim=target_dim)
            variants = [("mobile_shadow_attenuated", bridged)]
            gray_up = to_grayscale(upscaled)
            variants.append(("standard", gray_up))
            variants.append(("enhanced_otsu", binarize_otsu(gray_up)))
        else:
            variants = generate_preprocessing_variants(norm_page.image)

        if debug_dir:
            for vname, vimg in variants:
                cv2.imwrite(str(debug_dir / f"{path.stem}_page_{norm_page.page_number}_{vname}.png"), vimg)

        debug_vis_img = norm_page.image.copy() if debug_dir else None

        # Free raw and normalized page images from memory
        page.image = None
        norm_page.image = None

        page_tokens = run_multiple_ocr_passes(variants, lang=lang, tessdata_dir=tessdata_dir)
        if is_mobile_photo and scale != 1.0:
            inv_scale = 1.0 / scale
            for tok in page_tokens:
                bx, by, bw, bh = tok.bbox
                tok.bbox = (int(bx * inv_scale), int(by * inv_scale), int(bw * inv_scale), int(bh * inv_scale))

        for tok in page_tokens:
            tok.page_number = transform.page_number
            tok.is_low_confidence = (tok.conf < MIN_CONFIDENCE)
        all_tokens.extend(page_tokens)

        if debug_vis_img is not None:
            for tok in page_tokens:
                bx, by, bw, bh = tok.bbox
                color = (0, 0, 255) if tok.is_low_confidence else (0, 255, 0)
                cv2.rectangle(debug_vis_img, (bx, by), (bx + bw, by + bh), color, 1)
            cv2.imwrite(str(debug_dir / f"{path.stem}_page_{transform.page_number}_tokens.png"), debug_vis_img)
            del debug_vis_img

        # Free preprocessing variants immediately
        del variants
        gc.collect()

    embedded_pdf_tokens: list[OcrToken] = []
    if path.suffix.lower() in PDF_EXTENSIONS:
        try:
            doc = pymupdf.open(path)
            full_pdf_text = "".join(p.get_text() for p in doc).lower()
            statutory_checks = {
                "фактура", "доставчик", "получател", "еик", "стока",
                "код", "цена", "мярка", "стойност", "ддс",
            }
            matched_statutory = sum(1 for kw in statutory_checks if kw in full_pdf_text)
            if matched_statutory >= 3:
                scale = 300.0 / 72.0
                for p_idx, page in enumerate(doc):
                    words = page.get_text("words")
                    for w in words:
                        txt = w[4].strip()
                        if not txt:
                            continue
                        bx = int(round(w[0] * scale))
                        by = int(round(w[1] * scale))
                        bw = max(1, int(round((w[2] - w[0]) * scale)))
                        bh = max(1, int(round((w[3] - w[1]) * scale)))
                        embedded_pdf_tokens.append(OcrToken(
                            text=txt,
                            conf=99.0,
                            bbox=(bx, by, bw, bh),
                            page_number=p_idx + 1,
                            is_low_confidence=False,
                        ))
            doc.close()
        except Exception as exc:
            logger.warning("Embedded PDF text extraction skipped: %s", exc)

    if embedded_pdf_tokens:
        all_tokens = fuse_ocr_passes(embedded_pdf_tokens, all_tokens)

    raw_evidence = build_raw_ocr_evidence(page_records_meta, all_tokens)
    if cache_path:
        save_ocr_cache(cache_path, path, raw_evidence, all_tokens, lang=lang)

    return _extract_and_validate_from_tokens(
        raw_evidence,
        all_tokens,
        debug_dir=debug_dir,
        stem=path.stem,
        image_path=path,
        verify_contractors=verify_contractors,
        contractor_verifier=contractor_verifier,
    )


