"""Currency conversion and dual-currency parity checks for EUR and BGN."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from .constants import FIXED_EUR_BGN_RATE

def convert_eur_to_bgn(eur_amount: Decimal | float | int | str) -> Decimal:
    """Convert EUR to BGN using the statutory fixed parity under Art. 34/35 of the Euro Introduction Act (ЗВЕ).

    Formula: BGN = round(EUR × 1.95583, 2)
    """
    if not isinstance(eur_amount, Decimal):
        eur_amount = Decimal(str(eur_amount))
    return (eur_amount * FIXED_EUR_BGN_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def convert_bgn_to_eur(bgn_amount: Decimal | float | int | str) -> Decimal:
    """Convert BGN to EUR using the statutory fixed parity under Art. 34/35 of the Euro Introduction Act (ЗВЕ).

    Formula: EUR = round(BGN / 1.95583, 2)
    """
    if not isinstance(bgn_amount, Decimal):
        bgn_amount = Decimal(str(bgn_amount))
    return (bgn_amount / FIXED_EUR_BGN_RATE).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def verify_dual_currency_parity(
    total_eur: Decimal | float | int | str,
    total_bgn: Decimal | float | int | str,
    tolerance: Decimal = Decimal("0.02"),
) -> bool:
    """Verify whether EUR and BGN totals satisfy statutory parity: BGN = round(EUR * 1.95583, 2)."""
    if not isinstance(total_eur, Decimal):
        total_eur = Decimal(str(total_eur))
    if not isinstance(total_bgn, Decimal):
        total_bgn = Decimal(str(total_bgn))
    expected_bgn = convert_eur_to_bgn(total_eur)
    return abs(total_bgn - expected_bgn) <= tolerance


