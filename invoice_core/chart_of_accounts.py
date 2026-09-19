"""Backward-compatibility facade for invoice_core.accounting.chart_of_accounts."""
from __future__ import annotations

import sys
from .accounting import chart_of_accounts as _mod
from .accounting.chart_of_accounts import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
