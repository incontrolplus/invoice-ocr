"""Accounting subpackage: chart of accounts, account mapping, accounting engine, historical matcher, tax periods, compliance, and ERP exporters."""
from __future__ import annotations

from . import (
    chart_of_accounts,
    account_mapping,
    accounting_engine,
    engine,
    historical_matcher,
    tax_period_validator,
    legal_compliance,
    exporters,
)

_SUBMODULES = [chart_of_accounts, account_mapping, accounting_engine, engine, historical_matcher, tax_period_validator, legal_compliance, exporters]

for _mod in _SUBMODULES:
    for _k in dir(_mod):
        if not (_k.startswith("__") and _k.endswith("__")):
            if _k not in globals():
                globals()[_k] = getattr(_mod, _k)

__all__ = ['chart_of_accounts', 'account_mapping', 'accounting_engine', 'engine', 'historical_matcher', 'tax_period_validator', 'legal_compliance', 'exporters'] + [name for name in globals() if not (name.startswith("__") and name.endswith("__"))]
