"""Workers subpackage: OCR process pool, batch processing, master pipeline, and caching."""
from __future__ import annotations

from . import (
    worker_pool,
    batch,
    pipeline,
    cache,
)

_SUBMODULES = [worker_pool, batch, pipeline, cache]

for _mod in _SUBMODULES:
    for _k in dir(_mod):
        if not (_k.startswith("__") and _k.endswith("__")):
            if _k not in globals():
                globals()[_k] = getattr(_mod, _k)

__all__ = ['worker_pool', 'batch', 'pipeline', 'cache'] + [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
