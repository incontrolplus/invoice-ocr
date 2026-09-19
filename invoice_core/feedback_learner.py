"""Backward-compatibility facade for delta_learner.

Disambiguation (M18):
- `invoice_core.delta_learner`: Analyzes Delta Pro 'СЛЕД ПРЕГЛЕД' reviewed operations.
- `invoice_core.feedback_learning`: Manages RLHF continuous learning from HITL dashboard.

Deprecated: Use `invoice_core.delta_learner` directly.
"""
from __future__ import annotations

from .delta_learner import (
    extract_corrections,
    match_operation,
    normalize_doc_number,
    persist_learned_vendor_rules,
)

__all__ = [
    "extract_corrections",
    "match_operation",
    "normalize_doc_number",
    "persist_learned_vendor_rules",
]

