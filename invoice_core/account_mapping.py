"""Configurable Account Mapping Engine for Bulgarian Accounting Systems.

Maps line item descriptions, expense categories, and supplier EIKs to
standard Bulgarian Chart of Accounts (НСС / Национален сметкоплан) accounts:
  - 601 / 6012: Дизел / бензин / горива / масла
  - 601 / 6013: Канцеларски материали / офис консумативи
  - 602 / 6021: Ток / вода / топлоенергия (комунални услуги)
  - 602 / 6022: Телефон / интернет / телекомуникации
  - 602 / 6023: Наеми
  - 602 / 6024: Ремонти / сервиз / поддръжка
  - 602 / 6025: Счетоводни / правни / консултантски услуги
  - 602 / 6026: Реклама / маркетинг
  - 602 / 6027: Куриерски / транспортни услуги
  - 602 / 6028: Софтуер / облачни услуги / IT абонаменти
  - 204 / 206:  Дълготрайни материални активи (ДМА)
  - 304 / 3041: Стоки за препродажба
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
import json
import logging
import re
from typing import Any, Iterable

from .chart_of_accounts import DEFAULT_CHART_OF_ACCOUNTS, lookup_account

logger = logging.getLogger("invoice_ocr.account_mapping")


def _safe_to_dec(val: Any, default: Decimal = Decimal("0.00")) -> Decimal:
    """Safely convert any value (Decimal, int, float, str, dict, MoneyAmount, etc.) to Decimal."""
    if val is None or val == "":
        return default
    if isinstance(val, Decimal):
        return val
    if hasattr(val, "amount"):
        amt = getattr(val, "amount")
        return _safe_to_dec(amt, default) if amt is not None else default
    if isinstance(val, dict):
        if "amount" in val:
            return _safe_to_dec(val.get("amount"), default)
        if "value" in val:
            return _safe_to_dec(val.get("value"), default)
        return default
    if isinstance(val, (int, float)):
        try:
            return Decimal(str(val))
        except Exception:
            return default
    if isinstance(val, str):
        clean = val.strip().replace(" ", "").replace(",", ".")
        if not clean or clean.lower() == "none" or clean.lower() == "null":
            return default
        try:
            return Decimal(clean)
        except Exception:
            return default
    return default


# Standard statutory default accounts
DEFAULT_GOODS_ACCOUNT = "304"
DEFAULT_SERVICE_ACCOUNT = "602"
DEFAULT_MATERIALS_ACCOUNT = "601"
DEFAULT_VAT_ACCOUNT = "4531"
DEFAULT_SUPPLIER_ACCOUNT = "401"


@dataclass
class AccountMappingRule:
    """A single mapping rule matching keywords or regex."""
    rule_id: str
    target_account: str
    target_subledger: str | None = None
    keywords: list[str] = field(default_factory=list)
    regex_pattern: str | None = None
    description: str = ""
    priority: int = 10  # Higher value = higher priority

    def matches(self, text: str) -> bool:
        """Check if description matches the rule."""
        if not text:
            return False
        clean_text = text.lower().strip()
        if self.regex_pattern:
            if re.search(self.regex_pattern, clean_text, re.IGNORECASE):
                return True
        for kw in self.keywords:
            if kw.lower() in clean_text:
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "target_account": self.target_account,
            "target_subledger": self.target_subledger,
            "keywords": self.keywords,
            "regex_pattern": self.regex_pattern,
            "description": self.description,
            "priority": self.priority,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountMappingRule:
        return cls(
            rule_id=data["rule_id"],
            target_account=data["target_account"],
            target_subledger=data.get("target_subledger"),
            keywords=data.get("keywords", []),
            regex_pattern=data.get("regex_pattern"),
            description=data.get("description", ""),
            priority=data.get("priority", 10),
        )


@dataclass
class SupplierMappingRule:
    """A mapping rule specific to a supplier EIK or VAT number."""
    eik: str
    target_account: str
    target_subledger: str | None = None
    supplier_name: str = ""
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "eik": self.eik,
            "target_account": self.target_account,
            "target_subledger": self.target_subledger,
            "supplier_name": self.supplier_name,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SupplierMappingRule:
        return cls(
            eik=data["eik"],
            target_account=data["target_account"],
            target_subledger=data.get("target_subledger"),
            supplier_name=data.get("supplier_name", ""),
            description=data.get("description", ""),
        )


# ============================================================================
# BUILT-IN STANDARD BULGARIAN SUPPLIER RULES
# ============================================================================

_DEFAULT_SUPPLIER_RULES: list[SupplierMappingRule] = [
    # Petrol stations & Fuel -> 601 (or 6012)
    SupplierMappingRule("121687551", "6012", "121687551", "ЛУКОЙЛ БЪЛГАРИЯ ЕООД", "Горива и смазочни материали"),
    SupplierMappingRule("121852504", "6012", "121852504", "ШЕЛ БЪЛГАРИЯ ЕАД", "Горива и смазочни материали"),
    SupplierMappingRule("121528328", "6012", "121528328", "ОМВ БЪЛГАРИЯ ООД", "Горива и смазочни материали"),
    SupplierMappingRule("130962406", "6012", "130962406", "РОМПЕТРОЛ БЪЛГАРИЯ ЕАД", "Горива и смазочни материали"),
    SupplierMappingRule("130970873", "6012", "130970873", "ЕКО БЪЛГАРИЯ ЕАД", "Горива и смазочни материали"),
    SupplierMappingRule("201646056", "6012", "201646056", "НИС ПЕТРОЛ ЕООД", "Горива и смазочни материали"),
    SupplierMappingRule("103233887", "6012", "103233887", "ПЕТРОЛ АД", "Горива и смазочни материали"),

    # Utilities (Electricity, Water, Heating) -> 602 (or 6021)
    SupplierMappingRule("130007803", "6021", "130007803", "ЕЛЕКТРОХОЛД ПРОДАЖБИ ЕАД", "Електроенергия"),
    SupplierMappingRule("115714902", "6021", "115714902", "ЕВН БЪЛГАРИЯ ЕЛЕКТРОСНАБДЯВАНЕ ЕАД", "Електроенергия"),
    SupplierMappingRule("103533691", "6021", "103533691", "ЕНЕРГО-ПРО ПРОДАЖБИ АД", "Електроенергия"),
    SupplierMappingRule("130175000", "6021", "130175000", "СОФИЙСКА ВОДА АД", "Вода и канал"),
    SupplierMappingRule("831609046", "6021", "831609046", "ТОПЛОФИКАЦИЯ СОФИЯ ЕАД", "Топлоенергия"),

    # Telecom & Internet -> 602 (or 6022)
    SupplierMappingRule("131468980", "6022", "131468980", "А1 БЪЛГАРИЯ ЕАД", "Телекомуникационни услуги"),
    SupplierMappingRule("831642181", "6022", "831642181", "БТК ЕАД (VIVACOM)", "Телекомуникационни услуги"),
    SupplierMappingRule("130408101", "6022", "130408101", "ЙЕТТЕЛ БЪЛГАРИЯ ЕАД", "Телекомуникационни услуги"),
    SupplierMappingRule("121396123", "6022", "121396123", "БЪЛГАРСКИ ПОЩИ ЕАД", "Пощенски услуги"),

    # Courier & Transport -> 602 (or 6027)
    SupplierMappingRule("130177202", "6027", "130177202", "СПИДИ АД", "Куриерски и транспортни услуги"),
    SupplierMappingRule("117041887", "6027", "117041887", "ЕКОНТ ЕКСПРЕС ООД", "Куриерски и транспортни услуги"),
    SupplierMappingRule("831341020", "6027", "831341020", "ДИ ЕЙЧ ЕЛ ЕКСПРЕС БЪЛГАРИЯ ЕООД", "Куриерски услуги"),
    SupplierMappingRule("115764042", "6027", "115764042", "ИН ТАЙМ ООД", "Куриерски услуги UPS"),

    # Office supplies -> 601 (or 6013)
    SupplierMappingRule("121480450", "6013", "121480450", "КООПЕРАЦИЯ ПАНДА (OFFICE 1)", "Канцеларски материали"),
    SupplierMappingRule("131109082", "6013", "131109082", "ПЛЕСИО КОМПЮТЪРС ЕАД", "Офис консумативи и техника"),

    # Cloud & Software -> 602 (or 6028)
    SupplierMappingRule("IE6388047V", "6028", "IE6388047V", "GOOGLE IRELAND LIMITED", "Облачни и рекламни услуги"),
    SupplierMappingRule("IE9692404L", "6028", "IE9692404L", "MICROSOFT IRELAND OPERATIONS LIMITED", "Софтуерни лицензи и cloud"),
    SupplierMappingRule("LU20260743", "6028", "LU20260743", "AMAZON WEB SERVICES EMEA SARL", "AWS облачна инфраструктура"),

    # Wholesale merchandise -> 304 (or 3041)
    SupplierMappingRule("121644736", "3041", "121644736", "МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД", "Стоки за препродажба"),
    SupplierMappingRule("130987441", "3041", "130987441", "КАУФЛАНД БЪЛГАРИЯ ЕООД ЕНД КО КД", "Стоки"),
    SupplierMappingRule("130007884", "3041", "130007884", "БИЛЛА БЪЛГАРИЯ ЕООД", "Стоки"),
    SupplierMappingRule("202262252", "3041", "202262252", "АНДА 2012 2 АНКО ПЕТРОВ ЕООД", "Хранителни стоки на едро"),
]

# ============================================================================
# BUILT-IN STANDARD KEYWORD / REGEX RULES
# ============================================================================

_DEFAULT_KEYWORD_RULES: list[AccountMappingRule] = [
    # 1. Fuel & Lubricants -> 6012 (Priority 30)
    AccountMappingRule(
        rule_id="fuel_and_lubricants",
        target_account="6012",
        keywords=[
            "дизел", "бензин", "гориво", "автогаз", "пропан", "бутан", "метан",
            "adblue", "моторно масло", "масло моторно", "смазочни", "антифриз",
            "газьол", "дизелово гориво", "a-95", "а-95", "а95", "diesel", "fuel",
            "бензин а95", "бензин а-98", "масло трансмисионно",
        ],
        regex_pattern=r"(?i)\b(дизел|бензин|гориво|автогаз|пропан|бутан|метан|adblue|антифриз|diesel|fuel|a-?95|a-?98|lpg|cng)\b",
        description="Разходи за горива, смазочни материали и енергия",
        priority=30,
    ),

    # 2. Office supplies & Stationery -> 6013 (Priority 25)
    AccountMappingRule(
        rule_id="office_stationery",
        target_account="6013",
        keywords=[
            "хартия", "копирна хартия", "тонер", "тонер касета", "мастило",
            "химикал", "папка", "папки", "класьор", "класьори", "тетрадка",
            "джоб", "джобове", "канцеларски", "консумативи офис", "картридж",
            "перфоратор", "телбод", "ножица", "лепило", "маркер", "флумастер",
        ],
        regex_pattern=r"(?i)\b(хартия|тонер|класьор|папк[аи]|химикал|канцелар|телбод|джоб(ове)?)\b",
        description="Разходи за канцеларски материали и офис консумативи",
        priority=25,
    ),

    # 3. Spare parts -> 6014 (Priority 25)
    AccountMappingRule(
        rule_id="spare_parts",
        target_account="6014",
        keywords=[
            "резервни части", "авточасти", "гуми", "зимни гуми", "летни гуми",
            "маслен филтър", "въздушен филтър", "горивен филтър", "спирачни накладки",
            "накладки", "акумулатор", "свещи", "ремък", "амортисьор", "чистачки",
            "крушка h7", "крушки", "лагер", "шайба", "болт", "гайка",
        ],
        description="Разходи за резервни части",
        priority=25,
    ),

    # 4. Cleaning & Hygiene -> 6015 (Priority 25)
    AccountMappingRule(
        rule_id="cleaning_hygiene",
        target_account="6015",
        keywords=[
            "дезинфектант", "почистващ препарат", "сапун", "течен сапун",
            "тоалетна хартия", "кухненска ролка", "салфетки", "почистване",
            "веро", "препарат за под", "чували за смет", "гъби",
        ],
        description="Разходи за хигиенни и почистващи материали",
        priority=25,
    ),

    # 5. Utilities (Power, Water, District Heating) -> 6021 (Priority 28)
    AccountMappingRule(
        rule_id="utilities_power_water",
        target_account="6021",
        keywords=[
            "електроенергия", "активна енергия", "реактивна енергия", "мрежови услуги",
            "водоснабдяване", "канализация", "пречистване на вода", "топлоенергия",
            "парно", "студена вода", "топла вода", "електричество", "ток",
        ],
        regex_pattern=r"(?i)\b(електроенергия|ток|водоснабдяване|канализация|топлоенергия|парно|вик)\b",
        description="Електроенергия, вода и топлоенергия",
        priority=28,
    ),

    # 6. Telecommunications & Internet -> 6022 (Priority 28)
    AccountMappingRule(
        rule_id="telecom_and_internet",
        target_account="6022",
        keywords=[
            "месечен абонамент", "мобилен интернет", "оптичен интернет", "сим карта",
            "sim карта", "трафик данни", "гласови услуги", "телефон", "пощенски плик",
            "пощенски услуги", "sms", "роуминг", "минути за разговори",
        ],
        regex_pattern=r"(?i)\b(интернет|телефон|телекомуникаци|sim\s*карта|роуминг|пощенск)\b",
        description="Телекомуникационни и пощенски услуги",
        priority=28,
    ),

    # 7. Rent -> 6023 (Priority 28)
    AccountMappingRule(
        rule_id="rents",
        target_account="6023",
        keywords=[
            "наем офис", "наем склад", "наем на помещение", "наем паркомясто",
            "наем за месец", "наемна вноска", "rent", "оперативен лизинг",
        ],
        regex_pattern=r"(?i)\b(наем|rent|паркомясто)\b",
        description="Разходи за наеми",
        priority=28,
    ),

    # 8. Repairs and Maintenance -> 6024 (Priority 24)
    AccountMappingRule(
        rule_id="repairs_and_maintenance",
        target_account="6024",
        keywords=[
            "текущ ремонт", "техническо обслужване", "смяна на масло", "монтаж",
            "демонтаж", "ремонт на автомобил", "автомивка", "сервиз", "профилактика",
            "реглаж", "боядисване", "диагностика", "поддръжка",
        ],
        description="Разходи за текущ ремонт и техническа поддръжка",
        priority=24,
    ),

    # 9. Professional Services (Accounting, Legal, Audit) -> 6025 (Priority 26)
    AccountMappingRule(
        rule_id="professional_services",
        target_account="6025",
        keywords=[
            "счетоводно обслужване", "счетоводни услуги", "одит", "одиторски услуги",
            "правни услуги", "адвокатски хонорар", "нотариална такса", "консултантски",
            "данъчна консултация", "трз обслужване", "юридически услуги",
        ],
        regex_pattern=r"(?i)\b(счетовод|одит|правни|адвокат|нотариус|консултант|юрид)\b",
        description="Счетоводни, одиторски, правни и консултантски услуги",
        priority=26,
    ),

    # 10. Advertising & Marketing -> 6026 (Priority 24)
    AccountMappingRule(
        rule_id="advertising_marketing",
        target_account="6026",
        keywords=[
            "реклама", "маркетинг", "дигитален маркетинг", "рекламна кампания",
            "банер", "билборд", "флаери", "брошури", "рекламни материали",
            "google ads", "facebook ads", "linkedin ads", "pr услуги", "уеб дизайн",
        ],
        description="Разходи за реклама, маркетинг и PR",
        priority=24,
    ),

    # 11. Courier, Transport & Road Tolls -> 6027 (Priority 27)
    AccountMappingRule(
        rule_id="courier_and_transport",
        target_account="6027",
        keywords=[
            "куриерска услуга", "куриерска пратка", "куриерски услуги", "доставка",
            "транспорт", "превоз на товари", "спедиция", "карго", "пътна такса",
            "тол такса", "винетка", "е-винетка", "бгтол", "товаро-разтоварни",
        ],
        regex_pattern=r"(?i)\b(куриер|спедици|транспорт|превоз|тол\s*такс|винетк|бгтол)\b",
        description="Транспортни, куриерски и спедиторски услуги",
        priority=27,
    ),

    # 12. Software, Cloud & SaaS -> 6028 (Priority 27)
    AccountMappingRule(
        rule_id="software_cloud_saas",
        target_account="6028",
        keywords=[
            "софтуер", "софтуерен абонамент", "хостинг", "уеб хостинг", "домейн",
            "cloud", "облачни услуги", "лиценз", "saas", "aws", "azure",
            "google cloud", "office 365", "github", "zoom", "slack", "антивирусна",
            "it поддръжка", "ит поддръжка", "it услуги", "ит услуги",
            "софтуерна поддръжка", "поддръжка на софтуер", "системна администрация",
            "информационни технологии", "it support", "helpdesk",
        ],
        regex_pattern=r"(?i)\b(софтуер|хостинг|домейн|cloud|лиценз|saas|aws|azure|it\s*поддръжка|ит\s*поддръжка|it\s*услуги|ит\s*услуги|it\s*support)\b",
        description="Софтуерни абонаменти, хостинг, IT услуги и SaaS",
        priority=27,
    ),

    # 13. Security & Insurance -> 6029 (Priority 24)
    AccountMappingRule(
        rule_id="security_insurance",
        target_account="6029",
        keywords=[
            "охрана", "сот", "сигнално-охранителна", "видеонаблюдение",
            "застраховка", "каско", "гражданска отговорност", "имуществена застраховка",
        ],
        description="Охрана, застраховки и такси",
        priority=24,
    ),

    # 14. Fixed Assets (DMA) -> 204 / 206 (Priority 22)
    AccountMappingRule(
        rule_id="fixed_assets_equipment",
        target_account="206",
        keywords=[
            "лаптоп", "компютърна конфигурация", "сървър dell", "сървър hp",
            "климатик инверторен", "мултифункционално устройство", "струг",
        ],
        description="Дълготрайни материални активи и офис техника",
        priority=22,
    ),

    # 15. Merchandise / Goods for Resale -> 3041 (Priority 15)
    AccountMappingRule(
        rule_id="merchandise_goods",
        target_account="3041",
        keywords=[
            "стока", "стоки", "кафе", "еспресо", "лаваца", "напитки", "безалкохолно",
            "минерална вода", "вода минерална", "бира", "вино", "шоколад", "захарни изделия",
            "месни продукти", "сирене", "кашкавал", "колбаси", "хранителни стоки",
            "търговски артикули", "палет", "сок", "сокове", "снакс", "чипс", "вафли",
            "кроасан", "дъвки", "цигари", "тютюн",
        ],
        regex_pattern=r"(?i)\b(стока|стоки|кафе|еспресо|лаваца|шоколад|вафл[аи]|бира|вино|сок(ове)?)\b",
        description="Стоки за препродажба в търговията",
        priority=15,
    ),
]


class AccountMappingEngine:
    """Configurable rules engine for matching invoices to accounting accounts."""

    def __init__(
        self,
        supplier_rules: list[SupplierMappingRule] | None = None,
        keyword_rules: list[AccountMappingRule] | None = None,
        default_goods_account: str = DEFAULT_GOODS_ACCOUNT,
        default_service_account: str = DEFAULT_SERVICE_ACCOUNT,
        default_materials_account: str = DEFAULT_MATERIALS_ACCOUNT,
        default_vat_account: str = DEFAULT_VAT_ACCOUNT,
        default_supplier_account: str = DEFAULT_SUPPLIER_ACCOUNT,
    ):
        self._supplier_rules: dict[str, SupplierMappingRule] = {}
        for r in (supplier_rules if supplier_rules is not None else _DEFAULT_SUPPLIER_RULES):
            clean_eik = re.sub(r"[^A-Za-z0-9]", "", r.eik).upper()
            self._supplier_rules[clean_eik] = r

        self._keyword_rules: list[AccountMappingRule] = list(
            keyword_rules if keyword_rules is not None else _DEFAULT_KEYWORD_RULES
        )
        # Sort keyword rules by priority descending
        self._keyword_rules.sort(key=lambda x: x.priority, reverse=True)

        self.default_goods_account = default_goods_account
        self.default_service_account = default_service_account
        self.default_materials_account = default_materials_account
        self.default_vat_account = default_vat_account
        self.default_supplier_account = default_supplier_account

    # ------------------------------------------------------------------------
    # Rule Management API
    # ------------------------------------------------------------------------

    def add_supplier_rule(
        self,
        eik: str,
        account: str,
        subledger: str | None = None,
        supplier_name: str = "",
        description: str = "",
    ) -> None:
        """Register or override a supplier mapping rule."""
        clean_eik = re.sub(r"[^A-Za-z0-9]", "", eik).upper()
        rule = SupplierMappingRule(
            eik=clean_eik,
            target_account=account,
            target_subledger=subledger or clean_eik,
            supplier_name=supplier_name,
            description=description,
        )
        self._supplier_rules[clean_eik] = rule

    def remove_supplier_rule(self, eik: str) -> bool:
        """Remove a supplier rule."""
        clean_eik = re.sub(r"[^A-Za-z0-9]", "", eik).upper()
        return bool(self._supplier_rules.pop(clean_eik, None))

    def add_keyword_rule(
        self,
        rule_id: str,
        account: str,
        keywords: list[str],
        regex_pattern: str | None = None,
        subledger: str | None = None,
        priority: int = 50,
        description: str = "",
    ) -> None:
        """Register a custom keyword/regex mapping rule with priority."""
        rule = AccountMappingRule(
            rule_id=rule_id,
            target_account=account,
            target_subledger=subledger,
            keywords=keywords,
            regex_pattern=regex_pattern,
            description=description,
            priority=priority,
        )
        # Replace existing with same ID or append
        self._keyword_rules = [r for r in self._keyword_rules if r.rule_id != rule_id]
        self._keyword_rules.append(rule)
        self._keyword_rules.sort(key=lambda x: x.priority, reverse=True)

    def remove_keyword_rule(self, rule_id: str) -> bool:
        """Remove a keyword rule by ID."""
        initial_len = len(self._keyword_rules)
        self._keyword_rules = [r for r in self._keyword_rules if r.rule_id != rule_id]
        return len(self._keyword_rules) < initial_len

    # ------------------------------------------------------------------------
    # Classification Logic
    # ------------------------------------------------------------------------

    def classify_line_item(
        self,
        description: str,
        supplier_eik: str | None = None,
        supplier_name: str | None = None,
        unit_price: Decimal | None = None,
    ) -> tuple[str, str | None, str]:
        """Classify a single line item.
        
        Returns:
            (account_code, subledger_code, rule_explanation)
        """
        # 1. First, check line item description rules (highest specificity)
        if description:
            for rule in self._keyword_rules:
                if rule.matches(description):
                    acc = rule.target_account
                    expl = f"Съвпадение по правило '{rule.rule_id}' ({rule.description})"
                    return acc, rule.target_subledger, expl

        # 2. Check supplier EIK / VAT rules
        if supplier_eik:
            clean_eik = re.sub(r"[^A-Za-z0-9]", "", supplier_eik).upper()
            if clean_eik in self._supplier_rules:
                s_rule = self._supplier_rules[clean_eik]
                expl = f"Съвпадение по доставчик ЕИК {clean_eik} ({s_rule.supplier_name or s_rule.description})"
                return s_rule.target_account, s_rule.target_subledger, expl

        # 3. Check supplier name for keywords
        if supplier_name:
            for rule in self._keyword_rules:
                if rule.matches(supplier_name):
                    expl = f"Съвпадение по име на доставчик '{supplier_name}' към {rule.rule_id}"
                    return rule.target_account, rule.target_subledger, expl

        # 4. Fallback: default account
        expl = "Стандартно класифициране по подразбиране (Стоки 304)"
        return self.default_goods_account, None, expl

    def classify_invoice(
        self,
        invoice: Any,
        prefer_subaccounts: bool = False,
    ) -> str:
        """Classify an entire invoice into its primary expense/inventory account."""
        # Extract fields
        sup_eik = None
        sup_name = None
        items: list[str] = []

        if hasattr(invoice, "supplier") and getattr(invoice.supplier, "eik", None):
            sup_eik = invoice.supplier.eik
            sup_name = getattr(invoice.supplier, "name", "")
        elif isinstance(invoice, dict):
            s_dict = invoice.get("supplier") or {}
            sup_eik = s_dict.get("eik") or invoice.get("vendorEik")
            sup_name = s_dict.get("name") or invoice.get("vendorName")

        if hasattr(invoice, "line_items") and getattr(invoice, "line_items", None):
            items = [getattr(it, "description", "") or "" for it in invoice.line_items if it]
        elif isinstance(invoice, dict):
            raw_items = invoice.get("line_items") or invoice.get("items") or []
            items = [it.get("description", "") or it.get("name", "") for it in raw_items if it]

        # 1. Supplier match takes precedence at document level if configured
        if sup_eik:
            clean_eik = re.sub(r"[^A-Za-z0-9]", "", str(sup_eik)).upper()
            if clean_eik in self._supplier_rules:
                acc = self._supplier_rules[clean_eik].target_account
                return acc if prefer_subaccounts else acc[:3]

        # 2. Check line items
        for desc in items:
            acc, _, _ = self.classify_line_item(desc, supplier_eik=sup_eik, supplier_name=sup_name)
            if acc:
                return acc if prefer_subaccounts else acc[:3]

        # 3. Default fallback
        return self.default_goods_account

    def split_invoice_by_accounts(
        self,
        invoice: Any,
        prefer_subaccounts: bool = True,
    ) -> list[dict[str, Any]]:
        """Split invoice into multiple accounting distributions by line items.
        
        Returns a list of dicts:
        [
          {
            "account": "6012",
            "account_name": "Разходи за горива...",
            "subledger": "121687551",
            "amount": Decimal("50.00"),
            "vat_rate": Decimal("20.00"),
            "vat_amount": Decimal("10.00"),
            "description": "Дизелово гориво Б6",
            "item_count": 1,
          },
          ...
        ]
        """
        raw_items: list[Any] = []
        sup_eik = ""
        sup_name = ""
        tax_base_total = Decimal("0.00")
        vat_amount_total = Decimal("0.00")
        total_due = Decimal("0.00")

        if hasattr(invoice, "line_items"):
            raw_items = getattr(invoice, "line_items", []) or []
            if hasattr(invoice, "supplier"):
                sup_eik = str(getattr(invoice.supplier, "eik", "") or "")
                sup_name = str(getattr(invoice.supplier, "name", "") or "")
            if hasattr(invoice, "financial_summary"):
                fin = invoice.financial_summary
                tax_base_total = _safe_to_dec(getattr(fin, "tax_base", None))
                vat_amount_total = _safe_to_dec(getattr(fin, "vat_amount", None))
                total_due = _safe_to_dec(getattr(fin, "total_amount_due", None))
        elif isinstance(invoice, dict):
            raw_items = invoice.get("line_items") or invoice.get("items") or []
            sup = invoice.get("supplier") or {}
            sup_eik = str(sup.get("eik") or invoice.get("vendorEik") or "")
            sup_name = str(sup.get("name") or invoice.get("vendorName") or "")
            fin = invoice.get("financial_summary") or {}
            tax_base_total = _safe_to_dec(fin.get("tax_base") or invoice.get("subtotal"))
            vat_amount_total = _safe_to_dec(fin.get("vat_amount") or invoice.get("taxAmount"))
            total_due = _safe_to_dec(fin.get("total_amount_due") or invoice.get("totalAmount"))

        if total_due == Decimal("0.00") and (tax_base_total > 0 or vat_amount_total > 0):
            total_due = tax_base_total + vat_amount_total
        elif tax_base_total == Decimal("0.00") and total_due > 0:
            tax_base_total = total_due - vat_amount_total

        # If no items or single summary item, return single distribution
        if not raw_items:
            acc = self.classify_invoice(invoice, prefer_subaccounts=prefer_subaccounts)
            acc_def = lookup_account(acc)
            return [{
                "account": acc,
                "account_name": acc_def.name if acc_def else "Стоки/Услуги",
                "subledger": sup_eik or None,
                "amount": tax_base_total,
                "vat_rate": Decimal("20.00") if vat_amount_total > 0 else Decimal("0.00"),
                "vat_amount": vat_amount_total,
                "description": f"Доставка от {sup_name or 'доставчик'}",
                "item_count": 1,
            }]

        # Distribute each line item
        grouped: dict[tuple[str, str | None, Decimal], dict[str, Any]] = {}

        for item in raw_items:
            if hasattr(item, "description"):
                desc = getattr(item, "description", "") or "Стока/Услуга"
                net_amt = _safe_to_dec(getattr(item, "total_price_net", None))
                rate = _safe_to_dec(getattr(item, "vat_rate_pct", 20), default=Decimal("20.00"))
            else:
                desc = item.get("description") or item.get("name") or "Стока/Услуга"
                net_amt = _safe_to_dec(item.get("total_price_net") or item.get("totalPrice") or item.get("total"))
                rate = _safe_to_dec(item.get("vat_rate_pct") or item.get("vatRate") or 20, default=Decimal("20.00"))

            acc, subledger, _ = self.classify_line_item(desc, supplier_eik=sup_eik, supplier_name=sup_name)
            if not prefer_subaccounts and len(acc) > 3:
                acc = acc[:3]

            key = (acc, subledger, rate)
            if key not in grouped:
                acc_def = lookup_account(acc)
                grouped[key] = {
                    "account": acc,
                    "account_name": acc_def.name if acc_def else "Стоки/Услуги",
                    "subledger": subledger or sup_eik or None,
                    "amount": Decimal("0.00"),
                    "vat_rate": rate,
                    "vat_amount": Decimal("0.00"),
                    "descriptions": [],
                    "item_count": 0,
                }

            grouped[key]["amount"] += net_amt
            vat_part = (net_amt * (rate / Decimal("100"))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            grouped[key]["vat_amount"] += vat_part
            grouped[key]["item_count"] += 1
            if desc and desc not in grouped[key]["descriptions"]:
                grouped[key]["descriptions"].append(desc)

        distributions = []
        for grp in grouped.values():
            desc_text = ", ".join(grp["descriptions"][:2])
            distributions.append({
                "account": grp["account"],
                "account_name": grp["account_name"],
                "subledger": grp["subledger"],
                "amount": grp["amount"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                "vat_rate": grp["vat_rate"],
                "vat_amount": grp["vat_amount"].quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
                "description": desc_text[:50] if desc_text else "Покупка",
                "item_count": grp["item_count"],
            })

        # Sanity reconciliation: ensure sum of line distributions matches document tax_base
        dist_sum = sum(d["amount"] for d in distributions)
        if tax_base_total > 0 and distributions:
            if abs(dist_sum - tax_base_total) > Decimal("0.00"):
                diff = tax_base_total - dist_sum
                largest = max(distributions, key=lambda d: d["amount"])
                largest["amount"] += diff

        return distributions

    # ------------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialize configuration to dictionary."""
        return {
            "default_goods_account": self.default_goods_account,
            "default_service_account": self.default_service_account,
            "default_materials_account": self.default_materials_account,
            "default_vat_account": self.default_vat_account,
            "default_supplier_account": self.default_supplier_account,
            "supplier_rules": [r.to_dict() for r in self._supplier_rules.values()],
            "keyword_rules": [r.to_dict() for r in self._keyword_rules],
        }

    def to_json(self, indent: int = 2) -> str:
        """Export mapping configuration as JSON string."""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccountMappingEngine:
        """Create mapping engine from dictionary."""
        s_rules = [SupplierMappingRule.from_dict(r) for r in data.get("supplier_rules", [])]
        k_rules = [AccountMappingRule.from_dict(r) for r in data.get("keyword_rules", [])]
        return cls(
            supplier_rules=s_rules,
            keyword_rules=k_rules,
            default_goods_account=data.get("default_goods_account", DEFAULT_GOODS_ACCOUNT),
            default_service_account=data.get("default_service_account", DEFAULT_SERVICE_ACCOUNT),
            default_materials_account=data.get("default_materials_account", DEFAULT_MATERIALS_ACCOUNT),
            default_vat_account=data.get("default_vat_account", DEFAULT_VAT_ACCOUNT),
            default_supplier_account=data.get("default_supplier_account", DEFAULT_SUPPLIER_ACCOUNT),
        )


# Global default engine instance
DEFAULT_MAPPING_ENGINE = AccountMappingEngine()


def classify_invoice_account(invoice: Any, prefer_subaccounts: bool = False) -> str:
    """Classify invoice account using the default global mapping engine."""
    return DEFAULT_MAPPING_ENGINE.classify_invoice(invoice, prefer_subaccounts=prefer_subaccounts)


def split_invoice_postings(invoice: Any, prefer_subaccounts: bool = True) -> list[dict[str, Any]]:
    """Split invoice into line item account distributions."""
    return DEFAULT_MAPPING_ENGINE.split_invoice_by_accounts(invoice, prefer_subaccounts=prefer_subaccounts)
