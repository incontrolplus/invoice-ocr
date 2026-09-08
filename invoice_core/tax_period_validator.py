"""Tax Period Compliance Validator under Bulgarian VAT Act (чл. 124 ЗДДС).

Implements statutory requirements of:
- чл. 124, ал. 4 ЗДДС:
  "Регистрираното лице е длъжно да отрази получените данъчни документи
   в дневника за покупки за данъчния период, през който са издадени,
   или в един от следващите 12 данъчни периода."
- чл. 124, ал. 5 ЗДДС (връзка данъчно събитие - авансово плащане).
- чл. 72, ал. 1 ЗДДС (упражняване на правото на приспадане на данъчен кредит).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import logging
import re
from typing import Any

logger = logging.getLogger("invoice_ocr.tax_period")


class TaxPeriodStatus(str, Enum):
    """Compliance status for invoice tax period under Art. 124 VAT Act."""
    CURRENT_PERIOD = "current_period"                       # Съвпада с текущия данъчен период (0 месеца разлика)
    PRIOR_PERIOD_ALLOWED_12M = "prior_period_allowed_12m"   # В рамките на допустимите 12 месеца назад по чл. 124, ал. 4
    EXPIRED_PERIOD_OVER_12M = "expired_period_over_12m"     # Изтекъл 12-месечен срок! Данъчният кредит е погасен
    FUTURE_PERIOD_INVALID = "future_period_invalid"         # Невалидна бъдеща дата спрямо данъчния период


@dataclass(frozen=True)
class TaxPeriodValidationResult:
    """Detailed result of tax period verification under Art. 124 of Bulgarian VAT Act."""
    status: TaxPeriodStatus
    is_valid_for_filing: bool
    can_claim_tax_credit: bool
    period_offset_months: int
    doc_date: str
    tax_event_date: str
    target_period: str                                      # e.g. '202608' (ГГГГММ)
    target_period_formatted: str                            # e.g. '08.2026 г.'
    recommended_vat_cell: int                               # 10 (20% пълен ДК), 12 (9%), 16 (без право на ДК)
    severity: str                                           # 'ok', 'info', 'warning', 'error'
    message: str
    legal_basis: str = "чл. 124, ал. 4 ЗДДС"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "is_valid_for_filing": self.is_valid_for_filing,
            "can_claim_tax_credit": self.can_claim_tax_credit,
            "period_offset_months": self.period_offset_months,
            "doc_date": self.doc_date,
            "tax_event_date": self.tax_event_date,
            "target_period": self.target_period,
            "target_period_formatted": self.target_period_formatted,
            "recommended_vat_cell": self.recommended_vat_cell,
            "severity": self.severity,
            "message": self.message,
            "legal_basis": self.legal_basis,
        }


def _parse_iso_or_bg_date(d_val: Any) -> date | None:
    """Parse various Bulgarian and ISO date representations into datetime.date."""
    if not d_val:
        return None
    if isinstance(d_val, date) and not isinstance(d_val, datetime):
        return d_val
    if isinstance(d_val, datetime):
        return d_val.date()

    s = str(d_val).strip()
    # 2026-08-16
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # 16.08.2026 or 16/08/2026
    m = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass

    return None


def parse_target_vat_period(period: str | None = None) -> tuple[int, int, str]:
    """Parse target VAT reporting period.
    
    Accepts:
      - '202608' -> (2026, 8, '202608')
      - '2026-08' -> (2026, 8, '202608')
      - '08.2026' -> (2026, 8, '202608')
      - None -> current system year/month
    """
    if not period or not str(period).strip():
        now = datetime.now()
        return now.year, now.month, f"{now.year}{now.month:02d}"

    clean = str(period).strip().replace(" ", "")
    # '202608'
    if re.match(r"^\d{6}$", clean):
        y = int(clean[:4])
        m = int(clean[4:])
        return y, m, f"{y:04d}{m:02d}"

    # '2026-08' or '2026/08'
    m_iso = re.match(r"^(\d{4})[-/.](\d{1,2})$", clean)
    if m_iso:
        y = int(m_iso.group(1))
        m = int(m_iso.group(2))
        return y, m, f"{y:04d}{m:02d}"

    # '08.2026'
    m_bg = re.match(r"^(\d{1,2})[-/.](\d{4})$", clean)
    if m_bg:
        m = int(m_bg.group(1))
        y = int(m_bg.group(2))
        return y, m, f"{y:04d}{m:02d}"

    # Fallback
    now = datetime.now()
    return now.year, now.month, f"{now.year}{now.month:02d}"


class TaxPeriodValidator:
    """Validator for tax periods under Bulgarian VAT law."""

    @staticmethod
    def validate(
        invoice: Any,
        target_period: str | None = None,
        prefer_tax_event_date: bool = True,
    ) -> TaxPeriodValidationResult:
        """Validate an invoice against a target VAT tax period.
        
        Args:
            invoice: Invoice dataclass instance or dict.
            target_period: Target filing period (e.g. '202608' or '2026-08').
            prefer_tax_event_date: If True, prioritize date_tax_event over date_issued.
            
        Returns:
            TaxPeriodValidationResult with complete statutory status and diagnostic message.
        """
        # 1. Parse target period
        target_y, target_m, period_code = parse_target_vat_period(target_period)
        period_formatted = f"{target_m:02d}.{target_y} г."

        # 2. Extract document dates
        doc_dt_str = None
        event_dt_str = None

        if hasattr(invoice, "invoice_metadata"):
            meta = invoice.invoice_metadata
            doc_dt_str = getattr(meta, "date_issued", None)
            event_dt_str = getattr(meta, "date_tax_event", None)
        elif isinstance(invoice, dict):
            meta = invoice.get("invoice_metadata") or invoice
            doc_dt_str = meta.get("date_issued") or meta.get("invoiceDate")
            event_dt_str = meta.get("date_tax_event") or meta.get("taxEventDate")

        parsed_doc_dt = _parse_iso_or_bg_date(doc_dt_str)
        parsed_event_dt = _parse_iso_or_bg_date(event_dt_str)

        effective_dt = None
        if prefer_tax_event_date and parsed_event_dt:
            effective_dt = parsed_event_dt
        elif parsed_doc_dt:
            effective_dt = parsed_doc_dt
        elif parsed_event_dt:
            effective_dt = parsed_event_dt
        else:
            effective_dt = date(target_y, target_m, 1)

        str_doc_date = parsed_doc_dt.isoformat() if parsed_doc_dt else (doc_dt_str or "")
        str_event_date = parsed_event_dt.isoformat() if parsed_event_dt else (event_dt_str or str_doc_date)

        # 3. Calculate difference in calendar months
        # offset = (target_y - eff_y) * 12 + (target_m - eff_m)
        eff_y = effective_dt.year
        eff_m = effective_dt.month
        month_offset = (target_y - eff_y) * 12 + (target_m - eff_m)

        # 4. Classify according to Art. 124 of Bulgarian VAT Act (ЗДДС)
        if month_offset == 0:
            # Current VAT period
            return TaxPeriodValidationResult(
                status=TaxPeriodStatus.CURRENT_PERIOD,
                is_valid_for_filing=True,
                can_claim_tax_credit=True,
                period_offset_months=0,
                doc_date=str_doc_date,
                tax_event_date=str_event_date,
                target_period=period_code,
                target_period_formatted=period_formatted,
                recommended_vat_cell=10,
                severity="ok",
                message=(
                    f"Датата на данъчното събитие ({str_event_date}) съвпада с текущия "
                    f"данъчен период {period_formatted}. Документът се включва с пълен данъчен кредит "
                    f"в клетка 10 на Дневника за покупки по чл. 124, ал. 4 ЗДДС."
                ),
                legal_basis="чл. 124, ал. 4 ЗДДС",
            )

        elif 1 <= month_offset <= 12:
            # Prior period within allowed 12 months under Art. 124 (4)
            return TaxPeriodValidationResult(
                status=TaxPeriodStatus.PRIOR_PERIOD_ALLOWED_12M,
                is_valid_for_filing=True,
                can_claim_tax_credit=True,
                period_offset_months=month_offset,
                doc_date=str_doc_date,
                tax_event_date=str_event_date,
                target_period=period_code,
                target_period_formatted=period_formatted,
                recommended_vat_cell=10,
                severity="info",
                message=(
                    f"Документът е издаден в предходен данъчен период ({str_event_date}, "
                    f"отместване от {month_offset} месеца), но попада в законовия 12-месечен прозорец "
                    f"съгласно чл. 124, ал. 4 ЗДДС. Приспадането на данъчен кредит в текущия период "
                    f"{period_formatted} е напълно правомерно."
                ),
                legal_basis="чл. 124, ал. 4 ЗДДС и чл. 72, ал. 1 ЗДДС",
            )

        elif month_offset > 12:
            # Expired period > 12 months. Tax credit is legally extinguished.
            return TaxPeriodValidationResult(
                status=TaxPeriodStatus.EXPIRED_PERIOD_OVER_12M,
                is_valid_for_filing=True,  # May still be recorded in cell 16
                can_claim_tax_credit=False,
                period_offset_months=month_offset,
                doc_date=str_doc_date,
                tax_event_date=str_event_date,
                target_period=period_code,
                target_period_formatted=period_formatted,
                recommended_vat_cell=16,
                severity="warning",
                message=(
                    f"ВНИМАНИЕ: Изтекъл е 12-месечният преклузивен срок по чл. 124, ал. 4 ЗДДС! "
                    f"Датата на данъчното събитие е {str_event_date} ({month_offset} месеца преди {period_formatted}). "
                    f"Правото на приспадане на данъчен кредит е погасено. Документът НЕ може да се отрази "
                    f"в клетка 10, а единствено в клетка 16 (ДО на получени доставки без право на данъчен кредит)."
                ),
                legal_basis="чл. 124, ал. 4 ЗДДС във вр. с чл. 72, ал. 2 ЗДДС",
            )

        else:
            # Future period (month_offset < 0): invoice date is after the filing period
            return TaxPeriodValidationResult(
                status=TaxPeriodStatus.FUTURE_PERIOD_INVALID,
                is_valid_for_filing=False,
                can_claim_tax_credit=False,
                period_offset_months=month_offset,
                doc_date=str_doc_date,
                tax_event_date=str_event_date,
                target_period=period_code,
                target_period_formatted=period_formatted,
                recommended_vat_cell=0,
                severity="error",
                message=(
                    f"ГРЕШКА: Датата на документа/данъчното събитие ({str_event_date}) е в бъдещето "
                    f"спрямо декларирания данъчен период {period_formatted} (разлика: {abs(month_offset)} месеца напред). "
                    f"Документът не може да бъде включен в този данъчен период по ЗДДС. "
                    f"Преместете документа в периода на издаването му."
                ),
                legal_basis="чл. 124, ал. 4 ЗДДС и чл. 11, ал. 1 ЗДДС",
            )

    @classmethod
    def validate_batch(
        cls,
        invoices: Iterable[Any],
        target_period: str | None = None,
    ) -> dict[str, Any]:
        """Validate an entire batch of invoices against a tax period."""
        results = [cls.validate(inv, target_period=target_period) for inv in invoices]
        y, m, code = parse_target_vat_period(target_period)

        current_count = sum(1 for r in results if r.status == TaxPeriodStatus.CURRENT_PERIOD)
        prior_count = sum(1 for r in results if r.status == TaxPeriodStatus.PRIOR_PERIOD_ALLOWED_12M)
        expired_count = sum(1 for r in results if r.status == TaxPeriodStatus.EXPIRED_PERIOD_OVER_12M)
        future_count = sum(1 for r in results if r.status == TaxPeriodStatus.FUTURE_PERIOD_INVALID)

        overall_valid = future_count == 0

        return {
            "target_period": code,
            "target_period_formatted": f"{m:02d}.{y} г.",
            "total_documents": len(results),
            "is_batch_valid": overall_valid,
            "summary": {
                "current_period_count": current_count,
                "prior_period_allowed_12m_count": prior_count,
                "expired_period_count": expired_count,
                "future_period_invalid_count": future_count,
            },
            "results": [r.to_dict() for r in results],
        }


def validate_tax_period(invoice: Any, target_period: str | None = None) -> TaxPeriodValidationResult:
    """Convenience helper to validate tax period compliance."""
    return TaxPeriodValidator.validate(invoice, target_period=target_period)
