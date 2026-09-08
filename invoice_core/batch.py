"""Multi-core batch processing and console reporting."""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, as_completed
from decimal import Decimal
import gc
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, Generator, Sequence

from .constants import (
    DEFAULT_MAX_OCR_WORKERS,
    DEFAULT_MAX_PAGES_PER_WORKER,
    DEFAULT_MAX_WORKER_MEMORY_MB,
    DEFAULT_OCR_CACHE_DIR,
    DEFAULT_OCR_LANG,
    SUPPORTED_EXTENSIONS,
)
from .models import Invoice
from .pipeline import process_invoice, serialize_invoice
from .tesseract_env import setup_tessdata_prefix
from .worker_pool import OCRProcessPoolExecutor, get_open_fd_count, get_process_rss_mb

logger = logging.getLogger("invoice_ocr")

def format_batch_console_report(summary: dict[str, Any]) -> str:
    """Format an executive accounting batch report for terminal display."""
    s = summary.get("summary", {})
    total_docs = s.get("total_documents", 0)
    success_docs = s.get("processed_successfully", 0)
    failed_docs = s.get("failed", 0)
    valid_docs = s.get("valid_documents", 0)
    invalid_docs = s.get("invalid_documents", 0)
    pass_rate = s.get("validation_pass_rate_pct", 0.0)
    total_time = s.get("total_duration_seconds", 0.0)
    avg_time = s.get("average_duration_seconds", 0.0)
    total_items = s.get("total_line_items", 0)

    lines = [
        "=" * 80,
        "              ОБОБЩЕН СЧЕТОВОДЕН ОТЧЕТ ОТ ОБРАБОТКАТА (BATCH SUMMARY)          ",
        "=" * 80,
        f"Обработени документи:   {total_docs} общо ({success_docs} успешни, {failed_docs} грешки)",
        f"Счетоводна валидност:   {valid_docs} валидни ({pass_rate:.1f}%), {invalid_docs} с предупреждения/грешки",
        f"Извлечени артикули:     {total_items} реда",
        f"Време за обработка:     {total_time:.2f} сек. (средно {avg_time:.2f} сек./документ)",
    ]

    doc_types = s.get("document_types", {})
    if doc_types:
        lines.append("-" * 80)
        lines.append("РАЗПРЕДЕЛЕНИЕ ПО ВИДОВЕ ДОКУМЕНТИ:")
        for dtype, cnt in sorted(doc_types.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  • {dtype:<20}: {cnt:>2} бр.")

    lines.extend([
        "-" * 80,
        "ФИНАНСОВИ СБОРОВЕ ПО ВАЛУТИ:",
    ])

    by_curr = summary.get("financial_totals_by_currency", {})
    if by_curr:
        for curr, data in sorted(by_curr.items()):
            cnt = data.get("document_count", 0)
            tb = data.get("total_tax_base", "0.00")
            vat = data.get("total_vat_amount", "0.00")
            tot = data.get("total_amount_due", "0.00")
            lines.append(
                f"  • {curr:<7}: {cnt:>2} фактури | "
                f"Данъчна основа: {tb:>12} {curr} | "
                f"ДДС: {vat:>10} {curr} | "
                f"Общо: {tot:>12} {curr}"
            )
    else:
        lines.append("  (няма открити финансови суми)")

    suppliers = summary.get("suppliers", [])
    if suppliers:
        lines.append("-" * 80)
        lines.append("СПРАВКА ПО ДОСТАВЧИЦИ:")
        for sup in sorted(suppliers, key=lambda x: x.get("invoice_count", 0), reverse=True)[:10]:
            name = sup.get("name") or "Неизвестен доставчик"
            eik = sup.get("eik") or "N/A"
            cnt = sup.get("invoice_count", 0)
            totals_str_parts = []
            for c, ctot in sup.get("totals_by_currency", {}).items():
                totals_str_parts.append(f"{ctot.get('total_amount_due', '0.00')} {c}")
            totals_str = ", ".join(totals_str_parts) or "0.00"
            lines.append(f"  • {name} (ЕИК: {eik}): {cnt} фактури | Общо: {totals_str}")

    val_stats = summary.get("validation_issues_summary", {})
    errs = val_stats.get("errors", {})
    warns = val_stats.get("warnings", {})
    if errs or warns:
        lines.append("-" * 80)
        lines.append("ВАЛИДАЦИОННИ КОНСТАТАЦИИ:")
        if errs:
            lines.append(f"  Грешки: {', '.join(f'{k} ({v})' for k, v in sorted(errs.items()))}")
        if warns:
            lines.append(f"  Предупреждения: {', '.join(f'{k} ({v})' for k, v in sorted(warns.items()))}")

    acc_exports = summary.get("accounting_exports", {})
    if acc_exports:
        lines.append("-" * 80)
        lines.append("СЧЕТОВОДЕН ЕКСПОРТ (НАП И ERP):")
        if "pokupki" in acc_exports:
            lines.append(f"  • НАП Дневник покупки (POKUPKI.TXT): {acc_exports['pokupki']}")
        if "prodagbi" in acc_exports:
            lines.append(f"  • НАП Дневник продажби (PRODAGBI.TXT): {acc_exports['prodagbi']}")
        if "deklar" in acc_exports:
            lines.append(f"  • НАП Справка-декларация (DEKLAR.TXT): {acc_exports['deklar']}")
        if "nap_pkg_zip_package" in acc_exports:
            lines.append(f"  • НАП Пълен пакет (ZIP): {acc_exports['nap_pkg_zip_package']}")
        if "journal_entries_csv" in acc_exports:
            lines.append(f"  • Счетоводни статии (CSV): {acc_exports['journal_entries_csv']}")
        if "journal_entries_json" in acc_exports:
            lines.append(f"  • Счетоводни статии (JSON): {acc_exports['journal_entries_json']}")

    lines.append("-" * 80)
    out_dir = summary.get("output_directory", "results/")
    sum_file = summary.get("summary_file", "results/batch_summary.json")
    lines.append(f"Експортирани JSON файлове: {out_dir}")
    lines.append(f"Обобщен счетоводен отчет: {sum_file}")
    lines.append("=" * 80)
    return "\n".join(lines)


def _process_batch_file_worker(args: dict[str, Any]) -> dict[str, Any]:
    """Worker function executed in child processes for parallel batch processing."""
    # Prevent OpenMP and thread thrashing across parallel worker processes
    os.environ["OMP_THREAD_LIMIT"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"
    os.environ["MKL_NUM_THREADS"] = "1"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
    os.environ["NUMEXPR_NUM_THREADS"] = "1"

    file_path = Path(args["file_path"])
    rel_path = args["rel_path"]
    out_dir = Path(args["out_dir"])
    debug_dir = Path(args["debug_dir"]) if args.get("debug_dir") else None
    lang = args.get("lang", DEFAULT_OCR_LANG)
    tessdata_dir = args.get("tessdata_dir")
    use_cache = args.get("use_cache", True)
    ocr_cache_dir = args.get("ocr_cache_dir")
    file_idx = args.get("file_idx", 0)
    max_memory_mb = args.get("max_memory_mb", DEFAULT_MAX_WORKER_MEMORY_MB)
    return_invoice_object = args.get("return_invoice_object", True)

    file_start = time.perf_counter()
    try:
        invoice = process_invoice(
            file_path,
            debug_dir=debug_dir,
            lang=lang,
            tessdata_dir=tessdata_dir,
            use_cache=use_cache,
            ocr_cache_dir=ocr_cache_dir,
        )
        file_duration = time.perf_counter() - file_start

        # Count pages
        doc_pages = 1
        if invoice and invoice.raw_ocr_evidence:
            doc_pages = int(invoice.raw_ocr_evidence.get("total_pages", 1))

        # Serialize and write JSON output directly in worker
        json_output = serialize_invoice(invoice)
        out_file = out_dir / f"{file_path.stem}.json"
        out_file.write_text(json_output, encoding="utf-8")

        # Explicit garbage collection inside worker
        gc.collect()

        end_rss = get_process_rss_mb()
        memory_exceeded = end_rss > max_memory_mb

        return {
            "file_idx": file_idx,
            "file": file_path.name,
            "relative_path": rel_path,
            "output_file": str(out_file),
            "status": "success",
            "invoice": invoice if return_invoice_object else None,
            "pages": doc_pages,
            "worker_pid": os.getpid(),
            "memory_rss_mb": round(end_rss, 2),
            "memory_guard_triggered": memory_exceeded,
            "duration_seconds": round(file_duration, 3),
            "error": None,
        }
    except Exception as exc:
        gc.collect()
        file_duration = time.perf_counter() - file_start
        end_rss = get_process_rss_mb()
        logger.error("Failed processing %s: %s", rel_path, exc)
        return {
            "file_idx": file_idx,
            "file": file_path.name,
            "relative_path": rel_path,
            "output_file": None,
            "status": "error",
            "invoice": None,
            "pages": 0,
            "worker_pid": os.getpid(),
            "memory_rss_mb": round(end_rss, 2),
            "memory_guard_triggered": end_rss > max_memory_mb,
            "duration_seconds": round(file_duration, 3),
            "error": str(exc),
        }


def iter_process_batch(
    input_dir: Path | str,
    output_dir: Path | str = "results/",
    debug_dir: Path | str | None = None,
    lang: str = DEFAULT_OCR_LANG,
    tessdata_dir: Path | str | None = None,
    workers: int | None = None,
    use_cache: bool = True,
    ocr_cache_dir: Path | str | None = DEFAULT_OCR_CACHE_DIR,
    max_pages_per_worker: int | None = None,
    max_worker_memory_mb: float | None = None,
    return_invoice_object: bool = True,
    progress_callback: Any = None,
    quiet: bool = True,
) -> Generator[dict[str, Any], None, None]:
    """Stream processing of a directory of invoices yielding document results on-the-fly.
    
    Operates with constant O(1) memory by sliding tasks across isolated worker
    processes without loading all document results into RAM.
    """
    in_dir = Path(input_dir).resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if debug_dir:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)

    if not in_dir.exists():
        raise FileNotFoundError(f"Input directory does not exist: {in_dir}")

    files: list[Path] = []
    for p in sorted(in_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(p)

    if not quiet:
        logger.info("Found %d documents to stream process in %s", len(files), in_dir)

    worker_args_list = [
        {
            "file_idx": i,
            "file_path": str(fp),
            "rel_path": str(fp.relative_to(in_dir)),
            "out_dir": str(out_dir),
            "debug_dir": str(debug_dir) if debug_dir else None,
            "lang": lang,
            "tessdata_dir": str(tessdata_dir) if tessdata_dir else None,
            "use_cache": use_cache,
            "ocr_cache_dir": str(ocr_cache_dir) if ocr_cache_dir else None,
            "return_invoice_object": return_invoice_object,
            "max_memory_mb": max_worker_memory_mb or DEFAULT_MAX_WORKER_MEMORY_MB,
            "max_pages_per_worker": max_pages_per_worker or DEFAULT_MAX_PAGES_PER_WORKER,
        }
        for i, fp in enumerate(files)
    ]

    effective_workers = workers if (workers and workers > 0) else DEFAULT_MAX_OCR_WORKERS

    if len(files) == 0:
        return

    if effective_workers == 1 or len(files) <= 1:
        # Sequential execution
        for i, wargs in enumerate(worker_args_list):
            res = _process_batch_file_worker(wargs)
            if progress_callback:
                progress_callback(i + 1, len(files), res.get("file", ""))
            if not quiet:
                logger.info(
                    "[%d/%d] Processed %s in %.2fs (%s)",
                    i + 1, len(files), res["relative_path"], res["duration_seconds"], res["status"],
                )
            yield res
            gc.collect()
    else:
        # Parallel execution with sliding window over isolated OCRProcessPoolExecutor
        max_pool_workers = min(effective_workers, len(files))
        if not quiet:
            logger.info("Executing batch processing across %d worker processes...", max_pool_workers)
        with OCRProcessPoolExecutor(
            max_workers=max_pool_workers,
            max_pages_per_worker=max_pages_per_worker or DEFAULT_MAX_PAGES_PER_WORKER,
            max_memory_mb=max_worker_memory_mb or DEFAULT_MAX_WORKER_MEMORY_MB,
        ) as executor:
            completed_count = 0
            for res in executor.stream_batch(
                worker_args_list,
                window_size=max(4, max_pool_workers * 2),
                progress_callback=progress_callback,
                total_items=len(files),
            ):
                completed_count += 1
                if not quiet:
                    logger.info(
                        "[%d/%d] Processed %s in %.2fs (%s)",
                        completed_count, len(files), res["relative_path"], res["duration_seconds"], res["status"],
                    )
                yield res


def process_batch(
    input_dir: Path | str,
    output_dir: Path | str = "results/",
    debug_dir: Path | str | None = None,
    lang: str = DEFAULT_OCR_LANG,
    tessdata_dir: Path | str | None = None,
    summary_file: Path | str | None = None,
    quiet: bool = False,
    export_nap: bool = False,
    export_entries: bool = False,
    nap_period: str | None = None,
    nap_file: Path | str | None = None,
    nap_format: str = "fixed_width",
    nap_encoding: str = "cp1251",
    expense_account: str | None = None,
    erp_format: str = "universal",
    export_prodagbi: bool = False,
    prodagbi_file: Path | str | None = None,
    export_deklar: bool = False,
    deklar_file: Path | str | None = None,
    export_nap_package: bool = False,
    auto_protocol_117: bool = True,
    verify_contractors: bool = False,
    workers: int | None = None,
    use_cache: bool = True,
    ocr_cache_dir: Path | str | None = DEFAULT_OCR_CACHE_DIR,
    max_pages_per_worker: int | None = None,
    max_worker_memory_mb: float | None = None,
    progress_callback: Any = None,
) -> dict[str, Any]:
    """Process an entire directory of invoices and produce an accounting batch summary."""
    in_dir = Path(input_dir).resolve()
    out_dir = Path(output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if debug_dir:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)

    if not in_dir.exists():
        raise FileNotFoundError(f"Input directory does not exist: {in_dir}")

    # Discover supported files
    files: list[Path] = []
    for p in sorted(in_dir.rglob("*")):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(p)

    batch_start_time = time.perf_counter()
    doc_results: list[dict[str, Any]] = []
    processed_invoices: list[Invoice] = []
    currency_totals: dict[str, dict[str, Any]] = {}
    supplier_map: dict[str, dict[str, Any]] = {}
    validation_errors_count: dict[str, int] = {}
    validation_warnings_count: dict[str, int] = {}

    processed_count = 0
    failed_count = 0
    valid_count = 0
    total_line_items = 0
    document_types_count: dict[str, int] = {}

    if not quiet:
        logger.info("Found %d documents to process in %s", len(files), in_dir)

    effective_workers = workers
    if effective_workers is None or effective_workers <= 0:
        effective_workers = DEFAULT_MAX_OCR_WORKERS

    ordered_results: list[dict[str, Any]] = [None] * len(files)  # type: ignore

    if len(files) > 0:
        for res in iter_process_batch(
            input_dir=in_dir,
            output_dir=out_dir,
            debug_dir=debug_dir,
            lang=lang,
            tessdata_dir=tessdata_dir,
            workers=effective_workers,
            use_cache=use_cache,
            ocr_cache_dir=ocr_cache_dir,
            max_pages_per_worker=max_pages_per_worker,
            max_worker_memory_mb=max_worker_memory_mb,
            return_invoice_object=True,
            progress_callback=progress_callback,
            quiet=quiet,
        ):
            f_idx = res.get("file_idx", 0)
            ordered_results[f_idx] = res

    for res in ordered_results:
        if res is None:
            continue
        rel_path = res["relative_path"]
        file_name = res["file"]
        file_duration = res.get("duration_seconds", 0.0)

        if res["status"] == "success" and res["invoice"] is not None:
            invoice = res["invoice"]
            out_file = res["output_file"]
            is_valid = invoice.validation.is_valid
            if is_valid:
                valid_count += 1
            processed_count += 1
            processed_invoices.append(invoice)

            items_count = len(invoice.line_items)
            total_line_items += items_count

            tb_val = invoice.financial_summary.tax_base.amount
            vat_val = invoice.financial_summary.vat_amount.amount
            tot_val = invoice.financial_summary.total_amount_due.amount
            curr = (
                invoice.financial_summary.total_amount_due.currency
                or invoice.financial_summary.tax_base.currency
                or "UNKNOWN"
            )

            # Aggregate financial totals by currency
            if curr not in currency_totals:
                currency_totals[curr] = {
                    "document_count": 0,
                    "total_tax_base": Decimal("0.00"),
                    "total_vat_amount": Decimal("0.00"),
                    "total_amount_due": Decimal("0.00"),
                }
            curr_entry = currency_totals[curr]
            curr_entry["document_count"] += 1
            if tb_val is not None:
                curr_entry["total_tax_base"] += tb_val
            if vat_val is not None:
                curr_entry["total_vat_amount"] += vat_val
            if tot_val is not None:
                curr_entry["total_amount_due"] += tot_val

            # Aggregate suppliers
            sup_name = invoice.supplier.name or "Неизвестен доставчик"
            sup_eik = invoice.supplier.eik or "N/A"
            sup_key = f"{sup_eik}_{sup_name}"
            if sup_key not in supplier_map:
                supplier_map[sup_key] = {
                    "name": sup_name,
                    "eik": invoice.supplier.eik,
                    "vat_number": invoice.supplier.vat_number,
                    "invoice_count": 0,
                    "totals_by_currency": {},
                }
            sup_entry = supplier_map[sup_key]
            sup_entry["invoice_count"] += 1
            if curr not in sup_entry["totals_by_currency"]:
                sup_entry["totals_by_currency"][curr] = {
                    "tax_base": Decimal("0.00"),
                    "vat_amount": Decimal("0.00"),
                    "total_amount_due": Decimal("0.00"),
                }
            if tb_val is not None:
                sup_entry["totals_by_currency"][curr]["tax_base"] += tb_val
            if vat_val is not None:
                sup_entry["totals_by_currency"][curr]["vat_amount"] += vat_val
            if tot_val is not None:
                sup_entry["totals_by_currency"][curr]["total_amount_due"] += tot_val

            # Track validation codes
            err_codes = [issue.code for issue in invoice.validation.errors]
            warn_codes = [issue.code for issue in invoice.validation.warnings]
            for code in err_codes:
                validation_errors_count[code] = validation_errors_count.get(code, 0) + 1
            for code in warn_codes:
                validation_warnings_count[code] = validation_warnings_count.get(code, 0) + 1

            # Track document type
            doc_type = invoice.invoice_metadata.document_type
            document_types_count[doc_type] = document_types_count.get(doc_type, 0) + 1

            doc_record = {
                "file": file_name,
                "relative_path": rel_path,
                "output_file": str(out_file),
                "status": "success",
                "is_valid": is_valid,
                "document_type": doc_type,
                "invoice_number": invoice.invoice_metadata.invoice_number,
                "date_issued": invoice.invoice_metadata.date_issued,
                "date_tax_event": invoice.invoice_metadata.date_tax_event,
                "supplier": {
                    "name": invoice.supplier.name,
                    "eik": invoice.supplier.eik,
                    "vat_number": invoice.supplier.vat_number,
                },
                "recipient": {
                    "name": invoice.recipient.name,
                    "eik": invoice.recipient.eik,
                    "vat_number": invoice.recipient.vat_number,
                },
                "currency": curr if curr != "UNKNOWN" else None,
                "tax_base": str(tb_val) if tb_val is not None else None,
                "vat_amount": str(vat_val) if vat_val is not None else None,
                "total_amount_due": str(tot_val) if tot_val is not None else None,
                "line_items_count": items_count,
                "duration_seconds": round(file_duration, 3),
                "errors": err_codes,
                "warnings": warn_codes,
            }
            doc_results.append(doc_record)
        else:
            failed_count += 1
            doc_results.append({
                "file": file_name,
                "relative_path": rel_path,
                "output_file": None,
                "status": "error",
                "is_valid": False,
                "error": res.get("error"),
                "duration_seconds": round(file_duration, 3),
            })

    total_batch_duration = time.perf_counter() - batch_start_time
    total_docs = len(files)
    avg_duration = (total_batch_duration / total_docs) if total_docs > 0 else 0.0

    # Format decimals for JSON serialization
    formatted_currencies = {}
    for c, cdata in currency_totals.items():
        formatted_currencies[c] = {
            "document_count": cdata["document_count"],
            "total_tax_base": f"{cdata['total_tax_base']:.2f}",
            "total_vat_amount": f"{cdata['total_vat_amount']:.2f}",
            "total_amount_due": f"{cdata['total_amount_due']:.2f}",
        }

    formatted_suppliers = []
    for sup in supplier_map.values():
        curr_obj = {}
        for c, camts in sup["totals_by_currency"].items():
            curr_obj[c] = {
                "tax_base": f"{camts['tax_base']:.2f}",
                "vat_amount": f"{camts['vat_amount']:.2f}",
                "total_amount_due": f"{camts['total_amount_due']:.2f}",
            }
        formatted_suppliers.append({
            "name": sup["name"],
            "eik": sup["eik"],
            "vat_number": sup["vat_number"],
            "invoice_count": sup["invoice_count"],
            "totals_by_currency": curr_obj,
        })

    pass_rate = (valid_count / processed_count * 100.0) if processed_count > 0 else 0.0

    batch_summary = {
        "summary": {
            "total_documents": total_docs,
            "processed_successfully": processed_count,
            "failed": failed_count,
            "valid_documents": valid_count,
            "invalid_documents": processed_count - valid_count,
            "validation_pass_rate_pct": round(pass_rate, 2),
            "total_line_items": total_line_items,
            "document_types": document_types_count,
            "total_duration_seconds": round(total_batch_duration, 3),
            "average_duration_seconds": round(avg_duration, 3),
        },
        "financial_totals_by_currency": formatted_currencies,
        "suppliers": formatted_suppliers,
        "validation_issues_summary": {
            "errors": validation_errors_count,
            "warnings": validation_warnings_count,
        },
        "documents": doc_results,
        "output_directory": str(out_dir),
        "summary_file": str(summary_file or (out_dir / "batch_summary.json")),
        # Backward compatibility keys for existing test expectations
        "processed": processed_count,
        "failed": failed_count,
        "results": [
            {
                "file": d["file"],
                "status": d["status"],
                "invoice_no": d.get("invoice_number"),
            }
            if d["status"] == "success"
            else {
                "file": d["file"],
                "status": "error",
                "error": d.get("error"),
            }
            for d in doc_results
        ],
    }

    accounting_exports: dict[str, str] = {}
    if export_nap and processed_invoices:
        from accounting_export import invoices_to_pokupki_txt
        pokupki_target = Path(nap_file) if nap_file else (out_dir / "POKUPKI.TXT")
        pokupki_target.parent.mkdir(parents=True, exist_ok=True)
        raw_content = invoices_to_pokupki_txt(
            processed_invoices,
            format=nap_format,
            encoding=nap_encoding,
            period=nap_period,
        )
        if isinstance(raw_content, bytes):
            pokupki_target.write_bytes(raw_content)
        else:
            pokupki_target.write_text(raw_content, encoding="utf-8")
        accounting_exports["pokupki"] = str(pokupki_target)

    if export_entries and processed_invoices:
        from accounting_export import (
            invoices_to_journal_entries,
            export_journal_entries_csv,
            export_journal_entries_json,
        )
        entries = invoices_to_journal_entries(
            processed_invoices,
            default_expense_account=expense_account,
        )
        csv_target = out_dir / "journal_entries.csv"
        csv_text = export_journal_entries_csv(entries, format_type=erp_format)
        csv_target.write_text(csv_text, encoding="utf-8-sig")
        accounting_exports["journal_entries_csv"] = str(csv_target)

        json_target = out_dir / "journal_entries.json"
        json_text = export_journal_entries_json(entries)
        json_target.write_text(json_text, encoding="utf-8")
        accounting_exports["journal_entries_json"] = str(json_target)

    if export_prodagbi and processed_invoices:
        from accounting_export import invoices_to_prodagbi_txt
        prodagbi_target = Path(prodagbi_file) if prodagbi_file else (out_dir / "PRODAGBI.TXT")
        prodagbi_target.parent.mkdir(parents=True, exist_ok=True)
        raw_content = invoices_to_prodagbi_txt(
            processed_invoices,
            format=nap_format,
            encoding=nap_encoding,
            period=nap_period,
        )
        if isinstance(raw_content, bytes):
            prodagbi_target.write_bytes(raw_content)
        else:
            prodagbi_target.write_text(raw_content, encoding="utf-8")
        accounting_exports["prodagbi"] = str(prodagbi_target)

    if export_deklar and processed_invoices:
        from accounting_export import generate_vat_declaration, invoice_to_nap_entry
        pur_entries = [invoice_to_nap_entry(i, period=nap_period) for i in processed_invoices]
        decl = generate_vat_declaration(purchase_entries=pur_entries, period=nap_period or "202608")
        deklar_target = Path(deklar_file) if deklar_file else (out_dir / "DEKLAR.TXT")
        deklar_target.parent.mkdir(parents=True, exist_ok=True)
        deklar_bytes = decl.to_nap_deklar_txt(encoding=nap_encoding)
        if isinstance(deklar_bytes, bytes):
            deklar_target.write_bytes(deklar_bytes)
        else:
            deklar_target.write_text(deklar_bytes, encoding="utf-8")
        accounting_exports["deklar"] = str(deklar_target)

    if export_nap_package and processed_invoices:
        from accounting_export import export_nap_package as run_export_nap_package
        pkg_res = run_export_nap_package(
            purchase_invoices=processed_invoices,
            period=nap_period or "202608",
            output_dir=out_dir,
            format=nap_format,
            encoding=nap_encoding,
            auto_generate_protocols=auto_protocol_117,
            create_zip=True,
        )
        for k, v in pkg_res.get("exported_files", {}).items():
            accounting_exports[f"nap_pkg_{k}"] = v
        batch_summary["nap_package_audit"] = {
            "status": pkg_res.get("status"),
            "protocols_count": pkg_res.get("protocols_count"),
            "consistency_issues": pkg_res.get("consistency_issues"),
            "declaration": pkg_res.get("declaration"),
        }

    if verify_contractors and processed_invoices:
        from contractor_verification import verify_contractor
        verifications: dict[str, Any] = {}
        for inv in processed_invoices:
            if inv.supplier and (inv.supplier.eik or inv.supplier.vat_number):
                ident = inv.supplier.eik or inv.supplier.vat_number
                if ident not in verifications:
                    tax_dt = inv.invoice_metadata.date_tax_event or inv.invoice_metadata.date_issued
                    v_res = verify_contractor(ident, date_tax_event=tax_dt)
                    verifications[ident] = v_res.to_dict()
        batch_summary["contractor_verifications"] = verifications

    if accounting_exports:
        batch_summary["accounting_exports"] = accounting_exports

    target_sum_file = Path(summary_file) if summary_file else (out_dir / "batch_summary.json")
    target_sum_file.parent.mkdir(parents=True, exist_ok=True)
    target_sum_file.write_text(
        json.dumps(batch_summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return batch_summary

