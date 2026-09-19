"""Backward-compatibility facade for invoice_core.accounting.account_mapping."""
from __future__ import annotations

import sys
from .accounting import account_mapping as _mod
from .accounting.account_mapping import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
