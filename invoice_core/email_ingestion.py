"""Backward-compatibility facade for invoice_core.ingestion.email_ingestion."""
from __future__ import annotations

import sys
from .ingestion import email_ingestion as _mod
from .ingestion.email_ingestion import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
