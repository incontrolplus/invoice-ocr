"""Backward-compatibility facade for invoice_core.ocr.tesseract_env."""
from __future__ import annotations

import sys
from .ocr import tesseract_env as _mod
from .ocr.tesseract_env import *

for _k in dir(_mod):
    if not (_k.startswith("__") and _k.endswith("__")):
        globals()[_k] = getattr(_mod, _k)

sys.modules[__name__] = _mod
