"""Backward-compatibility facade for invoice_core.accounting.legal_compliance."""
from __future__ import annotations

import sys
from .accounting import legal_compliance as _mod
from .accounting.legal_compliance import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
