"""Backward-compatibility facade for invoice_core.accounting.tax_period_validator."""
from __future__ import annotations

import sys
from .accounting import tax_period_validator as _mod
from .accounting.tax_period_validator import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
