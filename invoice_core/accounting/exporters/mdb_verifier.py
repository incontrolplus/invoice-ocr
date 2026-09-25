"""Microinvest Delta Pro Jet 2.0 / Access 2.0 MDB Database Inspector & Verifier.

Parses native 2048-byte Jet 2.0 binary database pages (.MDB and .LOG files) directly
in pure Python without relying on external native binaries (such as mdbtools which
do not support Jet 2.0).

Supports:
1. Inspecting tables:
   - T_OPERACII / Operations / W#Transfer (TableID 545, TableID 25)
   - T_DOKUMENTI / Korespondent / W#System (TableID 481, TableID 23)
   - T_SMETKI / Accts / National (TableID 184)
2. Extracting double-entry journal entries (контировки) with analytical details.
3. 100% balance equality verification: Sum(Debit) == Sum(Credit) (difference < 0.01).
4. Comparing generated TRANSFER.LOG with MDB database schema and past entries.
"""
from __future__ import annotations

from collections import defaultdict
import datetime
from decimal import Decimal
import logging
from pathlib import Path
import re
import struct
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("mdb_verifier")

PAGE_SIZE = 2048
BASE_OLE_DATE = datetime.datetime(1899, 12, 30)


def ole_date_to_datetime(ole_val: float) -> Optional[datetime.datetime]:
    """Convert an OLE automation floating-point date into a Python datetime."""
    try:
        if ole_val <= 0 or ole_val > 100000:
            return None
        return BASE_OLE_DATE + datetime.timedelta(days=ole_val)
    except Exception:
        return None


def ole_date_to_str(ole_val: float) -> str:
    """Format OLE date as DD.MM.YYYY."""
    dt = ole_date_to_datetime(ole_val)
    if dt:
        return dt.strftime("%d.%m.%Y")
    return ""


class Jet2MdbVerifier:
    """Pure-Python Jet 2.0 / Access 2.0 MDB & TRANSFER.LOG database verifier."""

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = Path(db_path)
        if not self.db_path.exists():
            raise FileNotFoundError(f"Database file not found: {self.db_path}")
        self.data = self.db_path.read_bytes()
        self.total_pages = len(self.data) // PAGE_SIZE
        if self.total_pages < 1:
            raise ValueError(f"File too small to be a valid Jet 2.0 database: {len(self.data)} bytes")

        self.tdefs: dict[int, dict[str, Any]] = {}
        self.data_pages_by_table: dict[int, list[int]] = defaultdict(list)
        self.table_name_to_id: dict[str, int] = {}
        self._scan_pages()

    def _scan_pages(self) -> None:
        """Scan all 2048-byte pages for Table Definitions (0x05) and Data Pages (0x06)."""
        for pg_num in range(self.total_pages):
            pg = self.data[pg_num * PAGE_SIZE : (pg_num + 1) * PAGE_SIZE]
            ptype = pg[0]
            if ptype == 0x05:
                # Table Definition
                tid = struct.unpack("<I", pg[4:8])[0]
                first_pg, last_pg = struct.unpack("<II", pg[12:20])
                num_pgs, num_recs = struct.unpack("<II", pg[32:40])
                self.tdefs[tid] = {
                    "tdef_page": pg_num,
                    "table_id": tid,
                    "first_page": first_pg,
                    "last_page": last_pg,
                    "num_pages": num_pgs,
                    "num_records": num_recs,
                }
            elif ptype == 0x06:
                # Data Page
                tid = struct.unpack("<I", pg[4:8])[0]
                self.data_pages_by_table[tid].append(pg_num)

        # Map canonical / synonymous table names
        # Live Microinvest Delta Pro database IDs:
        # Table 545: Operations / T_OPERACII
        # Table 184: Accts / National / T_SMETKI
        # Table 25:  W#Transfer (TRANSFER.LOG)
        # Table 23:  W#System (TRANSFER.LOG)
        # Table 481: Korespondent / T_DOKUMENTI
        if 545 in self.tdefs:
            self.table_name_to_id["T_OPERACII"] = 545
            self.table_name_to_id["OPERATIONS"] = 545
            self.table_name_to_id["Operations"] = 545
        if 25 in self.tdefs:
            self.table_name_to_id["W#TRANSFER"] = 25
            self.table_name_to_id["W#Transfer"] = 25
            self.table_name_to_id["T_OPERACII"] = 25  # In TRANSFER.LOG, W#Transfer is the operations table
            self.table_name_to_id["TRANSFER"] = 25
        if 184 in self.tdefs:
            self.table_name_to_id["T_SMETKI"] = 184
            self.table_name_to_id["ACCTS"] = 184
            self.table_name_to_id["Accts"] = 184
            self.table_name_to_id["NATIONAL"] = 184
            self.table_name_to_id["National"] = 184
        if 23 in self.tdefs:
            self.table_name_to_id["W#SYSTEM"] = 23
            self.table_name_to_id["W#System"] = 23
            self.table_name_to_id["T_DOKUMENTI"] = 23
        if 481 in self.tdefs:
            self.table_name_to_id["T_DOKUMENTI"] = 481
            self.table_name_to_id["KORESPONDENT"] = 481
            self.table_name_to_id["Korespondent"] = 481

    def resolve_table_id(self, table_name_or_id: Union[str, int]) -> int:
        """Resolve a table name or ID to an existing TableID in this MDB."""
        if isinstance(table_name_or_id, int):
            return table_name_or_id
        clean = table_name_or_id.strip()
        if clean.isdigit():
            return int(clean)
        if clean in self.table_name_to_id:
            return self.table_name_to_id[clean]
        upper = clean.upper()
        if upper in self.table_name_to_id:
            return self.table_name_to_id[upper]
        # Fallback search
        for name, tid in self.table_name_to_id.items():
            if upper in name.upper():
                return tid
        # Defaults based on availability
        if upper in ("T_OPERACII", "OPERATIONS", "OPERACII"):
            if 25 in self.tdefs:
                return 25
            if 545 in self.tdefs:
                return 545
        if upper in ("T_SMETKI", "SMETKI", "ACCTS"):
            if 184 in self.tdefs:
                return 184
        if upper in ("T_DOKUMENTI", "DOKUMENTI"):
            if 23 in self.tdefs:
                return 23
            if 481 in self.tdefs:
                return 481
        raise KeyError(f"Table '{table_name_or_id}' not found in database {self.db_path.name}")

    def inspect_table(self, table_name_or_id: Union[str, int]) -> dict[str, Any]:
        """Inspect table metadata: TableID, declared records, data pages, and active records."""
        tid = self.resolve_table_id(table_name_or_id)
        tdef = self.tdefs.get(tid, {})
        data_pages = self.data_pages_by_table.get(tid, [])

        active_recs = 0
        deleted_slots = 0
        for pg_num in data_pages:
            pg = self.data[pg_num * PAGE_SIZE : (pg_num + 1) * PAGE_SIZE]
            cnt = struct.unpack("<H", pg[8:10])[0]
            for i in range(cnt):
                raw_ptr = struct.unpack("<H", pg[20 + i * 2 : 22 + i * 2])[0]
                if (raw_ptr & 0xF000) == 0x1000:
                    active_recs += 1
                elif raw_ptr != 0:
                    deleted_slots += 1

        return {
            "table_id": tid,
            "tdef_page": tdef.get("tdef_page"),
            "num_declared_records": tdef.get("num_records", 0),
            "num_active_records": active_recs,
            "num_deleted_slots": deleted_slots,
            "num_data_pages": len(data_pages),
            "first_data_page": tdef.get("first_page", 0),
            "last_data_page": tdef.get("last_page", 0),
        }

    def get_accounts(self) -> dict[str, str]:
        """Read chart of accounts (T_SMETKI / Accts / National, Table 184).
        
        Returns:
            Dictionary mapping account code (e.g. '101', '401', '601') -> account name.
        """
        accounts = {}
        tid = 184
        if tid not in self.tdefs and "T_SMETKI" not in self.table_name_to_id:
            return accounts

        data_pages = self.data_pages_by_table.get(tid, [])
        for pg_num in data_pages:
            pg = self.data[pg_num * PAGE_SIZE : (pg_num + 1) * PAGE_SIZE]
            cnt = struct.unpack("<H", pg[8:10])[0]
            for i in range(cnt):
                raw_ptr = struct.unpack("<H", pg[20 + i * 2 : 22 + i * 2])[0]
                if (raw_ptr & 0xF000) != 0x1000:
                    continue
                ptr = raw_ptr & 0x0FFF
                if ptr == 0 or ptr >= PAGE_SIZE:
                    continue
                rec = pg[ptr:]
                rlen = struct.unpack("<H", rec[:2])[0]
                if rlen < 10:
                    continue
                # Account code is at offset 6:8 as uint16
                code_int = struct.unpack("<H", rec[6:8])[0]
                # Account name text follows in CP1251
                name_bytes = rec[8:rlen]
                # Filter out control bytes
                clean_name = re.sub(rb"[\x00-\x1f\x7f-\x9f]", b"", name_bytes)
                name_str = clean_name.decode("cp1251", errors="replace").strip()
                if code_int > 0:
                    accounts[str(code_int)] = name_str

        return accounts

    def get_operations(self, table_name_or_id: Optional[Union[str, int]] = None) -> list[dict[str, Any]]:
        """Read accounting operations (контировки) from T_OPERACII (Table 25 or Table 545).
        
        Returns:
            List of operation records with kon_id, kon, is_kredit, acct, amount, date, doc info.
        """
        if table_name_or_id is None:
            # Default to W#Transfer (25) if present, else Operations (545)
            if 25 in self.tdefs:
                tid = 25
            elif 545 in self.tdefs:
                tid = 545
            else:
                tid = self.resolve_table_id("T_OPERACII")
        else:
            tid = self.resolve_table_id(table_name_or_id)

        operations = []
        data_pages = self.data_pages_by_table.get(tid, [])
        is_w_transfer = (tid == 25)

        for pg_num in data_pages:
            pg = self.data[pg_num * PAGE_SIZE : (pg_num + 1) * PAGE_SIZE]
            cnt = struct.unpack("<H", pg[8:10])[0]
            for i in range(cnt):
                raw_ptr = struct.unpack("<H", pg[20 + i * 2 : 22 + i * 2])[0]
                if (raw_ptr & 0xF000) != 0x1000:
                    continue
                ptr = raw_ptr & 0x0FFF
                if ptr == 0 or ptr >= PAGE_SIZE:
                    continue
                rec = pg[ptr:]
                rlen = struct.unpack("<H", rec[:2])[0]
                if rlen < 48:
                    continue

                try:
                    kon_id = struct.unpack("<I", rec[4:8])[0]
                    kon = struct.unpack("<I", rec[8:12])[0]
                    is_kredit = struct.unpack("<H", rec[12:14])[0]

                    if is_w_transfer:
                        # W#Transfer fixed layout:
                        # 14..16: acct
                        # 16..24: sub_acct (double)
                        # 24..32: priznak_id (double)
                        # 32..40: amount (double)
                        # 40..48: date_ole (double)
                        acct = struct.unpack("<H", rec[14:16])[0]
                        sub_acct = struct.unpack("<d", rec[16:24])[0]
                        amount = struct.unpack("<d", rec[32:40])[0]
                        date_ole = struct.unpack("<d", rec[40:48])[0]
                    else:
                        # Table 545 Operations layout:
                        # 22..24: acct
                        # 40..48: amount (double)
                        # 48..56: date_ole (double)
                        acct = struct.unpack("<H", rec[22:24])[0]
                        sub_acct = 0.0
                        amount = struct.unpack("<d", rec[40:48])[0]
                        date_ole = struct.unpack("<d", rec[48:56])[0]

                    # Extract variable text fields
                    var_chunk = rec[70:rlen] if len(rec) > 70 else b""
                    strs = [
                        m.group().decode("cp1251", errors="replace").strip()
                        for m in re.finditer(rb"[\x20-\x7e\xc0-\xff]{2,}", var_chunk)
                    ]

                    doc_num = ""
                    company = ""
                    bulstat = ""
                    for s in strs:
                        if re.match(r"^\d{10}$", s):
                            doc_num = s
                        elif re.match(r"^\d{9}$", s) or re.match(r"^\d{13}$", s):
                            bulstat = s
                        elif len(s) > 3 and not s.startswith("0") and not re.match(r"^\d", s):
                            if not company:
                                company = s

                    operations.append({
                        "kon_id": kon_id,
                        "kon": kon,
                        "is_kredit": is_kredit,  # 1 = Credit, 2 = Debit
                        "direction": "CR" if is_kredit == 1 else "DR",
                        "account": str(acct),
                        "subaccount": sub_acct,
                        "amount": amount,
                        "date_ole": date_ole,
                        "date_str": ole_date_to_str(date_ole),
                        "invoice_number": doc_num,
                        "company_name": company,
                        "bulstat": bulstat,
                        "raw_strings": strs,
                    })
                except Exception as ex:
                    logger.debug("Failed parsing record at page %d offset %d: %s", pg_num, ptr, ex)

        return operations

    def verify_balance(self, table_name_or_id: Optional[Union[str, int]] = None) -> dict[str, Any]:
        """Verify 100% mathematical double-entry balance equality (Debit == Credit).
        
        Calculates:
        - Total Debit
        - Total Credit
        - Transaction-level balance per (kon_id, kon)
        - Global balance equality
        """
        ops = self.get_operations(table_name_or_id)
        if not ops:
            return {
                "is_balanced": True,
                "total_debit": 0.0,
                "total_credit": 0.0,
                "difference": 0.0,
                "total_entries": 0,
                "balanced_entries_count": 0,
                "unbalanced_entries": [],
                "account_breakdown": {},
            }

        by_kon = defaultdict(lambda: {"dr": 0.0, "cr": 0.0, "ops": []})
        acct_breakdown: dict[str, dict[str, float]] = defaultdict(lambda: {"debit": 0.0, "credit": 0.0})

        for op in ops:
            key = (op["kon_id"], op["kon"])
            amt = op["amount"]
            acct = op["account"]

            if op["is_kredit"] == 2:
                # Debit
                by_kon[key]["dr"] += amt
                acct_breakdown[acct]["debit"] += abs(amt)
            elif op["is_kredit"] == 1:
                # Credit
                by_kon[key]["cr"] += amt
                acct_breakdown[acct]["credit"] += abs(amt)

            by_kon[key]["ops"].append(op)

        balanced_count = 0
        unbalanced = []
        total_dr = 0.0
        total_cr = 0.0

        for key, item in by_kon.items():
            dr = round(item["dr"], 2)
            cr = round(item["cr"], 2)
            total_dr += abs(dr)
            total_cr += abs(cr)

            # In Microinvest Delta Pro:
            # - For standard purchase postings: Debit is +X, Credit is -X, so dr + cr == 0
            # - For storno / credit notes: Debit is -X, Credit is +X, so dr + cr == 0
            # - Absolute sums: abs(dr) == abs(cr)
            if abs(dr + cr) < 0.01 or abs(abs(dr) - abs(cr)) < 0.01:
                balanced_count += 1
            else:
                unbalanced.append({
                    "kon_id": key[0],
                    "kon": key[1],
                    "debit": dr,
                    "credit": cr,
                    "difference": round(dr - cr, 2),
                    "operations": item["ops"],
                })

        diff = round(abs(total_dr - total_cr), 2)
        is_balanced = (len(unbalanced) == 0 and diff < 0.01)

        return {
            "is_balanced": is_balanced,
            "total_debit": round(total_dr, 2),
            "total_credit": round(total_cr, 2),
            "difference": diff,
            "total_entries": len(by_kon),
            "balanced_entries_count": balanced_count,
            "unbalanced_entries": unbalanced,
            "account_breakdown": {
                k: {
                    "debit": round(v["debit"], 2),
                    "credit": round(v["credit"], 2),
                    "balance": round(v["debit"] - v["credit"], 2),
                }
                for k, v in sorted(acct_breakdown.items())
            },
        }

    def compare_with_transfer_log(self, transfer_log_path: Union[str, Path]) -> dict[str, Any]:
        """Compare database schema and accounts against a generated TRANSFER.LOG."""
        log_verifier = Jet2MdbVerifier(transfer_log_path)
        log_balance = log_verifier.verify_balance(table_name_or_id=25)
        log_accounts = set(log_balance["account_breakdown"].keys())

        db_accounts = self.get_accounts()
        db_account_codes = set(db_accounts.keys())

        # Check account compatibility
        missing_in_db = [acc for acc in log_accounts if acc not in db_account_codes and not acc.startswith("453")]

        return {
            "transfer_log": {
                "path": str(Path(transfer_log_path).resolve()),
                "file_size": len(log_verifier.data),
                "is_balanced": log_balance["is_balanced"],
                "total_debit": log_balance["total_debit"],
                "total_credit": log_balance["total_credit"],
                "total_entries": log_balance["total_entries"],
                "accounts_used": sorted(list(log_accounts)),
            },
            "database": {
                "path": str(self.db_path.resolve()),
                "file_size": len(self.data),
                "total_accounts": len(db_accounts),
            },
            "compatibility": {
                "can_import": log_balance["is_balanced"] and len(missing_in_db) == 0,
                "accounts_matched": sorted(list(log_accounts.intersection(db_account_codes))),
                "accounts_unmatched": missing_in_db,
            },
        }
