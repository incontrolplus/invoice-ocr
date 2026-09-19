"""Backward-compatibility facade for invoice_core.ocr.layout."""
from __future__ import annotations

import sys
from .ocr import layout as _mod
from .ocr.layout import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
