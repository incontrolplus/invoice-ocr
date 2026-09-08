"""Vendor profiles engine for Bulgarian Invoice OCR.

Externalizes vendor-specific layout, identification, and banking heuristics
into YAML configuration files under `config/vendors/` (and `vendor_profiles/`),
with strict Pydantic schema validation, fail-safe fallbacks, and dynamic
hot-reloading without server restarts.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
import yaml

from .constants import PROJECT_ROOT

logger = logging.getLogger("invoice_ocr")

# Primary configuration directory for vendor profiles
CONFIG_VENDORS_DIR = PROJECT_ROOT / "config" / "vendors"
# Legacy / fallback directory for backward compatibility
VENDOR_PROFILES_DIR = PROJECT_ROOT / "vendor_profiles"

DEFAULT_CONFIG_DIRS: list[Path] = [CONFIG_VENDORS_DIR, VENDOR_PROFILES_DIR]

# Built-in fallback banking profiles for frequent Bulgarian counterparties
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


# ===========================================================================
# Strict Pydantic Schema for Vendor Profile
# ===========================================================================

class VendorProfile(BaseModel):
    """Strict Pydantic schema for externalized vendor profiles."""
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str = Field(..., min_length=1, description="Unique slug identifier (e.g. 'metro', 'omv')")
    name: str = Field(..., min_length=1, description="Official company name")
    eik: str = Field(..., description="Unified Identification Code (EIK/BULSTAT) - 9 or 13 digits")
    vat: Optional[str] = Field(default=None, description="VAT registration number (e.g. 'BG121644736')")
    vat_number: Optional[str] = Field(default=None, description="Alias for vat")
    alternate_eiks: list[str] = Field(default_factory=list, description="Alternative or previous EIKs")
    address: str = Field(default="", description="Registered address of vendor")
    keywords: list[str] = Field(default_factory=list, description="Keywords for vendor identification in OCR text")
    table_anchors: list[str] = Field(default_factory=list, description="Anchors for table header and line items")
    column_offsets: dict[str, Any] = Field(default_factory=dict, description="Relative horizontal column boundaries")
    fused_patterns: list[str] = Field(default_factory=list, description="Regexes or prefixes for fused numbers/EIKs")
    fused_eik_prefixes: list[str] = Field(default_factory=list, description="Compatibility alias for fused prefixes")
    dot_matrix: bool = Field(default=False, description="Flag indicating dot-matrix 9-pin/24-pin invoice print")
    invoice_number_series: dict[str, Any] = Field(default_factory=dict, description="Series prefix and length rules")
    layout: dict[str, Any] = Field(default_factory=dict, description="Layout configuration (e.g. customer_box, recapitulation_table)")
    ocr: dict[str, Any] = Field(default_factory=dict, description="OCR specific settings (e.g. dot_matrix, psm)")
    banking: list[dict[str, Any]] = Field(default_factory=list, description="Vendor banking details")

    @field_validator("eik", mode="before")
    @classmethod
    def validate_and_clean_eik(cls, v: Any) -> str:
        if v is None:
            raise ValueError("EIK cannot be empty")
        raw = str(v).strip()
        digits = re.sub(r"\D", "", raw)
        if len(digits) not in (9, 13):
            raise ValueError(f"EIK must contain 9 or 13 digits, got '{raw}' ({len(digits)} digits)")
        return digits

    @field_validator("alternate_eiks", mode="before")
    @classmethod
    def validate_alternate_eiks(cls, v: Any) -> list[str]:
        if not v:
            return []
        cleaned = []
        for item in v:
            digits = re.sub(r"\D", "", str(item).strip())
            if digits:
                cleaned.append(digits)
        return cleaned

    @field_validator("keywords", mode="before")
    @classmethod
    def clean_keywords(cls, v: Any) -> list[str]:
        if isinstance(v, (list, tuple, set)):
            return [str(k).strip().lower() for k in v if str(k).strip()]
        if isinstance(v, str) and v.strip():
            return [v.strip().lower()]
        return []

    @field_validator("table_anchors", mode="before")
    @classmethod
    def clean_table_anchors(cls, v: Any) -> list[str]:
        if isinstance(v, (list, tuple, set)):
            return [str(a).strip() for a in v if str(a).strip()]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    @field_validator("fused_patterns", "fused_eik_prefixes", mode="before")
    @classmethod
    def clean_fused(cls, v: Any) -> list[str]:
        if isinstance(v, (list, tuple, set)):
            return [str(p).strip() for p in v if str(p).strip()]
        if isinstance(v, str) and v.strip():
            return [v.strip()]
        return []

    @model_validator(mode="after")
    def reconcile_fields(self) -> "VendorProfile":
        # 1. VAT and vat_number bidirectional reconciliation
        if not self.vat and self.vat_number:
            self.vat = self.vat_number
        elif not self.vat_number and self.vat:
            self.vat_number = self.vat
        elif not self.vat and not self.vat_number:
            self.vat = f"BG{self.eik}"
            self.vat_number = f"BG{self.eik}"

        # 2. fused_patterns and fused_eik_prefixes bidirectional sync
        all_fused = list(dict.fromkeys(self.fused_patterns + self.fused_eik_prefixes))
        if not all_fused:
            all_fused = [self.eik] + [e for e in self.alternate_eiks]
        self.fused_patterns = all_fused
        self.fused_eik_prefixes = all_fused

        # 3. dot_matrix and ocr["dot_matrix"] sync
        if self.dot_matrix:
            self.ocr["dot_matrix"] = True
        elif self.ocr.get("dot_matrix"):
            self.dot_matrix = True

        # 4. table_anchors and layout["table_anchors"] sync
        if self.table_anchors and "table_anchors" not in self.layout:
            self.layout["table_anchors"] = self.table_anchors
        elif not self.table_anchors and "table_anchors" in self.layout:
            self.table_anchors = [str(x) for x in self.layout["table_anchors"]]

        # 5. column_offsets and layout["column_offsets"] sync
        if self.column_offsets and "column_offsets" not in self.layout:
            self.layout["column_offsets"] = self.column_offsets
        elif not self.column_offsets and "column_offsets" in self.layout:
            self.column_offsets = dict(self.layout["column_offsets"])

        return self

    def __getitem__(self, key: str) -> Any:
        """Allow dict-like subscription for full backward compatibility."""
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        """Allow dict-like get() for full backward compatibility."""
        return getattr(self, key, default)

    def to_dict(self) -> dict[str, Any]:
        """Serialize profile to clean dictionary for JSON responses."""
        return {
            "id": self.id,
            "name": self.name,
            "eik": self.eik,
            "vat": self.vat,
            "vat_number": self.vat_number,
            "alternate_eiks": self.alternate_eiks,
            "address": self.address,
            "keywords": self.keywords,
            "table_anchors": self.table_anchors,
            "column_offsets": self.column_offsets,
            "fused_patterns": self.fused_patterns,
            "fused_eik_prefixes": self.fused_eik_prefixes,
            "dot_matrix": self.dot_matrix,
            "invoice_number_series": self.invoice_number_series,
            "layout": self.layout,
            "ocr": self.ocr,
            "banking": self.banking,
        }


# ===========================================================================
# Hardcoded Base Heuristics / Built-in Fallbacks
# ===========================================================================

BUILTIN_DEFAULTS: dict[str, VendorProfile] = {
    "metro": VendorProfile(
        id="metro",
        name="МЕТРО КЕШ ЕНД КЕРИ БЪЛГАРИЯ ЕООД",
        eik="121644736",
        vat="BG121644736",
        alternate_eiks=["121644734"],
        address="гр. София 1784, бул. Цариградско шосе 7-11 км",
        fused_patterns=["121644736", "121644734"],
        keywords=["metro", "метро", "кеш енд кери"],
        table_anchors=["код", "артикул", "наименование", "количество", "мярка", "ед.цена", "стойност", "ддс"],
        column_offsets={
            "code": [0.0, 0.15],
            "description": [0.15, 0.52],
            "quantity": [0.52, 0.65],
            "unit_price": [0.65, 0.80],
            "total": [0.80, 1.0],
        },
        layout={"customer_box": True, "recapitulation_table": True},
        dot_matrix=False,
    ),
    "detelina": VendorProfile(
        id="detelina",
        name="ДЕТЕЛИНА-ДП ЕООД",
        eik="114609507",
        vat="BG114609507",
        alternate_eiks=["114609407"],
        address="гр. Плевен, ул. Гривишко шосе 1",
        invoice_number_series={"prefix": "100099", "length": 10},
        keywords=["детелина", "детелина-дп", "детелина дп"],
        table_anchors=["наименование на стоката", "мярка", "количество", "цена", "сума"],
        column_offsets={
            "description": [0.05, 0.45],
            "unit": [0.45, 0.55],
            "quantity": [0.55, 0.68],
            "unit_price": [0.68, 0.82],
            "total": [0.82, 0.98],
        },
        dot_matrix=True,
        ocr={"dot_matrix": True},
    ),
    "toplivo": VendorProfile(
        id="toplivo",
        name="ТОПЛИВО ГАЗ ЕООД",
        eik="130864186",
        vat="BG130864186",
        address="гр. София 1000, ул. Солунска 2",
        invoice_number_series={"prefix": "0703", "length": 10},
        keywords=["топливо", "топливо газ", "топаиво"],
        table_anchors=["наименование", "мярка", "количество", "ед.цена", "стойност"],
        column_offsets={
            "description": [0.05, 0.50],
            "unit": [0.50, 0.60],
            "quantity": [0.60, 0.72],
            "unit_price": [0.72, 0.85],
            "total": [0.85, 0.98],
        },
        dot_matrix=False,
    ),
    "omv": VendorProfile(
        id="omv",
        name="ОМВ БЪЛГАРИЯ ООД",
        eik="831101035",
        vat="BG831101035",
        address="гр. София 1784, бул. Цариградско шосе 115М, сграда Д",
        keywords=["omv", "омв", "омв българия"],
        invoice_number_series={"prefix": "0", "length": 10},
        table_anchors=["продукт", "артикул", "количество", "ед. цена", "стойност", "ддс"],
        column_offsets={
            "description": [0.05, 0.48],
            "quantity": [0.48, 0.62],
            "unit_price": [0.62, 0.78],
            "total": [0.78, 0.95],
        },
        fused_patterns=["831101035"],
        dot_matrix=False,
    ),
    "shell": VendorProfile(
        id="shell",
        name="ШЕЛ БЪЛГАРИЯ ЕАД",
        eik="831299905",
        vat="BG831299905",
        address="гр. София 1784, бул. Цариградско шосе 73",
        keywords=["shell", "шел", "шел българия"],
        invoice_number_series={"prefix": "0", "length": 10},
        table_anchors=["описание", "артикул", "количество", "цена", "сума"],
        column_offsets={
            "description": [0.05, 0.50],
            "quantity": [0.50, 0.65],
            "unit_price": [0.65, 0.80],
            "total": [0.80, 0.95],
        },
        fused_patterns=["831299905"],
        dot_matrix=False,
    ),
}


# ===========================================================================
# Dynamic Vendor Profile Loader
# ===========================================================================

class VendorProfileLoader:
    """Discovers, validates, and dynamic-reloads YAML vendor profiles."""

    def __init__(
        self,
        config_dirs: list[Path] | Path | None = None,
        auto_reload: bool = True,
        check_interval: float = 0.5,
    ) -> None:
        if config_dirs is None:
            self.config_dirs = list(DEFAULT_CONFIG_DIRS)
        elif isinstance(config_dirs, Path):
            self.config_dirs = [config_dirs]
        else:
            self.config_dirs = list(config_dirs)

        self.auto_reload = auto_reload
        self.check_interval = check_interval
        self._lock = threading.RLock()
        self._profiles: dict[str, VendorProfile] = {}
        self._banking_profiles: list[tuple[str, str, str, str]] = []
        self._file_mtimes: dict[str, float] = {}
        self._file_to_profile_id: dict[str, str] = {}
        self._errors: list[dict[str, Any]] = []
        self._last_reload_time: float = 0.0
        self._last_check_time: float = 0.0

        # Initial load
        self.reload()

    def _discover_yaml_files(self) -> list[Path]:
        """Find all YAML files across configured directories in priority order."""
        files: list[Path] = []
        seen_names: set[str] = set()
        for d in self.config_dirs:
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml")):
                if p.name not in seen_names:
                    seen_names.add(p.name)
                    files.append(p)
        return files

    def has_changes(self) -> bool:
        """Check if any YAML files were created, modified, or removed."""
        with self._lock:
            current_files = self._discover_yaml_files()
            current_mtimes: dict[str, float] = {}
            for p in current_files:
                try:
                    current_mtimes[str(p.resolve())] = p.stat().st_mtime
                except OSError:
                    pass
            return current_mtimes != self._file_mtimes

    def reload(self) -> dict[str, Any]:
        """Perform full reload and validation of all YAML files with fail-safe fallbacks."""
        with self._lock:
            new_profiles: dict[str, VendorProfile] = {}
            new_banking: list[tuple[str, str, str, str]] = []
            new_errors: list[dict[str, Any]] = []
            new_mtimes: dict[str, float] = {}

            yaml_files = self._discover_yaml_files()
            for filepath in yaml_files:
                resolved_path = str(filepath.resolve())
                try:
                    mtime = filepath.stat().st_mtime
                    new_mtimes[resolved_path] = mtime
                except OSError:
                    continue

                # Safely load YAML
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        raw_data = yaml.safe_load(f)
                except Exception as exc:
                    err_entry = {
                        "file": str(filepath),
                        "type": "yaml_syntax_error",
                        "error": str(exc),
                    }
                    logger.warning("YAML syntax error in %s: %s", filepath, exc)
                    new_errors.append(err_entry)
                    # FAIL-SAFE: if previous valid version existed, retain it
                    prev_id = self._file_to_profile_id.get(resolved_path) or filepath.stem
                    if prev_id in self._profiles and prev_id not in new_profiles:
                        new_profiles[prev_id] = self._profiles[prev_id]
                    continue

                if not isinstance(raw_data, dict):
                    err_entry = {
                        "file": str(filepath),
                        "type": "invalid_structure",
                        "error": "File content must be a YAML dictionary/mapping",
                    }
                    logger.warning("Invalid YAML structure in %s: expected dict, got %s", filepath, type(raw_data))
                    new_errors.append(err_entry)
                    continue

                # Check if this is a banking fallback file
                if filepath.name == "banking_fallbacks.yaml" or "banking_profiles" in raw_data:
                    bp_list = raw_data.get("banking_profiles", [])
                    if isinstance(bp_list, list):
                        for entry in bp_list:
                            if isinstance(entry, dict):
                                iban = str(entry.get("iban", "")).strip()
                                bic = str(entry.get("bic", "")).strip()
                                bank_name = str(entry.get("bank_name", "")).strip()
                                kws = entry.get("keywords", [])
                                if iban and kws:
                                    for kw in kws:
                                        new_banking.append((str(kw).strip().lower(), iban, bic, bank_name))
                    continue

                # Vendor profile configuration
                prof_id = raw_data.get("id") or filepath.stem
                raw_data["id"] = str(prof_id)

                try:
                    profile = VendorProfile.model_validate(raw_data)
                    new_profiles[profile.id] = profile
                    self._file_to_profile_id[resolved_path] = profile.id

                    # Register any bank accounts defined directly in the vendor profile
                    if profile.banking:
                        for b_entry in profile.banking:
                            iban = str(b_entry.get("iban", "")).strip()
                            bic = str(b_entry.get("bic", "")).strip()
                            b_name = str(b_entry.get("bank_name", "")).strip()
                            extra_kws = [str(k).lower() for k in b_entry.get("keywords", [])]
                            all_kws = list(set(profile.keywords + extra_kws))
                            if iban:
                                for kw in all_kws:
                                    new_banking.append((kw, iban, bic, b_name))

                except Exception as exc:
                    err_entry = {
                        "file": str(filepath),
                        "type": "validation_error",
                        "error": str(exc),
                    }
                    logger.warning("VendorProfile validation error in %s: %s", filepath, exc)
                    new_errors.append(err_entry)
                    # FAIL-SAFE: retain previous valid version if present
                    prev_id = self._file_to_profile_id.get(resolved_path) or prof_id
                    if prev_id in self._profiles and prev_id not in new_profiles:
                        new_profiles[prev_id] = self._profiles[prev_id]

            # Merge built-in defaults for standard vendors if not loaded from YAML
            for def_id, def_prof in BUILTIN_DEFAULTS.items():
                if def_id not in new_profiles:
                    new_profiles[def_id] = def_prof

            # If no banking profiles discovered from files, fallback to DEFAULT_BANKING_PROFILES
            if not new_banking:
                new_banking = list(DEFAULT_BANKING_PROFILES)
            else:
                # Merge default banking profiles so frequent vendors are never lost
                existing_ibans = {entry[1] for entry in new_banking}
                for def_entry in DEFAULT_BANKING_PROFILES:
                    if def_entry[1] not in existing_ibans:
                        new_banking.append(def_entry)

            self._profiles = new_profiles
            self._banking_profiles = new_banking
            self._file_mtimes = new_mtimes
            self._errors = new_errors
            self._last_reload_time = time.time()
            self._last_check_time = self._last_reload_time

            return {
                "success": True,
                "loaded_count": len(self._profiles),
                "profiles": sorted(list(self._profiles.keys())),
                "errors": self._errors,
                "reloaded_at": datetime.now(timezone.utc).isoformat(),
            }

    def _maybe_auto_reload(self) -> None:
        """Check filesystem for changes if auto_reload is enabled and interval elapsed."""
        if not self.auto_reload:
            return
        now = time.time()
        if now - self._last_check_time < self.check_interval:
            return
        self._last_check_time = now
        if self.has_changes():
            logger.info("Vendor profile files changed on disk. Auto-reloading...")
            self.reload()

    def get_profiles(self) -> dict[str, VendorProfile]:
        """Retrieve all loaded vendor profiles, auto-reloading if changed on disk."""
        with self._lock:
            self._maybe_auto_reload()
            return dict(self._profiles)

    def get_profile(self, identifier: str) -> VendorProfile | None:
        """Find a vendor profile by ID, EIK, alternate EIK, name, or keyword."""
        if not identifier:
            return None
        with self._lock:
            self._maybe_auto_reload()
            ident = str(identifier).strip().lower()

            # 1. By ID
            if ident in self._profiles:
                return self._profiles[ident]

            # 2. By EIK or alternate EIK
            digits = re.sub(r"\D", "", ident)
            if digits:
                for p in self._profiles.values():
                    if p.eik == digits or digits in p.alternate_eiks:
                        return p

            # 3. By name or keyword
            for p in self._profiles.values():
                if ident in p.name.lower():
                    return p
                if any(k in ident or ident in k for k in p.keywords):
                    return p

            return None

    def get_banking_profiles(self) -> list[tuple[str, str, str, str]]:
        """Retrieve all banking profiles."""
        with self._lock:
            self._maybe_auto_reload()
            return list(self._banking_profiles)

    def get_diagnostics(self) -> dict[str, Any]:
        """Return operational diagnostics for monitoring and health endpoints."""
        with self._lock:
            return {
                "loaded_count": len(self._profiles),
                "profiles": sorted(list(self._profiles.keys())),
                "errors": list(self._errors),
                "last_reloaded": (
                    datetime.fromtimestamp(self._last_reload_time, tz=timezone.utc).isoformat()
                    if self._last_reload_time > 0
                    else None
                ),
                "config_dirs": [str(d) for d in self.config_dirs if d.exists()],
            }


# Global singleton instance of VendorProfileLoader
_GLOBAL_LOADER: VendorProfileLoader | None = None
_LOADER_LOCK = threading.Lock()


def get_vendor_profile_loader() -> VendorProfileLoader:
    """Retrieve the global singleton instance of VendorProfileLoader."""
    global _GLOBAL_LOADER
    if _GLOBAL_LOADER is None:
        with _LOADER_LOCK:
            if _GLOBAL_LOADER is None:
                _GLOBAL_LOADER = VendorProfileLoader()
    return _GLOBAL_LOADER


# ===========================================================================
# Backward-Compatible Module-Level API
# ===========================================================================

def load_yaml_file(path: Path) -> dict[str, Any] | None:
    """Safely load and parse a YAML file."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("Failed to load vendor profile from %s: %s", path, exc)
        return None


def get_vendor_profiles() -> dict[str, VendorProfile]:
    """Retrieve all loaded vendor profiles (auto-reloaded if files change)."""
    return get_vendor_profile_loader().get_profiles()


def get_known_supplier_profiles() -> list[tuple[str, str, str, str]]:
    """Retrieve list of (keyword, IBAN, BIC, BankName) for frequent suppliers."""
    return get_vendor_profile_loader().get_banking_profiles()


def reset_vendor_profiles_cache() -> None:
    """Force an immediate reload of all vendor profiles."""
    get_vendor_profile_loader().reload()


def get_vendor_profile(identifier: str) -> dict[str, Any] | None:
    """Find a vendor profile by ID, EIK, or keyword and return it as a dictionary."""
    prof = get_vendor_profile_loader().get_profile(identifier)
    if prof:
        return prof.to_dict()
    return None


def list_known_profiles() -> list[dict[str, Any]]:
    """Return all known vendor profiles as simple dicts."""
    return [p.to_dict() for p in get_vendor_profiles().values()]


def get_fused_eik_prefixes() -> set[str]:
    """Return all prefixes of fused EIKs from loaded profiles."""
    prefixes: set[str] = set()
    for p in get_vendor_profiles().values():
        for prefix in p.fused_eik_prefixes:
            prefixes.add(prefix)
    return prefixes or {"121644736", "121644734"}


def get_dot_matrix_eiks() -> set[str]:
    """Return all EIKs matching dot-matrix profile."""
    eiks: set[str] = set()
    for p in get_vendor_profiles().values():
        if p.dot_matrix or (p.ocr and p.ocr.get("dot_matrix")):
            eiks.add(p.eik)
            for alt in p.alternate_eiks:
                eiks.add(alt)
    return eiks or {"114609507", "114609407"}


def is_dot_matrix_vendor(text: str) -> bool:
    """Check if the provided text matches any dot-matrix supplier profile."""
    if not text:
        return False
    txt_low = text.lower()
    for p in get_vendor_profiles().values():
        if p.dot_matrix or (p.ocr and p.ocr.get("dot_matrix")):
            if p.eik in text or any(alt in text for alt in p.alternate_eiks):
                return True
            if any(k in txt_low for k in p.keywords):
                return True
    return False


def get_recapitulation_eiks() -> set[str]:
    """Return all supplier EIKs that use recapitulation tables / customer boxes."""
    eiks: set[str] = set()
    for p in get_vendor_profiles().values():
        if p.layout and (p.layout.get("recapitulation_table") or p.layout.get("customer_box")):
            eiks.add(p.eik)
            for alt in p.alternate_eiks:
                eiks.add(alt)
    return eiks or {"121644736", "121644734"}


def get_protected_supplier_eiks() -> set[str]:
    """Return all supplier EIKs that should never be accidentally inferred as recipient."""
    eiks: set[str] = set()
    for p in get_vendor_profiles().values():
        eiks.add(p.eik)
        for alt in p.alternate_eiks:
            eiks.add(alt)
    return eiks


KNOWN_SUPPLIER_PROFILES = get_known_supplier_profiles()
