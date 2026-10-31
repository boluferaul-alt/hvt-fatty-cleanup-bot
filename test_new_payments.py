"""Tests for tax_watch.new_payments and the note listing every new payment.

Run: python test_new_payments.py   (no network / API keys needed)
"""
from __future__ import annotations

from types import SimpleNamespace

from county_adapters import Payment
from tax_watch import build_note, new_payments


def pay(date, amount, payer="ROSELLA RODRIGUEZ"):
    return Payment(date=date, amount=amount, tax_year="2025", kind="Payment", payer=payer)


def note(text):
    return [{"content": text}]


def main() -> None:
    # Rodriguez: the 10/24 note named the $200, the older $500 must not come back as new.
    recent = [pay("2026-10-22", 200.0), pay("2026-10-20", 500.0)]
    old = note("TAX UPDATE 10/24/26 - TAXES PAID: $200.00 on 10/22/26 by ROSELLA RODRIGUEZ.")
    assert new_payments(recent, old, set(), 1) == []

    # Arredondo: two receipts on one day, the unnamed one is not new either.
    same_day = [pay("2026-10-20", 2947.0, "A"), pay("2026-10-20", 1053.0, "A")]
    assert new_payments(same_day, note("PAID: $2,947.00 on 10/20/26 by A."), set(), 1) == []

    # A later payment is still new.
    later = [pay("2026-10-29", 300.0)] + recent
    assert [p.amount for p in new_payments(later, old, set(), 1)] == [300.0]

    # Nothing reported yet: all new, and the note names each one so none returns next week.
    fresh = new_payments(recent, [], set(), 1)
    assert len(fresh) == 2
    st = SimpleNamespace(county="EL PASO", total_due=100.0, active_lawsuit="")
    text = build_note(st, fresh, "10/31/26")
    assert "$200.00 on 10/22/26" in text and "$500.00 on 10/20/26" in text
    assert new_payments(recent, note(text), set(), 1) == []

    print("ALL PASS")


if __name__ == "__main__":
    main()
