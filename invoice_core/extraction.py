"""Backward-compatibility facade for invoice_core.extraction.extraction."""
from __future__ import annotations

import sys
from .extraction import extraction as _mod
from .extraction.extraction import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
