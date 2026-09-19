"""Backward-compatibility facade for invoice_core.accounting.exporters.microinvest_export."""
from __future__ import annotations

import sys
from .accounting.exporters import microinvest_export as _mod
from .accounting.exporters.microinvest_export import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
