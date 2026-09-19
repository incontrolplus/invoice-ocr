"""Backward-compatibility facade for invoice_core.accounting.accounting_engine."""
from __future__ import annotations

import sys
from .accounting import accounting_engine as _mod
from .accounting.accounting_engine import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
