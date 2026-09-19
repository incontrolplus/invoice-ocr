"""Backward-compatibility facade for invoice_core.accounting.exporters.delta_pro_generator."""
from __future__ import annotations

import sys
from .accounting.exporters import delta_pro_generator as _mod
from .accounting.exporters.delta_pro_generator import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
