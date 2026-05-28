"""Pure dataclasses — no third-party imports.

Lifted from lofty-overdue-bot/src/models.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Payment:
    date: str | None = None
    amount: float | None = None


@dataclass
class TaxRecord:
    found: bool = False
    total_due: float | None = None
    paid_last_12_months: float | None = None
    payments: list[Payment] = field(default_factory=list)
    raw_notes: str = ""
    error: str | None = None
