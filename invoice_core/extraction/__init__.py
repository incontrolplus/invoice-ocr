"""Extraction subpackage: document classification, field extraction, financials, normalizers, validation, and data models."""
from __future__ import annotations

from . import (
    document_classifier,
    classification,
    extraction,
    financials,
    normalizers,
    validation,
    models,
)

_SUBMODULES = [document_classifier, classification, extraction, financials, normalizers, validation, models]

for _mod in _SUBMODULES:
    for _k in dir(_mod):
        if not (_k.startswith("__") and _k.endswith("__")):
            if _k not in globals():
                globals()[_k] = getattr(_mod, _k)

__all__ = ['document_classifier', 'classification', 'extraction', 'financials', 'normalizers', 'validation', 'models'] + [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
