"""Backward-compatibility facade for invoice_core.extraction.document_classifier."""
from __future__ import annotations

import sys
from .extraction import document_classifier as _mod
from .extraction.document_classifier import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
