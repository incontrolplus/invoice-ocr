#!/usr/bin/env python3
"""Analyze batch OCR results and produce a comprehensive audit report."""
import json
import sys
from decimal import Decimal
from pathlib import Path

def analyze_results(results_dir: str = "results/"):
    results_path = Path(results_dir)
    
    if not results_path.exists():
        print("Results directory not found!", file=sys.stderr)
        sys.exit(1)
    
    # Check for batch_summary first
    summary_path = results_path / "batch_summary.json"
    if summary_path.exists():
        with open(summary_path) as f:
            summary = json.load(f)
        print("=" * 80)
        print("BATCH SUMMARY")
        print("=" * 80)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print()
    
    # Analyze individual results
    json_files = sorted(results_path.glob("*.json"))
    json_files = [f for f in json_files if f.name != "batch_summary.json"]
    
    print("=" * 80)
    print(f"INDIVIDUAL INVOICE ANALYSIS ({len(json_files)} files)")
    print("=" * 80)
    
    all_results = []
    
    for jf in json_files:
        with open(jf) as f:
            data = json.load(f)
        
        # Support both wrapped normalized_data and top-level format
        nd = data.get("normalized_data", data)
        meta = nd.get("invoice_metadata", {})
        supplier = nd.get("supplier", {})
        recipient = nd.get("recipient", {})
        items = nd.get("line_items", [])
        fin = nd.get("financial_summary", {})
        payment = nd.get("payment_details", {})
        validation = data.get("validation_results", data.get("validation", {}))
        raw = data.get("raw_ocr_evidence", {})
        
        # Extract financial amounts
        def get_amount(obj):
            if isinstance(obj, dict):
                return obj.get("amount"), obj.get("currency")
            return None, None
        
        tax_base_amt, tax_base_cur = get_amount(fin.get("tax_base", {}))
        vat_amt, vat_cur = get_amount(fin.get("vat_amount", {}))
        total_amt, total_cur = get_amount(fin.get("total_amount_due", {}))
        
        result = {
            "file": jf.name,
            "invoice_number": meta.get("invoice_number"),
            "date_issued": meta.get("date_issued"),
            "date_tax_event": meta.get("date_tax_event"),
            "place_issued": meta.get("place_issued"),
            "supplier_name": supplier.get("name"),
            "supplier_eik": supplier.get("eik"),
            "recipient_name": recipient.get("name"),
            "recipient_eik": recipient.get("eik"),
            "line_items_count": len(items),
            "tax_base": tax_base_amt,
            "vat": vat_amt,
            "total": total_amt,
            "currency": total_cur or tax_base_cur,
            "pages": raw.get("total_pages", 0),
            "mean_confidence": raw.get("mean_confidence", 0),
            "is_valid": validation.get("is_valid", False),
            "errors": len(validation.get("errors", [])),
            "warnings": len(validation.get("warnings", [])),
            "error_details": [e.get("code", "") for e in validation.get("errors", [])],
        }
        all_results.append(result)
        
        status = "✅ VALID" if result["is_valid"] else f"❌ {result['errors']} errors"
        print(f"\n{'─' * 70}")
        print(f"📄 {jf.name}")
        print(f"   Invoice #:  {result['invoice_number'] or 'N/A'}")
        print(f"   Date:       {result['date_issued'] or 'N/A'}")
        print(f"   Supplier:   {result['supplier_name'] or 'N/A'} (EIK: {result['supplier_eik'] or 'N/A'})")
        print(f"   Recipient:  {result['recipient_name'] or 'N/A'} (EIK: {result['recipient_eik'] or 'N/A'})")
        print(f"   Items:      {result['line_items_count']}")
        print(f"   Tax base:   {result['tax_base']} {result['currency'] or ''}")
        print(f"   VAT:        {result['vat']} {result['currency'] or ''}")
        print(f"   Total:      {result['total']} {result['currency'] or ''}")
        print(f"   Pages:      {result['pages']}")
        print(f"   Confidence: {result['mean_confidence']:.1f}%")
        print(f"   Status:     {status}")
        if result["error_details"]:
            print(f"   Errors:     {', '.join(result['error_details'])}")
    
    # Summary table
    print(f"\n\n{'=' * 80}")
    print("SUMMARY TABLE")
    print("=" * 80)
    
    total_files = len(all_results)
    valid_count = sum(1 for r in all_results if r["is_valid"])
    has_number = sum(1 for r in all_results if r["invoice_number"])
    has_supplier = sum(1 for r in all_results if r["supplier_name"])
    has_items = sum(1 for r in all_results if r["line_items_count"] > 0)
    has_total = sum(1 for r in all_results if r["total"] is not None)
    
    print(f"  Total files processed:    {total_files}")
    print(f"  Valid (no errors):        {valid_count}/{total_files}")
    print(f"  Invoice number extracted: {has_number}/{total_files}")
    print(f"  Supplier name extracted:  {has_supplier}/{total_files}")
    print(f"  Line items extracted:     {has_items}/{total_files}")
    print(f"  Total amount extracted:   {has_total}/{total_files}")
    
    # Group by supplier
    print(f"\n{'=' * 80}")
    print("BY SUPPLIER")
    print("=" * 80)
    suppliers = {}
    for r in all_results:
        s = r["supplier_name"] or "Unknown"
        if s not in suppliers:
            suppliers[s] = []
        suppliers[s].append(r)
    
    for supplier, invoices in sorted(suppliers.items()):
        total_sum = sum(float(r["total"]) for r in invoices if r["total"] is not None)
        print(f"\n  {supplier}: {len(invoices)} invoice(s), total: {total_sum:.2f}")
        for r in invoices:
            print(f"    - {r['file']}: #{r['invoice_number']} / {r['date_issued']} / {r['total']} {r['currency'] or ''}")


if __name__ == "__main__":
    analyze_results(sys.argv[1] if len(sys.argv) > 1 else "results/")
