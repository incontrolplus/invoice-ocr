"""Backward-compatibility facade for invoice_core.verification.partner_verification."""
from __future__ import annotations

import sys
from .verification import partner_verification as _mod
from .verification.partner_verification import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
