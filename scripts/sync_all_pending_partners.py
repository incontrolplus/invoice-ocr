#!/usr/bin/env python3
"""
Batch sync runner for accounting.partners in Supabase.
Finds all partners where companybook_id IS NULL and calls the n8n
'companybook-sync-partner' workflow with rate-limit pacing (1.8s delay)
and exponential backoff on HTTP 429.
"""

import json
import time
import sys
import subprocess
import urllib.request
import urllib.error

SSH_TARGET = "diokarabaz@10.0.25.205"
WEBHOOK_URL = "http://10.0.25.205:5679/webhook/companybook-sync-partner"
PACE_SECONDS = 1.8  # Safe throttle to avoid Cloudflare/CompanyBook rate limits


def get_pending_partners():
    """Query PostgreSQL on macmini-primary for partners needing sync."""
    query = """
    SELECT json_agg(row_to_json(t)) FROM (
        SELECT eik, country_code, vat_number, legal_name 
        FROM accounting.partners 
        WHERE companybook_id IS NULL OR companybook_id = ''
        ORDER BY eik
    ) t;
    """
    cmd = [
        "ssh", "-o", "ConnectTimeout=5", SSH_TARGET,
        f"docker exec -i supabase-db psql -U postgres -d postgres -t -A -c \"{query}\""
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print("Error querying database:", res.stderr)
        sys.exit(1)
    
    out = res.stdout.strip()
    if not out or out == "null":
        return []
    return json.loads(out)


def call_sync_webhook(eik: str, max_retries: int = 3):
    """Invoke n8n workflow with retry logic for 429."""
    url = f"{WEBHOOK_URL}?eik={eik}"
    req = urllib.request.Request(url, headers={"User-Agent": "BatchPartnerSync/1.0"})
    
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                status = resp.status
                body = json.loads(resp.read().decode("utf-8"))
                return status, body
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = {"error": "HTTP_ERROR", "message": str(e)}
            
            if status == 429 and attempt < max_retries - 1:
                backoff = 10 * (attempt + 1)
                print(f"      [429 Rate Limit] Backing off for {backoff}s...")
                time.sleep(backoff)
                continue
            return status, body
        except Exception as e:
            return 500, {"error": "CONNECTION_ERROR", "message": str(e)}
            
    return 500, {"error": "MAX_RETRIES_EXCEEDED"}


def main():
    print("=" * 70)
    print("Starting CompanyBook Partner Batch Validation & Sync via n8n")
    print("=" * 70)
    
    partners = get_pending_partners()
    total = len(partners)
    print(f"Found {total} partners pending CompanyBook validation in accounting.partners.")
    
    if total == 0:
        print("No pending partners to sync. Database is 100% up to date.")
        return

    results = {
        "success": [],
        "not_found": [],
        "invalid_format": [],
        "failed": []
    }
    
    start_time = time.time()
    
    for idx, p in enumerate(partners, 1):
        eik = p.get("eik")
        country = p.get("country_code", "BG")
        name = p.get("legal_name", "")
        
        print(f"[{idx:02d}/{total:02d}] EIK: {eik:<12} ({country}) - {name[:30]:<30}", end=" ... ", flush=True)
        
        status, resp = call_sync_webhook(eik)
        
        if status == 200 and resp.get("success"):
            partner_data = resp.get("partner", {})
            cb_id = partner_data.get("companybook_id")
            updated_name = partner_data.get("legal_name")
            print(f"OK (200) -> ID: {cb_id} | Name: {updated_name}")
            results["success"].append({
                "eik": eik,
                "companybook_id": cb_id,
                "legal_name": updated_name,
                "address": partner_data.get("address"),
                "vat_status": partner_data.get("vat_status"),
                "capital": f"{partner_data.get('capital_amount')} {partner_data.get('capital_currency')}"
            })
        elif status == 404:
            msg = resp.get("message", "Not found in CompanyBook")
            print(f"NOT FOUND (404) -> {msg}")
            results["not_found"].append({"eik": eik, "legal_name": name, "reason": msg})
        elif status == 400:
            msg = resp.get("message", "Invalid EIK format")
            print(f"INVALID (400) -> {msg}")
            results["invalid_format"].append({"eik": eik, "legal_name": name, "reason": msg})
        else:
            err = resp.get("error", "Unknown error")
            print(f"FAILED ({status}) -> {err}")
            results["failed"].append({"eik": eik, "status": status, "error": err})
            
        # Pacing to protect rate limits
        time.sleep(PACE_SECONDS)
        
    elapsed = time.time() - start_time
    print("=" * 70)
    print("BATCH SYNC SUMMARY")
    print("=" * 70)
    print(f"Total Processed   : {total}")
    print(f"Successfully Synced: {len(results['success'])}")
    print(f"Not Found (404)   : {len(results['not_found'])}")
    print(f"Invalid Format (400): {len(results['invalid_format'])}")
    print(f"Failed / Errors   : {len(results['failed'])}")
    print(f"Total Time Taken  : {elapsed:.2f} seconds ({elapsed/60:.2f} min)")
    print("=" * 70)
    
    # Write audit log to file
    with open("/tmp/companybook_sync_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("Audit log saved to /tmp/companybook_sync_results.json")


if __name__ == "__main__":
    main()
