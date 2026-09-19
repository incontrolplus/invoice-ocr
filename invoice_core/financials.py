"""Backward-compatibility facade for invoice_core.extraction.financials."""
from __future__ import annotations

import sys
from .extraction import financials as _mod
from .extraction.financials import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
