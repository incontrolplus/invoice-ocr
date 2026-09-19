"""Verification subpackage: contractor and partner verification, VIES, and N8N sync."""
from __future__ import annotations

from . import (
    partner_verification,
)

_SUBMODULES = [partner_verification]

for _mod in _SUBMODULES:
    for _k in dir(_mod):
        if not (_k.startswith("__") and _k.endswith("__")):
            if _k not in globals():
                globals()[_k] = getattr(_mod, _k)

__all__ = ['partner_verification'] + [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
