"""Backward-compatibility facade for invoice_core.accounting.exporters.business_navigator_export."""
from __future__ import annotations

import sys
from .accounting.exporters import business_navigator_export as _mod
from .accounting.exporters.business_navigator_export import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
