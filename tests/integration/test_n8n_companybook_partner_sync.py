"""
Unit and integration tests for the n8n CompanyBook Partner Sync workflow.
Verifies:
1. Normalization of EIK (with and without 'BG' prefix)
2. Handling of invalid UIC/EIK formats (HTTP 400)
3. Handling of non-existent UIC/EIK queries (HTTP 404)
4. Successful end-to-end sync to Supabase accounting.partners via GET and POST
"""

import json
import urllib.request
import urllib.error
import pytest

WEBHOOK_URL = "http://100.83.83.8:5679/webhook/companybook-sync-partner"
PUBLIC_WEBHOOK_URL = "https://n8n.openbalancer.com/webhook/companybook-sync-partner"


def _http_get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Pytest-Runner"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _http_post_json(url: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "Pytest-Runner", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_webhook_invalid_eik_returns_400():
    """Verify that malformed or non-digit EIK returns 400 Bad Request."""
    status, res = _http_get(f"{WEBHOOK_URL}?eik=BAD_EIK_123")
    assert status == 400
    assert res.get("success") is False
    assert res.get("error") == "INVALID_EIK_FORMAT"


def test_webhook_non_existent_eik_returns_404():
    """Verify that a syntactically valid but non-existent EIK returns 404."""
    status, res = _http_get(f"{WEBHOOK_URL}?eik=000000000")
    assert status == 404
    assert res.get("success") is False
    assert res.get("error") == "COMPANY_NOT_FOUND"


def test_webhook_sync_with_bg_prefix_get():
    """Verify GET request with BG prefix successfully syncs to accounting.partners."""
    status, res = _http_get(f"{WEBHOOK_URL}?eik=BG203818240")
    assert status == 200
    assert res.get("success") is True
    partner = res.get("partner")
    assert partner is not None
    assert partner.get("eik") == "203818240"
    assert partner.get("vat_number") == "BG203818240"
    assert "ДЖЕНТЪЛМЕН" in partner.get("legal_name", "")
    assert partner.get("verified_source") == "COMPANYBOOK_API"
    assert partner.get("is_verified") is True


def test_webhook_sync_without_bg_prefix_post():
    """Verify POST request with JSON body successfully syncs to accounting.partners."""
    status, res = _http_post_json(WEBHOOK_URL, {"eik": "203818240"})
    assert status == 200
    assert res.get("success") is True
    partner = res.get("partner")
    assert partner is not None
    assert partner.get("eik") == "203818240"
    assert partner.get("legal_status") == "ACTIVE"
