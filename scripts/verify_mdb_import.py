#!/usr/bin/env python3
"""CLI Utility: Microinvest Delta Pro Jet 2.0 MDB Database & TRANSFER.LOG Verifier.

Usage:
  # Verify live Delta Pro MDB database:
  python scripts/verify_mdb_import.py --mdb /path/to/fasttop.MDB

  # Verify generated TRANSFER.LOG:
  python scripts/verify_mdb_import.py --transfer-log Building_11/TRANSFER.LOG

  # Compare TRANSFER.LOG with MDB:
  python scripts/verify_mdb_import.py --mdb fasttop.MDB --transfer-log TRANSFER.LOG --compare
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

# Ensure repository root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from invoice_core.accounting.exporters.mdb_verifier import Jet2MdbVerifier


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Microinvest Delta Pro Jet 2.0 MDB databases and TRANSFER.LOG files."
    )
    parser.add_argument("--mdb", type=str, help="Path to Jet 2.0 MDB database (e.g. fasttop.MDB)")
    parser.add_argument("--transfer-log", type=str, help="Path to TRANSFER.LOG file")
    parser.add_argument("--compare", action="store_true", help="Compare TRANSFER.LOG against MDB database")
    parser.add_argument("--json", action="store_true", help="Output results in JSON format")

    args = parser.parse_args()

    if not args.mdb and not args.transfer_log:
        # Default search paths
        candidate_mdbs = [
            Path("/Users/diokarabaz/teamwork_projects/microinvest_vm_validation/wine_prefix/drive_c/Program Files/Microinvest/Delta Pro/fasttop.MDB"),
            Path("/Users/diokarabaz/MICROINVEST-OCR/resources/01-Schetovodstvo-FastTop/fasttop.MDB"),
            Path("/Users/diokarabaz/MICROINVEST-OCR/microinvest/philips-ssd/01-Schetovodstvo-FastTop/fasttop.MDB"),
        ]
        for c in candidate_mdbs:
            if c.exists():
                args.mdb = str(c)
                break

        candidate_logs = [
            Path("Building_11/TRANSFER.LOG"),
            Path("comparison_export/TRANSFER.LOG"),
        ]
        for cl in candidate_logs:
            if cl.exists():
                args.transfer_log = str(cl)
                break

    output_data: dict[str, Any] = {"status": "ok", "checks": {}}

    # 1. Verify MDB if provided
    if args.mdb:
        mdb_path = Path(args.mdb)
        if not mdb_path.exists():
            print(f"Error: MDB file not found: {args.mdb}", file=sys.stderr)
            return 1

        v = Jet2MdbVerifier(mdb_path)
        tables = list(v.tdefs.keys())
        accounts = v.get_accounts()
        bal = v.verify_balance()

        output_data["checks"]["mdb"] = {
            "path": str(mdb_path.resolve()),
            "size_bytes": len(v.data),
            "total_pages": v.total_pages,
            "table_count": len(tables),
            "accounts_count": len(accounts),
            "balance": bal,
        }

        if not args.json:
            print("==================================================================")
            print(f"JET 2.0 MDB DATABASE INSPECTION: {mdb_path.name}")
            print("==================================================================")
            print(f"File: {mdb_path.resolve()}")
            print(f"Size: {len(v.data):,} bytes ({v.total_pages} pages @ 2048 bytes)")
            print(f"Tables defined: {len(tables)}")
            print(f"Chart of Accounts (T_SMETKI / Accts): {len(accounts)} accounts registered")
            print(f"Operations (T_OPERACII): {bal['total_entries']} journal entries")
            print(f"  - Total Debit : {bal['total_debit']:>12.2f} BGN")
            print(f"  - Total Credit: {bal['total_credit']:>12.2f} BGN")
            print(f"  - Difference  : {bal['difference']:>12.2f} BGN")
            print(f"  - Balanced    : {'✓ 100% BALANCED' if bal['is_balanced'] else '✗ UNBALANCED'}")
            print("==================================================================")

    # 2. Verify TRANSFER.LOG if provided
    if args.transfer_log:
        log_path = Path(args.transfer_log)
        if not log_path.exists():
            print(f"Error: TRANSFER.LOG file not found: {args.transfer_log}", file=sys.stderr)
            return 1

        vl = Jet2MdbVerifier(log_path)
        log_bal = vl.verify_balance(table_name_or_id=25)
        output_data["checks"]["transfer_log"] = {
            "path": str(log_path.resolve()),
            "size_bytes": len(vl.data),
            "total_pages": vl.total_pages,
            "balance": log_bal,
        }

        if not args.json:
            print("==================================================================")
            print(f"MICROINVEST DELTA PRO TRANSFER.LOG VERIFICATION: {log_path.name}")
            print("==================================================================")
            print(f"File: {log_path.resolve()}")
            print(f"Size: {len(vl.data):,} bytes ({vl.total_pages} pages @ 2048 bytes)")
            print(f"W#Transfer Entries: {log_bal['total_entries']} journal postings")
            print(f"  - Total Debit : {log_bal['total_debit']:>12.2f}")
            print(f"  - Total Credit: {log_bal['total_credit']:>12.2f}")
            print(f"  - Difference  : {log_bal['difference']:>12.2f}")
            print(f"  - Balanced    : {'✓ 100% BALANCED' if log_bal['is_balanced'] else '✗ UNBALANCED'}")
            print("==================================================================")

    # 3. Compare if requested
    if args.compare and args.mdb and args.transfer_log:
        v_mdb = Jet2MdbVerifier(args.mdb)
        comp = v_mdb.compare_with_transfer_log(args.transfer_log)
        output_data["checks"]["comparison"] = comp

        if not args.json:
            print("\n==================================================================")
            print("CROSS-DATABASE IMPORT VERIFICATION & COMPATIBILITY CHECK")
            print("==================================================================")
            print(f"Can Import Cleanly: {'✓ YES' if comp['compatibility']['can_import'] else '✗ NO'}")
            print(f"Accounts Matched  : {comp['compatibility']['accounts_matched']}")
            if comp['compatibility']['accounts_unmatched']:
                print(f"Accounts Unmatched: {comp['compatibility']['accounts_unmatched']}")
            print("==================================================================")

    if args.json:
        print(json.dumps(output_data, indent=2, ensure_ascii=False))

    # Exit code
    all_balanced = True
    if "mdb" in output_data["checks"] and not output_data["checks"]["mdb"]["balance"]["is_balanced"]:
        all_balanced = False
    if "transfer_log" in output_data["checks"] and not output_data["checks"]["transfer_log"]["balance"]["is_balanced"]:
        all_balanced = False

    return 0 if all_balanced else 1


if __name__ == "__main__":
    sys.exit(main())
