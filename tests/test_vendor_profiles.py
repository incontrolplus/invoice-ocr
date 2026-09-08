"""Tests for externalized vendor profiles (YAML loaders, fallbacks, Pydantic schema, dynamic hot-reload, and REST API)."""
import sys
import threading
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tempfile
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from invoice_core.vendor_profiles import (
    BUILTIN_DEFAULTS,
    DEFAULT_BANKING_PROFILES,
    VendorProfile,
    VendorProfileLoader,
    get_known_supplier_profiles,
    get_vendor_profile,
    get_vendor_profile_loader,
    get_vendor_profiles,
    list_known_profiles,
    load_yaml_file,
    reset_vendor_profiles_cache,
)
from invoice_ocr import KNOWN_SUPPLIER_PROFILES
from api_server import app


# ===========================================================================
# 1. Base Loader and Built-in Fallbacks Tests
# ===========================================================================

def test_get_vendor_profiles_contains_defaults():
    """Verify built-in default profiles (Metro, Detelina, Toplivo, OMV, Shell) are loaded."""
    reset_vendor_profiles_cache()
    profiles = get_vendor_profiles()
    assert "metro" in profiles
    assert "detelina" in profiles
    assert "toplivo" in profiles
    assert "omv" in profiles
    assert "shell" in profiles

    metro = profiles["metro"]
    assert metro.eik == "121644736"
    assert "121644734" in metro.alternate_eiks
    assert metro.layout.get("customer_box") is True
    assert "артикул" in metro.table_anchors

    detelina = profiles["detelina"]
    assert detelina.eik == "114609507"
    assert detelina.dot_matrix is True
    assert detelina.ocr.get("dot_matrix") is True

    toplivo = profiles["toplivo"]
    assert toplivo.eik == "130864186"


def test_get_known_supplier_profiles():
    """Verify banking profiles loaded from YAML match expectations."""
    reset_vendor_profiles_cache()
    profiles = get_known_supplier_profiles()
    assert len(profiles) >= len(DEFAULT_BANKING_PROFILES)

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


# ===========================================================================
# 2. Strict Pydantic Schema Validation Tests
# ===========================================================================

def test_vendor_profile_pydantic_valid():
    """Verify valid creation of a VendorProfile model with all required and optional fields."""
    vp = VendorProfile(
        id="lukoil",
        name="ЛУКОЙЛ БЪЛГАРИЯ ЕООД",
        eik="130533884",
        vat="BG130533884",
        keywords=["лукойл", "lukoil"],
        table_anchors=["гориво", "количество", "ед. цена", "стойност"],
        column_offsets={"fuel": [0.0, 0.4], "amount": [0.4, 1.0]},
        fused_patterns=["130533884"],
        dot_matrix=False,
    )
    assert vp.id == "lukoil"
    assert vp.name == "ЛУКОЙЛ БЪЛГАРИЯ ЕООД"
    assert vp.eik == "130533884"
    assert vp.vat == "BG130533884"
    assert vp.vat_number == "BG130533884"
    assert "лукойл" in vp.keywords
    assert "гориво" in vp.table_anchors
    assert vp.column_offsets["fuel"] == [0.0, 0.4]
    assert "130533884" in vp.fused_patterns
    assert "130533884" in vp.fused_eik_prefixes
    assert vp.dot_matrix is False


def test_vendor_profile_eik_validation():
    """Verify that EIK validation rejects invalid formats and digit lengths."""
    # Too short
    with pytest.raises(ValidationError):
        VendorProfile(id="bad1", name="Bad", eik="12345")

    # Non-digits only / letters
    with pytest.raises(ValidationError):
        VendorProfile(id="bad2", name="Bad", eik="abcdefghij")

    # Clean stripping of spaces and BG prefix if in EIK
    vp = VendorProfile(id="valid_spaces", name="Good", eik=" 121644736 ")
    assert vp.eik == "121644736"

    # 13-digit EIK
    vp13 = VendorProfile(id="valid_13", name="Good 13", eik="1234567890123")
    assert vp13.eik == "1234567890123"


def test_vendor_profile_bidirectional_reconciliations():
    """Verify bidirectional syncing between top-level fields and nested structures."""
    # Automatic VAT generation when missing
    vp1 = VendorProfile(id="test_vat", name="Vat Test", eik="123456789")
    assert vp1.vat == "BG123456789"
    assert vp1.vat_number == "BG123456789"

    # dot_matrix syncing with ocr["dot_matrix"]
    vp2 = VendorProfile(id="test_dm", name="DM Test", eik="123456789", dot_matrix=True)
    assert vp2.dot_matrix is True
    assert vp2.ocr.get("dot_matrix") is True

    vp3 = VendorProfile(id="test_ocr_dm", name="OCR DM Test", eik="123456789", ocr={"dot_matrix": True})
    assert vp3.dot_matrix is True

    # table_anchors and column_offsets syncing with layout
    vp4 = VendorProfile(
        id="test_layout",
        name="Layout Test",
        eik="123456789",
        table_anchors=["сума", "ддс"],
        column_offsets={"total": [0.8, 1.0]},
    )
    assert vp4.layout.get("table_anchors") == ["сума", "ддс"]
    assert vp4.layout.get("column_offsets") == {"total": [0.8, 1.0]}


def test_vendor_profile_dict_and_subscript_access():
    """Verify to_dict(), item subscripting, and get() for backward compatibility."""
    vp = VendorProfile(id="sub_test", name="Subscript Test", eik="123456789")
    assert vp["id"] == "sub_test"
    assert vp["eik"] == "123456789"
    assert vp.get("name") == "Subscript Test"
    assert vp.get("nonexistent", "default") == "default"

    d = vp.to_dict()
    assert isinstance(d, dict)
    assert d["id"] == "sub_test"
    assert d["eik"] == "123456789"
    assert "keywords" in d
    assert "table_anchors" in d
    assert "column_offsets" in d


# ===========================================================================
# 3. Base YAML Profiles in config/vendors/
# ===========================================================================

def test_config_vendors_base_profiles():
    """Verify that config/vendors/ YAML profiles (Metro, Toplivo, Detelina, OMV, Shell) exist and load cleanly."""
    reset_vendor_profiles_cache()
    profiles = get_vendor_profiles()

    # OMV Petrol Station
    assert "omv" in profiles
    omv = profiles["omv"]
    assert omv.eik == "831101035"
    assert omv.vat == "BG831101035"
    assert any("omv" in k for k in omv.keywords)
    assert "продукт" in omv.table_anchors
    assert "description" in omv.column_offsets
    assert len(omv.banking) >= 1
    assert omv.banking[0]["iban"] == "BG10STSA93000027446545"

    # Shell Petrol Station
    assert "shell" in profiles
    shell = profiles["shell"]
    assert shell.eik == "831299905"
    assert shell.vat == "BG831299905"
    assert any("shell" in k for k in shell.keywords)
    assert "описание" in shell.table_anchors
    assert len(shell.banking) >= 1
    assert shell.banking[0]["iban"] == "BG39UNCR70001500876294"

    # Metro Cash & Carry
    assert "metro" in profiles
    metro = profiles["metro"]
    assert metro.eik == "121644736"
    assert metro.layout.get("customer_box") is True
    assert metro.layout.get("recapitulation_table") is True
    assert "артикул" in metro.table_anchors

    # Detelina-DP
    assert "detelina" in profiles
    detelina = profiles["detelina"]
    assert detelina.eik == "114609507"
    assert detelina.dot_matrix is True

    # Toplivo Gas
    assert "toplivo" in profiles
    toplivo = profiles["toplivo"]
    assert toplivo.eik == "130864186"


# ===========================================================================
# 4. Fail-Safe with Fallbacks on Broken / Invalid YAML
# ===========================================================================

def test_fail_safe_on_broken_yaml_syntax(tmp_path):
    """Verify that a syntax-broken YAML file is safely caught and does not crash the loader."""
    broken_file = tmp_path / "broken_vendor.yaml"
    broken_file.write_text("id: broken\nname: 'Broken\n  unclosed quote: [", encoding="utf-8")

    loader = VendorProfileLoader(config_dirs=[tmp_path], auto_reload=False)
    diag = loader.get_diagnostics()

    # Diagnostics should log the syntax error
    assert any(err.get("type") == "yaml_syntax_error" for err in diag["errors"])
    # Loader should still provide built-in defaults (Metro, Toplivo, etc.)
    profiles = loader.get_profiles()
    assert "metro" in profiles
    assert "toplivo" in profiles


def test_fail_safe_on_schema_validation_error(tmp_path):
    """Verify that YAML failing Pydantic schema validation is safely skipped and does not crash."""
    invalid_file = tmp_path / "invalid_schema.yaml"
    # EIK is invalid (only 3 digits)
    invalid_file.write_text("id: invalid_eik\nname: 'Bad EIK EOOD'\neik: '123'\n", encoding="utf-8")

    loader = VendorProfileLoader(config_dirs=[tmp_path], auto_reload=False)
    diag = loader.get_diagnostics()

    assert any(err.get("type") == "validation_error" for err in diag["errors"])
    profiles = loader.get_profiles()
    assert "invalid_eik" not in profiles
    assert "metro" in profiles


def test_fail_safe_on_non_dict_yaml(tmp_path):
    """Verify that YAML files with non-dict root (e.g. lists or strings) are handled safely."""
    list_file = tmp_path / "list_vendor.yaml"
    list_file.write_text("- item1\n- item2\n", encoding="utf-8")

    loader = VendorProfileLoader(config_dirs=[tmp_path], auto_reload=False)
    diag = loader.get_diagnostics()

    assert any(err.get("type") == "invalid_structure" for err in diag["errors"])
    profiles = loader.get_profiles()
    assert "metro" in profiles


def test_fail_safe_retains_previous_valid_version_on_bad_edit(tmp_path):
    """Verify that if an existing valid vendor profile file is edited to broken YAML, previous valid state is retained."""
    vendor_file = tmp_path / "supplier_alpha.yaml"
    vendor_file.write_text(
        "id: alpha\nname: 'Alpha OOD'\neik: '123456789'\nkeywords: ['alpha']\n",
        encoding="utf-8",
    )

    loader = VendorProfileLoader(config_dirs=[tmp_path], auto_reload=False)
    assert "alpha" in loader.get_profiles()
    assert loader.get_profiles()["alpha"].name == "Alpha OOD"

    # Now simulate a bad edit by an accountant / admin (broken syntax)
    time.sleep(0.01)
    vendor_file.write_text("id: alpha\nname: {broken syntax: [", encoding="utf-8")
    loader.reload()

    # Previous valid version is retained!
    assert "alpha" in loader.get_profiles()
    assert loader.get_profiles()["alpha"].name == "Alpha OOD"


# ===========================================================================
# 5. Dynamic Hot-Reloading Without Restart Tests
# ===========================================================================

def test_dynamic_hot_reload_on_file_addition(tmp_path):
    """Verify that adding a new YAML file dynamically is detected on next get_profiles() without server restart."""
    loader = VendorProfileLoader(config_dirs=[tmp_path], auto_reload=True, check_interval=0.0)
    assert "custom_co" not in loader.get_profiles()

    # Create new file
    time.sleep(0.01)
    new_file = tmp_path / "custom_co.yaml"
    new_file.write_text(
        "id: custom_co\nname: 'Къстъм Къмпани ООД'\neik: '987654321'\nkeywords: ['custom']\n",
        encoding="utf-8",
    )

    # Next call should automatically discover it
    profiles = loader.get_profiles()
    assert "custom_co" in profiles
    assert profiles["custom_co"].eik == "987654321"
    assert profiles["custom_co"].name == "Къстъм Къмпани ООД"


def test_dynamic_hot_reload_on_file_modification(tmp_path):
    """Verify that editing an existing YAML file is automatically picked up."""
    v_file = tmp_path / "mod_vendor.yaml"
    v_file.write_text(
        "id: mod_vendor\nname: 'Version 1'\neik: '111222333'\n",
        encoding="utf-8",
    )

    loader = VendorProfileLoader(config_dirs=[tmp_path], auto_reload=True, check_interval=0.0)
    assert loader.get_profiles()["mod_vendor"].name == "Version 1"

    # Edit file
    time.sleep(0.01)
    v_file.write_text(
        "id: mod_vendor\nname: 'Version 2 Updated'\neik: '111222333'\n",
        encoding="utf-8",
    )

    # Automatically reflected
    assert loader.get_profiles()["mod_vendor"].name == "Version 2 Updated"


def test_dynamic_hot_reload_on_file_deletion(tmp_path):
    """Verify that deleting a YAML file removes the custom profile."""
    v_file = tmp_path / "temp_del.yaml"
    v_file.write_text(
        "id: temp_del\nname: 'Temporary'\neik: '555666777'\n",
        encoding="utf-8",
    )

    loader = VendorProfileLoader(config_dirs=[tmp_path], auto_reload=True, check_interval=0.0)
    assert "temp_del" in loader.get_profiles()

    time.sleep(0.01)
    v_file.unlink()

    # Profile should be gone
    assert "temp_del" not in loader.get_profiles()


def test_thread_safety_during_concurrent_access_and_reloads(tmp_path):
    """Verify thread safety when concurrent readers access get_profiles() while reload() runs."""
    v_file = tmp_path / "concurrent_co.yaml"
    v_file.write_text("id: conc\nname: 'Concurrent'\neik: '123456789'\n", encoding="utf-8")

    loader = VendorProfileLoader(config_dirs=[tmp_path], auto_reload=True, check_interval=0.0)
    stop_event = threading.Event()
    read_errors = []

    def reader_worker():
        while not stop_event.is_set():
            try:
                profs = loader.get_profiles()
                assert len(profs) >= 1
            except Exception as e:
                read_errors.append(e)

    threads = [threading.Thread(target=reader_worker) for _ in range(5)]
    for t in threads:
        t.start()

    # Trigger multiple reloads
    for i in range(10):
        v_file.write_text(f"id: conc\nname: 'Concurrent v{i}'\neik: '123456789'\n", encoding="utf-8")
        loader.reload()
        time.sleep(0.005)

    stop_event.set()
    for t in threads:
        t.join()

    assert not read_errors


# ===========================================================================
# 6. REST API Endpoints Tests (/api/v1/vendors & /api/v1/vendors/reload)
# ===========================================================================

@pytest.fixture
def client():
    return TestClient(app)


def test_api_get_vendors_list(client):
    """Verify GET /api/v1/vendors returns all active vendor profiles."""
    response = client.get("/api/v1/vendors")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["total"] >= 5

    vendor_ids = [v["id"] for v in data["vendors"]]
    assert "metro" in vendor_ids
    assert "toplivo" in vendor_ids
    assert "detelina" in vendor_ids
    assert "omv" in vendor_ids
    assert "shell" in vendor_ids

    # Verify structured fields in list
    metro = next(v for v in data["vendors"] if v["id"] == "metro")
    assert metro["eik"] == "121644736"
    assert "артикул" in metro["table_anchors"]
    assert "customer_box" in metro["layout"]


def test_api_get_vendor_by_id(client):
    """Verify GET /api/v1/vendors/{vendor_id} returns details for existing vendors."""
    # By ID
    res = client.get("/api/v1/vendors/omv")
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["vendor"]["id"] == "omv"
    assert data["vendor"]["eik"] == "831101035"
    assert data["vendor"]["vat_number"] == "BG831101035"
    assert len(data["vendor"]["banking"]) >= 1

    # By EIK
    res_eik = client.get("/api/v1/vendors/831299905")
    assert res_eik.status_code == 200
    data_eik = res_eik.json()
    assert data_eik["vendor"]["id"] == "shell"


def test_api_get_vendor_not_found(client):
    """Verify GET /api/v1/vendors/{vendor_id} returns 404 for unknown vendor."""
    res = client.get("/api/v1/vendors/unknown_nonexistent_vendor_999")
    assert res.status_code == 404
    assert "not found" in res.json()["detail"].lower()


def test_api_post_vendors_reload(client):
    """Verify POST /api/v1/vendors/reload dynamically refreshes profiles without restart."""
    res = client.post("/api/v1/vendors/reload")
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["reloaded_count"] >= 5
    assert "metro" in data["vendors"]
    assert "omv" in data["vendors"]
    assert "shell" in data["vendors"]
    assert "timestamp" in data
