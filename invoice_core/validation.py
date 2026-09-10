"""Statutory, mathematical, structural, and contractor validation engine."""
from __future__ import annotations

import datetime
from decimal import Decimal, ROUND_HALF_UP
import logging
import re
from typing import Any

from .classification import validate_budget_payment_order
from .constants import (
    DUAL_DISPLAY_END_DATE,
    EUR_MANDATORY_DATE,
    FIXED_EUR_BGN_RATE,
    MIN_CONFIDENCE,
    TOTAL_TOLERANCE,
    VAT_TOLERANCE,
    ZDDS_DISCOUNT_TOLERANCE,
)
from .currency import convert_eur_to_bgn, verify_dual_currency_parity
from .financials import _detect_all_currencies
from .legal_compliance import audit_legal_compliance
from .models import DocumentType, Invoice, MoneyAmount, OcrToken, ValidationIssue, ValidationResult
from .normalizers import is_valid_eik9, is_valid_eik13, sanitize_vat_rate, validate_eik, validate_iban_modulo97
from .vendor_profiles import get_recapitulation_eiks

logger = logging.getLogger("invoice_ocr")

def _validate_required_fields(invoice: Invoice) -> list[ValidationIssue]:
    """Check for missing critical fields."""
    if invoice.invoice_metadata.document_type == DocumentType.PAYMENT_ORDER_NAP.value:
        return []

    issues: list[ValidationIssue] = []

    if not invoice.invoice_metadata.invoice_number:
        issues.append(ValidationIssue(
            code="MISSING_INVOICE_NUMBER",
            message="Invoice number was not detected",
            severity="error",
            field="invoice_metadata.invoice_number",
        ))

    if not invoice.invoice_metadata.date_issued:
        issues.append(ValidationIssue(
            code="MISSING_DATE_ISSUED",
            message="Issue date was not detected",
            severity="error",
            field="invoice_metadata.date_issued",
        ))

    if not invoice.supplier.name:
        issues.append(ValidationIssue(
            code="MISSING_SUPPLIER_NAME",
            message="Supplier name was not detected",
            severity="error",
            field="supplier.name",
        ))

    if not invoice.supplier.eik:
        issues.append(ValidationIssue(
            code="MISSING_SUPPLIER_EIK",
            message="Supplier EIK/BULSTAT was not detected (Art. 114(1)(2) VAT Act / ЗДДС)",
            severity="error",
            field="supplier.eik",
        ))

    if not invoice.recipient.name:
        issues.append(ValidationIssue(
            code="MISSING_RECIPIENT_NAME",
            message="Recipient name was not detected",
            severity="error",
            field="recipient.name",
        ))

    # Reference check for corrective documents under Art. 115(4) VAT Act (ЗДДС)
    if invoice.invoice_metadata.is_credit_note or invoice.invoice_metadata.is_debit_note:
        if not invoice.invoice_metadata.original_invoice_number:
            issues.append(ValidationIssue(
                code="MISSING_CORRECTED_INVOICE_REFERENCE",
                message=(
                    "Credit/Debit note under Art. 115(4) VAT Act (ЗДДС) must reference "
                    "the number of the original invoice being corrected"
                ),
                severity="warning",
                field="invoice_metadata.original_invoice_number",
            ))

    return issues


def _validate_identifiers(invoice: Invoice) -> list[ValidationIssue]:
    """Validate EIK, VAT number formats, and statutory invoice number independence."""
    issues: list[ValidationIssue] = []

    # Party separation invariant: Supplier and Recipient cannot share the same EIK
    if invoice.supplier and invoice.recipient:
        sup_eik = re.sub(r'\D', '', invoice.supplier.eik or '')
        rec_eik = re.sub(r'\D', '', invoice.recipient.eik or '')
        if sup_eik and rec_eik and sup_eik == rec_eik:
            issues.append(ValidationIssue(
                code="PARTY_COLLISION_SAME_EIK",
                message=(
                    f"Supplier EIK '{invoice.supplier.eik}' is identical to Recipient EIK '{invoice.recipient.eik}'. "
                    f"Under Bulgarian invoicing rules (Art. 114 ЗДДС), recipient and supplier must be distinct legal entities."
                ),
                severity="error",
                field="recipient.eik",
                detected_value=invoice.recipient.eik,
                expected_value="Distinct legal entity EIK",
            ))

    for role, party in [("supplier", invoice.supplier), ("recipient", invoice.recipient)]:
        if party.eik:
            digits = re.sub(r'\D', '', party.eik)
            if len(digits) not in (9, 10, 13):
                issues.append(ValidationIssue(
                    code="INVALID_EIK_FORMAT",
                    message=f"{role.title()} EIK '{party.eik}' does not match expected 9/13 digit format",
                    severity="warning",
                    field=f"{role}.eik",
                    detected_value=party.eik,
                ))

        if party.vat_number:
            if not re.match(r'^BG\d{9,13}$', party.vat_number):
                issues.append(ValidationIssue(
                    code="INVALID_VAT_FORMAT",
                    message=f"{role.title()} VAT number '{party.vat_number}' does not match BG + 9-13 digits",
                    severity="warning",
                    field=f"{role}.vat_number",
                    detected_value=party.vat_number,
                ))

    # Statutory validation: Invoice number cannot equal party EIK (Art. 6 ЗСч / Art. 114 ЗДДС)
    inv_num = invoice.invoice_metadata.invoice_number
    if inv_num:
        inv_clean = re.sub(r'\D', '', inv_num).lstrip('0')
        for role, party in [("supplier", invoice.supplier), ("recipient", invoice.recipient)]:
            if party and party.eik:
                party_eik_clean = re.sub(r'\D', '', party.eik).lstrip('0')
                if party_eik_clean and inv_clean == party_eik_clean:
                    issues.append(ValidationIssue(
                        code="INVOICE_NUMBER_MATCHES_EIK",
                        message=(
                            f"Invoice number '{inv_num}' is identical to {role} EIK '{party.eik}'. "
                            f"Under Art. 6 Accountancy Act (ЗСч) and Art. 114 VAT Act (ЗДДС), "
                            f"the invoice number must be a distinct sequential document identifier."
                        ),
                        severity="error",
                        field="invoice_metadata.invoice_number",
                        detected_value=inv_num,
                        expected_value=None,
                    ))

    return issues


def _validate_dates(invoice: Invoice) -> list[ValidationIssue]:
    """Validate date formats and plausibility.

    Statutory semantic validation (ЗДДС чл. 25, чл. 114):
    - date_issued (чл. 114): must be valid format and <= today (FUTURE_DATE error if > today).
    - date_tax_event (чл. 25): must be valid format and <= today (FUTURE_DATE error if > today).
    - due_date (падеж): must be valid format if present.
      CRITICAL INVARIANT: due_date represents payment terms and is NOT checked for FUTURE_DATE,
      as payment due dates in commercial contracts legitimately occur in the future.
    """
    issues: list[ValidationIssue] = []
    today = datetime.date.today()

    for field_name, date_val in [
        ("date_issued", invoice.invoice_metadata.date_issued),
        ("date_tax_event", invoice.invoice_metadata.date_tax_event),
    ]:
        if date_val:
            if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_val):
                issues.append(ValidationIssue(
                    code="INVALID_DATE_FORMAT",
                    message=f"Date '{date_val}' is not in YYYY-MM-DD format",
                    severity="warning",
                    field=f"invoice_metadata.{field_name}",
                    detected_value=date_val,
                ))
            else:
                try:
                    dt = datetime.date.fromisoformat(date_val)
                    if dt > today:
                        issues.append(ValidationIssue(
                            code="FUTURE_DATE",
                            message=f"Date '{date_val}' is in the future (today is {today.isoformat()})",
                            severity="error",
                            field=f"invoice_metadata.{field_name}",
                            detected_value=date_val,
                            expected_value=f"<= {today.isoformat()}",
                        ))
                except (ValueError, TypeError):
                    issues.append(ValidationIssue(
                        code="INVALID_DATE",
                        message=f"Date '{date_val}' is not a valid calendar date",
                        severity="error",
                        field=f"invoice_metadata.{field_name}",
                        detected_value=date_val,
                    ))

    # Validate due_date (падеж) format and calendar validity WITHOUT FUTURE_DATE check
    due_field = "invoice_metadata.due_date" if invoice.invoice_metadata.due_date else "payment_details.due_date"
    due_date = invoice.invoice_metadata.due_date or invoice.payment_details.due_date
    if due_date:
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', due_date):
            issues.append(ValidationIssue(
                code="INVALID_DATE_FORMAT",
                message=f"Due date '{due_date}' is not in YYYY-MM-DD format",
                severity="warning",
                field=due_field,
                detected_value=due_date,
            ))
        else:
            try:
                datetime.date.fromisoformat(due_date)
                # Note: due_date > today is explicitly allowed and expected for future payment terms!
            except (ValueError, TypeError):
                issues.append(ValidationIssue(
                    code="INVALID_DATE",
                    message=f"Due date '{due_date}' is not a valid calendar date",
                    severity="error",
                    field=due_field,
                    detected_value=due_date,
                ))

    return issues


def _validate_line_items(invoice: Invoice) -> list[ValidationIssue]:
    """Validate individual line items for mathematical consistency."""
    issues: list[ValidationIssue] = []

    for i, item in enumerate(invoice.line_items):
        field_prefix = f"line_items[{i}]"

        # Check description (Feature 19: Strict Null Fallback)
        banned_desc = {"item", "unknown", "placeholder", "n/a", "none", "артикул", "null", ""}
        desc_clean = item.description.strip().lower() if item.description else ""
        if not desc_clean or desc_clean in banned_desc:
            item.description = None
            issues.append(ValidationIssue(
                code="MISSING_DESCRIPTION",
                message=f"Line {i+1} has missing or unresolvable description due to occlusion or illegibility",
                severity="warning",
                field=f"{field_prefix}.description",
                detected_value=None,
                expected_value="Valid item description",
            ))

        u_price = item.unit_price_net.amount if isinstance(item.unit_price_net, MoneyAmount) else item.unit_price_net
        t_price = item.total_price_net.amount if isinstance(item.total_price_net, MoneyAmount) else item.total_price_net

        # Check quantity × unit_price ≈ total_price
        if (
            item.quantity is not None
            and u_price is not None
            and t_price is not None
        ):
            expected = (item.quantity * u_price).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP,
            )
            diff = abs(expected - t_price)
            if invoice.invoice_metadata.is_credit_note:
                diff = min(diff, abs(abs(expected) - abs(t_price)))
            line_tolerance = ZDDS_DISCOUNT_TOLERANCE if (
                len(invoice.line_items) > 15
                or getattr(invoice.financial_summary.tax_base, "currency", None) == "EUR"
                or getattr(invoice.financial_summary.total_amount_due, "currency", None) == "EUR"
                or getattr(item, "discount_pct", None) is not None
            ) else VAT_TOLERANCE
            if diff > line_tolerance:
                issues.append(ValidationIssue(
                    code="LINE_ITEM_CALC_MISMATCH",
                    message=(
                        f"Line {i+1}: quantity ({item.quantity}) × unit_price "
                        f"({u_price}) = {expected}, but total is "
                        f"{t_price}"
                    ),
                    severity="warning",
                    field=f"{field_prefix}.total_price_net",
                    detected_value=str(t_price),
                    expected_value=str(expected),
                    difference=str(diff),
                ))

        # Negative quantity (permitted on credit notes)
        if not invoice.invoice_metadata.is_credit_note and item.quantity is not None and item.quantity < 0:
            issues.append(ValidationIssue(
                code="NEGATIVE_QUANTITY",
                message=f"Line {i+1} has negative quantity: {item.quantity}",
                severity="warning",
                field=f"{field_prefix}.quantity",
                detected_value=str(item.quantity),
            ))

        # Negative price (permitted on credit notes)
        if not invoice.invoice_metadata.is_credit_note and u_price is not None and u_price < 0:
            issues.append(ValidationIssue(
                code="NEGATIVE_PRICE",
                message=f"Line {i+1} has negative unit price: {u_price}",
                severity="warning",
                field=f"{field_prefix}.unit_price_net",
                detected_value=str(u_price),
            ))

        # VAT rate plausibility
        if item.vat_rate_pct is not None:
            sanitized = sanitize_vat_rate(item.vat_rate_pct)
            if sanitized is not None:
                item.vat_rate_pct = sanitized
            if item.vat_rate_pct < 0 or item.vat_rate_pct > 25:
                issues.append(ValidationIssue(
                    code="VAT_RATE_IMPLAUSIBLE",
                    message=f"Line {i+1}: VAT rate {item.vat_rate_pct}% is outside plausible range (0-25%)",
                    severity="warning",
                    field=f"{field_prefix}.vat_rate_pct",
                    detected_value=str(item.vat_rate_pct),
                ))

    return issues


def _validate_totals(invoice: Invoice) -> list[ValidationIssue]:
    """Validate financial summary consistency.

    Checks:
        1. sum(line_items.total_price_net) ≈ tax_base
        2. tax_base × inferred_vat_rate ≈ vat_amount
        3. tax_base + vat_amount ≈ total_amount_due
    """
    issues: list[ValidationIssue] = []
    fs = invoice.financial_summary

    # Dynamic tolerance for multi-item invoices, volume discounts, EUR conversion (Art. 26 ZDDS)
    is_eur = (
        getattr(fs.tax_base, "currency", None) == "EUR"
        or getattr(fs.total_amount_due, "currency", None) == "EUR"
    )
    has_discounts = any(
        getattr(item, "discount_pct", None) is not None
        or (item.description and any(kw in item.description.lower() for kw in ("отстъпка", "отст.", "discount", "рабат")))
        for item in invoice.line_items
    )
    recap_eiks = get_recapitulation_eiks()
    is_recap_vendor = (
        (invoice.supplier and invoice.supplier.eik in recap_eiks)
        or ("МЕТРО" in ((invoice.supplier.name if invoice.supplier else "") or "").upper())
    )
    if len(invoice.line_items) > 15 and (has_discounts or is_eur or is_recap_vendor):
        cur_vat_tol = ZDDS_DISCOUNT_TOLERANCE
        cur_tot_tol = ZDDS_DISCOUNT_TOLERANCE
    else:
        cur_vat_tol = VAT_TOLERANCE
        cur_tot_tol = TOTAL_TOLERANCE

    # 1. Line items total vs tax base
    items_with_total = [
        item for item in invoice.line_items
        if item.total_price_net is not None and item.total_price_net.amount is not None
    ]
    if items_with_total and fs.tax_base.amount is not None:
        items_sum = sum(
            ((item.total_price_net.amount if isinstance(item.total_price_net, MoneyAmount) else item.total_price_net) or Decimal(0))
            for item in items_with_total
        )
        peg = Decimal("1.95583")
        bgn_to_eur = (items_sum / peg).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        eur_to_bgn = (items_sum * peg).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        tb_val = fs.tax_base.amount
        tb_eur = tb_val if getattr(fs.tax_base, "currency", None) == "EUR" else (tb_val / peg).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        tb_bgn = (tb_val * peg).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if getattr(fs.tax_base, "currency", None) == "EUR" else tb_val
        max_allowed_tb = max(tb_eur, tb_bgn)

        all_items_have_total = (len(items_with_total) == len(invoice.line_items))

        diff = abs(items_sum - tb_val)
        if invoice.invoice_metadata.is_credit_note:
            diff = min(diff, abs(abs(items_sum) - abs(tb_val)))
        is_dual_match = (
            diff <= cur_tot_tol
            or abs(bgn_to_eur - tb_val) <= Decimal("0.50")
            or abs(eur_to_bgn - tb_val) <= Decimal("0.50")
            or abs(items_sum - tb_bgn) <= Decimal("0.50")
            or abs(items_sum - tb_eur) <= Decimal("0.50")
        )

        if not is_dual_match:
            # If some items are missing totals (e.g. occluded by receipt or unreadable in faint print),
            # the partial sum is valid as long as it does not exceed the tax base
            if not all_items_have_total and (items_sum <= max_allowed_tb * Decimal("1.02")):
                pass
            elif (
                not all_items_have_total
                or (getattr(invoice.invoice_metadata, "ocr_confidence_score", 1.0) or 1.0) < 0.70
                or (invoice.supplier and invoice.supplier.eik in recap_eiks)
                or ("МЕТРО" in (invoice.supplier.name or "").upper())
            ):
                issues.append(ValidationIssue(
                    code="LINE_ITEMS_TOTAL_MISMATCH",
                    message=(
                        f"Sum of line item totals ({items_sum}) does not match "
                        f"tax base ({fs.tax_base.amount}) on low-confidence/partial table"
                    ),
                    severity="warning",
                    field="financial_summary.tax_base",
                    detected_value=str(fs.tax_base.amount),
                    expected_value=str(items_sum),
                    difference=str(diff),
                ))
            else:
                issues.append(ValidationIssue(
                    code="LINE_ITEMS_TOTAL_MISMATCH",
                    message=(
                        f"Sum of line item totals ({items_sum}) does not match "
                        f"tax base ({fs.tax_base.amount})"
                    ),
                    severity="error",
                    field="financial_summary.tax_base",
                    detected_value=str(fs.tax_base.amount),
                    expected_value=str(items_sum),
                    difference=str(diff),
                ))

    # 2. VAT calculation
    if fs.tax_base.amount is not None and fs.vat_amount.amount is not None and fs.tax_base.amount != Decimal("0.00"):
        vat_20 = (fs.tax_base.amount * Decimal("0.20")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        vat_9 = (fs.tax_base.amount * Decimal("0.09")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        vat_0 = Decimal("0.00")
        
        diff_20 = abs(vat_20 - fs.vat_amount.amount)
        diff_9 = abs(vat_9 - fs.vat_amount.amount)
        diff_0 = abs(vat_0 - fs.vat_amount.amount)

        if invoice.invoice_metadata.is_credit_note:
            diff_20 = min(diff_20, abs(abs(vat_20) - abs(fs.vat_amount.amount)))
            diff_9 = min(diff_9, abs(abs(vat_9) - abs(fs.vat_amount.amount)))
            diff_0 = min(diff_0, abs(fs.vat_amount.amount))

        if diff_20 > cur_vat_tol and diff_9 > cur_vat_tol and diff_0 > cur_vat_tol:
            issues.append(ValidationIssue(
                code="VAT_CALCULATION_MISMATCH",
                message=(
                    f"VAT amount ({fs.vat_amount.amount}) does not match "
                    f"tax base ({fs.tax_base.amount}) at 20%, 9%, or 0% rate"
                ),
                severity="error",
                field="financial_summary.vat_amount",
                detected_value=str(fs.vat_amount.amount),
                expected_value=f"~{vat_20} (at 20%)",
            ))

    # 3. Total = tax_base + vat_amount
    if (
        fs.tax_base.amount is not None
        and fs.vat_amount.amount is not None
        and fs.total_amount_due.amount is not None
    ):
        expected_total = fs.tax_base.amount + fs.vat_amount.amount
        diff = abs(expected_total - fs.total_amount_due.amount)
        if invoice.invoice_metadata.is_credit_note:
            diff = min(
                diff,
                abs(abs(fs.tax_base.amount) + abs(fs.vat_amount.amount) - abs(fs.total_amount_due.amount)),
                abs(abs(expected_total) - abs(fs.total_amount_due.amount)),
            )
        if diff > cur_tot_tol:
            issues.append(ValidationIssue(
                code="TOTAL_SUM_MISMATCH",
                message=(
                    f"Tax base ({fs.tax_base.amount}) + VAT ({fs.vat_amount.amount}) "
                    f"= {expected_total}, but total is {fs.total_amount_due.amount}"
                ),
                severity="error",
                field="financial_summary.total_amount_due",
                detected_value=str(fs.total_amount_due.amount),
                expected_value=str(expected_total),
                difference=str(diff),
            ))

    # Zero total warning
    if fs.total_amount_due.amount is not None and fs.total_amount_due.amount == 0:
        issues.append(ValidationIssue(
            code="ZERO_TOTAL",
            message="Total amount due is zero",
            severity="warning",
            field="financial_summary.total_amount_due",
            detected_value="0",
        ))

    return issues


def _validate_currency(invoice: Invoice, tokens: list[OcrToken] | None = None) -> list[ValidationIssue]:
    """Validate currency consistency and euro-transition logic.

    This is the CRITICAL currency validation that:
        - Checks consistency across all MoneyAmount fields
        - Detects BGN usage after euro adoption (2026-01-01)
        - Detects BGN usage after dual-display period (2026-08-08)
        - Detects dual-currency documents
        - Checks amount-in-words currency vs numeric currency
        - NEVER auto-converts BGN → EUR
    """
    issues: list[ValidationIssue] = []
    fs = invoice.financial_summary

    # Collect all currencies from financial fields
    field_currencies: dict[str, str | None] = {
        "tax_base": fs.tax_base.currency,
        "vat_amount": fs.vat_amount.currency,
        "total_amount_due": fs.total_amount_due.currency,
    }
    non_null_currencies = {
        v for v in field_currencies.values() if v is not None
    }

    # Check consistency
    if len(non_null_currencies) > 1:
        issues.append(ValidationIssue(
            code="CURRENCY_MISMATCH",
            message=(
                f"Inconsistent currencies across financial fields: "
                f"{dict((k, v) for k, v in field_currencies.items() if v)}"
            ),
            severity="warning",
            field="financial_summary",
        ))

    primary_currency = non_null_currencies.pop() if len(non_null_currencies) == 1 else None

    # Detect all currencies in the document
    all_detected = _detect_all_currencies(tokens or [])

    # Dual currency detection
    if "EUR" in all_detected and "BGN" in all_detected:
        issues.append(ValidationIssue(
            code="DUAL_CURRENCY_DETECTED",
            message="Both EUR and BGN are present in the document",
            severity="warning",
        ))

    # Euro transition logic
    date_issued = invoice.invoice_metadata.date_issued
    if date_issued and primary_currency == "BGN":
        if date_issued >= DUAL_DISPLAY_END_DATE:
            issues.append(ValidationIssue(
                code="CURRENCY_AFTER_DUAL_PERIOD",
                message=(
                    f"BGN detected as primary currency on {date_issued}, "
                    f"after the mandatory dual-display period ended "
                    f"({DUAL_DISPLAY_END_DATE}). EUR should be the primary "
                    f"sales/payable currency. BGN may appear only as an "
                    f"informational equivalent."
                ),
                severity="warning",
                field="financial_summary",
                detected_value="BGN",
            ))
        elif date_issued >= EUR_MANDATORY_DATE:
            issues.append(ValidationIssue(
                code="CURRENCY_POST_EURO_BGN_DETECTED",
                message=(
                    f"BGN detected as primary currency on {date_issued}, "
                    f"after euro adoption date ({EUR_MANDATORY_DATE}). "
                    f"This may indicate: (a) an informational BGN reference, "
                    f"(b) a pre-euro document with incorrect date, or "
                    f"(c) an OCR error. The value has NOT been auto-converted."
                ),
                severity="warning",
                field="financial_summary",
                detected_value="BGN",
                expected_value="EUR",
            ))

    # Dual currency total parity check under Euro Introduction Act (ЗВЕ Art. 34/35):
    # Formula: BGN = round(EUR * 1.95583, 2)
    eur_val = (
        fs.total_amount_eur.amount
        if (fs.total_amount_eur and fs.total_amount_eur.amount is not None)
        else (
            fs.total_amount_due.amount
            if (fs.total_amount_due and fs.total_amount_due.currency == "EUR" and fs.total_amount_due.amount is not None)
            else None
        )
    )
    bgn_val = (
        fs.total_amount_bgn.amount
        if (fs.total_amount_bgn and fs.total_amount_bgn.amount is not None)
        else (
            fs.total_amount_due.amount
            if (fs.total_amount_due and fs.total_amount_due.currency == "BGN" and fs.total_amount_due.amount is not None)
            else None
        )
    )
    if eur_val is not None and bgn_val is not None:
        expected_bgn = convert_eur_to_bgn(eur_val)
        diff = abs(bgn_val - expected_bgn)
        if diff > TOTAL_TOLERANCE:
            issues.append(ValidationIssue(
                code="DUAL_CURRENCY_PARITY_MISMATCH",
                message=(
                    f"Dual-currency total parity mismatch under Euro Introduction Act (ЗВЕ): "
                    f"{eur_val} EUR × {FIXED_EUR_BGN_RATE} = {expected_bgn} BGN, "
                    f"but BGN total is {bgn_val} (difference: {diff})"
                ),
                severity="warning",
                field="financial_summary",
                detected_value=str(bgn_val),
                expected_value=str(expected_bgn),
                difference=str(diff),
            ))

    return issues


def _validate_amount_in_words(invoice: Invoice) -> list[ValidationIssue]:
    """Check amount-in-words currency against the numeric currency.

    E.g. words say ``лева и стотинки`` but amounts are in EUR.
    """
    issues: list[ValidationIssue] = []
    words = invoice.financial_summary.total_amount_words
    if not words:
        return issues

    words_lower = words.lower()

    # Isolate the main words clause from informational conversion notes
    # e.g. "тридесет и три . 32ЕЦ Сума вОМ: 6517"
    words_primary = re.sub(
        r'(?i)\b(?:сума\s+(?:в\s+)?(?:вом|bom|бгн|bgn|bgr|бгр|лв\.?|eur|евро)|равностойност\b.*?)(?:$|[;,.|])',
        '',
        words_lower,
    ).strip()

    # Detect currency in words
    words_has_bgn = bool(re.search(
        r'(?i)\b(?:лева|лев|стотинк\w*|лв\.?|bgn|бгн|bgr|бгр|bom|вом)\b',
        words_primary,
    ))
    words_has_eur = bool(re.search(
        r'(?i)(?:\b(?:евро|евроцент\w*|цента|euro|eob|еоб|eub|еуб)\b|\w*евро\w*|(?:\b|\d)(?:е\.ц\.?|ец|еш\??)(?:\b|\W))',
        words_primary,
    ))

    primary_currency = invoice.financial_summary.total_amount_due.currency

    if primary_currency == "EUR" and words_has_bgn and not words_has_eur:
        issues.append(ValidationIssue(
            code="AMOUNT_WORDS_CURRENCY_MISMATCH",
            message=(
                f"Amount-in-words mentions BGN ('лева'/'стотинки') but "
                f"numeric currency is EUR. The text has NOT been auto-corrected."
            ),
            severity="warning",
            field="financial_summary.total_amount_words",
            detected_value=words[:100],
            expected_value="EUR-denominated text",
        ))

    if primary_currency == "BGN" and words_has_eur and not words_has_bgn:
        issues.append(ValidationIssue(
            code="AMOUNT_WORDS_CURRENCY_MISMATCH",
            message=(
                f"Amount-in-words mentions EUR ('евро') but numeric "
                f"currency is BGN."
            ),
            severity="warning",
            field="financial_summary.total_amount_words",
            detected_value=words[:100],
            expected_value="BGN-denominated text",
        ))

    return issues


def _validate_ocr_confidence(
    invoice: Invoice,
    tokens: list[OcrToken],
) -> list[ValidationIssue]:
    """Flag low overall confidence and low-confidence critical fields."""
    issues: list[ValidationIssue] = []

    score = invoice.invoice_metadata.ocr_confidence_score
    if score is not None and score < 0.5:
        issues.append(ValidationIssue(
            code="LOW_OCR_CONFIDENCE",
            message=f"Overall OCR confidence is low ({score:.2f})",
            severity="warning",
        ))

    # Check if many tokens are low-confidence
    if tokens:
        low_conf_count = sum(1 for t in tokens if 0 < t.conf < MIN_CONFIDENCE)
        low_conf_pct = low_conf_count / len(tokens)
        if low_conf_pct > 0.3:
            issues.append(ValidationIssue(
                code="HIGH_LOW_CONFIDENCE_RATIO",
                message=(
                    f"{low_conf_pct:.0%} of OCR tokens have confidence "
                    f"below {MIN_CONFIDENCE}"
                ),
                severity="warning",
            ))

    return issues


def validate_contractor_eligibility(
    invoice: Invoice,
    verifier: Any = None,
) -> list[ValidationIssue]:
    """Validate supplier and recipient legal and VAT status online/via registry.

    Under statutory rules of the Bulgarian VAT Act (ЗДДС) and Commercial Register (ТР):
    - Rejects or flags bankrupt, liquidated, or deleted entities.
    - Rejects tax credit if supplier is not registered under Art. 94 ЗДДС.
    - Rejects tax credit under Art. 71 ЗДДС if transaction date precedes VAT registration
      or follows VAT deregistration.
    - Rejects or flags foreign counterparties failing EU VIES validation.
    """
    issues: list[ValidationIssue] = []
    try:
        from contractor_verification import default_verifier, CompanyStatus, VatRegistrationStatus
        active_verifier = verifier or default_verifier

        if invoice.supplier and (invoice.supplier.eik or invoice.supplier.vat_number):
            sup_id = invoice.supplier.eik or invoice.supplier.vat_number
            tax_dt = invoice.invoice_metadata.date_tax_event or invoice.invoice_metadata.date_issued
            res = active_verifier.verify_sync(sup_id, date_tax_event=tax_dt)
            if not res.is_valid_for_tax_credit:
                for msg in res.issues:
                    code = "CONTRACTOR_TAX_CREDIT_DENIED"
                    if "неактивен правен статус" in msg or res.legal_status == CompanyStatus.BANKRUPTCY:
                        code = "SUPPLIER_BANKRUPT_OR_INACTIVE"
                    elif "ДЕРЕГИСТРИРАНА" in msg:
                        code = "SUPPLIER_VAT_DEREGISTERED"
                    elif "НЕ Е регистрирана" in msg or res.vat_status == VatRegistrationStatus.NOT_REGISTERED:
                        code = "SUPPLIER_NOT_VAT_REGISTERED"
                    elif "ПРЕДИ датата на регистрация" in msg:
                        code = "TAX_CREDIT_DENIED_BEFORE_REGISTRATION"
                    elif "СЛЕД датата на дерегистрация" in msg:
                        code = "TAX_CREDIT_DENIED_AFTER_DEREGISTRATION"
                    elif "VIES" in msg:
                        code = "VIES_VERIFICATION_FAILED"

                    issues.append(ValidationIssue(
                        code=code,
                        message=msg,
                        severity="error",
                        field="supplier.eik",
                        detected_value=sup_id,
                    ))
    except Exception as exc:
        logger.warning("Contractor verification failed: %s", exc)

    return issues


def validate_invoice(
    invoice: Invoice,
    tokens: list[OcrToken],
    verify_contractors: bool = False,
    contractor_verifier: Any = None,
) -> ValidationResult:
    """Run all validators and aggregate results.

    Sets ``is_valid = True`` only when there are zero errors.
    """
    if invoice.invoice_metadata.document_type == DocumentType.PAYMENT_ORDER_NAP.value:
        return validate_budget_payment_order(invoice, tokens)

    result = ValidationResult()

    # Collect all issues from sub-validators
    all_issues: list[ValidationIssue] = []
    all_issues.extend(_validate_required_fields(invoice))
    all_issues.extend(_validate_identifiers(invoice))
    all_issues.extend(_validate_dates(invoice))
    all_issues.extend(_validate_line_items(invoice))
    all_issues.extend(_validate_totals(invoice))
    all_issues.extend(_validate_currency(invoice, tokens))
    all_issues.extend(_validate_amount_in_words(invoice))
    all_issues.extend(_validate_ocr_confidence(invoice, tokens))

    # Statutory Legal & Tax Compliance Audit (ЗСч чл. 6 & 7, ЗДДС чл. 114)
    legal_report, legal_issues = audit_legal_compliance(invoice, tokens=tokens)
    result.legal_compliance_report = legal_report
    invoice.legal_compliance_report = legal_report
    all_issues.extend(legal_issues)

    if verify_contractors:
        all_issues.extend(validate_contractor_eligibility(invoice, verifier=contractor_verifier))
        try:
            from invoice_core.partner_verification import verify_invoice_parties
            parties_rep = verify_invoice_parties(
                invoice.supplier,
                invoice.recipient,
                verifier=contractor_verifier,
            )
            result.parties_verification_report = parties_rep
            all_issues.extend(parties_rep.validation_issues)
            if invoice.raw_ocr_evidence is not None:
                invoice.raw_ocr_evidence["parties_verification"] = parties_rep.to_dict()
        except Exception as exc:
            logger.warning("Partner verification against accounting.partners failed: %s", exc)

    for issue in all_issues:
        if issue.severity == "error":
            result.errors.append(issue)
        else:
            result.warnings.append(issue)

    result.is_valid = len(result.errors) == 0
    invoice.validation = result
    return result

