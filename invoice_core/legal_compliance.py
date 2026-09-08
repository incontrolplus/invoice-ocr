"""Statutory legal and tax compliance validator for Bulgarian invoices.

Implements automated verification under:
  - Accountancy Act (Закон за счетоводството - ЗСч, чл. 6 и 7)
  - VAT Act (Закон за данък върху добавената стойност - ЗДДС, чл. 114, 113, 86, 163а, 141, 28, 53)
  - BNB banking requisites & ISO 7064 Modulo 97-10 IBAN validation
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import logging
import re
from typing import Any

from .models import (
    BankComplianceDetails,
    Invoice,
    LegalComplianceReport,
    LogicalLine,
    MoneyAmount,
    OcrToken,
    Party,
    SignatoriesComplianceDetails,
    ValidationIssue,
    VatRegimeComplianceDetails,
)
from .normalizers import (
    normalize_bic,
    normalize_iban,
    validate_eik,
    validate_iban_modulo97,
)

logger = logging.getLogger("invoice_ocr.legal_compliance")


# ===========================================================================
# 1. Official Bulgarian Banks Registry & BIC / IBAN Cross-Verification
# ===========================================================================

@dataclass(frozen=True)
class BankInfo:
    """Official Bulgarian bank metadata."""
    code: str                  # 4-letter BAFO / IBAN code (e.g. UNCR)
    name: str                  # Official Bulgarian commercial bank name
    bic: str                   # Primary BIC/SWIFT code
    aliases: tuple[str, ...]   # Search / token aliases


# Official directory of commercial banks and foreign branches licensed by BNB in Bulgaria
BULGARIAN_BANKS: tuple[BankInfo, ...] = (
    BankInfo("UNCR", "УниКредит Булбанк АД", "UNCRBGSF", ("unicredit", "булбанк", "уникредит", "unicredit bulbank")),
    BankInfo("STSA", "Банка ДСК АД", "STSABGSF", ("дск", "dsk", "dsk bank", "банка дск", "dskbank")),
    BankInfo("UBBS", "Обединена българска банка АД", "UBBSBGSF", ("обб", "ubb", "united bulgarian bank", "о.б.б.")),
    BankInfo("BPBI", "Юробанк България АД (Пощенска банка)", "BPBIBGSF", ("пощенска", "пощенска банка", "postbank", "eurobank", "юробанк")),
    BankInfo("FINV", "Първа инвестиционна банка АД (Fibank)", "FINVBGSF", ("пиб", "fibank", "първа инвестиционна", "fibank.bg")),
    BankInfo("CECB", "Централна кооперативна банка АД (ЦКБ)", "CECBBGSF", ("цкб", "ccb", "централна кооперативна")),
    BankInfo("CEKO", "Централна кооперативна банка АД (ЦКБ)", "CEKOBGSF", ("цкб", "ccb")),
    BankInfo("PRCB", "ПроКредит Банк (България) ЕАД", "PRCBBGSF", ("прокредит", "procredit", "procredit bank")),
    BankInfo("PRIB", "ПроКредит Банк (България) ЕАД", "PRIBBGSF", ("прокредит", "procredit")),
    BankInfo("BACB", "Българо-американска кредитна банка АД (БАКБ)", "BACBBGSF", ("бакб", "bacb", "българо-американска")),
    BankInfo("BACX", "Българо-американска кредитна банка АД (БАКБ)", "BACXBGSF", ("бакб", "bacb")),
    BankInfo("IHTT", "Инвестбанк АД", "IHTTBGSF", ("инвестбанк", "investbank")),
    BankInfo("TTBB", "Инвестбанк АД", "TTBBBGSF", ("инвестбанк", "investbank")),
    BankInfo("TEXI", "Тексим Банк АД", "TEXIBGSF", ("тексим", "texim", "тексим банк")),
    BankInfo("IABG", "Интернешънъл Асет Банк АД", "IABGBGSF", ("асет банк", "asset bank", "интернешънъл асет")),
    BankInfo("IORT", "Интернешънъл Асет Банк АД", "IORTBGSF", ("асет банк", "asset bank")),
    BankInfo("TBBI", "Ти Би Ай Банк ЕАД (TBI Bank)", "TBBIBGSF", ("ти би ай", "tbi", "tbi bank")),
    BankInfo("BUIN", "Алианц Банк България АД", "BUINBGSF", ("алианц", "allianz", "allianz bank")),
    BankInfo("BNPA", "БНП Париба С.А. - клон София", "BNPABGSF", ("бнп париба", "bnp paribas")),
    BankInfo("INGB", "ИНГ Банк Н.В. - клон София", "INGBBGSF", ("инг", "ing bank", "инг банк")),
    BankInfo("CITI", "Ситибанк Европа АД - клон България", "CITIBGSF", ("ситибанк", "citibank")),
    BankInfo("SOMB", "Общинска банка АД", "SOMBBGSF", ("общинска банка", "municipal bank")),
    BankInfo("BFTB", "Българска банка за развитие АД (ББР)", "BFTBBGSF", ("ббр", "банка за развитие", "development bank")),
    BankInfo("BNBG", "Българска народна банка (БНБ)", "BNBGBGSF", ("бнб", "bnb", "българска народна")),
    BankInfo("BGUS", "Българска народна банка (БНБ)", "BGUSBGSF", ("бнб", "bnb")),
    BankInfo("CRES", "Токуда Банк АД", "CRESBGSF", ("токуда", "tokuda", "токуда банк")),
    BankInfo("RZBB", "Райфайзенбанк (България) ЕАД / ОББ", "RZBBBGSF", ("райфайзен", "raiffeisen", "кбц", "kbc")),
    BankInfo("DEMI", "Търговска Банка Д АД", "DEMIBGSF", ("д банк", "търговска банка д", "d bank")),
    BankInfo("VABN", "Варенголд Банк АГ - клон София", "VABNBGSF", ("варенголд", "varengold")),
    BankInfo("BAPX", "Алфа Банка - клон България (исторически / Юробанк)", "BAPXBGSF", ("алфа банка", "alpha bank")),
    BankInfo("PIRB", "Банка Пиреос България АД (исторически / Юробанк)", "PIRBBGSF", ("пиреос", "piraeus")),
    BankInfo("KORB", "Корпоративна търговска банка АД (в несъстоятелност)", "KORBBGSF", ("ктб", "корпоративна търговска")),
)

BANK_CODE_MAP: dict[str, BankInfo] = {b.code: b for b in BULGARIAN_BANKS}
BIC_MAP: dict[str, BankInfo] = {b.bic: b for b in BULGARIAN_BANKS}

# Bank code alias equivalence groups (for legacy BAFO migration or multi-code banks)
BANK_ALIAS_GROUPS: list[set[str]] = [
    {"CECB", "CEKO"},
    {"PRCB", "PRIB"},
    {"BACB", "BACX"},
    {"IHTT", "TTBB"},
    {"IABG", "IORT"},
    {"BNBG", "BGUS"},
    {"RZBB", "UBBS"},  # Merged Raiffeisenbank into UBB
    {"BPBI", "BAPX", "PIRB"},  # Merged Alpha & Piraeus into Postbank
]


def find_bank_by_code(code: str | None) -> BankInfo | None:
    """Find bank information by 4-letter BAFO bank identifier."""
    if not code:
        return None
    return BANK_CODE_MAP.get(code.upper().strip())


def find_bank_by_bic(bic: str | None) -> BankInfo | None:
    """Find bank information by 8 or 11 character BIC code."""
    if not bic:
        return None
    cleaned = bic.upper().strip()
    if cleaned in BIC_MAP:
        return BIC_MAP[cleaned]
    prefix = cleaned[:4]
    return BANK_CODE_MAP.get(prefix)


def find_bank_by_name(name: str | None) -> BankInfo | None:
    """Search for Bulgarian bank by name substring or alias."""
    if not name:
        return None
    cleaned = name.lower()
    for bank in BULGARIAN_BANKS:
        if bank.name.lower() in cleaned:
            return bank
        for alias in bank.aliases:
            if alias in cleaned:
                return bank
    return None


def are_bank_codes_compatible(code_a: str, code_b: str) -> bool:
    """Check if two bank codes represent the same bank or institutional merger."""
    ca, cb = code_a.upper(), code_b.upper()
    if ca == cb:
        return True
    for grp in BANK_ALIAS_GROUPS:
        if ca in grp and cb in grp:
            return True
    return False


def validate_bank_requisites(
    iban: str | None,
    bic: str | None,
    bank_name: str | None = None,
    payment_method: str | None = None,
) -> tuple[BankComplianceDetails, list[ValidationIssue]]:
    """Validate banking details, execute Mod-97 checksum, and identify servicing bank."""
    issues: list[ValidationIssue] = []
    details = BankComplianceDetails()

    clean_iban = "".join(iban.split()).upper() if iban else None
    clean_bic = "".join(bic.split()).upper() if bic else None

    details.iban = clean_iban
    details.bic = clean_bic
    details.bank_name = bank_name

    # 1. IBAN Validation
    if clean_iban:
        if not clean_iban.startswith("BG"):
            # Foreign IBAN
            if len(clean_iban) < 15 or len(clean_iban) > 34:
                issues.append(ValidationIssue(
                    code="INVALID_IBAN_FORMAT",
                    message=f"Международният IBAN '{clean_iban}' има невалидна дължина ({len(clean_iban)} знака).",
                    severity="error",
                    field="payment_details.iban",
                    detected_value=clean_iban,
                ))
            elif not validate_iban_modulo97(clean_iban):
                issues.append(ValidationIssue(
                    code="INVALID_IBAN_MOD97",
                    message=f"Невалидна контролна сума на IBAN '{clean_iban}' по алгоритъма ISO 7064 Modulo 97-10.",
                    severity="error",
                    field="payment_details.iban",
                    detected_value=clean_iban,
                ))
            else:
                details.is_iban_valid = True
        else:
            # Bulgarian IBAN: strictly 22 characters
            # Format: BG + 2 check digits + 4 letters bank code + 4 digits branch + 2 digits type + 8 chars account
            if len(clean_iban) != 22 or not re.match(r'^BG\d{2}[A-Z]{4}\d{6}[A-Z0-9]{8}$', clean_iban):
                issues.append(ValidationIssue(
                    code="INVALID_IBAN_FORMAT",
                    message=f"Българският IBAN '{clean_iban}' не съответства на 22-значния законов формат (BG + 2 контролни цифри + 4 букви банков код + 14 цифри/символи).",
                    severity="error",
                    field="payment_details.iban",
                    detected_value=clean_iban,
                    expected_value="BG + 20 знака (общо 22)",
                ))
            elif not validate_iban_modulo97(clean_iban):
                issues.append(ValidationIssue(
                    code="INVALID_IBAN_MOD97",
                    message=f"Невалидна контролна сума на български IBAN '{clean_iban}' съгласно алгоритъм ISO 7064 Modulo 97-10.",
                    severity="error",
                    field="payment_details.iban",
                    detected_value=clean_iban,
                ))
            else:
                details.is_iban_valid = True
                bank_code = clean_iban[4:8]
                details.bank_code = bank_code

                # Identify servicing bank
                bank_info = find_bank_by_code(bank_code)
                if bank_info:
                    details.bank_recognized = True
                    details.servicing_bank = bank_info.name
                    if not details.bank_name:
                        details.bank_name = bank_info.name
                else:
                    details.bank_recognized = False
                    issues.append(ValidationIssue(
                        code="UNRECOGNIZED_BULGARIAN_BANK",
                        message=f"Банковият код '{bank_code}' в IBAN '{clean_iban}' не е открит в официалния регистър на БНБ за лицензирани търговски банки.",
                        severity="warning",
                        field="payment_details.iban",
                        detected_value=bank_code,
                    ))

    # 2. BIC Validation
    if clean_bic:
        if not re.match(r'^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$', clean_bic):
            issues.append(ValidationIssue(
                code="INVALID_BIC_FORMAT",
                message=f"BIC/SWIFT кодът '{clean_bic}' има невалиден формат (очаква се 8 или 11 буквено-цифрови символа).",
                severity="error",
                field="payment_details.bic",
                detected_value=clean_bic,
            ))
        else:
            details.is_bic_valid = True
            bic_bank_info = find_bank_by_bic(clean_bic)
            if bic_bank_info and not details.servicing_bank:
                details.servicing_bank = bic_bank_info.name
                details.bank_recognized = True

    # 3. Cross-check IBAN vs BIC
    if details.is_iban_valid and details.is_bic_valid and clean_iban and clean_bic and clean_iban.startswith("BG"):
        iban_code = clean_iban[4:8]
        bic_code = clean_bic[:4]
        if not are_bank_codes_compatible(iban_code, bic_code):
            issues.append(ValidationIssue(
                code="IBAN_BIC_BANK_MISMATCH",
                message=(
                    f"Несъответствие между банковия код в IBAN ('{iban_code}') и посочения BIC код ('{clean_bic}'). "
                    f"Очаква се BIC с префикс '{iban_code}'."
                ),
                severity="error",
                field="payment_details.iban",
                detected_value=f"IBAN:{clean_iban} vs BIC:{clean_bic}",
            ))

    # 4. Check if bank transfer was selected but no IBAN was provided
    if payment_method:
        meth_low = payment_method.lower()
        if any(k in meth_low for k in ("превод", "по банков път", "bank transfer", "банкова сметка")):
            if not clean_iban:
                issues.append(ValidationIssue(
                    code="MISSING_IBAN_FOR_BANK_TRANSFER",
                    message="Указан е начин на плащане по банков път, но във фактурата липсва попълнен IBAN на банкова сметка.",
                    severity="warning",
                    field="payment_details.iban",
                ))

    details.issues = [f"{iss.code}: {iss.message}" for iss in issues]
    return details, issues


# ===========================================================================
# 2. Statutory Zero / Non-Charged VAT Regimes Catalog (ЗДДС чл. 114)
# ===========================================================================

@dataclass(frozen=True)
class VatLegalGround:
    """Definition of a statutory ground for 0% or uncharged VAT."""
    code: str
    article: str
    regime_name: str
    patterns: tuple[re.Pattern, ...]
    description: str


# Authoritative catalog of legal grounds under Bulgarian VAT Act (ЗДДС) & EU Directives
VAT_LEGAL_GROUNDS_CATALOG: tuple[VatLegalGround, ...] = (
    # 1. Reverse Charge for scrap / waste (чл. 163а, ал. 1 ЗДДС)
    VatLegalGround(
        code="CHL_163A_REVERSE_CHARGE_SCRAP",
        article="чл. 163а, ал. 1 от ЗДДС",
        regime_name="Обратно начисляване на отпадъци / скрап (чл. 163а, ал. 1 ЗДДС)",
        patterns=(
            re.compile(r'(?i)\b(?:чл\.?\s*163\s*а\b|163а\b).*?(?:скрап|отпадъц|черни|цветни\s*метали)', re.DOTALL),
            re.compile(r'(?i)\b(?:приложение\s*№?\s*2\b|обратно\s+начисляване).*?(?:скрап|отпадъц)', re.DOTALL),
            re.compile(r'(?i)\bчл\.?\s*163\s*а\s*,?\s*ал\.?\s*1\b'),
        ),
        description="Доставка на отпадъци по Приложение № 2, част I от ЗДДС - данъкът е изискуем от получателя",
    ),

    # 2. Reverse Charge for grain and technical crops (чл. 163а, ал. 2 ЗДДС)
    VatLegalGround(
        code="CHL_163A_REVERSE_CHARGE_GRAIN",
        article="чл. 163а, ал. 2 от ЗДДС",
        regime_name="Обратно начисляване на зърнени и технически култури (чл. 163а, ал. 2 ЗДДС)",
        patterns=(
            re.compile(r'(?i)\b(?:чл\.?\s*163\s*а\b|163а\b).*?(?:зърн|пшениц|царевиц|слънчоглед|рапиц|ечемик)', re.DOTALL),
            re.compile(r'(?i)\b(?:приложение\s*№?\s*2\b|обратно\s+начисляване).*?(?:зърн|пшениц|царевиц|слънчоглед|рапиц)', re.DOTALL),
            re.compile(r'(?i)\bчл\.?\s*163\s*а\s*,?\s*ал\.?\s*2\b'),
        ),
        description="Доставка на зърнени и технически култури по Приложение № 2, част II от ЗДДС - данъкът е изискуем от получателя",
    ),

    # 3. General Reverse Charge under чл. 163а (Annex 2)
    VatLegalGround(
        code="CHL_163A_REVERSE_CHARGE",
        article="чл. 163а от ЗДДС",
        regime_name="Обратно начисляване по чл. 163а от ЗДДС (Приложение № 2)",
        patterns=(
            re.compile(r'(?i)\bчл\.?\s*163\s*а\b'),
            re.compile(r'(?i)\b163а\s*от\s*зддс\b'),
            re.compile(r'(?i)\bприложение\s*№?\s*2\s*от\s*зддс\b'),
        ),
        description="Обратно начисляване съгласно чл. 163а от ЗДДС за стоки по Приложение № 2",
    ),

    # 4. Cross-border reverse charge under Art. 82(2) ЗДДС
    VatLegalGround(
        code="CHL_82_AL_2_REVERSE_CHARGE",
        article="чл. 82, ал. 2 от ЗДДС",
        regime_name="Обратно начисляване от чуждестранно лице (чл. 82, ал. 2 ЗДДС)",
        patterns=(
            re.compile(r'(?i)\bчл\.?\s*82\s*,?\s*ал\.?\s*2\b'),
            re.compile(r'(?i)\bчл\.?\s*82\(2\)\b'),
            re.compile(r'(?i)\btax\s+liability\s+shifted\b'),
        ),
        description="Доставка от данъчно задължено лице, неустановено в страната - данъкът се дължи от получателя",
    ),

    # 5. General Reverse Charge clause
    VatLegalGround(
        code="REVERSE_CHARGE_GENERAL",
        article="чл. 82 / чл. 163а от ЗДДС",
        regime_name="Обратно начисляване (Reverse Charge)",
        patterns=(
            re.compile(r'(?i)\bобратно\s+начисляване\b'),
            re.compile(r'(?i)\breverse\s+charge\b'),
            re.compile(r'(?i)\bсамоначисляване\s+на\s+ддс\b'),
        ),
        description="Обратно начисляване съгласно ЗДДС - данъкът е изискуем от получателя",
    ),

    # 6. Intra-community supply (ВОД) under Art. 53 ЗДДС (0% VAT)
    VatLegalGround(
        code="CHL_53_VOD",
        article="чл. 53 от ЗДДС",
        regime_name="Вътреобщностна доставка / ВОД (чл. 53 ЗДДС)",
        patterns=(
            re.compile(r'(?i)\bчл\.?\s*53\s*(?:от\s*зддс)?\b'),
            re.compile(r'(?i)\bвод\b'),
            re.compile(r'(?i)\bвътреобщностна\s+доставка\b'),
            re.compile(r'(?i)\bintra-community\s+supply\b'),
            re.compile(r'(?i)\bart(?:icle)?\.?\s*138\b'),
        ),
        description="Вътреобщностна доставка (ВОД) на стоки с нулева ставка по чл. 53 от ЗДДС / чл. 138 Директива 2006/112/ЕО",
    ),

    # 7. Intra-community acquisition (ВОП) under Art. 62 / 84 ЗДДС
    VatLegalGround(
        code="CHL_62_84_VOP",
        article="чл. 62 / чл. 84 от ЗДДС",
        regime_name="Вътреобщностно придобиване / ВОП (чл. 62 / 84 ЗДДС)",
        patterns=(
            re.compile(r'(?i)\bчл\.?\s*62\b'),
            re.compile(r'(?i)\bчл\.?\s*84\b'),
            re.compile(r'(?i)\bвоп\b'),
            re.compile(r'(?i)\bвътреобщностно\s+придобиване\b'),
            re.compile(r'(?i)\bintra-community\s+acquisition\b'),
        ),
        description="Вътреобщностно придобиване (ВОП) съгласно чл. 62 и чл. 84 от ЗДДС",
    ),

    # 8. Triangular operations under Art. 141 ЗДДС
    VatLegalGround(
        code="CHL_141_TRIANGULAR",
        article="чл. 141 от ЗДДС",
        regime_name="Тристранни операции (чл. 141 ЗДДС)",
        patterns=(
            re.compile(r'(?i)\bчл\.?\s*141\s*(?:от\s*зддс)?\b'),
            re.compile(r'(?i)\bтристранна\s+операция\b'),
            re.compile(r'(?i)\btriangular\s+operation\b'),
            re.compile(r'(?i)\bart(?:icle)?\.?\s*141\b'),
        ),
        description="Тристранна операция съгласно чл. 141 от ЗДДС - данъкът се дължи от крайния придобиващ в третата държава членка",
    ),

    # 9. Direct export outside EU under Art. 28 ЗДДС (0% VAT)
    VatLegalGround(
        code="CHL_28_EXPORT",
        article="чл. 28 от ЗДДС",
        regime_name="Износ на стоки извън ЕС (чл. 28 ЗДДС)",
        patterns=(
            re.compile(r'(?i)\bчл\.?\s*28\s*(?:от\s*зддс)?\b'),
            re.compile(r'(?i)\bчл\.?\s*28,?\s*т\.?\s*[12]\b'),
            re.compile(r'(?i)\bизнос\b'),
            re.compile(r'(?i)\bexport\b'),
            re.compile(r'(?i)\bдоставка\s+извън\s+(?:ес|общността)\b'),
            re.compile(r'(?i)\bart(?:icle)?\.?\s*146\b'),
        ),
        description="Износ на стоки извън територията на Европейския съюз с нулева ставка съгласно чл. 28 от ЗДДС",
    ),

    # 10. International transport under Art. 29, 30, 31 ЗДДС
    VatLegalGround(
        code="CHL_29_31_INTL_TRANSPORT",
        article="чл. 29, 30, 31 от ЗДДС",
        regime_name="Международен транспорт (чл. 29-31 ЗДДС)",
        patterns=(
            re.compile(r'(?i)\bчл\.?\s*29\b'),
            re.compile(r'(?i)\bчл\.?\s*30\b'),
            re.compile(r'(?i)\bчл\.?\s*31\b'),
            re.compile(r'(?i)\bмеждународен\s+транспорт\b'),
            re.compile(r'(?i)\binternational\s+transport\b'),
        ),
        description="Международен транспорт на стоки или пътници с нулева ставка по чл. 29, 30, 31 от ЗДДС",
    ),

    # 11. Non-VAT registered supplier under Art. 113(9) ЗДДС
    VatLegalGround(
        code="CHL_113_AL_9_NOT_REGISTERED",
        article="чл. 113, ал. 9 от ЗДДС",
        regime_name="Неначисляване поради липса на регистрация (чл. 113, ал. 9 ЗДДС)",
        patterns=(
            re.compile(r'(?i)\bчл\.?\s*113\s*,?\s*ал\.?\s*9\b'),
            re.compile(r'(?i)\bчл\.?\s*113\(9\)\b'),
            re.compile(r'(?i)\bнерегистрирано\s*(?:по\s*зддс)?\s*лице\b'),
            re.compile(r'(?i)\bлицето\s+не\s+е\s+регистрирано\s+по\s+зддс\b'),
            re.compile(r'(?i)\bдоставчикът\s+не\s+е\s+регистриран\s+по\s+зддс\b'),
            re.compile(r'(?i)\bне\s+е\s+рег\.?\s*по\s*зддс\b'),
        ),
        description="Неначисляване на ДДС поради липса на регистрация на доставчика по чл. 113, ал. 9 от ЗДДС",
    ),

    # 12. Exempt supplies under Chapter Four (Art. 38 - 50 ЗДДС)
    VatLegalGround(
        code="EXEMPT_SUPPLY_CHAPTER_4",
        article="Глава четвърта от ЗДДС (чл. 38 - 50)",
        regime_name="Освободени доставки по Глава 4 от ЗДДС",
        patterns=(
            re.compile(r'(?i)\bчл\.?\s*46\b'),  # Financial
            re.compile(r'(?i)\bчл\.?\s*47\b'),  # Insurance
            re.compile(r'(?i)\bчл\.?\s*45\b'),  # Real estate
            re.compile(r'(?i)\bчл\.?\s*38\b'),  # Healthcare
            re.compile(r'(?i)\bчл\.?\s*39\b'),  # Social
            re.compile(r'(?i)\bчл\.?\s*41\b'),  # Education
            re.compile(r'(?i)\bчл\.?\s*50\b'),  # No tax credit
            re.compile(r'(?i)\bосвободена\s+доставка\b'),
            re.compile(r'(?i)\bфинансови\s+услуги\b'),
            re.compile(r'(?i)\bзастрахователни\s+услуги\b'),
        ),
        description="Освободена доставка от данък съгласно Глава четвърта от ЗДДС (чл. 38 - 50)",
    ),

    # 13. EU Directive 2006/112/EC
    VatLegalGround(
        code="EU_DIRECTIVE_2006_112",
        article="Директива 2006/112/ЕО",
        regime_name="Директива 2006/112/ЕО",
        patterns=(
            re.compile(r'(?i)\bdirective\s+2006/112(?:/ec)?\b'),
            re.compile(r'(?i)\bдиректива\s+2006/112(?:/ео)?\b'),
            re.compile(r'(?i)\bart(?:icle)?\.?\s*196\b'),
        ),
        description="Неначисляване на данък съгласно разпоредбите на Директива 2006/112/ЕО",
    ),

    # 14. Explicit legal grounds label with custom explanation
    VatLegalGround(
        code="EXPLICIT_STATUTORY_GROUNDS",
        article="чл. 114, ал. 1, т. 11 от ЗДДС",
        regime_name="Посочено основание за неначисляване на ДДС",
        patterns=(
            re.compile(r'(?i)\bоснование\s+за\s+(?:неначисляване|прилагане\s+на\s+нулева\s+ставка|0%\s*ддс|нулево\s+ддс)\s*[:.\-]?\s*([^\n;,]{3,80})'),
            re.compile(r'(?i)\bправно\s+основание\s*[:.\-]?\s*([^\n;,]{3,80})'),
        ),
        description="Посочено задължително законово основание за неначисляване съгласно чл. 114, ал. 1, т. 11 от ЗДДС",
    ),
)


def detect_vat_exemption_grounds(
    invoice: Invoice,
    full_text: str | None = None,
) -> tuple[VatRegimeComplianceDetails, list[ValidationIssue]]:
    """Detect and validate statutory grounds for zero or non-charged VAT under Art. 114(1)(11) ЗДДС."""
    issues: list[ValidationIssue] = []
    details = VatRegimeComplianceDetails()

    fs = invoice.financial_summary
    tb_val = fs.tax_base.amount if fs.tax_base else None
    vat_val = fs.vat_amount.amount if fs.vat_amount else None

    details.tax_base = tb_val
    details.vat_amount = vat_val

    # Detect effective VAT rate from line items or summary
    effective_vat_rate = None
    for item in invoice.line_items:
        if item.vat_rate_pct is not None:
            effective_vat_rate = item.vat_rate_pct
            break

    details.vat_rate_pct = effective_vat_rate

    # Determine whether invoice has zero or uncharged VAT
    tot_val = fs.total_amount_due.amount if fs.total_amount_due else None

    is_zero_vat = False
    if vat_val is not None and vat_val == Decimal("0"):
        is_zero_vat = True
    elif effective_vat_rate is not None and effective_vat_rate == Decimal("0"):
        is_zero_vat = True
    elif tb_val is not None and tb_val > 0 and vat_val is None:
        has_positive_vat_margin = bool(tot_val is not None and tot_val > tb_val + Decimal("0.05"))
        has_positive_item_rate = bool(effective_vat_rate is not None and effective_vat_rate > Decimal("0"))
        if not has_positive_vat_margin and not has_positive_item_rate:
            is_zero_vat = True
        elif has_positive_vat_margin and details.vat_amount is None:
            details.vat_amount = tot_val - tb_val

    details.is_zero_or_exempt = is_zero_vat

    if not is_zero_vat:
        # Standard positive VAT (20% or 9%)
        details.is_valid_basis = True
        return details, issues

    # Assemble comprehensive search corpus from invoice lines, line items, supplier, tokens
    corpus_parts = []
    if full_text:
        corpus_parts.append(full_text)
    for item in invoice.line_items:
        if item.description:
            corpus_parts.append(item.description)
    if invoice.financial_summary.total_amount_words:
        corpus_parts.append(invoice.financial_summary.total_amount_words)
    if invoice.supplier:
        if invoice.supplier.name:
            corpus_parts.append(invoice.supplier.name)
        if invoice.supplier.address:
            corpus_parts.append(invoice.supplier.address)
    if invoice.invoice_metadata.correction_reason:
        corpus_parts.append(invoice.invoice_metadata.correction_reason)

    search_corpus = " ".join(corpus_parts)

    matched_ground: VatLegalGround | None = None
    matched_snippet: str | None = None

    for ground in VAT_LEGAL_GROUNDS_CATALOG:
        for pat in ground.patterns:
            m = pat.search(search_corpus)
            if m:
                matched_ground = ground
                matched_snippet = m.group(0).strip()
                break
        if matched_ground:
            break

    if matched_ground:
        details.is_valid_basis = True
        details.legal_basis_code = matched_ground.code
        details.legal_basis_article = matched_ground.article
        details.regime_name = matched_ground.regime_name
        details.legal_basis_text = matched_snippet
    else:
        # Check foreign supplier fallback:
        # Cross-border B2B from foreign EU or non-BG entity without explicit text operates under Art. 82(2) ЗДДС
        sup_vat = (invoice.supplier.vat_number or "").upper() if invoice.supplier else ""
        if sup_vat and not sup_vat.startswith("BG") and len(sup_vat) >= 4 and sup_vat[:2].isalpha():
            details.is_valid_basis = True
            details.legal_basis_code = "CHL_82_AL_2_FOREIGN"
            details.legal_basis_article = "чл. 82, ал. 2 от ЗДДС"
            details.regime_name = "Обратно начисляване при вътреобщностни доставки от чуждестранно лице"
            details.legal_basis_text = f"Чуждестранен доставчик с ДДС номер {sup_vat} (чл. 82, ал. 2 ЗДДС / Art. 196 Directive 2006/112/EC)"
        else:
            # Statutory violation: Zero or uncharged VAT on domestic invoice without statutory grounds
            details.is_valid_basis = False
            msg = (
                "Фактурата съдържа нулева ставка или неначислено ДДС (0.00), но липсва задължително законово основание "
                "за неначисляване съгласно изискванията на чл. 114, ал. 1, т. 11 от ЗДДС, чл. 113, ал. 9 и чл. 86, ал. 3 от ЗДДС "
                "(напр. чл. 163а за скрап/зърно, чл. 82 ал. 2 за обратно начисляване, чл. 53 за ВОД, чл. 141 за тристранни операции, "
                "чл. 28 за износ, чл. 113 ал. 9 за нерегистрирано лице или чл. 38-50 за освободени доставки)."
            )
            issues.append(ValidationIssue(
                code="MISSING_VAT_EXEMPTION_REASON",
                message=msg,
                severity="error",
                field="financial_summary.vat_amount",
                detected_value="0.00",
                expected_value="Законово основание по ЗДДС (напр. чл. 163а, чл. 82 ал. 2, чл. 53, чл. 141, чл. 28)",
            ))
            details.issues.append(f"MISSING_VAT_EXEMPTION_REASON: {msg}")

    return details, issues


# ===========================================================================
# 3. Signatories & Accountability (ЗСч чл. 6, ал. 1, т. 5 и чл. 7)
# ===========================================================================

def clean_signatory_name(raw: str | None) -> str | None:
    """Clean and validate an extracted individual name (compiler, MOL, receiver)."""
    if not raw:
        return None
    cleaned = re.sub(r'^[\s:./\-#_–—]+|[\s:./\-#_–—]+$', '', raw)
    cleaned = re.sub(r'(?i)\b(?:подпис|печат|дата|стр|лв|егн|лк|тел|фактура|номер|bg|ддс|получател|доставчик)\b.*', '', cleaned).strip()
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    words = cleaned.split()
    if 1 <= len(words) <= 4:
        first_word = words[0]
        if len(first_word) >= 2 and any(c.isalpha() for c in first_word):
            return " ".join(words)
    return None


def extract_signatories_from_text(
    full_text: str,
    lines: list[LogicalLine] | None = None,
) -> tuple[str | None, str | None]:
    """Extract compiler (съставител / издал / предал) and recipient (получил / приел)."""
    compiled_by: str | None = None
    received_by: str | None = None

    comp_patterns = [
        r'(?i)(?:съставител|съставил|издал|предал|лице,\s*съставило\s*документа)\s*[:./\-]?\s*([А-Яа-яA-Za-z\s.\-]+)',
    ]
    for pat in comp_patterns:
        m = re.search(pat, full_text)
        if m:
            cand = clean_signatory_name(m.group(1))
            if cand:
                compiled_by = cand
                break

    recv_patterns = [
        r'(?i)(?:получил|приел|лице,\s*получило\s*документа)\s*[:./\-]?\s*([А-Яа-яA-Za-z\s.\-]+)',
    ]
    for pat in recv_patterns:
        m = re.search(pat, full_text)
        if m:
            cand = clean_signatory_name(m.group(1))
            if cand:
                received_by = cand
                break

    if lines:
        for idx, line in enumerate(lines):
            lt = line.text.strip()
            if not compiled_by and re.search(r'(?i)\b(?:съставител|съставил|издал|предал)\b', lt):
                sub_m = re.search(r'(?i)(?:съставител|съставил|издал|предал)\s*[:./\-]?\s*([А-Яа-яA-Za-z\s]+)', lt)
                if sub_m:
                    cand = clean_signatory_name(sub_m.group(1))
                    if cand:
                        compiled_by = cand
                if not compiled_by and idx + 1 < len(lines):
                    next_cand = clean_signatory_name(lines[idx + 1].text)
                    if next_cand and not any(k in next_cand.lower() for k in ("получил", "ддс", "банка", "iban")):
                        compiled_by = next_cand

            if not received_by and re.search(r'(?i)\b(?:получил|приел)\b', lt):
                sub_m = re.search(r'(?i)(?:получил|приел)\s*[:./\-]?\s*([А-Яа-яA-Za-z\s]+)', lt)
                if sub_m:
                    cand = clean_signatory_name(sub_m.group(1))
                    if cand:
                        received_by = cand
                if not received_by and idx + 1 < len(lines):
                    next_cand = clean_signatory_name(lines[idx + 1].text)
                    if next_cand and not any(k in next_cand.lower() for k in ("съставил", "ддс", "банка", "iban")):
                        received_by = next_cand

    return compiled_by, received_by


def validate_signatories_compliance(
    invoice: Invoice,
    full_text: str | None = None,
    lines: list[LogicalLine] | None = None,
) -> tuple[SignatoriesComplianceDetails, list[ValidationIssue]]:
    """Validate accountability and compiler requirements under Art. 6(1)(5) & Art. 7 Accountancy Act (ЗСч)."""
    issues: list[ValidationIssue] = []
    details = SignatoriesComplianceDetails()

    compiled_by = getattr(invoice.invoice_metadata, "compiled_by", None)
    received_by = getattr(invoice.invoice_metadata, "received_by", None)
    supplier_mol = invoice.supplier.mol if invoice.supplier else None
    recipient_mol = invoice.recipient.mol if invoice.recipient else None

    if (not compiled_by or not received_by) and (full_text or lines):
        text_corpus = full_text or " ".join(l.text for l in (lines or []))
        ext_comp, ext_recv = extract_signatories_from_text(text_corpus, lines=lines)
        if not compiled_by and ext_comp:
            compiled_by = ext_comp
            invoice.invoice_metadata.compiled_by = ext_comp
        if not received_by and ext_recv:
            received_by = ext_recv
            invoice.invoice_metadata.received_by = ext_recv

    details.compiled_by = compiled_by
    details.supplier_mol = supplier_mol
    details.recipient_mol = recipient_mol
    details.received_by = received_by

    has_accountable_person = bool(compiled_by or supplier_mol)
    details.is_compliant = has_accountable_person

    if not has_accountable_person:
        msg = (
            "Липсва посочено име на съставител или МОЛ на предприятието издател съгласно изискванията на "
            "чл. 6, ал. 1, т. 5 от Закона за счетоводството (ЗСч). Първичният счетоводен документ трябва да съдържа "
            "име на съставителя или материално отговорното лице (МОЛ / управител)."
        )
        issues.append(ValidationIssue(
            code="MISSING_ISSUER_NAME_OR_MOL",
            message=msg,
            severity="warning",
            field="invoice_metadata.compiled_by",
            detected_value=None,
            expected_value="Име на съставител или МОЛ на доставчика",
        ))
        details.issues.append(f"MISSING_ISSUER_NAME_OR_MOL: {msg}")

    return details, issues


# ===========================================================================
# 4. Master Legal Compliance Auditor
# ===========================================================================

def audit_legal_compliance(
    invoice: Invoice,
    tokens: list[OcrToken] | None = None,
    lines: list[LogicalLine] | None = None,
) -> tuple[LegalComplianceReport, list[ValidationIssue]]:
    """Execute complete statutory audit of an invoice under Bulgarian ЗСч and ЗДДС.
    
    Generates structured LegalComplianceReport and emits statutory issues.
    """
    all_issues: list[ValidationIssue] = []
    report = LegalComplianceReport()

    full_text_parts = []
    if tokens:
        full_text_parts.append(" ".join(t.text for t in tokens))
    elif lines:
        full_text_parts.append(" ".join(l.text for l in lines))
    full_text = " ".join(full_text_parts)

    # 1. Bank Requisites Audit
    pd = invoice.payment_details
    bank_details, bank_issues = validate_bank_requisites(
        iban=pd.iban if pd else None,
        bic=pd.bic if pd else None,
        bank_name=pd.bank_name if pd else None,
        payment_method=pd.method if pd else None,
    )
    report.bank_requisites = bank_details
    all_issues.extend(bank_issues)

    if pd:
        if bank_details.bank_code:
            pd.bank_code = bank_details.bank_code
        pd.is_iban_valid = bank_details.is_iban_valid
        pd.is_bic_valid = bank_details.is_bic_valid
        pd.bank_recognized = bank_details.bank_recognized
        if bank_details.servicing_bank and (not pd.bank_name or len(pd.bank_name) < 4):
            pd.bank_name = bank_details.servicing_bank

    # 2. VAT Regime Audit (0% / Exempt / Statutory Basis)
    vat_details, vat_issues = detect_vat_exemption_grounds(invoice, full_text=full_text)
    report.vat_regime = vat_details
    all_issues.extend(vat_issues)

    # 3. Signatories Audit (чл. 6, ал. 1, т. 5 ЗСч)
    sign_details, sign_issues = validate_signatories_compliance(invoice, full_text=full_text, lines=lines)
    report.signatories = sign_details
    all_issues.extend(sign_issues)

    # 4. Mandatory Requisites Checklist (чл. 6 ЗСч и чл. 114 ЗДДС)
    meta = invoice.invoice_metadata
    sup = invoice.supplier
    rec = invoice.recipient
    fs = invoice.financial_summary

    has_doc_num = bool(meta.invoice_number)
    has_date_issued = bool(meta.date_issued)
    has_date_tax_event = bool(meta.date_tax_event or meta.date_issued)
    has_supplier_name = bool(sup and sup.name)
    has_supplier_id = bool(
        sup and (
            (sup.eik and (validate_eik(sup.eik) or len(re.sub(r'\D', '', sup.eik)) in (9, 10, 13) or len(sup.eik.strip()) >= 3))
            or (sup.vat_number and len(sup.vat_number.strip()) >= 5)
        )
    )
    has_recipient_name = bool(rec and rec.name)
    has_recipient_id = bool(
        rec and (
            (rec.eik and (len(re.sub(r'\D', '', rec.eik)) in (9, 10, 13) or len(rec.eik.strip()) >= 3))
            or (rec.vat_number and len(rec.vat_number.strip()) >= 5)
        )
    )
    has_goods_desc = bool(len(invoice.line_items) > 0 and any(it.description for it in invoice.line_items))
    has_tax_base = bool(fs and fs.tax_base and fs.tax_base.amount is not None)
    has_vat_compliance = bool(
        not vat_details.is_zero_or_exempt
        or vat_details.is_valid_basis
    )
    has_signatory = bool(sign_details.is_compliant)

    bank_valid_or_absent = (not pd.iban) or (bank_details.is_iban_valid and not any(i.severity == "error" for i in bank_issues))

    checklist = {
        "document_number": has_doc_num,
        "date_issued": has_date_issued,
        "date_tax_event": has_date_tax_event,
        "supplier_identity": has_supplier_name and has_supplier_id,
        "recipient_identity": has_recipient_name and has_recipient_id,
        "goods_services_description": has_goods_desc,
        "tax_base": has_tax_base,
        "vat_rate_and_amount_or_grounds": has_vat_compliance,
        "compiler_or_mol": has_signatory,
        "bank_requisites_valid": bank_valid_or_absent,
    }
    report.mandatory_requisites_check = checklist

    # 5. Statutes compliance
    report.zsch_compliant = bool(
        has_doc_num
        and has_date_issued
        and checklist["supplier_identity"]
        and checklist["recipient_identity"]
        and has_goods_desc
        and has_tax_base
        and has_signatory
    )

    report.zdds_compliant = bool(
        has_doc_num
        and has_date_issued
        and checklist["supplier_identity"]
        and checklist["recipient_identity"]
        and has_goods_desc
        and has_tax_base
        and has_vat_compliance
    )

    errors = [iss for iss in all_issues if iss.severity == "error"]
    warnings = [iss for iss in all_issues if iss.severity != "error"]

    report.errors = errors
    report.warnings = warnings

    report.is_compliant = bool(
        len(errors) == 0
        and report.zdds_compliant
        and bank_valid_or_absent
    )

    return report, all_issues
