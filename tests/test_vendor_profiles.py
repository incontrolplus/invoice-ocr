"""Tests for externalized vendor profiles (YAML loaders, fallbacks, and banking profiles)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tempfile
import pytest

from invoice_core.vendor_profiles import (
    DEFAULT_BANKING_PROFILES,
    VendorProfile,
    get_known_supplier_profiles,
    get_vendor_profiles,
    load_yaml_file,
    reset_vendor_profiles_cache,
)
from invoice_ocr import KNOWN_SUPPLIER_PROFILES


def test_get_vendor_profiles_contains_defaults():
    """Verify built-in default profiles (Metro, Detelina, Toplivo) are loaded."""
    reset_vendor_profiles_cache()
    profiles = get_vendor_profiles()
    assert "metro" in profiles
    assert "detelina" in profiles
    assert "toplivo" in profiles

    metro = profiles["metro"]
    assert metro.eik == "121644736"
    assert "121644734" in metro.alternate_eiks
    assert metro.layout.get("customer_box") is True

    detelina = profiles["detelina"]
    assert detelina.eik == "114609507"
    assert detelina.ocr.get("dot_matrix") is True

    toplivo = profiles["toplivo"]
    assert toplivo.eik == "130864186"


def test_get_known_supplier_profiles():
    """Verify banking profiles loaded from YAML match expectations."""
    reset_vendor_profiles_cache()
    profiles = get_known_supplier_profiles()
    assert len(profiles) >= len(DEFAULT_BANKING_PROFILES)

    # Verify tuple structure: (keyword, iban, bic, bank_name)
    for entry in profiles:
        assert len(entry) == 4
        kw, iban, bic, bank = entry
        assert isinstance(kw, str) and kw
        assert iban.startswith("BG")
        assert len(iban) == 22


def test_reexport_in_invoice_ocr():
    """Verify that KNOWN_SUPPLIER_PROFILES is exported in top-level invoice_ocr facade."""
    assert KNOWN_SUPPLIER_PROFILES is not None
    assert any("капина" == p[0] for p in KNOWN_SUPPLIER_PROFILES)


def test_load_yaml_file_nonexistent():
    """Verify safe handling of nonexistent YAML paths."""
    res = load_yaml_file(Path("/nonexistent/path/to/profile.yaml"))
    assert res is None


def test_load_yaml_file_custom_temp():
    """Verify parsing of valid temporary YAML file."""
    with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", encoding="utf-8") as f:
        f.write("id: test_vendor\nname: 'Тест ООД'\neik: '999999999'\n")
        f.flush()
        data = load_yaml_file(Path(f.name))
        assert data is not None
        assert data["id"] == "test_vendor"
        assert data["eik"] == "999999999"
