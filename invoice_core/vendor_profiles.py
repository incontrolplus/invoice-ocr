"""Vendor profiles engine for Bulgarian Invoice OCR.

Externalizes vendor-specific layout, identification, and banking heuristics
into YAML configuration files under `vendor_profiles/`, with robust
in-code fallbacks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any

from .constants import PROJECT_ROOT

logger = logging.getLogger("invoice_ocr")

VENDOR_PROFILES_DIR = PROJECT_ROOT / "vendor_profiles"

# Built-in fallback profiles in case YAML files are not deployed or fail to parse
DEFAULT_BANKING_PROFILES: list[tuple[str, str, str, str]] = [
    ("анда", "BG69UBBS80021086890030", "UBBSBGSF", "Обединена българска банка АД"),
    ("анко петров", "BG69UBBS80021086890030", "UBBSBGSF", "Обединена българска банка АД"),
    ("капина", "BG13BPBI81701060091301", "BPBIBGSF", "Юробанк България АД"),
    ("оскари", "BG86PRCB92301000593422", "PRCBBGSF", "ПроКредит Банк (България) ЕАД"),
    ("грестокомерс", "BG39UNCR70001500876294", "UNCRBGSF", "УниКредит Булбанк АД"),
    ("теменужка", "BG57UNCR70001523788049", "UNCRBGSF", "УниКредит Булбанк АД"),
    ("елико", "BG65UBBS80021071504350", "UBBSBGSF", "Обединена българска банка АД"),
    ("валборген", "BG10STSA93000027446545", "STSABGSF", "Банка ДСК АД"),
]


@dataclass
class VendorProfile:
    """Configurable profile for an invoice vendor/counterparty."""
    id: str
    name: str
    eik: str
    vat_number: str
    alternate_eiks: list[str] = field(default_factory=list)
    address: str = ""
    fused_eik_prefixes: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    invoice_number_series: dict[str, Any] = field(default_factory=dict)
    layout: dict[str, Any] = field(default_factory=dict)
    ocr: dict[str, Any] = field(default_factory=dict)


_PROFILES_CACHE: dict[str, VendorProfile] | None = None
_BANKING_CACHE: list[tuple[str, str, str, str]] | None = None


def load_yaml_file(path: Path) -> dict[str, Any] | None:
    """Safely load and parse a YAML file."""
    if not path.exists():
        return None
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("Failed to load vendor profile from %s: %s", path, exc)
        return None


def get_vendor_profiles() -> dict[str, VendorProfile]:
    """Retrieve all loaded vendor profiles, cached in memory."""
    global _PROFILES_CACHE
    if _PROFILES_CACHE is not None:
        return _PROFILES_CACHE

    profiles: dict[str, VendorProfile] = {}

    # Define defaults first
    defaults = {
        "metro": VendorProfile(
            id="metro",
            name="МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД",
            eik="121644736",
            vat_number="BG121644736",
            alternate_eiks=["121644734"],
            address="гр. София 1784, бул. Цариградско шосе 7-11 км",
            fused_eik_prefixes=["121644736", "121644734"],
            keywords=["metro", "метро", "кеш енд кери"],
            layout={"customer_box": True, "recapitulation_table": True},
        ),
        "detelina": VendorProfile(
            id="detelina",
            name="ДЕТЕЛИНА-ДП ЕООД",
            eik="114609507",
            vat_number="BG114609507",
            alternate_eiks=["114609407"],
            address="гр. Плевен, ул. Гривишко шосе 1",
            invoice_number_series={"prefix": "100099", "length": 10},
            keywords=["детелина", "детелина-дп", "детелина дп"],
            ocr={"dot_matrix": True},
        ),
        "toplivo": VendorProfile(
            id="toplivo",
            name="ТОПЛИВО ГАЗ ЕООД",
            eik="130864186",
            vat_number="BG130864186",
            invoice_number_series={"prefix": "0703", "length": 10},
            keywords=["топливо", "топливо газ", "топаиво"],
        ),
    }

    # Override/supplement with YAML files if present
    if VENDOR_PROFILES_DIR.is_dir():
        for yaml_path in VENDOR_PROFILES_DIR.glob("*.yaml"):
            if yaml_path.name == "banking_fallbacks.yaml":
                continue
            data = load_yaml_file(yaml_path)
            if data and "id" in data and "eik" in data:
                prof_id = str(data["id"])
                profiles[prof_id] = VendorProfile(
                    id=prof_id,
                    name=data.get("name", ""),
                    eik=str(data["eik"]),
                    vat_number=data.get("vat_number", f"BG{data['eik']}"),
                    alternate_eiks=[str(e) for e in data.get("alternate_eiks", [])],
                    address=data.get("address", ""),
                    fused_eik_prefixes=[str(p) for p in data.get("fused_eik_prefixes", [])],
                    keywords=[str(k).lower() for k in data.get("keywords", [])],
                    invoice_number_series=data.get("invoice_number_series", {}),
                    layout=data.get("layout", {}),
                    ocr=data.get("ocr", {}),
                )

    # Merge defaults for any missing profile
    for k, v in defaults.items():
        if k not in profiles:
            profiles[k] = v

    _PROFILES_CACHE = profiles
    return _PROFILES_CACHE


def get_known_supplier_profiles() -> list[tuple[str, str, str, str]]:
    """Retrieve list of (keyword, IBAN, BIC, BankName) for frequent suppliers."""
    global _BANKING_CACHE
    if _BANKING_CACHE is not None:
        return _BANKING_CACHE

    yaml_path = VENDOR_PROFILES_DIR / "banking_fallbacks.yaml"
    data = load_yaml_file(yaml_path)
    if data and "banking_profiles" in data and isinstance(data["banking_profiles"], list):
        result: list[tuple[str, str, str, str]] = []
        for entry in data["banking_profiles"]:
            keywords = entry.get("keywords", [])
            iban = entry.get("iban", "")
            bic = entry.get("bic", "")
            bank_name = entry.get("bank_name", "")
            if iban and keywords:
                for kw in keywords:
                    result.append((kw.lower(), iban, bic, bank_name))
        if result:
            _BANKING_CACHE = result
            return _BANKING_CACHE

    _BANKING_CACHE = list(DEFAULT_BANKING_PROFILES)
    return _BANKING_CACHE


def reset_vendor_profiles_cache() -> None:
    """Reset in-memory profile caches (useful for testing)."""
    global _PROFILES_CACHE, _BANKING_CACHE
    _PROFILES_CACHE = None
    _BANKING_CACHE = None


def get_vendor_profile(identifier: str) -> dict[str, Any] | None:
    """Find a vendor profile by ID, EIK, or keyword."""
    if not identifier:
        return None
    ident = str(identifier).strip().lower()
    profiles = get_vendor_profiles()
    # 1. By ID
    if ident in profiles:
        p = profiles[ident]
        return {
            "id": p.id,
            "name": p.name,
            "eik": p.eik,
            "vat_number": p.vat_number,
            "address": p.address,
        }
    # 2. By EIK
    for p in profiles.values():
        if p.eik == ident or ident in p.alternate_eiks:
            return {
                "id": p.id,
                "name": p.name,
                "eik": p.eik,
                "vat_number": p.vat_number,
                "address": p.address,
            }
    # 3. By name or keyword
    for p in profiles.values():
        if ident in p.name.lower() or any(k in ident or ident in k for k in p.keywords):
            return {
                "id": p.id,
                "name": p.name,
                "eik": p.eik,
                "vat_number": p.vat_number,
                "address": p.address,
            }
    return None


def list_known_profiles() -> list[dict[str, Any]]:
    """Return all known vendor profiles as simple dicts."""
    return [
        {
            "id": p.id,
            "name": p.name,
            "eik": p.eik,
            "vat_number": p.vat_number,
            "address": p.address,
        }
        for p in get_vendor_profiles().values()
    ]


KNOWN_SUPPLIER_PROFILES = get_known_supplier_profiles()
