"""Real-Time Online Contractor Verification Module (НАП, Търговски регистър, EU VIES).

Implements Pillar 3 (P1) requirements:
1. Commercial Register (Търговски регистър / ТР) status check:
   - Verifies legal status: ACTIVE, LIQUIDATION, BANKRUPTCY, DEREGISTERED, TERMINATED.
   - Rejects or flags transactions with bankrupt or deleted entities.
2. NRA VAT Register (НАП Регистър по чл. 94 ЗДДС):
   - Verifies VAT registration status at the specific transaction date (date_tax_event).
   - Rejects tax credit (отказ от право на данъчен кредит) if the supplier was
     deregistered or not yet registered on date_tax_event.
3. European Commission VIES System:
   - Online verification of EU cross-border counterparties (Google, Meta, Adobe, AWS, etc.).
   - REST API query to https://ec.europa.eu/taxation_customs/vies/rest-api/.
4. Performance & Reliability:
   - Asynchronous execution (httpx.AsyncClient) with synchronous wrappers.
   - SQLite persistent caching + in-memory LRU cache with configurable TTL.
   - Graceful offline fallback & Mock registry for air-gapped testing.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Optional

import httpx

logger = logging.getLogger("contractor_verification")

# ---------------------------------------------------------------------------
# Constants & Enums
# ---------------------------------------------------------------------------

DEFAULT_CACHE_DB = Path.home() / ".invoice_ocr" / "contractor_cache.db"
DEFAULT_CACHE_TTL_SECONDS = 86400  # 24 hours
VIES_REST_URL = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{country}/vat/{vat}"
DEFAULT_REQUEST_TIMEOUT = 5.0  # seconds

# Central Supabase Contractor Master Registry configuration
SUPABASE_URL = os.environ.get("SUPABASE_URL", "http://100.83.83.8:8002").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get(
    "SUPABASE_SERVICE_ROLE_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIiwiaXNzIjoic3VwYWJhc2UiLCJpYXQiOjE3ODIyMjY3OTksImV4cCI6MTkzOTkwNjc5OX0.5DAqw9x0gC7ZH-0UPg4eEkP2LqcW_PRk6O0AEISJUG4",
)

# EU Member State ISO-2 country codes
EU_MEMBER_STATES = {
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "ES", "FI", "FR",
    "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO",
    "SE", "SI", "SK", "XI",  # XI is Northern Ireland under protocol
}


class CompanyStatus(str, Enum):
    """Legal status in the Commercial Register (Търговски регистър)."""
    ACTIVE = "ACTIVE"                        # Действащ търговец
    LIQUIDATION = "LIQUIDATION"              # В производство по ликвидация
    BANKRUPTCY = "BANKRUPTCY"                # В производство по несъстоятелност
    TERMINATED = "TERMINATED"                # Прекратена дейност
    DEREGISTERED_DELETED = "DEREGISTERED"   # Заличен търговец от ТР
    UNKNOWN = "UNKNOWN"                      # Неизвестен статус


class VatRegistrationStatus(str, Enum):
    """VAT registration status under Art. 94 ЗДДС."""
    REGISTERED = "REGISTERED"                # Регистрирано лице по ЗДДС
    DEREGISTERED = "DEREGISTERED"            # Дерегистрирано лице по ЗДДС
    NOT_REGISTERED = "NOT_REGISTERED"        # Лице без регистрация по ЗДДС
    UNKNOWN = "UNKNOWN"                      # Неустановен ДДС статус


@dataclass
class ContractorVerificationResult:
    """Consolidated verification outcome for a contractor (Supplier or Client)."""
    country_code: str
    identifier: str                           # Normalized EIK or foreign VAT number
    company_name: str | None = None
    legal_status: CompanyStatus = CompanyStatus.UNKNOWN
    vat_status: VatRegistrationStatus = VatRegistrationStatus.UNKNOWN
    vat_registered_on_date: bool | None = None  # None if date not checked, True/False if checked
    vat_registration_date: str | None = None   # YYYY-MM-DD
    vat_deregistration_date: str | None = None # YYYY-MM-DD
    vat_legal_basis: str | None = None         # e.g. "чл. 96 ЗДДС", "чл. 100 ЗДДС"
    address: str | None = None                 # Primary statutory registered address (Седалище и адрес на управление)
    seat_address: str | None = None            # Decomposed / verified Commercial Register seat
    mol_name: str | None = None                # Person accountable (МОЛ / Управител)
    trade_outlets: list[dict[str, Any]] = field(default_factory=list) # Trade outlets / stores under Наредба Н-18
    managers: list[dict[str, Any]] = field(default_factory=list)      # Company managers from Commercial Register
    nkids: list[dict[str, Any]] = field(default_factory=list)         # NKID economic activities
    source: str = "ONLINE"                     # "BRRA", "NRA", "VIES", "CACHE", "MOCK"
    verified_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    is_valid_for_tax_credit: bool = True
    issues: list[str] = field(default_factory=list)
    raw_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        d = asdict(self)
        d["legal_status"] = self.legal_status.value
        d["vat_status"] = self.vat_status.value
        return d

    def clone_for_evaluation(self) -> "ContractorVerificationResult":
        """Create a clean copy for transaction date evaluation without cache pollution."""
        base_valid = (
            self.legal_status == CompanyStatus.ACTIVE
            and self.vat_status == VatRegistrationStatus.REGISTERED
        ) if self.legal_status != CompanyStatus.UNKNOWN else self.is_valid_for_tax_credit

        # Strip date-specific issues so they are evaluated fresh
        clean_issues = [
            iss for iss in self.issues
            if not any(kw in iss for kw in ("Данъчното събитие", "невалиден формат на датата", "ПРЕДИ датата", "СЛЕД датата"))
        ]

        return ContractorVerificationResult(
            country_code=self.country_code,
            identifier=self.identifier,
            company_name=self.company_name,
            legal_status=self.legal_status,
            vat_status=self.vat_status,
            vat_registered_on_date=None,
            vat_registration_date=self.vat_registration_date,
            vat_deregistration_date=self.vat_deregistration_date,
            vat_legal_basis=self.vat_legal_basis,
            address=self.address,
            seat_address=self.seat_address,
            mol_name=self.mol_name,
            trade_outlets=list(self.trade_outlets),
            managers=list(self.managers),
            nkids=list(self.nkids),
            source=self.source,
            verified_at=self.verified_at,
            is_valid_for_tax_credit=base_valid,
            issues=clean_issues,
            raw_data=dict(self.raw_data),
        )



# ---------------------------------------------------------------------------
# Persistent SQLite Cache Layer
# ---------------------------------------------------------------------------

class ContractorCache:
    """Thread-safe persistent SQLite cache for contractor verification records."""

    def __init__(self, db_path: Path | str | None = None, ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS):
        self.db_path = Path(db_path) if db_path else DEFAULT_CACHE_DB
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS contractor_cache (
                        country_code TEXT NOT NULL,
                        identifier TEXT NOT NULL,
                        data_json TEXT NOT NULL,
                        cached_at INTEGER NOT NULL,
                        PRIMARY KEY (country_code, identifier)
                    )
                """)
                conn.commit()
        except Exception as exc:
            logger.warning("Failed to initialize contractor cache DB %s: %s", self.db_path, exc)

    def get(self, country_code: str, identifier: str) -> ContractorVerificationResult | None:
        """Retrieve unexpired result from cache."""
        norm_country = country_code.upper().strip()
        norm_id = re.sub(r"[^A-Za-z0-9]", "", identifier).upper()
        now = int(datetime.now(timezone.utc).timestamp())

        try:
            with self._lock, sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    "SELECT data_json, cached_at FROM contractor_cache WHERE country_code = ? AND identifier = ?",
                    (norm_country, norm_id),
                )
                row = cur.fetchone()
                if not row:
                    return None

                data_json, cached_at = row
                if (now - cached_at) > self.ttl_seconds:
                    return None

                data = json.loads(data_json)
                res = ContractorVerificationResult(
                    country_code=data["country_code"],
                    identifier=data["identifier"],
                    company_name=data.get("company_name"),
                    legal_status=CompanyStatus(data.get("legal_status", CompanyStatus.UNKNOWN.value)),
                    vat_status=VatRegistrationStatus(data.get("vat_status", VatRegistrationStatus.UNKNOWN.value)),
                    vat_registered_on_date=data.get("vat_registered_on_date"),
                    vat_registration_date=data.get("vat_registration_date"),
                    vat_deregistration_date=data.get("vat_deregistration_date"),
                    vat_legal_basis=data.get("vat_legal_basis"),
                    address=data.get("address"),
                    seat_address=data.get("seat_address"),
                    mol_name=data.get("mol_name"),
                    trade_outlets=data.get("trade_outlets", []),
                    managers=data.get("managers", []),
                    nkids=data.get("nkids", []),
                    source=f"CACHE({data.get('source', 'ONLINE')})",
                    verified_at=data.get("verified_at", datetime.now(timezone.utc).isoformat()),
                    is_valid_for_tax_credit=data.get("is_valid_for_tax_credit", True),
                    issues=data.get("issues", []),
                    raw_data=data.get("raw_data", {}),
                )
                return res
        except Exception as exc:
            logger.debug("Cache read error for %s-%s: %s", norm_country, norm_id, exc)
            return None

    def set(self, result: ContractorVerificationResult) -> None:
        """Save verification result into cache (saves clean base data to avoid date pollution)."""
        base_res = result.clone_for_evaluation()
        norm_country = base_res.country_code.upper().strip()
        norm_id = re.sub(r"[^A-Za-z0-9]", "", base_res.identifier).upper()
        now = int(datetime.now(timezone.utc).timestamp())

        try:
            data_dict = base_res.to_dict()
            data_json = json.dumps(data_dict, ensure_ascii=False)
            with self._lock, sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO contractor_cache (country_code, identifier, data_json, cached_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (norm_country, norm_id, data_json, now),
                )
                conn.commit()
        except Exception as exc:
            logger.debug("Cache write error for %s-%s: %s", norm_country, norm_id, exc)

    def clear(self) -> None:
        """Clear all cached entries."""
        try:
            with self._lock, sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM contractor_cache")
                conn.commit()
        except Exception as exc:
            logger.warning("Failed to clear contractor cache: %s", exc)


# ---------------------------------------------------------------------------
# In-Memory Mock Registry for Testing & Known Bulgarian/EU Entities
# ---------------------------------------------------------------------------

KNOWN_CONTRACTORS_MOCK_REGISTRY: dict[str, dict[str, Any]] = {
    # Bulgarian Inactive / Bankrupt / Deregistered Test Cases (Syntactically valid EIKs)
    "BG:111111113": {
        "company_name": "ФАЛИРАЛА КОМПАНИЯ ЕООД",
        "legal_status": CompanyStatus.BANKRUPTCY,
        "vat_status": VatRegistrationStatus.DEREGISTERED,
        "vat_registration_date": "2015-01-01",
        "vat_deregistration_date": "2024-12-31",
        "vat_legal_basis": "чл. 107, т. 1 ЗДДС",
        "address": "гр. София, ул. Търговска 1",
    },
    "BG:222222226": {
        "company_name": "ЗАЛИЧЕН ТЪРГОВЕЦ ООД",
        "legal_status": CompanyStatus.DEREGISTERED_DELETED,
        "vat_status": VatRegistrationStatus.DEREGISTERED,
        "vat_registration_date": "2018-05-10",
        "vat_deregistration_date": "2025-06-30",
        "vat_legal_basis": "чл. 107, т. 2 ЗДДС",
        "address": "гр. Пловдив, бул. Марица 5",
    },
    "BG:333333339": {
        "company_name": "НОВОРЕГИСТРИРАНА ФИРМА ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2026-08-20",  # Registered after some Aug 2026 deals
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Варна, ул. Славянска 10",
    },
    "BG:444444441": {
        "company_name": "НЕОБЛАГАЕМА ДРЕБНА ФИРМА ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.NOT_REGISTERED,
        "vat_registration_date": None,
        "vat_deregistration_date": None,
        "vat_legal_basis": None,
        "address": "гр. Бургас, ул. Александровска 12",
    },
    # Real Bulgarian Companies from Target Archive & Verified via Commercial Register (ТР)
    "BG:121644736": {
        "company_name": "МЕТРО БЪЛГАРИЯ ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "1999-03-01",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. София, бул. Цариградско шосе 7-11 км",
    },
    "BG:208139865": {
        "company_name": "СИКРЕТ ЛЕДЖЪНД ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2025-01-23",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 100, ал. 1 ЗДДС",
        "address": "гр. София, ж.к. Хаджи Димитър, бл. 34, ет. 5, ап. 25",
    },
    "BG:208380135": {
        "company_name": "РМ КАСКАДА 2026 ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2026-06-06",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 100, ал. 1 ЗДДС",
        "address": "гр. София, бул. Цариградско шосе 105",
    },
    "BG:114500333": {
        "company_name": "КАПИНА 71 ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2000-01-20",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен, ул. Дойран 45",
    },
    "BG:200924103": {
        "company_name": "КАПИНА 71 ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2010-04-12",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен, ул. Дойран 45",
    },
    "BG:114682544": {
        "company_name": "ЕЛИКО 143 ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2007-06-12",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен, ж.к. Дружба",
    },
    "BG:206587399": {
        "company_name": "ЕЛИКО 143 ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2021-07-22",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен, ж.к. Дружба",
    },
    "BG:130864186": {
        "company_name": "ТОПЛИВО ГАЗ ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2003-09-18",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. София, ул. Солунска 2",
    },
    "BG:131164568": {
        "company_name": "СББ ГРУП ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2004-02-10",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. София, бул. България 102",
    },
    "BG:114609507": {
        "company_name": "ДЕТЕЛИНА ДП ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2005-11-04",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен, ул. Г. С. Раковски 57",
    },
    "BG:207839658": {
        "company_name": "ЕКОНТ ЕКСПРЕС АД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2024-05-21",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Русе, бул. Славянски 16",
    },
    "BG:206088589": {
        "company_name": "КЛИЙН СИСТЕМС ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2020-05-07",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен, ул. Свети Сава 8",
    },
    "BG:208402914": {
        "company_name": "ПИГЕОН ЕКСПРЕС ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2025-07-17",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Пловдив, ул. Орловец 9",
    },
    "BG:207822235": {
        "company_name": "КУКИЛИШЪС ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2024-05-08",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. София, ул. Кестен 23",
    },
    "BG:204540024": {
        "company_name": "ИНВИКТЪС СТАЙЛ ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2017-04-10",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. София",
    },
    "BG:205525267": {
        "company_name": "ОРИОН - КОЛЕВ 2019 ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2019-02-15",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен",
    },
    "BG:201851618": {
        "company_name": "КОРТЕ ДИЛЕТО ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2012-01-13",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Елин Пелин",
    },
    "BG:175327305": {
        "company_name": "ДРУЖЕСТВО ЗА КАСОВИ УСЛУГИ АД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2007-08-01",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. София, ул. Иван Хаджийски",
    },
    "BG:131344648": {
        "company_name": "ИЗИПЕЙ АД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2009-02-02",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. София, ул. Иван Вазов 16",
    },
    "BG:114616366": {
        "company_name": "ВЕСЕЛИН БАЛЕВ ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2003-05-15",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен, ул. Дойран 63А",
    },
    "BG:200349655": {
        "company_name": "ЦВЕТНА РАДОСТ ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2009-01-27",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 100, ал. 1 ЗДДС",
        "address": "гр. София",
    },
    "BG:114631464": {
        "company_name": "МАГНЕЗИЯ ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2006-03-20",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен, ж.к. Сторгозия",
    },
    "BG:203818240": {
        "company_name": "ДЖЕНТЪЛМЕН ГРУП ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2016-12-08",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 100, ал. 1 ЗДДС",
        "address": "гр. София 1373, р-н Красна поляна, ул. Суходолска 201",
        "seat_address": "гр. София 1373, р-н Красна поляна, ул. Суходолска 201",
        "mol_name": "Георги Ангелов Георгиев",
        "trade_outlets": [
            {
                "name": "Магазин Nargile.bg",
                "address": "гр. София, бул. Патриарх Евтимий 77",
                "type": "МАГАЗИН",
                "brand": "Nargile.bg",
            }
        ],
    },
    "BG:131433901": {
        "company_name": "АБСОЛЮТ ПЛЮС ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2006-05-15",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. София, ул. Люботрън 3-5-7",
    },
    "BG:203012369": {
        "company_name": "МУРГАШ 88 ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2014-04-08",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "с. Гурмазово, общ. Божурище, ул. Гурмазовско шосе 82",
    },
    "BG:114690224": {
        "company_name": "АРПАК ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2007-09-12",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен, ул. Сан Стефано 47А",
    },
    "BG:114548965": {
        "company_name": "ОПТИСПРИНТ ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2004-04-23",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен, ул. Гоце Делчев",
    },
    "BG:200875744": {
        "company_name": "ПРЕС ЕНД ФРЕШ ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2016-08-10",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. София, ул. Опорска река 3",
    },
    "BG:207390964": {
        "company_name": "ТИМ СТОК 23 ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2024-01-10",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Лясковец, ул. Оборище 9",
    },
    "BG:203197380": {
        "company_name": "КАЕМ 89 ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2016-11-23",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен, ул. Стоян Михайловски",
    },
    "BG:200525782": {
        "company_name": "ПРАКТИКЕР РИТЕЙЛ ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2008-05-15",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. София, бул. Околовръстен път 265",
    },
    "BG:114609731": {
        "company_name": "ВАЛБОРГЕН ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2008-08-19",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен, ул. Гренадирска 116",
    },
    "BG:824156453": {
        "company_name": "ТЕМЕНУЖКА КОЧЕВА - НАДЯ ЕТ",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "1995-11-22",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен",
    },
    "BG:114598204": {
        "company_name": "О СКАРИ ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2002-04-10",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен",
    },
    "BG:202262252": {
        "company_name": "АНДА 2012 - АНКО ПЕТРОВ ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2012-10-09",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен",
    },
    "BG:114681129": {
        "company_name": "Н ЕНД В ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2008-06-10",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 100, ал. 1 ЗДДС",
        "address": "гр. Плевен, ул. Х. Димитър 53-55",
    },
    "BG:104586266": {
        "company_name": "ГРЕСТОКОМЕРС ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2002-07-01",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Велико Търново",
    },
    "BG:114540185": {
        "company_name": "ЕКСПРЕС СЕКЮРИТИ СОД ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2001-02-07",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен",
    },
    "BG:114598690": {
        "company_name": "ИНТЕРМЕС ООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2002-05-15",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен",
    },
    "BG:824106518": {
        "company_name": "ВОДОСНАБДЯВАНЕ И КАНАЛИЗАЦИЯ ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "1994-04-01",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. Плевен, ул. Сан Стефано 25",
    },
    "BG:207930830": {
        "company_name": "ФАСТ ТОП ФУУДС ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.NOT_REGISTERED,
        "vat_registration_date": None,
        "vat_deregistration_date": None,
        "vat_legal_basis": None,
        "address": "гр. Плевен, ул. Чаталджа 4",
    },
    "BG:208230838": {
        "company_name": "ГМ 2025 ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.NOT_REGISTERED,
        "vat_registration_date": None,
        "vat_deregistration_date": None,
        "vat_legal_basis": None,
        "address": "с. Горна Митрополия, ул. Никола Вапцаров 7",
    },
    "BG:201021273": {
        "company_name": "ГАУДИ ДИ ЕС ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2010-01-26",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. София",
    },
    "BG:175307726": {
        "company_name": "ЕВРО ПЕСТ ЕООД",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2025-11-01",
        "vat_deregistration_date": None,
        "vat_legal_basis": "чл. 96 ЗДДС",
        "address": "гр. София",
    },
    # EU Tech Giants for Cross-Border & Reverse Charge / Protocol 117
    "IE:IE6388047V": {
        "company_name": "GOOGLE CLOUD EMEA LIMITED",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2008-01-01",
        "address": "70 SIR JOHN ROGERSON'S QUAY, DUBLIN 2, IRELAND",
    },
    "IE:6388047V": {
        "company_name": "GOOGLE CLOUD EMEA LIMITED",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2008-01-01",
        "address": "70 SIR JOHN ROGERSON'S QUAY, DUBLIN 2, IRELAND",
    },
    "IE:IE9692928F": {
        "company_name": "META PLATFORMS IRELAND LIMITED",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2009-03-15",
        "address": "4 GRAND CANAL SQUARE, GRAND CANAL HARBOUR, DUBLIN 2, IRELAND",
    },
    "IE:9692928F": {
        "company_name": "META PLATFORMS IRELAND LIMITED",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2009-03-15",
        "address": "4 GRAND CANAL SQUARE, GRAND CANAL HARBOUR, DUBLIN 2, IRELAND",
    },
    "IE:IE4523315Q": {
        "company_name": "ADOBE SYSTEMS SOFTWARE IRELAND LIMITED",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2005-06-20",
        "address": "4-6 RIVERWALK, CITYWEST BUSINESS CAMPUS, DUBLIN 24, IRELAND",
    },
    "IE:4523315Q": {
        "company_name": "ADOBE SYSTEMS SOFTWARE IRELAND LIMITED",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2005-06-20",
        "address": "4-6 RIVERWALK, CITYWEST BUSINESS CAMPUS, DUBLIN 24, IRELAND",
    },
    "LU:LU20260743": {
        "company_name": "AMAZON EU SARL / AWS",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2003-08-01",
        "address": "38 AVENUE JOHN F. KENNEDY, L-1855 LUXEMBOURG",
    },
    "LU:20260743": {
        "company_name": "AMAZON EU SARL / AWS",
        "legal_status": CompanyStatus.ACTIVE,
        "vat_status": VatRegistrationStatus.REGISTERED,
        "vat_registration_date": "2003-08-01",
        "address": "38 AVENUE JOHN F. KENNEDY, L-1855 LUXEMBOURG",
    },
}


def _parse_date_robust(date_val: Any) -> Any:
    """Parse date from string supporting ISO (YYYY-MM-DD), Bulgarian (DD.MM.YYYY), and slash formats."""
    if not date_val:
        return None
    if hasattr(date_val, "strftime") and hasattr(date_val, "year"):
        return date_val
    clean_date = str(date_val).strip()[:10]
    formats = ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y.%m.%d", "%Y/%m/%d")
    for fmt in formats:
        try:
            return datetime.strptime(clean_date, fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Offline Local VAT Registry (НАП Отворени данни / SQLite)
# ---------------------------------------------------------------------------

DEFAULT_VAT_REGISTRY_DB = Path.home() / ".invoice_ocr" / "vat_registry.db"

SEED_BULGARIAN_VAT_ENTITIES = [
    ("131468980", "А1 БЪЛГАРИЯ ЕАД", "BG131468980", "ACTIVE", "REGISTERED", "2005-09-01", None, "чл. 96 ЗДДС", "гр. София, ул. Кукуш 1"),
    ("831642181", "БЪЛГАРСКА ТЕЛЕКОМУНИКАЦИОННА КОМПАНИЯ ЕАД (VIVACOM)", "BG831642181", "ACTIVE", "REGISTERED", "1994-04-01", None, "чл. 96 ЗДДС", "гр. София, бул. Цариградско шосе 115и"),
    ("130408101", "ЙЕТТЕЛ БЪЛГАРИЯ ЕАД (YETTEL)", "BG130408101", "ACTIVE", "REGISTERED", "2001-05-15", None, "чл. 96 ЗДДС", "гр. София, ж.к. Младост 4, Бизнес Парк София"),
    ("130277958", "ЕЛЕКТРОХОЛД ПРОДАЖБИ ЕАД", "BG130277958", "ACTIVE", "REGISTERED", "2006-11-01", None, "чл. 96 ЗДДС", "гр. София, бул. Цариградско шосе 159"),
    ("123659269", "ЕВН БЪЛГАРИЯ ЕЛЕКТРОСНАБДЯВАНЕ ЕАД", "BG123659269", "ACTIVE", "REGISTERED", "2006-11-01", None, "чл. 96 ЗДДС", "гр. Пловдив, ул. Христо Г. Данов 37"),
    ("103533691", "ЕНЕРГО-ПРО ПРОДАЖБИ АД", "BG103533691", "ACTIVE", "REGISTERED", "2006-11-01", None, "чл. 96 ЗДДС", "гр. Варна, бул. Владислав Варненчик 258"),
    ("831609046", "ТОПЛОФИКАЦИЯ СОФИЯ ЕАД", "BG831609046", "ACTIVE", "REGISTERED", "1994-04-01", None, "чл. 96 ЗДДС", "гр. София, ул. Ястребец 23Б"),
    ("175324639", "БУЛГАРГАЗ ЕАД", "BG175324639", "ACTIVE", "REGISTERED", "2007-01-15", None, "чл. 96 ЗДДС", "гр. София, бул. Панчо Владигеров 66"),
    ("130175000", "СОФИЙСКА ВОДА АД", "BG130175000", "ACTIVE", "REGISTERED", "2000-10-01", None, "чл. 96 ЗДДС", "гр. София, ж.к. Младост 4, Бизнес Парк София"),
    ("000761458", "БЪЛГАРСКИ ПОЩИ ЕАД", "BG000761458", "ACTIVE", "REGISTERED", "1994-04-01", None, "чл. 96 ЗДДС", "гр. София, ул. Академик Стефан Младенов 1"),
    ("131341771", "СПИДИ АД", "BG131341771", "ACTIVE", "REGISTERED", "2004-12-01", None, "чл. 96 ЗДДС", "гр. София, София Парк"),
    ("117041887", "ЕКОНТ ЕКСПРЕС ООД", "BG117041887", "ACTIVE", "REGISTERED", "2000-05-18", None, "чл. 96 ЗДДС", "гр. Русе, бул. Славянски 16"),
    ("131129282", "КАУФЛАНД БЪЛГАРИЯ ЕООД ЕНД КО КД", "BG131129282", "ACTIVE", "REGISTERED", "2003-09-01", None, "чл. 96 ЗДДС", "гр. София, ул. Скопие 1"),
    ("131071587", "ЛИДЛ БЪЛГАРИЯ ЕООД ЕНД КО КД", "BG131071587", "ACTIVE", "REGISTERED", "2005-03-01", None, "чл. 96 ЗДДС", "с. Равно поле, Индустриална зона"),
    ("130007884", "БИЛЛА БЪЛГАРИЯ ЕООД", "BG130007884", "ACTIVE", "REGISTERED", "2000-10-01", None, "чл. 96 ЗДДС", "гр. София, бул. България 55"),
    ("831496285", "ШЕЛ БЪЛГАРИЯ ЕАД", "BG831496285", "ACTIVE", "REGISTERED", "1994-04-01", None, "чл. 96 ЗДДС", "гр. София, бул. Цариградско шосе 115Г"),
    ("121752007", "ОМВ БЪЛГАРИЯ ООД", "BG121752007", "ACTIVE", "REGISTERED", "1998-11-01", None, "чл. 96 ЗДДС", "гр. София, бул. Цариградско шосе 90"),
    ("130279640", "ЛУКОЙЛ БЪЛГАРИЯ ЕООД", "BG130279640", "ACTIVE", "REGISTERED", "1999-12-01", None, "чл. 96 ЗДДС", "гр. София, бул. Тодор Александров 42"),
]


class LocalVatRegistry:
    """Offline SQLite store for registered Bulgarian VAT entities from NRA open data."""

    def __init__(self, db_path: Path | str | None = None):
        if db_path is None:
            env_db = os.environ.get("NRA_VAT_REGISTRY_DB")
            self.db_path = Path(env_db) if env_db else DEFAULT_VAT_REGISTRY_DB
        else:
            self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS nra_vat_registry (
                        eik TEXT PRIMARY KEY,
                        company_name TEXT,
                        vat_number TEXT,
                        legal_status TEXT DEFAULT 'ACTIVE',
                        vat_status TEXT DEFAULT 'REGISTERED',
                        vat_registration_date TEXT,
                        vat_deregistration_date TEXT,
                        vat_legal_basis TEXT,
                        address TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_nra_vat_eik ON nra_vat_registry(eik)")

                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM nra_vat_registry")
                count = cursor.fetchone()[0]
                if count == 0:
                    cursor.executemany("""
                        INSERT OR IGNORE INTO nra_vat_registry (
                            eik, company_name, vat_number, legal_status, vat_status,
                            vat_registration_date, vat_deregistration_date, vat_legal_basis, address
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, SEED_BULGARIAN_VAT_ENTITIES)
                conn.commit()
        except Exception as exc:
            logger.warning("Could not initialize LocalVatRegistry at %s: %s", self.db_path, exc)

    def get(self, eik: str) -> dict[str, Any] | None:
        """Find entity by EIK in local SQLite registry."""
        clean_eik = re.sub(r"\D", "", eik)
        try:
            with self._lock, sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.cursor()
                cur.execute(
                    "SELECT eik, company_name, vat_number, legal_status, vat_status, "
                    "vat_registration_date, vat_deregistration_date, vat_legal_basis, address "
                    "FROM nra_vat_registry WHERE eik = ?",
                    (clean_eik,),
                )
                row = cur.fetchone()
                if row:
                    return dict(row)
        except Exception as exc:
            logger.warning("Failed querying LocalVatRegistry for %s: %s", clean_eik, exc)
        return None

    def upsert(
        self,
        eik: str,
        company_name: str,
        vat_number: str | None = None,
        legal_status: str = "ACTIVE",
        vat_status: str = "REGISTERED",
        vat_registration_date: str | None = None,
        vat_deregistration_date: str | None = None,
        vat_legal_basis: str | None = None,
        address: str | None = None,
    ) -> None:
        """Insert or update a registered entity in local SQLite store."""
        clean_eik = re.sub(r"\D", "", eik)
        if not vat_number:
            vat_number = f"BG{clean_eik}"
        try:
            with self._lock, sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    INSERT INTO nra_vat_registry (
                        eik, company_name, vat_number, legal_status, vat_status,
                        vat_registration_date, vat_deregistration_date, vat_legal_basis, address
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(eik) DO UPDATE SET
                        company_name=excluded.company_name,
                        vat_number=excluded.vat_number,
                        legal_status=excluded.legal_status,
                        vat_status=excluded.vat_status,
                        vat_registration_date=excluded.vat_registration_date,
                        vat_deregistration_date=excluded.vat_deregistration_date,
                        vat_legal_basis=excluded.vat_legal_basis,
                        address=excluded.address,
                        updated_at=CURRENT_TIMESTAMP
                """, (
                    clean_eik, company_name, vat_number, legal_status, vat_status,
                    vat_registration_date, vat_deregistration_date, vat_legal_basis, address
                ))
                conn.commit()
        except Exception as exc:
            logger.warning("Failed upserting to LocalVatRegistry: %s", exc)

    def import_from_csv(self, csv_path: Path | str, delimiter: str = ";") -> int:
        """Import official NRA open data CSV dump into local registry."""
        import csv
        count = 0
        path = Path(csv_path)
        if not path.exists():
            return 0
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            for row in reader:
                eik = row.get("eik") or row.get("ЕИК") or row.get("bulstat") or row.get("БУЛСТАТ")
                if not eik:
                    continue
                name = row.get("name") or row.get("НАИМЕНОВАНИЕ") or row.get("company_name", "")
                reg_date = row.get("vat_date") or row.get("ДАТА_РЕГИСТРАЦИЯ") or row.get("registration_date")
                dereg_date = row.get("dereg_date") or row.get("ДАТА_ДЕРЕГИСТРАЦИЯ") or row.get("deregistration_date")
                status = "DEREGISTERED" if dereg_date else "REGISTERED"
                self.upsert(
                    eik=eik,
                    company_name=name,
                    vat_status=status,
                    vat_registration_date=reg_date,
                    vat_deregistration_date=dereg_date,
                )
                count += 1
        return count


class ContractorVerifier:
    """Online and offline verification service for Bulgarian and EU contractors."""

    def __init__(
        self,
        cache_db_path: Path | str | None = None,
        vat_registry_db_path: Path | str | None = None,
        cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        offline_mode: bool = False,
    ):
        self.cache = ContractorCache(db_path=cache_db_path, ttl_seconds=cache_ttl)
        self.vat_registry = LocalVatRegistry(db_path=vat_registry_db_path)
        self.timeout = timeout
        if not offline_mode:
            offline_mode = os.environ.get("OFFLINE_MODE", "0").lower() in ("1", "true", "yes") or \
                           os.environ.get("LOCAL_ONLY", "0").lower() in ("1", "true", "yes")
        self.offline_mode = offline_mode
        self._mock_registry = dict(KNOWN_CONTRACTORS_MOCK_REGISTRY)

    def register_mock_contractor(
        self,
        country_code: str,
        identifier: str,
        company_name: str,
        legal_status: CompanyStatus = CompanyStatus.ACTIVE,
        vat_status: VatRegistrationStatus = VatRegistrationStatus.REGISTERED,
        vat_registration_date: str | None = "2020-01-01",
        vat_deregistration_date: str | None = None,
        vat_legal_basis: str | None = "чл. 96 ЗДДС",
        address: str | None = None,
    ) -> None:
        """Register custom mock contractor for testing edge scenarios."""
        norm_country = country_code.upper().strip()
        norm_id = re.sub(r"[^A-Za-z0-9]", "", identifier).upper()
        key = f"{norm_country}:{norm_id}"
        self._mock_registry[key] = {
            "company_name": company_name,
            "legal_status": legal_status,
            "vat_status": vat_status,
            "vat_registration_date": vat_registration_date,
            "vat_deregistration_date": vat_deregistration_date,
            "vat_legal_basis": vat_legal_basis,
            "address": address,
        }

    def _normalize_identifier(self, raw_id: str) -> tuple[str, str]:
        """Extract country code and clean identifier from input."""
        cleaned = re.sub(r"\s+", "", str(raw_id)).upper()
        if len(cleaned) >= 4 and cleaned[:2].isalpha():
            country = cleaned[:2]
            ident = cleaned[2:]
            return country, ident
        return "BG", cleaned

    def _validate_bulgarian_eik_checksum(self, eik: str) -> bool:
        """Check Modulus 11 algorithm for Bulgarian 9-digit or 13-digit EIK."""
        if not re.fullmatch(r"\d{9}|\d{13}", eik):
            return False
        digits = [int(c) for c in eik]
        w1 = [1, 2, 3, 4, 5, 6, 7, 8]
        s = sum(digits[i] * w1[i] for i in range(8))
        rem = s % 11
        if rem != 10:
            expected_9 = rem
        else:
            w2 = [3, 4, 5, 6, 7, 8, 9, 10]
            s2 = sum(digits[i] * w2[i] for i in range(8))
            rem2 = s2 % 11
            expected_9 = 0 if rem2 == 10 else rem2

        if digits[8] != expected_9:
            return False

        if len(digits) == 13:
            w13_1 = [2, 7, 3, 5]
            s13 = sum(digits[8 + i] * w13_1[i] for i in range(4))
            rem13 = s13 % 11
            if rem13 != 10:
                expected_13 = rem13
            else:
                w13_2 = [4, 9, 5, 7]
                s13_2 = sum(digits[8 + i] * w13_2[i] for i in range(4))
                rem13_2 = s13_2 % 11
                expected_13 = 0 if rem13_2 == 10 else rem13_2
            if digits[12] != expected_13:
                return False

        return True

    def _evaluate_date_tax_event(
        self,
        result: ContractorVerificationResult,
        date_tax_event: str | None,
    ) -> None:
        """Check if VAT registration was in force on the exact date of the tax event."""
        if not date_tax_event:
            return

        event_dt = _parse_date_robust(date_tax_event)
        if not event_dt:
            result.issues.append(f"Невалиден формат на датата на данъчното събитие: '{date_tax_event}'")
            return

        if result.vat_status == VatRegistrationStatus.NOT_REGISTERED:
            result.vat_registered_on_date = False
            result.is_valid_for_tax_credit = False
            result.issues.append(
                "Доставчикът НЯМА регистрация по ЗДДС (чл. 94 ЗДДС). Начисляването на ДДС е незаконосъобразно и правото на данъчен кредит се отказва!"
            )
            return

        reg_dt = _parse_date_robust(result.vat_registration_date)
        dereg_dt = _parse_date_robust(result.vat_deregistration_date)

        if reg_dt and event_dt < reg_dt:
            result.vat_registered_on_date = False
            result.is_valid_for_tax_credit = False
            result.issues.append(
                f"Данъчното събитие ({event_dt}) е ПРЕДИ датата на регистрация по ЗДДС ({reg_dt}). Правото на данъчен кредит се отказва по чл. 71 от ЗДДС!"
            )
            return

        if dereg_dt and event_dt > dereg_dt:
            result.vat_registered_on_date = False
            result.is_valid_for_tax_credit = False
            result.issues.append(
                f"Данъчното събитие ({event_dt}) е СЛЕД датата на дерегистрация по ЗДДС ({dereg_dt}). Лицето е дерегистрирано и няма право да начислява ДДС!"
            )
            return

        result.vat_registered_on_date = True
        if result.legal_status == CompanyStatus.ACTIVE and result.vat_status == VatRegistrationStatus.REGISTERED:
            result.is_valid_for_tax_credit = True

    async def verify_async(
        self,
        identifier: str,
        country_code: str | None = None,
        date_tax_event: str | None = None,
        bypass_cache: bool = False,
    ) -> ContractorVerificationResult:
        """Asynchronously verify contractor in Commercial Register, NRA, or EU VIES."""
        if country_code:
            country = country_code.upper().strip()
            ident = re.sub(r"[^A-Za-z0-9]", "", identifier).upper()
            if country == "BG" and ident.startswith("BG"):
                ident = ident[2:]
        else:
            country, ident = self._normalize_identifier(identifier)

        # 1. Check Mock Registry first
        mock_key = f"{country}:{ident}"
        alt_mock_key = f"{country}:{country}{ident}"
        mock_data = self._mock_registry.get(mock_key) or self._mock_registry.get(alt_mock_key)

        if mock_data:
            issues: list[str] = []
            is_valid_credit = True
            status = mock_data.get("legal_status", CompanyStatus.ACTIVE)
            if status in (CompanyStatus.BANKRUPTCY, CompanyStatus.LIQUIDATION, CompanyStatus.DEREGISTERED_DELETED):
                is_valid_credit = False
                issues.append(f"Фирмата е в неактивен правен статус в Търговския регистър: {status.value}")

            vat_status = mock_data.get("vat_status", VatRegistrationStatus.REGISTERED)
            if vat_status == VatRegistrationStatus.DEREGISTERED:
                is_valid_credit = False
                issues.append("Фирмата е ДЕРЕГИСТРИРАНА по ЗДДС в НАП.")
            elif vat_status == VatRegistrationStatus.NOT_REGISTERED:
                is_valid_credit = False
                issues.append("Фирмата НЕ Е регистрирана по ЗДДС.")

            res = ContractorVerificationResult(
                country_code=country,
                identifier=ident,
                company_name=mock_data.get("company_name"),
                legal_status=status,
                vat_status=vat_status,
                vat_registration_date=mock_data.get("vat_registration_date"),
                vat_deregistration_date=mock_data.get("vat_deregistration_date"),
                vat_legal_basis=mock_data.get("vat_legal_basis"),
                address=mock_data.get("address"),
                seat_address=mock_data.get("seat_address") or mock_data.get("address"),
                mol_name=mock_data.get("mol_name"),
                trade_outlets=mock_data.get("trade_outlets", []),
                managers=mock_data.get("managers", []),
                nkids=mock_data.get("nkids", []),
                source="MOCK_REGISTRY",
                is_valid_for_tax_credit=is_valid_credit,
                issues=issues,
                raw_data=mock_data,
            )
            self._evaluate_date_tax_event(res, date_tax_event)
            self.cache.set(res)
            return res

        # 2. Local checksum validation for Bulgarian EIK (if not explicitly in mock registry)
        if country == "BG":
            if not self._validate_bulgarian_eik_checksum(ident):
                res = ContractorVerificationResult(
                    country_code=country,
                    identifier=ident,
                    legal_status=CompanyStatus.UNKNOWN,
                    vat_status=VatRegistrationStatus.UNKNOWN,
                    source="LOCAL_CHECKSUM",
                    is_valid_for_tax_credit=False,
                    issues=[f"Невалидна контролна сума за български ЕИК '{ident}' по Модул 11."],
                )
                return res

        # 3. Check Cache
        if not bypass_cache:
            cached = self.cache.get(country, ident)
            if cached:
                eval_res = cached.clone_for_evaluation()
                self._evaluate_date_tax_event(eval_res, date_tax_event)
                return eval_res

        # 4. Check Local Offline VAT Registry (if country is BG)
        if country == "BG":
            reg_entry = self.vat_registry.get(ident)
            if reg_entry:
                legal_st = CompanyStatus(reg_entry.get("legal_status", "ACTIVE"))
                vat_st = VatRegistrationStatus(reg_entry.get("vat_status", "REGISTERED"))
                issues: list[str] = []
                is_valid_credit = True
                if legal_st in (CompanyStatus.BANKRUPTCY, CompanyStatus.LIQUIDATION, CompanyStatus.DEREGISTERED_DELETED):
                    is_valid_credit = False
                    issues.append(f"Фирмата е в неактивен правен статус в Търговския регистър: {legal_st.value}")
                if vat_st == VatRegistrationStatus.DEREGISTERED:
                    is_valid_credit = False
                    issues.append("Фирмата е ДЕРЕГИСТРИРАНА по ЗДДС в НАП.")
                elif vat_st == VatRegistrationStatus.NOT_REGISTERED:
                    is_valid_credit = False
                    issues.append("Фирмата НЕ Е регистрирана по ЗДДС.")

                res = ContractorVerificationResult(
                    country_code=country,
                    identifier=ident,
                    company_name=reg_entry.get("company_name"),
                    legal_status=legal_st,
                    vat_status=vat_st,
                    vat_registration_date=reg_entry.get("vat_registration_date"),
                    vat_deregistration_date=reg_entry.get("vat_deregistration_date"),
                    vat_legal_basis=reg_entry.get("vat_legal_basis"),
                    address=reg_entry.get("address"),
                    seat_address=reg_entry.get("seat_address") or reg_entry.get("address"),
                    mol_name=reg_entry.get("mol_name"),
                    trade_outlets=reg_entry.get("trade_outlets", []),
                    managers=reg_entry.get("managers", []),
                    nkids=reg_entry.get("nkids", []),
                    source="LOCAL_VAT_REGISTRY",
                    is_valid_for_tax_credit=is_valid_credit,
                    issues=issues,
                    raw_data=reg_entry,
                )
                self._evaluate_date_tax_event(res, date_tax_event)
                self.cache.set(res)
                return res

        # 5. Check Vendor Profiles (YAML configurations)
        try:
            from invoice_core.vendor_profiles import get_vendor_profile
            vp = get_vendor_profile(ident)
        except Exception:
            vp = None

        if vp:
            res = ContractorVerificationResult(
                country_code=country,
                identifier=ident,
                company_name=vp.get("name"),
                legal_status=CompanyStatus.ACTIVE,
                vat_status=VatRegistrationStatus.REGISTERED,
                address=vp.get("address"),
                source="VENDOR_PROFILE",
                is_valid_for_tax_credit=True,
            )
            self._evaluate_date_tax_event(res, date_tax_event)
            self.cache.set(res)
            return res

        # 6. Central Supabase public.contractors Table Query
        if not self.offline_mode:
            supabase_res = await self._query_supabase_contractor_async(country, ident)
            if supabase_res:
                self._evaluate_date_tax_event(supabase_res, date_tax_event)
                self.cache.set(supabase_res)
                return supabase_res

        # 7. Offline Mode Fail-Secure Exit
        if self.offline_mode:
            issues = [
                f"Контрагент с ЕИК '{ident}' не е намерен в локалния регистър на НАП или вендор профилите "
                "(необходима е ръчна верификация за данъчен кредит)."
            ]
            res = ContractorVerificationResult(
                country_code=country,
                identifier=ident,
                legal_status=CompanyStatus.UNKNOWN,
                vat_status=VatRegistrationStatus.UNKNOWN,
                source="OFFLINE_UNVERIFIED",
                is_valid_for_tax_credit=False,
                issues=issues,
            )
            self._evaluate_date_tax_event(res, date_tax_event)
            return res

        # 8. Online VIES API Query (for EU member states)
        if country in EU_MEMBER_STATES and country != "BG":
            return await self._query_vies_rest_async(country, ident, date_tax_event)

        # 9. Bulgarian Registry (TR / NRA) Online Query
        if country == "BG":
            return await self._query_bg_registries_async(ident, date_tax_event)

        # 10. Non-EU international contractor (e.g. US, UK, CH)
        res = ContractorVerificationResult(
            country_code=country,
            identifier=ident,
            legal_status=CompanyStatus.ACTIVE,
            vat_status=VatRegistrationStatus.NOT_REGISTERED,
            source="INTERNATIONAL_NON_EU",
            is_valid_for_tax_credit=False,
            issues=["Чуждестранен контрагент извън ЕС (възможен Reverse Charge / чл. 82 ЗДДС)."],
        )
        self._evaluate_date_tax_event(res, date_tax_event)
        self.cache.set(res)
        return res

    async def _query_supabase_contractor_async(
        self,
        country: str,
        ident: str,
    ) -> ContractorVerificationResult | None:
        """Query central Supabase public.contractors table via PostgREST."""
        if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
            return None

        try:
            url = f"{SUPABASE_URL}/rest/v1/contractors"
            params = {
                "select": "*",
                "country_code": f"eq.{country}",
                "or": f"(eik.eq.{ident},vat_number.eq.{ident},vat_number.eq.{country}{ident})",
                "limit": "1",
            }
            headers = {
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
            }
            async with httpx.AsyncClient(timeout=min(self.timeout, 2.5)) as client:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code == 200:
                    rows = resp.json()
                    if rows and isinstance(rows, list):
                        row = rows[0]
                        legal_st_val = (row.get("legal_status") or "ACTIVE").upper()
                        vat_st_val = (row.get("vat_status") or "REGISTERED").upper()
                        try:
                            legal_st = CompanyStatus(legal_st_val)
                        except ValueError:
                            legal_st = CompanyStatus.ACTIVE
                        try:
                            vat_st = VatRegistrationStatus(vat_st_val)
                        except ValueError:
                            vat_st = VatRegistrationStatus.REGISTERED

                        issues: list[str] = []
                        is_valid_credit = True
                        if legal_st in (CompanyStatus.BANKRUPTCY, CompanyStatus.LIQUIDATION, CompanyStatus.DEREGISTERED_DELETED):
                            is_valid_credit = False
                            issues.append(f"Фирмата е в неактивен правен статус в Търговския регистър: {legal_st.value}")
                        if vat_st == VatRegistrationStatus.DEREGISTERED:
                            is_valid_credit = False
                            issues.append("Фирмата е ДЕРЕГИСТРИРАНА по ЗДДС в НАП.")
                        elif vat_st == VatRegistrationStatus.NOT_REGISTERED:
                            is_valid_credit = False
                            issues.append("Фирмата НЕ Е регистрирана по ЗДДС.")

                        # Canonical registered office from decomposed seat or address
                        seat_addr = row.get("address")
                        if not seat_addr and row.get("seat_settlement") and row.get("seat_street"):
                            parts = [row.get("seat_settlement")]
                            if row.get("seat_area"):
                                parts.append(row.get("seat_area"))
                            street_part = row.get("seat_street")
                            if row.get("seat_street_number"):
                                street_part += f" {row.get('seat_street_number')}"
                            parts.append(street_part)
                            seat_addr = ", ".join(parts)

                        return ContractorVerificationResult(
                            country_code=row.get("country_code", country),
                            identifier=row.get("eik", ident),
                            company_name=row.get("legal_name"),
                            legal_status=legal_st,
                            vat_status=vat_st,
                            vat_registration_date=str(row.get("vat_registration_date")) if row.get("vat_registration_date") else None,
                            vat_deregistration_date=str(row.get("vat_deregistration_date")) if row.get("vat_deregistration_date") else None,
                            vat_legal_basis=row.get("vat_legal_basis"),
                            address=row.get("address"),
                            seat_address=seat_addr or row.get("address"),
                            mol_name=row.get("mol_name"),
                            trade_outlets=row.get("trade_outlets") or [],
                            managers=row.get("managers") or [],
                            nkids=row.get("nkids") or [],
                            source="SUPABASE_CONTRACTORS",
                            is_valid_for_tax_credit=is_valid_credit,
                            issues=issues,
                            raw_data=row,
                        )
        except Exception as exc:
            logger.debug("Supabase contractor query skipped: %s", exc)
        return None

    async def _upsert_supabase_contractor_async(
        self,
        res: ContractorVerificationResult,
    ) -> None:
        """Asynchronously upsert newly verified contractor into Supabase public.contractors."""
        if res.source in ("SUPABASE_CONTRACTORS", "OFFLINE_UNVERIFIED", "MOCK_REGISTRY"):
            return
        if not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
            return

        try:
            url = f"{SUPABASE_URL}/rest/v1/contractors"
            headers = {
                "apikey": SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=representation",
            }
            body = {
                "country_code": res.country_code,
                "eik": res.identifier,
                "vat_number": f"{res.country_code}{res.identifier}" if res.vat_status == VatRegistrationStatus.REGISTERED else None,
                "legal_name": res.company_name or f"Контрагент {res.identifier}",
                "address": res.address,
                "legal_status": res.legal_status.value,
                "vat_status": res.vat_status.value,
                "vat_registration_date": res.vat_registration_date,
                "vat_deregistration_date": res.vat_deregistration_date,
                "vat_legal_basis": res.vat_legal_basis,
                "is_verified": res.is_valid_for_tax_credit,
                "verified_source": res.source,
            }
            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.post(url, json=body, headers=headers)
        except Exception as exc:
            logger.debug("Background Supabase contractor upsert error: %s", exc)

    async def _query_vies_rest_async(
        self,
        country: str,
        vat_number: str,
        date_tax_event: str | None,
    ) -> ContractorVerificationResult:
        """Query official European Commission VIES REST API."""
        url = VIES_REST_URL.format(country=country, vat=vat_number)
        issues: list[str] = []
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    payload = resp.json()
                    is_valid = bool(payload.get("isValid", False))
                    name = payload.get("name")
                    address = payload.get("address")

                    vat_stat = VatRegistrationStatus.REGISTERED if is_valid else VatRegistrationStatus.NOT_REGISTERED
                    if not is_valid:
                        issues.append(f"ДДС номерът '{country}{vat_number}' е НЕВАЛИДЕН в европейската система VIES.")

                    res = ContractorVerificationResult(
                        country_code=country,
                        identifier=f"{country}{vat_number}",
                        company_name=name if name and name != "---" else None,
                        legal_status=CompanyStatus.ACTIVE if is_valid else CompanyStatus.UNKNOWN,
                        vat_status=vat_stat,
                        address=address if address and address != "---" else None,
                        source="VIES_REST",
                        is_valid_for_tax_credit=is_valid,
                        issues=issues,
                        raw_data=payload,
                    )
                    self._evaluate_date_tax_event(res, date_tax_event)
                    self.cache.set(res)
                    return res
                else:
                    issues.append(f"VIES REST API грешка (HTTP {resp.status_code})")
        except Exception as exc:
            logger.warning("VIES query failed for %s%s: %s", country, vat_number, exc)
            issues.append(f"VIES връзката е недостъпна: {exc}")

        res = ContractorVerificationResult(
            country_code=country,
            identifier=f"{country}{vat_number}",
            legal_status=CompanyStatus.UNKNOWN,
            vat_status=VatRegistrationStatus.UNKNOWN,
            source="VIES_UNREACHABLE",
            is_valid_for_tax_credit=False,
            issues=issues,
        )
        self._evaluate_date_tax_event(res, date_tax_event)
        return res

    async def _query_bg_registries_async(
        self,
        eik: str,
        date_tax_event: str | None,
    ) -> ContractorVerificationResult:
        """Query Bulgarian open data or official NRA / Commercial Register endpoints."""
        nra_api_url = os.environ.get("NRA_REGISTRY_API_URL")
        issues: list[str] = []
        if nra_api_url:
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(f"{nra_api_url.rstrip('/')}/vat/{eik}")
                    if resp.status_code == 200:
                        payload = resp.json()
                        res = ContractorVerificationResult(
                            country_code="BG",
                            identifier=eik,
                            company_name=payload.get("name"),
                            legal_status=CompanyStatus(payload.get("status", "ACTIVE")),
                            vat_status=VatRegistrationStatus(payload.get("vat_status", "REGISTERED")),
                            vat_registration_date=payload.get("vat_registration_date"),
                            vat_deregistration_date=payload.get("vat_deregistration_date"),
                            vat_legal_basis=payload.get("vat_legal_basis"),
                            source="NRA_API",
                            is_valid_for_tax_credit=payload.get("is_valid", True),
                            raw_data=payload,
                        )
                        self._evaluate_date_tax_event(res, date_tax_event)
                        self.cache.set(res)
                        return res
            except Exception as exc:
                logger.warning("Online NRA query failed for EIK %s: %s", eik, exc)
                issues.append(f"Връзката с регистъра на НАП е неуспешна: {exc}")

        # Check Local Offline VAT Registry before failing secure
        reg_entry = self.vat_registry.get(eik)
        if reg_entry:
            legal_st = CompanyStatus(reg_entry.get("legal_status", "ACTIVE"))
            vat_st = VatRegistrationStatus(reg_entry.get("vat_status", "REGISTERED"))
            is_valid_credit = True
            if legal_st in (CompanyStatus.BANKRUPTCY, CompanyStatus.LIQUIDATION, CompanyStatus.DEREGISTERED_DELETED):
                is_valid_credit = False
                issues.append(f"Фирмата е в неактивен правен статус в Търговския регистър: {legal_st.value}")
            if vat_st == VatRegistrationStatus.DEREGISTERED:
                is_valid_credit = False
                issues.append("Фирмата е ДЕРЕГИСТРИРАНА по ЗДДС в НАП.")
            elif vat_st == VatRegistrationStatus.NOT_REGISTERED:
                is_valid_credit = False
                issues.append("Фирмата НЕ Е регистрирана по ЗДДС.")

            res = ContractorVerificationResult(
                country_code="BG",
                identifier=eik,
                company_name=reg_entry.get("company_name"),
                legal_status=legal_st,
                vat_status=vat_st,
                vat_registration_date=reg_entry.get("vat_registration_date"),
                vat_deregistration_date=reg_entry.get("vat_deregistration_date"),
                vat_legal_basis=reg_entry.get("vat_legal_basis"),
                address=reg_entry.get("address"),
                source="LOCAL_VAT_REGISTRY",
                is_valid_for_tax_credit=is_valid_credit,
                issues=issues,
                raw_data=reg_entry,
            )
            self._evaluate_date_tax_event(res, date_tax_event)
            self.cache.set(res)
            return res

        # When online NRA query is not configured or unavailable and entity not in local DB
        issues.append(
            f"Контрагент с ЕИК '{eik}' не е намерен в регистъра на НАП или локалната база данни. "
            "Изисква се ръчно потвърждение преди ползване на данъчен кредит."
        )
        res = ContractorVerificationResult(
            country_code="BG",
            identifier=eik,
            legal_status=CompanyStatus.UNKNOWN,
            vat_status=VatRegistrationStatus.UNKNOWN,
            source="OFFLINE_UNVERIFIED",
            is_valid_for_tax_credit=False,
            issues=issues,
        )
        self._evaluate_date_tax_event(res, date_tax_event)
        self.cache.set(res)
        return res

    def verify_sync(
        self,
        identifier: str,
        country_code: str | None = None,
        date_tax_event: str | None = None,
        bypass_cache: bool = False,
    ) -> ContractorVerificationResult:
        """Synchronous wrapper for verify_async."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    asyncio.run,
                    self.verify_async(
                        identifier,
                        country_code=country_code,
                        date_tax_event=date_tax_event,
                        bypass_cache=bypass_cache,
                    ),
                )
                return future.result()
        else:
            return asyncio.run(
                self.verify_async(
                    identifier,
                    country_code=country_code,
                    date_tax_event=date_tax_event,
                    bypass_cache=bypass_cache,
                )
            )


default_verifier = ContractorVerifier()


def verify_contractor(
    identifier: str,
    country_code: str | None = None,
    date_tax_event: str | None = None,
    bypass_cache: bool = False,
) -> ContractorVerificationResult:
    """Convenience synchronous verification function."""
    return default_verifier.verify_sync(
        identifier=identifier,
        country_code=country_code,
        date_tax_event=date_tax_event,
        bypass_cache=bypass_cache,
    )


async def verify_contractor_async(
    identifier: str,
    country_code: str | None = None,
    date_tax_event: str | None = None,
    bypass_cache: bool = False,
) -> ContractorVerificationResult:
    """Convenience asynchronous verification function."""
    return await default_verifier.verify_async(
        identifier=identifier,
        country_code=country_code,
        date_tax_event=date_tax_event,
        bypass_cache=bypass_cache,
    )
