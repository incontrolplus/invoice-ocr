#!/usr/bin/env python3
"""invoice_ocr.py — Production-grade Bulgarian Invoice OCR Pipeline.

=================================================================
Accepts a Bulgarian invoice image (.png, .jpg, .jpeg) or PDF (.pdf),
performs OCR with Tesseract, applies coordinate-based layout analysis,
extracts structured invoice fields, validates mathematical/structural/currency
consistency, and outputs strictly-defined JSON to stdout.

Unified top-level module preserving 100% backward compatibility for all
existing scripts, tests, and external importers while delegating the
entire implementation to the modular `invoice_core` package.
"""
from __future__ import annotations

import sys
import types
import invoice_core
from invoice_core import *
from invoice_core.cli import main

# Re-export all internal/private symbols from invoice_core for full backward compatibility
for _name in dir(invoice_core):
    if not _name.startswith("__"):
        globals()[_name] = getattr(invoice_core, _name)


class _InvoiceOcrModule(types.ModuleType):
    """Dynamic module proxy ensuring mock/patch/monkeypatch on invoice_ocr propagates to invoice_core."""

    def __getattr__(self, name: str):
        if hasattr(invoice_core, name):
            return getattr(invoice_core, name)
        raise AttributeError(f"module 'invoice_ocr' has no attribute {name!r}")

    def __setattr__(self, name: str, value):
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        if hasattr(invoice_core, name):
            try:
                setattr(invoice_core, name, value)
            except Exception:
                pass
        for mod in list(sys.modules.values()):
            if mod and getattr(mod, "__name__", "").startswith("invoice_core."):
                if hasattr(mod, name):
                    try:
                        setattr(mod, name, value)
                    except Exception:
                        pass


# Install the proxy class onto this module instance
sys.modules[__name__].__class__ = _InvoiceOcrModule

if __name__ == "__main__":
    sys.exit(main())
