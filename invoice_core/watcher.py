"""Backward-compatibility facade for invoice_core.ingestion.watcher."""
from __future__ import annotations

import sys
from .ingestion import watcher as _mod
from .ingestion.watcher import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
