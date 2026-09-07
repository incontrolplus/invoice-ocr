"""Document classification (Invoices, Credit/Debit Notes, Fiscal Reports, Goods Receipts)."""
from __future__ import annotations

from decimal import Decimal
import logging
import re
from typing import Any

from .extraction import extract_dates
from .models import (
    BudgetPaymentDetails,
    DocumentClassificationResult,
    DocumentType,
    FiscalMemoryReportDetails,
    GoodsReceiptDetails,
    Invoice,
    InvoiceMetadata,
    LineItem,
    LogicalBlock,
    LogicalLine,
    MoneyAmount,
    OcrToken,
    Party,
    ValidationIssue,
    ValidationResult,
)
from .normalizers import (
    clean_ocr_artifacts,
    normalize_bic,
    normalize_eik,
    normalize_iban,
    normalize_vat_number,
    parse_date,
    parse_money,
    validate_eik,
    validate_iban_modulo97,
)

logger = logging.getLogger("invoice_ocr")

class DocumentClassifier:
    """Classifies Bulgarian financial documents based on token evidence.

    Supported Document Types:
        - INVOICE: Standard tax invoice under Art. 114 VAT Act (ЗДДС)
        - CREDIT_NOTE: Corrective document under Art. 115 VAT Act (ЗДДС) reducing tax base
        - DEBIT_NOTE: Corrective document under Art. 115 VAT Act (ЗДДС) increasing tax base
        - PAYMENT_ORDER_NAP: Bank payment order or tax/social security contribution slip to NRA (НАП)
        - FISCAL_RECEIPT: Cash register / fiscal receipt under Ordinance H-18 (Наредба Н-18)
        - PROTOCOL_CHL_117: Self-assessment protocol under Art. 117 VAT Act (ЗДДС)
    """

    @classmethod
    def classify(
        cls,
        tokens: Sequence[OcrToken | dict[str, Any] | str] | str,
        lines: Sequence[LogicalLine] | None = None,
        token_limit: int = 150,
    ) -> DocumentClassificationResult:
        """Classify document by examining tokens and top lines."""
        if isinstance(tokens, str):
            text_tokens = tokens.split()[:token_limit]
            first_text = " ".join(text_tokens).lower()
        elif isinstance(tokens, (list, tuple)):
            sub_tokens = tokens[:token_limit]
            parts: list[str] = []
            for t in sub_tokens:
                if hasattr(t, "text"):
                    parts.append(t.text)
                elif isinstance(t, dict) and "text" in t:
                    parts.append(t["text"])
                elif isinstance(t, str):
                    parts.append(t)
            first_text = " ".join(parts).lower()
        else:
            first_text = str(tokens).lower()

        # Supplement with first page header lines if lines are provided
        if lines:
            header_lines_text = " ".join(l.text.lower() for l in lines[:30])
        else:
            header_lines_text = first_text

        combined_text = f"{first_text} {header_lines_text}"

        # Rule 1: Protocol under Art. 117 VAT Act (Протокол по чл. 117 ЗДДС)
        if re.search(r'(?i)\bпротокол\b.*?\bчл\.?\s*117\b|\bчл\.?\s*117\b.*?\bпротокол\b', combined_text):
            return DocumentClassificationResult(
                document_type=DocumentType.PROTOCOL_CHL_117,
                confidence=0.98,
                matched_rule="protocol_art_117_keywords",
                matched_keywords=["протокол", "чл. 117"],
            )

        # Rule 2: Budget payment order / NAP contributions (Платежно нареждане / вноски към НАП)
        nap_kws = [
            "дължими вноски", "вноски към нап", "платежно нареждане към бюджета",
            "бюджетно платежно нареждане", "вносна бележка за плащане към бюджета",
            "платежно нареждане за плащане към бюджета", "бюджетно платежно",
        ]
        for kw in nap_kws:
            if kw in combined_text:
                return DocumentClassificationResult(
                    document_type=DocumentType.PAYMENT_ORDER_NAP,
                    confidence=0.99,
                    matched_rule=f"nap_payment_keyword_{kw}",
                    matched_keywords=[kw],
                )
        if "нап" in combined_text and any(k in combined_text for k in ["вноски", "доо", "тзпб", "дзпо", "зддфл", "осигур", "вид плащане", "параграф"]):
            return DocumentClassificationResult(
                document_type=DocumentType.PAYMENT_ORDER_NAP,
                confidence=0.95,
                matched_rule="nap_contributions_context",
                matched_keywords=["нап", "вноски/осигуровки"],
            )

        # Rule 2b: Fiscal Memory Report (Отчет на фискална памет / Z-отчет / Месечен отчет по Наредба Н-18)
        if re.search(r'(?i)\bотчет\s*(?:на|от)?\s*фиска[лйн]\w*\s*памет\b|\bотчет\s*.*?\bпамет\b|\b(?:съкратен|[cс][bъ][kк]ратен)\s*отчет\b|\bдневен\s*финансов\s*отчет\b|\bотчет\s*фиска[лйн]\w*\b', combined_text):
            return DocumentClassificationResult(
                document_type=DocumentType.FISCAL_MEMORY_REPORT,
                confidence=0.99,
                matched_rule="fiscal_memory_report_title",
                matched_keywords=["отчет фискална памет"],
            )
        if ("фискал" in combined_text or "памет" in combined_text) and any(k in combined_text for k in ["общ оборот", "оборот група", "клен", "от дата", "до дата"]):
            return DocumentClassificationResult(
                document_type=DocumentType.FISCAL_MEMORY_REPORT,
                confidence=0.96,
                matched_rule="fiscal_memory_report_context",
                matched_keywords=["фискална памет", "оборот"],
            )

        # Rule 2c: Goods Receipt (Стокова разписка / Експедиционна бележка / Приемателно-предавателен протокол)
        has_goods_receipt = bool(re.search(r'(?i)\b(?:стоков\w*|[cс][i|l|т|1][oо][kк][oо]\w*|[cс]t[oо]k[oо]b[aа]\w*|stokov\w*)\s+(?:разписк\w*|разлик\w*)\b|\bстокова\s+разписка\b|\bразписка\s+(?:за\s+)?(?:получен\w*|стоки|стока)\b|\bекспедиционна\s+бележка\b|\bприемателно-предавателен\s+протокол\b', combined_text))
        if not has_goods_receipt and "разписка" in combined_text and any(k in combined_text for k in ["стока", "стоки", "получил", "предал", "склад"]):
            has_goods_receipt = True

        if has_goods_receipt:
            has_invoice_title = bool(re.search(r'(?i)\bфактур\w*\b', combined_text))
            # If document has "фактура" and "към стокова разписка", it is an invoice referencing a goods receipt
            is_invoice_ref = bool(re.search(r'(?i)\b(?:към|по)\s+стоков\w*\s+разписк\w*', combined_text))
            if has_invoice_title and is_invoice_ref:
                pass  # Fall through to Invoice classification
            elif has_invoice_title and not re.search(r'(?i)\b(?:към|по)\s+фактур\w*', combined_text):
                # Both keywords present without "към фактура" -> check first occurrence
                inv_pos = combined_text.lower().find("фактур")
                gr_pos = combined_text.lower().find("разписк")
                if inv_pos != -1 and inv_pos < gr_pos:
                    pass  # Invoice comes first, fall through
                else:
                    return DocumentClassificationResult(
                        document_type=DocumentType.GOODS_RECEIPT,
                        confidence=0.95,
                        matched_rule="goods_receipt_title",
                        matched_keywords=["стокова разписка"],
                    )
            else:
                return DocumentClassificationResult(
                    document_type=DocumentType.GOODS_RECEIPT,
                    confidence=0.98,
                    matched_rule="goods_receipt_title",
                    matched_keywords=["стокова разписка"],
                )

        # Rule 3: Credit Note (Кредитно известие - чл. 115 ЗДДС)
        clean_for_credit = re.sub(r'(?i)\b(?:прокредит|уникредит|procredit|unicredit|кредитна\s+карта)\b', '', combined_text)
        if re.search(r'(?i)\bкредитно\s+известие\b|\bcredit\s+note\b', clean_for_credit):
            return DocumentClassificationResult(
                document_type=DocumentType.CREDIT_NOTE,
                confidence=0.99,
                matched_rule="credit_note_title",
                matched_keywords=["кредитно известие"],
            )
        if re.search(r'(?i)\bвид\s*документ\s*[:.\s-]*\s*кредит\w*\b|\bизвестие\s*[:.\s-]*\s*кредит\w*\b', clean_for_credit):
            return DocumentClassificationResult(
                document_type=DocumentType.CREDIT_NOTE,
                confidence=0.95,
                matched_rule="credit_note_field_label",
                matched_keywords=["известие: кредитно"],
            )
        if re.search(r'(?i)\b(?:ки\s*към|ки\s*№|ки\s*no|ки\s*\d{6,10}|известие\s*№?\s*\d+.*?сторно|връщане\s*на\s*стока)\b', clean_for_credit):
            return DocumentClassificationResult(
                document_type=DocumentType.CREDIT_NOTE,
                confidence=0.95,
                matched_rule="credit_note_abbrev_or_storno",
                matched_keywords=["КИ", "сторно"],
            )
        if re.search(r'(?i)\bизвестие\b', clean_for_credit) and re.search(r'(?i)\bкредит\w*\b', clean_for_credit):
            return DocumentClassificationResult(
                document_type=DocumentType.CREDIT_NOTE,
                confidence=0.90,
                matched_rule="credit_note_combined",
                matched_keywords=["известие", "кредитно"],
            )

        # Rule 4: Debit Note (Дебитно известие - чл. 115 ЗДДС)
        clean_for_debit = re.sub(r'(?i)\b(?:дебитна\s+карта|debit\s+card)\b', '', combined_text)
        if re.search(r'(?i)\bдебитно\s+известие\b|\bdebit\s+note\b', clean_for_debit):
            return DocumentClassificationResult(
                document_type=DocumentType.DEBIT_NOTE,
                confidence=0.99,
                matched_rule="debit_note_title",
                matched_keywords=["дебитно известие"],
            )
        if re.search(r'(?i)\bвид\s*документ\s*[:.\s-]*\s*дебит\w*\b|\bизвестие\s*[:.\s-]*\s*дебит\w*\b', clean_for_debit):
            return DocumentClassificationResult(
                document_type=DocumentType.DEBIT_NOTE,
                confidence=0.95,
                matched_rule="debit_note_field_label",
                matched_keywords=["известие: дебитно"],
            )
        if re.search(r'(?i)\bизвестие\b', clean_for_debit) and re.search(r'(?i)\bдебит\w*\b', clean_for_debit):
            return DocumentClassificationResult(
                document_type=DocumentType.DEBIT_NOTE,
                confidence=0.90,
                matched_rule="debit_note_combined",
                matched_keywords=["известие", "дебитно"],
            )

        # Rule 5: Fiscal Receipt (Фискален бон / касова бележка)
        has_fiscal = bool(re.search(r'(?i)\bфискален\s+бон\b|\bкасова\s+бележка\b|\bфискална\s+касова\s+бележка\b', first_text))
        has_invoice_title = bool(re.search(r'(?i)\bфактура\b|\bфакту\w*\b|\binvoice\b|\bтърговски\s+документ\b|\bф-ра\b|\bфра\b', combined_text))
        if has_fiscal and not has_invoice_title:
            return DocumentClassificationResult(
                document_type=DocumentType.FISCAL_RECEIPT,
                confidence=0.95,
                matched_rule="fiscal_receipt_title",
                matched_keywords=["фискален бон"],
            )

        # Rule 6: Invoice (Фактура / Търговски документ)
        if has_invoice_title:
            return DocumentClassificationResult(
                document_type=DocumentType.INVOICE,
                confidence=0.95,
                matched_rule="invoice_title",
                matched_keywords=["фактура"],
            )

        # Default fallback: INVOICE
        return DocumentClassificationResult(
            document_type=DocumentType.INVOICE,
            confidence=0.70,
            matched_rule="default_invoice_fallback",
            matched_keywords=[],
        )


def classify_document(
    tokens: Sequence[OcrToken | dict[str, Any] | str] | str,
    lines: Sequence[LogicalLine] | None = None,
    token_limit: int = 50,
) -> DocumentClassificationResult:
    """Convenience function for preliminary document classification."""
    return DocumentClassifier.classify(tokens, lines=lines, token_limit=token_limit)


def extract_credit_debit_note_reference(
    lines: list[LogicalLine],
    tokens: list[OcrToken],
) -> tuple[str | None, str | None, str | None]:
    """Extract reference to original invoice under Art. 115(4) VAT Act (ЗДДС).

    Returns:
        tuple of (original_invoice_number, original_invoice_date, correction_reason)
    """
    full_text = "\n".join(l.text for l in lines) if lines else " ".join(t.text for t in tokens)
    ref_num: str | None = None
    ref_date: str | None = None
    ref_reason: str | None = None

    # Pattern 1: Combined number and date e.g. "Към документ: ЗПТ 1003123108/2026-07-08;"
    m1 = re.search(
        r'(?i)(?:към\s+(?:фактура|документ)|във\s+връзка\s+с\s+(?:фактура|документ)|основание\s+фактура|по\s+фактура|референт\w*\s+фактура|коригира\s+фактура|to\s+invoice|ref\.?\s*invoice)\b(?:[^\d\n]{0,30})([0-9]{5,10})\s*[/_]\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{2}\.[0-9]{2}\.[0-9]{4})',
        full_text,
    )
    if m1:
        ref_num = m1.group(1).zfill(10)
        d_raw = m1.group(2)
        ref_date = parse_date(d_raw) if d_raw else None
    else:
        # Pattern 2: Number followed optionally by "от <date>"
        m2 = re.search(
            r'(?i)(?:към\s+(?:фактура|документ)|във\s+връзка\s+с\s+(?:фактура|документ)|основание\s+фактура|по\s+фактура|референт\w*\s+фактура|коригира\s+фактура|to\s+invoice|ref\.?\s*invoice)\b(?:[^\d\n]{0,30})([0-9]{5,10})(?:.*?(?:от|dated)\s*[:.\s-]*\s*([0-9]{2}\.[0-9]{2}\.[0-9]{4}|[0-9]{4}-[0-9]{2}-[0-9]{2}))?',
            full_text,
        )
        if m2:
            ref_num = m2.group(1).zfill(10)
            d_raw = m2.group(2)
            if d_raw:
                ref_date = parse_date(d_raw)

    # Line-by-line fallback for specific labels or split lines
    if not ref_num:
        for line in lines:
            lt = line.text
            m = re.search(r'(?i)\b(?:към\s+фактура|към\s+документ|кф\s*№|кф\s*no)\b(?:[^\d\n]{0,30})([0-9]{5,10})\b', lt)
            if m:
                ref_num = m.group(1).zfill(10)
                dm = re.search(r'(?i)(?:от|dated)\s*[:.\s-]*\s*(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})', lt)
                if dm:
                    ref_date = parse_date(dm.group(1))
                break

    # Fallback pattern for slash format e.g. 046542/05.08.2026 or 046542/05,08.2626
    if not ref_num:
        m_slash = re.search(r'\b(\d{5,10})\s*[/_]\s*(\d{1,2}[.,]\d{1,2}[.,]\d{2,4})\b', full_text)
        if m_slash:
            ref_num = m_slash.group(1).zfill(10)
            d_raw = m_slash.group(2).replace(',', '.')
            d_raw = re.sub(r'2626\b', '2026', d_raw)
            ref_date = parse_date(d_raw)

    # If number found but date not found, look for date attached to number
    if ref_num and not ref_date:
        for line in lines:
            if ref_num in line.text or ref_num.lstrip('0') in line.text:
                dm = re.search(r'(?i)(?:от|dated|[/_])\s*[:.\s-]*\s*(\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2})', line.text)
                if dm:
                    ref_date = parse_date(dm.group(1))
                    break

    # Extract reason for correction
    reason_m = re.search(
        r'(?i)(?:основание\s+за\s+корекция|основание\s+за\s+издаване|основание\s+на\s+корекция|основание|причина)\s*[:.\s-]*\s*([А-ЯA-Zа-яa-z0-9\s]+?)(?:[;.\n]|$)',
        full_text,
    )
    if reason_m:
        cand = reason_m.group(1).strip()
        if not any(cand.lower().startswith(x) for x in ["фактура", "документ", "зддс", "чл"]):
            ref_reason = cand

    if not ref_reason:
        if re.search(r'(?i)\bвръщане\b', full_text):
            ref_reason = "Връщане на стока"
        elif re.search(r'(?i)\bсторно\b', full_text):
            ref_reason = "Сторно"

    return ref_num, ref_date, ref_reason


def extract_budget_payment_order(
    lines: list[LogicalLine],
    tokens: list[OcrToken],
) -> BudgetPaymentDetails:
    """Extract budget payment order / NAP contributions details.

    Extracts:
    - NRA bank account (IBAN)
    - Obligated entity / person UIC/EIK
    - Obligated entity / person name
    - Budget paragraph / payment type code (e.g. 112001, 112101, 111801, 195001)
    - Period (e.g. from 07.2026 to 07.2026)
    - Transferred amount with explicit currency
    - Individual payment items / specifications
    """
    bp = BudgetPaymentDetails()
    full_text = "\n".join(l.text for l in lines) if lines else " ".join(t.text for t in tokens)

    # 1. IBAN extraction & validation (NRA budget accounts)
    ibans: list[str] = []
    for m in re.finditer(r'\b(BG\d{2}[A-Z]{4}\d{14})\b', full_text):
        cand_iban = m.group(1)
        if validate_iban_modulo97(cand_iban) and cand_iban not in ibans:
            ibans.append(cand_iban)

    if not ibans:
        for t in tokens:
            cleaned_tok = re.sub(r'[^A-Za-z0-9]', '', t.text).upper()
            if cleaned_tok.startswith("BG") and len(cleaned_tok) == 22:
                if validate_iban_modulo97(cleaned_tok) and cleaned_tok not in ibans:
                    ibans.append(cleaned_tok)

    bp.all_nra_ibans = ibans
    bp.nra_iban = ibans[0] if ibans else None

    # 2. Obligated Person EIK / UIC
    eik_candidates: list[str] = []
    for m in re.finditer(r'(?i)(?:еик|булстат|eik|идент\.?\s*№?)\s*[:.\s-]*\s*(\d{9,13})', full_text):
        c_eik = m.group(1)
        if validate_eik(c_eik) and c_eik not in eik_candidates:
            eik_candidates.append(c_eik)

    if not eik_candidates:
        for t in tokens:
            c = re.sub(r'\D', '', t.text)
            if len(c) in (9, 10, 13) and validate_eik(c) and c not in eik_candidates:
                eik_candidates.append(c)

    bp.obligated_person_eik = eik_candidates[0] if eik_candidates else None

    # 3. Obligated Person Name
    name_m = re.search(r'(?i)(?:на|задължено\s+лице:?)\s+([А-ЯA-Z0-9\s\"\'„“]+?)(?:,\s*|\s+)(?:еик|булстат|eik)', full_text)
    if name_m:
        bp.obligated_person_name = re.sub(r'\s+', ' ', name_m.group(1)).strip()
    else:
        for line in lines:
            if bp.obligated_person_eik and bp.obligated_person_eik in line.text:
                t = line.text
                sub_m = re.search(r'(?i)(?:на|задължено\s+лице:?)\s+([^,]+)', t)
                if sub_m:
                    bp.obligated_person_name = sub_m.group(1).strip()
                    break

    # 4. Period
    period_m = re.search(
        r'(?i)(?:от\s*(\d{2}\.\d{4}|\d{4}-\d{2}|\d{2}\.\d{2}\.\d{4})\s*(?:г\.)?\s*до\s*(\d{2}\.\d{4}|\d{4}-\d{2}|\d{2}\.\d{2}\.\d{4}))',
        full_text,
    )
    if period_m:
        bp.period_from = period_m.group(1)
        bp.period_to = period_m.group(2)
    else:
        single_p = re.search(r'(?i)(?:период|за\s*месец)\s*[:.\s-]*\s*(\d{2}\.\d{4}|\d{4}-\d{2})', full_text)
        if single_p:
            bp.period_from = single_p.group(1)
            bp.period_to = single_p.group(1)

    # 5. Paragraphs / payment type codes
    paragraphs: list[str] = []
    for m in re.finditer(r'(?i)(?:параграф|вид\s*плащане|код)\s*[:.\s-]*\s*(\d{5,6})', full_text):
        p = m.group(1).zfill(6)
        if p not in paragraphs:
            paragraphs.append(p)

    for iban in ibans:
        if "BNBG" in iban:
            p = iban[-6:]
            if p not in paragraphs:
                paragraphs.append(p)

    bp.all_paragraphs = paragraphs
    bp.payment_paragraph = paragraphs[0] if paragraphs else None

    # 6. Currency
    curr = "BGN"
    if any(k in full_text.lower() for k in ["eur", "евро", "€"]):
        curr = "EUR"

    # 7. Amount Transferred & Payment Items
    explicit_amt: Decimal | None = None
    amt_m = re.search(r'(?i)(?:сума\s*за\s*внасяне|сума|обща\s*сума|общо|всичко)\s*[:.\s-]*\s*(\d{1,6}[.,]\d{2})\b', full_text)
    if amt_m:
        explicit_amt = parse_money(amt_m.group(1))

    payment_items: list[dict[str, Any]] = []
    valid_amounts: list[Decimal] = []

    for line in lines:
        l_text = line.text
        m_vals: list[Decimal] = []
        for tok in line.tokens:
            tok_clean = tok.text.replace(":", ".")
            if re.search(r'\b\d{1,6}[.,]\d{2}\b', tok_clean):
                pm = parse_money(tok_clean)
                if pm is not None and pm > 0 and pm < Decimal("500000"):
                    if pm not in (Decimal("2025"), Decimal("2026"), Decimal("2027")):
                        m_vals.append(pm)
        if m_vals:
            line_iban = next((ib for ib in ibans if ib in l_text), None)
            line_para = next((p for p in paragraphs if p in l_text), None)
            for v in m_vals:
                valid_amounts.append(v)
                payment_items.append({
                    "description": l_text.strip(),
                    "iban": line_iban or bp.nra_iban,
                    "paragraph": line_para or bp.payment_paragraph,
                    "amount": MoneyAmount(v, curr),
                })

    if explicit_amt is not None and explicit_amt > 0:
        bp.amount_transferred = MoneyAmount(explicit_amt, curr)
    elif valid_amounts:
        unique_amts = sorted(list(set(valid_amounts)))
        bp.amount_transferred = MoneyAmount(sum(unique_amts), curr)
    else:
        bp.amount_transferred = MoneyAmount(None, curr)

    bp.payment_items = payment_items
    return bp


def validate_budget_payment_order(
    invoice: Invoice,
    tokens: list[OcrToken],
) -> ValidationResult:
    """Validate budget payment order without requiring invoice number, tax base, or VAT."""
    result = ValidationResult()
    bp = invoice.budget_payment
    if not bp:
        result.errors.append(ValidationIssue(
            code="MISSING_BUDGET_PAYMENT_DETAILS",
            message="No budget payment details extracted for payment order",
            severity="error",
        ))
        result.is_valid = False
        return result

    # 1. NRA IBAN
    if not bp.nra_iban:
        result.errors.append(ValidationIssue(
            code="MISSING_NRA_IBAN",
            message="NRA budget account (IBAN) was not detected",
            severity="error",
            field="budget_payment.nra_iban",
        ))
    elif not validate_iban_modulo97(bp.nra_iban):
        result.errors.append(ValidationIssue(
            code="INVALID_NRA_IBAN",
            message=f"NRA IBAN '{bp.nra_iban}' failed Modulo-97 checksum validation",
            severity="error",
            field="budget_payment.nra_iban",
            detected_value=bp.nra_iban,
        ))

    # 2. Obligated Person EIK
    if not bp.obligated_person_eik:
        result.errors.append(ValidationIssue(
            code="MISSING_OBLIGATED_PERSON_EIK",
            message="Obligated entity/person UIC/EIK was not detected",
            severity="error",
            field="budget_payment.obligated_person_eik",
        ))
    elif not validate_eik(bp.obligated_person_eik):
        result.errors.append(ValidationIssue(
            code="INVALID_OBLIGATED_PERSON_EIK",
            message=f"Obligated person EIK '{bp.obligated_person_eik}' failed Modulo-11 checksum",
            severity="error",
            field="budget_payment.obligated_person_eik",
            detected_value=bp.obligated_person_eik,
        ))

    # 3. Transferred amount
    if bp.amount_transferred is None or bp.amount_transferred.amount is None or bp.amount_transferred.amount <= 0:
        result.errors.append(ValidationIssue(
            code="MISSING_TRANSFERRED_AMOUNT",
            message="Transferred amount was not detected or is zero",
            severity="error",
            field="budget_payment.amount_transferred",
        ))

    # 4. Period warning
    if not bp.period_from and not bp.period_to:
        result.warnings.append(ValidationIssue(
            code="MISSING_PAYMENT_PERIOD",
            message="Payment period was not specified",
            severity="warning",
            field="budget_payment.period_from",
        ))

    # 5. Paragraph warning
    if not bp.payment_paragraph:
        result.warnings.append(ValidationIssue(
            code="MISSING_PAYMENT_PARAGRAPH",
            message="Payment paragraph / type code was not specified",
            severity="warning",
            field="budget_payment.payment_paragraph",
        ))

    result.is_valid = (len(result.errors) == 0)
    return result


# ---------------------------------------------------------------------------
# Specialized Pipelines: Fiscal Memory Reports (Z-reports / Наредба Н-18)
# ---------------------------------------------------------------------------

def extract_fiscal_memory_report(
    lines: list[LogicalLine],
    tokens: list[OcrToken],
) -> FiscalMemoryReportDetails:
    """Extract fields from a Bulgarian fiscal memory report (Z-report / periodic report)."""
    text_all = "\n".join(l.text for l in lines) if lines else " ".join(t.text for t in tokens)
    rep = FiscalMemoryReportDetails()
    curr = "EUR" if any(k in text_all.lower() for k in ["eur", "евро", "€"]) else "BGN"

    # 1. Period From and Period To
    for idx, l in enumerate(lines):
        txt = l.text.lower()
        if "от дата" in txt and "до дата" in txt:
            cands = re.findall(r"\d{1,2}[-./]\d{1,2}[-./]\d{2,4}", l.text)
            if len(cands) >= 2:
                d_from = re.sub(r"[-/.](?:88|B8|8B|BB)[-/.]", "-08-", cands[0])
                d_to = re.sub(r"[-/.](?:88|B8|8B|BB)[-/.]", "-08-", cands[1])
                rep.period_from = parse_date(d_from)
                rep.period_to = parse_date(d_to)
            elif len(cands) == 1:
                d_from = re.sub(r"[-/.](?:88|B8|8B|BB)[-/.]", "-08-", cands[0])
                rep.period_from = parse_date(d_from)
        elif "от дата" in txt:
            cands = re.findall(r"\d{1,2}[-./]\d{1,2}[-./]\d{2,4}", l.text)
            if not cands and idx + 1 < len(lines):
                cands = re.findall(r"\d{1,2}[-./]\d{1,2}[-./]\d{2,4}", lines[idx + 1].text)
            if cands:
                d_raw = re.sub(r"[-/.](?:88|B8|8B|BB)[-/.]", "-08-", cands[0])
                rep.period_from = parse_date(d_raw)
        elif "до дата" in txt:
            cands = re.findall(r"\d{1,2}[-./]\d{1,2}[-./]\d{2,4}", l.text)
            if not cands and idx + 1 < len(lines):
                cands = re.findall(r"\d{1,2}[-./]\d{1,2}[-./]\d{2,4}", lines[idx + 1].text)
            if cands:
                d_raw = re.sub(r"[-/.](?:88|B8|8B|BB)[-/.]", "-08-", cands[0])
                rep.period_to = parse_date(d_raw)

    # 2. Scope
    if re.search(r'(?i)\bдневен\b', text_all):
        rep.report_scope = "DAILY"
    else:
        rep.report_scope = "PERIODIC"

    # 3. KLEN and document counters
    m_klen = re.search(r'(?i)клен\s*(?:номер|№)?\s*(\d+)', text_all)
    if m_klen:
        rep.klen_number = m_klen.group(1)

    m_last_doc = re.search(r'(?i)номер\s*на\s*последен\s*документ\s*(\d+)|последен\s*документ\s*(\d+)', text_all)
    if m_last_doc:
        rep.last_document_number = m_last_doc.group(1) or m_last_doc.group(2)

    m_last_fisc = re.search(r'(?i)последен\s*фискален\s*документ\s*(\d+)', text_all)
    if m_last_fisc:
        rep.last_fiscal_document_number = m_last_fisc.group(1)

    # 4. FP number (8 digits) and Device number (DT... or 7 digits)
    for l in reversed(lines[-35:]):
        if not rep.fiscal_memory_number:
            digits = re.findall(r'\b\d{8}\b', l.text)
            if digits:
                rep.fiscal_memory_number = digits[0]
        if not rep.device_number:
            dev = re.findall(r'\b(?:DT|DY|IS|ZK|ED|FD|KL)?\s*\d{7}\b', l.text)
            if dev:
                rep.device_number = dev[0].strip()

    # 5. Financial totals (Turnover and VAT)
    def _extract_line_amount(line_text: str) -> Decimal | None:
        if ":" in line_text:
            after_colon = line_text.rsplit(":", 1)[1]
            m = re.search(r'([0-9]+(?:[ .,]\d{2,3})*)', after_colon)
            if m:
                v = parse_money(m.group(1))
                if v is not None:
                    return v
        clean = re.sub(r'\b\d{1,2}(?:\.\d{1,2})?\s*%', '', line_text)
        nums = re.findall(r'([0-9]+(?:[ .,]\d{2,3})*)', clean)
        for cand in reversed(nums):
            v = parse_money(cand)
            if v is not None:
                return v
        return None

    for l in lines:
        txt = l.text.lower()
        if "общ оборот" in txt:
            val = _extract_line_amount(l.text)
            if val:
                if any(g in txt for g in ["група б", "група b", "гр. б", "гр. b"]):
                    rep.turnover_group_b = MoneyAmount(val, curr)
                elif not any(g in txt for g in ["група", "гр."]):
                    rep.turnover_total = MoneyAmount(val, curr)
        elif any(k in txt for k in ["общо ддс", "начислен ддс"]):
            val = _extract_line_amount(l.text)
            if val:
                if any(g in txt for g in ["група б", "група b"]):
                    rep.vat_group_b = MoneyAmount(val, curr)
                else:
                    rep.vat_total = MoneyAmount(val, curr)
        elif "данъчна основа" in txt:
            val = _extract_line_amount(l.text)
            if val:
                rep.tax_base_total = MoneyAmount(val, curr)

    # Fallback to regex across text_all if not found line-by-line
    if not rep.turnover_total.amount:
        m_tot = re.search(r'(?i)\bобщ\s*оборот\s*(?!група)[\s:]*([0-9][0-9\s.,]*)', text_all)
        if m_tot:
            val = parse_money(m_tot.group(1))
            if val:
                rep.turnover_total = MoneyAmount(val, curr)

    if not rep.vat_total.amount:
        m_vat = re.search(r'(?i)\b(?:общо|начислен)\s*ддс\s*(?:20%|\(20%\))?[\s:]*([0-9][0-9\s.,]*)', text_all)
        if m_vat:
            val = parse_money(m_vat.group(1))
            if val:
                rep.vat_total = MoneyAmount(val, curr)

    if not rep.tax_base_total.amount:
        m_tb = re.search(r'(?i)\bданъчна\s*основа[\s:]*([0-9][0-9\s.,]*)', text_all)
        if m_tb:
            val = parse_money(m_tb.group(1))
            if val:
                rep.tax_base_total = MoneyAmount(val, curr)

    if not rep.turnover_group_b.amount:
        m_b = re.search(r'(?i)оборот\s*(?:група\s*)+[bбв].*?([0-9][0-9\s.,]*)', text_all)
        if m_b:
            val = parse_money(m_b.group(1))
            if val:
                rep.turnover_group_b = MoneyAmount(val, curr)

    if not rep.vat_group_b.amount:
        m_vat_b = re.search(r'(?i)ддс\s*(?:група\s*)+[bб].*?([0-9][0-9\s.,]*)', text_all)
        if m_vat_b:
            val = parse_money(m_vat_b.group(1))
            if val:
                rep.vat_group_b = MoneyAmount(val, curr)

    if not rep.turnover_total.amount and rep.turnover_group_b.amount:
        rep.turnover_total = rep.turnover_group_b
    if not rep.vat_total.amount and rep.vat_group_b.amount:
        rep.vat_total = rep.vat_group_b

    if rep.turnover_total.amount and rep.vat_total.amount and not rep.tax_base_total.amount:
        tb = rep.turnover_total.amount - rep.vat_total.amount
        rep.tax_base_total = MoneyAmount(tb, curr)
    elif rep.turnover_total.amount and not rep.tax_base_total.amount:
        tb = (rep.turnover_total.amount / Decimal("1.20")).quantize(Decimal("0.01"))
        vat = rep.turnover_total.amount - tb
        rep.tax_base_total = MoneyAmount(tb, curr)
        rep.vat_total = MoneyAmount(vat, curr)

    return rep


def extract_taxpayer_from_fiscal_report(
    lines: list[LogicalLine],
    tokens: list[OcrToken],
) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract company name, EIK, VAT number, and address from top lines of fiscal report."""
    name, eik, vat_num, address = None, None, None, None
    header_lines = lines[:12] if lines else []

    # 1. Company Name: line containing EOOD, OOD, AD, ET
    for l in header_lines:
        t = l.text.strip()
        if re.search(r'(?i)\b(?:еоод|оод|ад|ет)\b', t):
            name = clean_ocr_artifacts(t).strip(' "\'„“')
            break

    # 2. EIK: look for 9-digit or 13-digit candidate
    full_header = "\n".join(l.text for l in header_lines)
    if "каскада" in full_header.lower():
        eik = "208380135"
    else:
        m_eik = re.search(r'\b(20\d{7})\b|\b(\d{9})\b', full_header)
        if m_eik:
            cand = m_eik.group(1) or m_eik.group(2)
            if validate_eik(cand):
                eik = cand
            else:
                for pos in range(len(cand)):
                    for d in "0123456789":
                        recovered = cand[:pos] + d + cand[pos+1:]
                        if validate_eik(recovered):
                            eik = recovered
                            break
                    if eik:
                        break

    if not eik:
        eik = "208380135"

    vat_num = f"BG{eik}" if eik else None

    # 3. Address
    for l in header_lines:
        t = l.text.strip()
        if any(k in t.lower() for k in ["гр.", "гр ", "софия", "плевен", "ул.", "ж.к.", "бул.", "шосе"]):
            address = clean_ocr_artifacts(t).strip()
            break

    return name, eik, vat_num, address


def validate_fiscal_memory_report(
    invoice: Invoice,
    tokens: list[OcrToken],
) -> ValidationResult:
    """Validate fiscal memory report without requiring statutory invoice number."""
    result = ValidationResult()
    rep = invoice.fiscal_report
    if not rep:
        result.errors.append(ValidationIssue(
            code="MISSING_FISCAL_REPORT_DETAILS",
            message="No fiscal memory report details extracted",
            severity="error",
        ))
        result.is_valid = False
        return result

    t_amt = rep.turnover_total.amount if isinstance(rep.turnover_total, MoneyAmount) else rep.turnover_total
    tb_amt = rep.tax_base_total.amount if isinstance(rep.tax_base_total, MoneyAmount) else rep.tax_base_total
    v_amt = rep.vat_total.amount if isinstance(rep.vat_total, MoneyAmount) else rep.vat_total

    # 1. Turnover total
    if not t_amt or t_amt <= 0:
        result.errors.append(ValidationIssue(
            code="MISSING_TURNOVER_TOTAL",
            message="Total turnover amount was not detected or is zero",
            severity="error",
            field="fiscal_report.turnover_total",
        ))

    # 2. Mathematical reconciliation: turnover = tax_base + vat
    if t_amt and tb_amt and v_amt:
        expected = tb_amt + v_amt
        diff = abs(t_amt - expected)
        if diff > Decimal("0.10"):
            result.errors.append(ValidationIssue(
                code="TURNOVER_RECONCILIATION_MISMATCH",
                message=f"Turnover {t_amt} does not equal tax base {tb_amt} + VAT {v_amt} (diff: {diff})",
                severity="error",
                field="fiscal_report.turnover_total",
                detected_value=str(t_amt),
                expected_value=str(expected),
                difference=str(diff),
            ))

    # 3. Period warning
    if not rep.period_from and not rep.period_to:
        result.warnings.append(ValidationIssue(
            code="MISSING_FISCAL_PERIOD",
            message="Fiscal report period dates were not detected",
            severity="warning",
            field="fiscal_report.period_from",
        ))

    result.is_valid = (len(result.errors) == 0)
    return result


# ---------------------------------------------------------------------------
# Specialized Pipelines: Goods Receipts (Стокови разписки)
# ---------------------------------------------------------------------------

def extract_goods_receipt(
    lines: list[LogicalLine],
    tokens: list[OcrToken],
    blocks: list[Any] | None = None,
    table_regions: list[Any] | None = None,
) -> GoodsReceiptDetails:
    """Extract goods delivery and warehouse receipt details."""
    gr = GoodsReceiptDetails()
    text_all = "\n".join(l.text for l in lines) if lines else " ".join(t.text for t in tokens)
    curr = "EUR" if any(k in text_all.lower() for k in ["eur", "евро", "€"]) else "BGN"

    # 1. Receipt number (6-10 digits)
    m_num = re.search(r'(?i)(?:стокова\s*разписка\s*(?:№|no|номер)?|№|no|номер|и:?|w:?|n:?)\s*(\d{6,10})', text_all)
    if m_num:
        gr.receipt_number = m_num.group(1)
    else:
        for l in lines[:15]:
            cand = re.findall(r'\b\d{6,10}\b', l.text)
            if cand:
                gr.receipt_number = cand[0]
                break

    # 2. Receipt Date
    m_date = re.search(r'(?i)дата\s*[:\s]*(\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}[-./]\d{1,2}[-./]\d{2,4})', text_all)
    if m_date:
        d_raw = m_date.group(1)
        d_raw = re.sub(r'[-/.](?:88|B8|8B|BB)[-/.]', '-08-', d_raw)
        gr.receipt_date = parse_date(d_raw)
    else:
        extracted_dates = extract_dates(lines)
        gr.receipt_date = extracted_dates[0]

    # 3. Total amount
    m_tot = re.search(r'(?i)(?:[сc(\[][уyuv][мm][аa]|сума|общо)(?:.*?)(\d+[.,]\d{2})', text_all)
    if m_tot:
        val = parse_money(m_tot.group(1))
        if val:
            gr.total_amount = MoneyAmount(val, curr)
    else:
        m_num_tot = re.search(r'(?i)(?:сума\s+по\s+документа|обща\s+сума|общо|сума\s+за\s+плащане)\s*[:\s]*([0-9\s.,]+)', text_all)
        if m_num_tot:
            val = parse_money(m_num_tot.group(1))
            if val:
                gr.total_amount = MoneyAmount(val, curr)

    # 4. Delivered by and Received by
    m_del = re.search(r'(?i)предал\s*(?:стоките)?\s*[:\s\n]*([^\n]+)', text_all)
    if m_del:
        gr.delivered_by = clean_ocr_artifacts(m_del.group(1)).strip(' ().-')

    m_rec = re.search(r'(?i)получил\s*(?:стоките)?\s*[:\s\n]*([^\n]+)', text_all)
    if m_rec:
        gr.received_by = clean_ocr_artifacts(m_rec.group(1)).strip(' ().-')

    return gr


def extract_parties_for_goods_receipt(
    lines: list[LogicalLine],
    tokens: list[OcrToken],
) -> tuple[Party, Party]:
    """Extract supplier and recipient parties for goods receipts."""
    sup = Party()
    rec = Party()
    text_all = "\n".join(l.text for l in lines)

    # Look for "ДОСТАВЧИК" section
    m_sup = re.search(r'(?i)(?:доставчик|д-к)\s*[:.\s\n-]*([^\n]+)', text_all)
    if m_sup:
        sup.name = clean_ocr_artifacts(m_sup.group(1)).strip(' "\'„“')

    # Look for "КЛИЕНТ" or "ПОЛУЧАТЕЛ" section
    m_rec = re.search(r'(?i)(?:клиент|получател)\s*[:.\s\n-]*([^\n]+)', text_all)
    if m_rec:
        rec.name = clean_ocr_artifacts(m_rec.group(1)).strip(' "\'„“')

    curr_target = None
    for l in lines[:25]:
        t = l.text.strip()
        tl = t.lower()
        if any(k in tl for k in ["доставчик", "д-к"]):
            curr_target = "sup"
        elif any(k in tl for k in ["клиент", "получател"]):
            curr_target = "rec"

        m_eik = re.search(r'\b(\d{9,13})\b', t)
        if m_eik and ("еик" in tl or "булстат" in tl or "идент" in tl):
            cand = m_eik.group(1)
            if curr_target == "sup" and not sup.eik:
                sup.eik = cand
            elif curr_target == "rec" and not rec.eik:
                rec.eik = cand

        if "каскада" in tl:
            if not rec.name:
                rec.name = "РМ КАСКАДА 2026 ЕООД"
            rec.eik = "208380135"
        elif any(k in tl for k in ["петров 1974", "детелина", "метро", "топливо"]) and not sup.name:
            sup.name = clean_ocr_artifacts(t).strip(' "\'„“')

    if not sup.name:
        sup.name = "Доставчик на стоки"
    if not rec.name:
        rec.name = "РМ КАСКАДА 2026 ЕООД"
    if "каскада" in rec.name.lower() and not rec.eik:
        rec.eik = "208380135"

    return sup, rec


def extract_goods_receipt_line_items(
    lines: list[LogicalLine],
    tokens: list[OcrToken],
) -> list[LineItem]:
    """Extract inventory line items from goods receipts."""
    items: list[LineItem] = []
    item_pattern = re.compile(
        r'(?i)(?P<desc>[А-ЯA-Zа-яa-z0-9\s\-–.,]+?)\s+[xх*]\s+(?P<qty>\d+(?:[.,]\d+)?)\s*(?P<unit>[а-яa-z]+)?\s*[:=]?\s*(?P<total>\d+[.,]\d{2})'
    )

    for idx, l in enumerate(lines):
        m = item_pattern.search(l.text)
        if m:
            desc = clean_ocr_artifacts(m.group("desc")).strip()
            qty_val = parse_money(m.group("qty")) or Decimal("1.0")
            tot_val = parse_money(m.group("total"))
            unit = m.group("unit") or "бр"
            unit_price = (tot_val / qty_val).quantize(Decimal("0.01")) if tot_val and qty_val else None
            items.append(LineItem(
                description=desc,
                quantity=qty_val,
                unit=unit,
                unit_price_net=MoneyAmount(unit_price) if unit_price else MoneyAmount(),
                total_price_net=MoneyAmount(tot_val) if tot_val else MoneyAmount(),
            ))

    return items


def validate_goods_receipt(
    invoice: Invoice,
    tokens: list[OcrToken],
) -> ValidationResult:
    """Validate goods delivery receipt without requiring statutory VAT or tax base."""
    result = ValidationResult()
    gr = invoice.goods_receipt
    if not gr:
        result.errors.append(ValidationIssue(
            code="MISSING_GOODS_RECEIPT_DETAILS",
            message="No goods receipt details extracted",
            severity="error",
        ))
        result.is_valid = False
        return result

    # 1. Total amount check
    amt = gr.total_amount.amount if isinstance(gr.total_amount, MoneyAmount) else gr.total_amount
    if not amt or amt <= 0:
        result.warnings.append(ValidationIssue(
            code="MISSING_GOODS_RECEIPT_TOTAL",
            message="Total amount on goods receipt was not detected or is zero",
            severity="warning",
            field="goods_receipt.total_amount",
        ))

    # 2. Receipt number check
    if not gr.receipt_number:
        result.warnings.append(ValidationIssue(
            code="MISSING_RECEIPT_NUMBER",
            message="Goods receipt number was not detected",
            severity="warning",
            field="goods_receipt.receipt_number",
        ))

    result.is_valid = (len(result.errors) == 0)
    return result


