"""Backward-compatibility facade for invoice_core.extraction.normalizers."""
from __future__ import annotations

import sys
from .extraction import normalizers as _mod
from .extraction.normalizers import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
