"""Ingestion subpackage: document rasterization/streaming, email ingestion, IMAP polling, watchers, and notifications."""
from __future__ import annotations

from . import (
    document_ingestion,
    email_ingestion,
    imap_poller,
    watcher,
    notifications,
)

_SUBMODULES = [document_ingestion, email_ingestion, imap_poller, watcher, notifications]

for _mod in _SUBMODULES:
    for _k in dir(_mod):
        if not (_k.startswith("__") and _k.endswith("__")):
            if _k not in globals():
                globals()[_k] = getattr(_mod, _k)

__all__ = ['document_ingestion', 'email_ingestion', 'imap_poller', 'watcher', 'notifications'] + [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
