"""ERP exporters subpackage: Delta Pro generator, Microinvest, Ajur, and Business Navigator exporters."""
from __future__ import annotations

from . import (
    delta_pro_generator,
    microinvest_export,
    ajur_export,
    business_navigator_export,
)

_SUBMODULES = [delta_pro_generator, microinvest_export, ajur_export, business_navigator_export]

for _mod in _SUBMODULES:
    for _k in dir(_mod):
        if not (_k.startswith("__") and _k.endswith("__")):
            if _k not in globals():
                globals()[_k] = getattr(_mod, _k)

__all__ = ['delta_pro_generator', 'microinvest_export', 'ajur_export', 'business_navigator_export'] + [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
