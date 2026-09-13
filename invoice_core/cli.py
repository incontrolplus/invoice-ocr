"""Command-line interface (CLI) entry point for single and batch processing."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys

from .batch import format_batch_console_report, process_batch
from .cache import clear_ocr_cache, get_ocr_cache_stats
from .constants import (
    DEFAULT_MAX_PAGES_PER_WORKER,
    DEFAULT_MAX_WORKER_MEMORY_MB,
    DEFAULT_OCR_CACHE_DIR,
    DEFAULT_OCR_LANG,
    SUPPORTED_EXTENSIONS,
)
from .pipeline import process_invoice, serialize_invoice
from .tesseract_env import (
    TesseractLanguageMissingError,
    ensure_tesseract_ready,
    get_installed_ocr_languages,
    setup_tessdata_prefix,
    verify_tesseract_languages,
)

logger = logging.getLogger("invoice_ocr")

def main() -> None:
    """CLI entry point: parse args, process single invoice or batch directory."""
    parser = argparse.ArgumentParser(
        description="Bulgarian Invoice OCR & Accounting Document Understanding Pipeline",
    )
    parser.add_argument(
        "image",
        type=str,
        nargs="?",
        help="Path to invoice document (.pdf, .png, .jpg, .jpeg) (for single file mode)",
    )
    parser.add_argument(
        "--input-dir",
        type=str,
        default=None,
        help="Directory containing multiple invoice documents for batch processing",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="results/",
        help="Directory to save JSON outputs (default: results/)",
    )
    parser.add_argument(
        "--summary-file",
        type=str,
        default=None,
        help="Custom path to save batch_summary.json (default: <output-dir>/batch_summary.json)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug mode to save visual and layout artifacts (pages, tokens, layout tree)",
    )
    parser.add_argument(
        "--debug-dir",
        type=str,
        default="debug/",
        help="Directory to save debug artifacts (default: debug/)",
    )
    parser.add_argument(
        "--lang",
        type=str,
        default=DEFAULT_OCR_LANG,
        help=f"Tesseract OCR language(s) (default: {DEFAULT_OCR_LANG})",
    )
    parser.add_argument(
        "--tessdata-dir",
        type=str,
        default=None,
        help="Path to custom tessdata directory containing traineddata files",
    )
    parser.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Quiet mode: suppress detailed progress logs on stderr",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose mode: output debug-level logs on stderr",
    )
    parser.add_argument(
        "--export-nap",
        action="store_true",
        help="Generate statutory НАП POKUPKI.TXT purchase ledger (Приложение 12 от ППЗДДС)",
    )
    parser.add_argument(
        "--nap-file",
        type=str,
        default=None,
        help="Custom output path for POKUPKI.TXT",
    )
    parser.add_argument(
        "--nap-period",
        type=str,
        default=None,
        help="Tax period for НАП purchase ledger (format YYYYMM, e.g. 202608)",
    )
    parser.add_argument(
        "--nap-format",
        type=str,
        choices=["fixed_width", "tsv", "csv"],
        default="fixed_width",
        help="Format for POKUPKI.TXT: 'fixed_width', 'tsv', or 'csv' (default: fixed_width)",
    )
    parser.add_argument(
        "--nap-encoding",
        type=str,
        choices=["cp1251", "utf-8"],
        default="cp1251",
        help="Encoding for POKUPKI.TXT: 'cp1251' or 'utf-8' (default: cp1251)",
    )
    parser.add_argument(
        "--export-entries",
        action="store_true",
        help="Generate double-entry bookkeeping journal entries (контировки 304/602, 4531, 401)",
    )
    parser.add_argument(
        "--export-prodagbi",
        action="store_true",
        help="Generate statutory НАП PRODAGBI.TXT sales ledger (Приложение 10 от ППЗДДС)",
    )
    parser.add_argument(
        "--prodagbi-file",
        type=str,
        default=None,
        help="Custom output path for PRODAGBI.TXT",
    )
    parser.add_argument(
        "--export-deklar",
        action="store_true",
        help="Generate statutory НАП DEKLAR.TXT VAT declaration (Приложение 13 от ППЗДДС)",
    )
    parser.add_argument(
        "--deklar-file",
        type=str,
        default=None,
        help="Custom output path for DEKLAR.TXT",
    )
    parser.add_argument(
        "--export-nap-package",
        action="store_true",
        help="Generate full statutory 3-file package (POKUPKI.TXT, PRODAGBI.TXT, DEKLAR.TXT, ZIP)",
    )
    parser.add_argument(
        "--auto-protocol-117",
        action="store_true",
        default=True,
        help="Automatically generate Art. 117 protocols for reverse charge invoices",
    )
    parser.add_argument(
        "--verify-contractors",
        action="store_true",
        help="Online verify contractors against Commercial Register, NRA VAT register, and EU VIES",
    )
    parser.add_argument(
        "--expense-account",
        type=str,
        default=None,
        help="Default expense account (e.g. '304' for goods, '602' for services)",
    )
    parser.add_argument(
        "--erp-format",
        type=str,
        choices=["universal", "microinvest", "business_navigator", "ajur", "sap"],
        default="universal",
        help="ERP journal entries CSV format (default: universal)",
    )
    parser.add_argument(
        "-w", "--workers",
        type=int,
        default=None,
        help="Number of worker processes for parallel batch execution (default: auto-detect all CPU cores)",
    )
    parser.add_argument(
        "--max-pages-per-worker",
        type=int,
        default=DEFAULT_MAX_PAGES_PER_WORKER,
        help=f"Recycle worker process after processing N pages (Memory Guard, default: {DEFAULT_MAX_PAGES_PER_WORKER})",
    )
    parser.add_argument(
        "--max-worker-memory-mb",
        type=float,
        default=DEFAULT_MAX_WORKER_MEMORY_MB,
        help=f"Maximum resident memory limit in MB per worker process (default: {DEFAULT_MAX_WORKER_MEMORY_MB})",
    )
    parser.add_argument(
        "--ocr-cache-dir",
        type=str,
        default=str(DEFAULT_OCR_CACHE_DIR),
        help="Directory for caching raw OCR tokens (default: .ocr_cache)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable token-level OCR caching and force re-running OCR passes",
    )
    parser.add_argument(
        "--clear-cache",
        action="store_true",
        help="Clear OCR token cache directory before processing",
    )
    parser.add_argument(
        "--export-delta-pro",
        action="store_true",
        help="Generate Microinvest Delta Pro transfer package (TRANSFER.LOG & TRANSFER.ldb)",
    )
    parser.add_argument(
        "--delta-pro-dir",
        type=str,
        default=None,
        help="Custom destination directory for TRANSFER.LOG & TRANSFER.ldb (default: <company_name> or Building_11)",
    )
    parser.add_argument(
        "--sync-supabase",
        action="store_true",
        help="Synchronize extracted invoice and accounting package to Supabase database",
    )
    args = parser.parse_args()

    # Configure logging to stderr
    log_level = logging.DEBUG if args.verbose else (logging.WARNING if args.quiet else logging.INFO)
    logging.basicConfig(
        level=log_level,
        stream=sys.stderr,
        format="%(levelname)s: %(message)s",
    )

    if args.clear_cache:
        deleted = clear_ocr_cache(args.ocr_cache_dir)
        sys.stderr.write(f"Cleared {deleted} cached OCR file(s) from {args.ocr_cache_dir}\n")

    # Check Tesseract availability and required language packs upfront
    try:
        req_langs = [l.strip() for l in args.lang.split("+") if l.strip()]
        ensure_tesseract_ready(
            required_langs=req_langs,
            custom_tessdata=args.tessdata_dir,
        )
    except (RuntimeError, TesseractLanguageMissingError) as exc:
        logger.error("%s", exc)
        sys.exit(1)

    debug_dir = Path(args.debug_dir) if args.debug else None
    use_cache = not args.no_cache
    ocr_cache_dir = args.ocr_cache_dir

    # Batch mode
    if args.input_dir:
        try:
            summary = process_batch(
                input_dir=args.input_dir,
                output_dir=args.output_dir,
                debug_dir=debug_dir,
                lang=args.lang,
                tessdata_dir=args.tessdata_dir,
                summary_file=args.summary_file,
                quiet=args.quiet,
                export_nap=args.export_nap,
                export_entries=args.export_entries,
                nap_period=args.nap_period,
                nap_file=args.nap_file,
                nap_format=args.nap_format,
                nap_encoding=args.nap_encoding,
                expense_account=args.expense_account,
                erp_format=args.erp_format,
                export_prodagbi=args.export_prodagbi,
                prodagbi_file=args.prodagbi_file,
                export_deklar=args.export_deklar,
                deklar_file=args.deklar_file,
                export_nap_package=args.export_nap_package,
                auto_protocol_117=args.auto_protocol_117,
                verify_contractors=args.verify_contractors,
                workers=args.workers,
                use_cache=use_cache,
                ocr_cache_dir=ocr_cache_dir,
                max_pages_per_worker=args.max_pages_per_worker,
                max_worker_memory_mb=args.max_worker_memory_mb,
            )
            # Print executive summary report to stderr
            report = format_batch_console_report(summary)
            sys.stderr.write(f"\n{report}\n")
            sys.exit(0)
        except Exception as exc:
            logger.error("Batch processing failed: %s", exc)
            sys.exit(1)

    # Single-file mode
    if not args.image:
        parser.error("Either image or --input-dir must be provided")

    image_path = Path(args.image)
    if not image_path.exists():
        logger.error("File not found: %s", image_path)
        sys.exit(1)
    if image_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        logger.error(
            "Unsupported file type: %s (supported: %s)",
            image_path.suffix,
            ", ".join(sorted(SUPPORTED_EXTENSIONS)),
        )
        sys.exit(1)

    try:
        invoice = process_invoice(
            image_path,
            debug_dir=debug_dir,
            lang=args.lang,
            tessdata_dir=args.tessdata_dir,
            use_cache=use_cache,
            ocr_cache_dir=ocr_cache_dir,
            verify_contractors=args.verify_contractors,
        )
        json_output = serialize_invoice(invoice)

        if args.verify_contractors:
            from contractor_verification import verify_contractor
            tax_dt = invoice.invoice_metadata.date_tax_event or invoice.invoice_metadata.date_issued
            for role, party in [("supplier", invoice.supplier), ("recipient", invoice.recipient)]:
                ident = party.eik or party.vat_number
                if ident:
                    v_res = verify_contractor(ident, date_tax_event=tax_dt)
                    sys.stderr.write(
                        f"[{role.upper()} VERIFICATION] {party.name or ident} ({v_res.country_code}:{v_res.identifier}): "
                        f"Status={v_res.legal_status.value}, VAT={v_res.vat_status.value}, "
                        f"TaxCreditValid={v_res.is_valid_for_tax_credit}\n"
                    )
                    for issue in v_res.issues:
                        sys.stderr.write(f"  - {issue}\n")

        # Optional accounting exports
        if args.export_nap:
            from accounting_export import invoices_to_pokupki_txt
            out_p = Path(args.output_dir)
            out_p.mkdir(parents=True, exist_ok=True)
            target = Path(args.nap_file) if args.nap_file else (out_p / "POKUPKI.TXT")
            raw = invoices_to_pokupki_txt(
                [invoice],
                format=args.nap_format,
                encoding=args.nap_encoding,
                period=args.nap_period,
            )
            if isinstance(raw, bytes):
                target.write_bytes(raw)
            else:
                target.write_text(raw, encoding="utf-8")

        if args.export_entries:
            from accounting_export import (
                invoices_to_journal_entries,
                export_journal_entries_csv,
                export_journal_entries_json,
            )
            out_p = Path(args.output_dir)
            out_p.mkdir(parents=True, exist_ok=True)
            entries = invoices_to_journal_entries(
                [invoice],
                default_expense_account=args.expense_account,
            )
            (out_p / "journal_entries.csv").write_text(
                export_journal_entries_csv(entries, format_type=args.erp_format),
                encoding="utf-8-sig",
            )
            (out_p / "journal_entries.json").write_text(
                export_journal_entries_json(entries),
                encoding="utf-8",
            )

        if args.export_prodagbi:
            from accounting_export import invoices_to_prodagbi_txt
            out_p = Path(args.output_dir)
            out_p.mkdir(parents=True, exist_ok=True)
            target = Path(args.prodagbi_file) if args.prodagbi_file else (out_p / "PRODAGBI.TXT")
            raw_prod = invoices_to_prodagbi_txt(
                [invoice],
                format=args.nap_format,
                encoding=args.nap_encoding,
                period=args.nap_period,
            )
            if isinstance(raw_prod, bytes):
                target.write_bytes(raw_prod)
            else:
                target.write_text(raw_prod, encoding="utf-8")

        if args.export_deklar:
            from accounting_export import generate_vat_declaration, invoice_to_nap_entry
            out_p = Path(args.output_dir)
            out_p.mkdir(parents=True, exist_ok=True)
            target = Path(args.deklar_file) if args.deklar_file else (out_p / "DEKLAR.TXT")
            decl = generate_vat_declaration(
                purchase_entries=[invoice_to_nap_entry(invoice, period=args.nap_period)],
                period=args.nap_period or "202608",
            )
            raw_dek = decl.to_nap_deklar_txt(encoding=args.nap_encoding)
            if isinstance(raw_dek, bytes):
                target.write_bytes(raw_dek)
            else:
                target.write_text(raw_dek, encoding="utf-8")

        if args.export_nap_package:
            from accounting_export import export_nap_package
            out_p = Path(args.output_dir)
            out_p.mkdir(parents=True, exist_ok=True)
            export_nap_package(
                purchase_invoices=[invoice],
                period=args.nap_period or "202608",
                output_dir=out_p,
                format=args.nap_format,
                encoding=args.nap_encoding,
                auto_generate_protocols=args.auto_protocol_117,
                create_zip=True,
            )

        # Microinvest Delta Pro Package Export & Accounting Operation
        is_building_11 = (
            str(getattr(invoice.recipient, "eik", "") or "") == "206062202"
            or str(getattr(invoice.supplier, "eik", "") or "") == "206062202"
            or "билдинг 11" in str(getattr(invoice.recipient, "name", "") or "").lower()
            or "билдинг 11" in str(getattr(invoice.supplier, "name", "") or "").lower()
        )

        bundle = None
        if args.export_delta_pro or is_building_11 or args.delta_pro_dir:
            try:
                from invoice_core.historical_matcher import process_invoice_and_create_accounting_package
                inv_dict = json.loads(json_output)
                bundle = process_invoice_and_create_accounting_package(inv_dict)
                if bundle:
                    log_bytes = bundle.get("transfer_log_bytes")
                    ldb_bytes = bundle.get("transfer_ldb_bytes")
                    acc_op = bundle.get("accounting_operation", {})
                    co_name = acc_op.get("client_company") or "Building_11"
                    safe_co = "Building_11" if "11" in co_name else co_name.replace(" ", "_")

                    target_dir = Path(args.delta_pro_dir) if args.delta_pro_dir else Path(safe_co)
                    target_dir.mkdir(parents=True, exist_ok=True)

                    if log_bytes:
                        (target_dir / "TRANSFER.LOG").write_bytes(log_bytes)
                    if ldb_bytes:
                        (target_dir / "TRANSFER.ldb").write_bytes(ldb_bytes)

                    # Also sync to USB if /Volumes/NO NAME is mounted
                    usb_dir = Path(f"/Volumes/NO NAME/{safe_co}")
                    if usb_dir.parent.exists():
                        try:
                            usb_dir.mkdir(parents=True, exist_ok=True)
                            if log_bytes:
                                (usb_dir / "TRANSFER.LOG").write_bytes(log_bytes)
                            if ldb_bytes:
                                (usb_dir / "TRANSFER.ldb").write_bytes(ldb_bytes)
                        except Exception as usb_err:
                            logger.debug("Could not copy to USB: %s", usb_err)

                    # Output user-facing T-образен счетоводен запис report on stderr
                    curr = acc_op.get("currency", "BGN")
                    sys.stderr.write("\n" + "=" * 80 + "\n")
                    sys.stderr.write("              MICROINVEST DELTA PRO СЧЕТОВОДЕН ТРАНСФЕРЕН ПАКЕТ\n")
                    sys.stderr.write("=" * 80 + "\n")
                    sys.stderr.write(f"Клиент:         {acc_op.get('client_company')} (ЕИК: {bundle.get('match_report', {}).get('client_company_eik')})\n")
                    sys.stderr.write(f"Партньор:       {acc_op.get('contractor_name')} (ЕИК: {acc_op.get('contractor_eik')})\n")
                    sys.stderr.write(f"Документ №:     {acc_op.get('document_number')} от {acc_op.get('document_date')} ({acc_op.get('document_type')})\n")
                    sys.stderr.write(f"Валута:         {curr}\n")
                    sys.stderr.write("-" * 80 + "\n")
                    sys.stderr.write("Счетоводна операция (Т-образен счетоводен запис):\n")
                    for de in acc_op.get("debit_entries", []):
                        sys.stderr.write(f"  Д-т {de['account']:<5} ({de['account_name']:<30}): {de['amount']:>10.2f} {curr}\n")
                    for ce in acc_op.get("credit_entries", []):
                        sys.stderr.write(f"  К-т {ce['account']:<5} ({ce['account_name']:<30}): {ce['amount']:>10.2f} {curr}\n")
                    sys.stderr.write("-" * 80 + "\n")
                    sys.stderr.write("Файлове готови за директен импорт в Microinvest Delta Pro:\n")
                    sys.stderr.write(f"  • TRANSFER.LOG:  {target_dir / 'TRANSFER.LOG'} ({len(log_bytes or b''):,} байта)\n")
                    sys.stderr.write(f"  • TRANSFER.ldb:  {target_dir / 'TRANSFER.ldb'} ({len(ldb_bytes or b''):,} байта)\n")
                    sys.stderr.write("=" * 80 + "\n\n")
            except Exception as dp_err:
                logger.warning("Could not generate Delta Pro transfer files: %s", dp_err)

        if args.sync_supabase:
            try:
                from supabase_sync import is_supabase_configured, sync_invoice_to_supabase
                import asyncio
                if is_supabase_configured():
                    inv_dict = json.loads(json_output)
                    asyncio.run(sync_invoice_to_supabase(
                        doc_id=image_path.stem,
                        ocr_result=inv_dict,
                        file_name=image_path.name,
                        file_path=image_path,
                        source_channel="CLI_MANUAL",
                        accounting_bundle=bundle,
                    ))
                    sys.stderr.write("✓ Успешно синхронизирано със Supabase база данни.\n")
            except Exception as sb_err:
                logger.warning("Supabase sync error: %s", sb_err)

        # stdout: ONLY the valid JSON document
        print(json_output)
    except FileNotFoundError as exc:
        logger.error("File error: %s", exc)
        sys.exit(1)
    except ValueError as exc:
        logger.error("Image error: %s", exc)
        sys.exit(1)
    except (RuntimeError, TesseractLanguageMissingError) as exc:
        logger.error("%s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.error("Processing failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
