"""Backward-compatibility facade for invoice_core.ocr.ocr_passes."""
from __future__ import annotations

import sys
from .ocr import ocr_passes as _mod
from .ocr.ocr_passes import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
