"""OCR subpackage: image preprocessing, layout analysis, table recovery, OCR passes, and Tesseract environment."""
from __future__ import annotations

from . import (
    preprocessing,
    layout,
    table_recovery,
    ocr_passes,
    tesseract_env,
)

_SUBMODULES = [preprocessing, layout, table_recovery, ocr_passes, tesseract_env]

for _mod in _SUBMODULES:
    for _k in dir(_mod):
        if not (_k.startswith("__") and _k.endswith("__")):
            if _k not in globals():
                globals()[_k] = getattr(_mod, _k)

__all__ = ['preprocessing', 'layout', 'table_recovery', 'ocr_passes', 'tesseract_env'] + [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
