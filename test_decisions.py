"""
Regression tests for hvt_fatty_decision.decide().

Each case feeds synthetic inputs (parsed summary + Zillow status + tax result)
and asserts the expected category. Run:

    python test_decisions.py

Exit code 0 if all pass, 1 if any fail.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional, Any

from note_parser import parse_summary
from hvt_fatty_decision import (
    decide,
    VAULT, FLAG, STAY,
)


@dataclass
class Case:
    label: str
    expected: str
    note: str                                # Researcher Bot Summary text (or "" for missing)
    zillow_listed: Optional[bool]            # True/False/None
    tax_result: Optional[dict]               # dict or None


# Sample note used across the "happy-path" cases. Big-value, dead-owner,
# vacant — exactly the kind of lead that should be sitting in HVT/Fatty.
SAMPLE_NOTE = """SUMMARY REPORT
County: Brazoria
Owner: GUERRERO CLOTILDE ESTATE
Property: 309 AVENUE E 1/2, ALVIN
Type: SFR
Total Taxes Owed: $6,525.59
Total Back Years Tax: 3
Tax Lawsuit: NO
County Assessed: $102,520
Occupancy: VACANT
Deceased
"""


CASES: list[Case] = [
    Case(
        label="Active Zillow listing → VAULT",
        expected=VAULT,
        note=SAMPLE_NOTE,
        zillow_listed=True,
        tax_result={
            "current_balance": 6525.59,
            "payment_last_12mo_amount": 0,
            "last_payment_date": None,
            "error": None,
        },
    ),
    Case(
        label="$2,400 paid in last 12mo → VAULT",
        expected=VAULT,
        note=SAMPLE_NOTE,
        zillow_listed=False,
        tax_result={
            "current_balance": 4125.59,
            "payment_last_12mo_amount": 2400.00,
            "last_payment_date": "2026-02-15",
            "error": None,
        },
    ),
    Case(
        label="$300 paid in last 12mo → FLAG (small payment)",
        expected=FLAG,
        note=SAMPLE_NOTE,
        zillow_listed=False,
        tax_result={
            "current_balance": 6225.59,
            "payment_last_12mo_amount": 300.00,
            "last_payment_date": "2026-01-10",
            "error": None,
        },
    ),
    Case(
        label="No listing + no payment → STAY",
        expected=STAY,
        note=SAMPLE_NOTE,
        zillow_listed=False,
        tax_result={
            "current_balance": 6525.59,
            "payment_last_12mo_amount": 0,
            "last_payment_date": None,
            "error": None,
        },
    ),
    Case(
        label="Zillow returned None + no payment → FLAG (undetermined)",
        expected=FLAG,
        note=SAMPLE_NOTE,
        zillow_listed=None,
        tax_result={
            "current_balance": 6525.59,
            "payment_last_12mo_amount": 0,
            "last_payment_date": None,
            "error": None,
        },
    ),
    Case(
        label="Missing summary note → FLAG",
        expected=FLAG,
        note="",                                # parse_summary returns found=False
        zillow_listed=False,
        tax_result={
            "current_balance": None,
            "payment_last_12mo_amount": 0,
            "last_payment_date": None,
            "error": None,
        },
    ),
    Case(
        label="Tax scrape failed → FLAG",
        expected=FLAG,
        note=SAMPLE_NOTE,
        zillow_listed=False,
        tax_result={
            "current_balance": None,
            "payment_last_12mo_amount": None,
            "last_payment_date": None,
            "error": "exception:TimeoutError:network",
        },
    ),
    Case(
        label="Tax scrape never ran (None) → FLAG",
        expected=FLAG,
        note=SAMPLE_NOTE,
        zillow_listed=False,
        tax_result=None,
    ),
]


def main() -> int:
    # Force UTF-8 stdout so em-dashes / arrows in test labels don't
    # crash on Windows cp1252.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    passed = failed = 0
    for c in CASES:
        parsed = parse_summary(c.note) if c.note else parse_summary("")
        d = decide(parsed, c.zillow_listed, c.tax_result)
        ok = d.category == c.expected
        tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {c.label}")
        print(f"         expected={c.expected}  got={d.category}")
        if not ok:
            print(f"         reason: {d.reason}")
            failed += 1
        else:
            passed += 1
    print()
    print(f"  {passed} passed, {failed} failed (of {len(CASES)} total)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
