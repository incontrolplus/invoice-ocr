"""Backward-compatibility facade for invoice_core.ingestion.imap_poller."""
from __future__ import annotations

import sys
from .ingestion import imap_poller as _mod
from .ingestion.imap_poller import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
