"""Backward-compatibility facade for invoice_core.workers.batch."""
from __future__ import annotations

import sys
from .workers import batch as _mod
from .workers.batch import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
