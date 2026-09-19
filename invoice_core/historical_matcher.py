"""Backward-compatibility facade for invoice_core.accounting.historical_matcher."""
from __future__ import annotations

import sys
from .accounting import historical_matcher as _mod
from .accounting.historical_matcher import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
