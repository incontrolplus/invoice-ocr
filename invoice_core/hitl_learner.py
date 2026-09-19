"""Alias for `invoice_core.feedback_learning` (HITL RLHF learner).

Disambiguation (M18):
- `invoice_core.hitl_learner` (alias for `feedback_learning`): RLHF continuous learning from HITL dashboard.
- `invoice_core.delta_learner`: Analyzes Delta Pro 'СЛЕД ПРЕГЛЕД' reviewed operations.
"""
from __future__ import annotations

from .feedback_learning import (
    deduce_invoice_number_rules,
    extract_company_keywords,
    get_feedback_statistics,
    normalize_bbox,
    process_human_feedback,
    update_or_create_vendor_profile,
)

__all__ = [
    "deduce_invoice_number_rules",
    "extract_company_keywords",
    "get_feedback_statistics",
    "normalize_bbox",
    "process_human_feedback",
    "update_or_create_vendor_profile",
]
