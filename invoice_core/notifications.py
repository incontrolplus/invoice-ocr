"""Backward-compatibility facade for invoice_core.ingestion.notifications."""
from __future__ import annotations

import sys
from .ingestion import notifications as _mod
from .ingestion.notifications import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
