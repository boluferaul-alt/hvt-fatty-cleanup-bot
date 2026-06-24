"""
Regression tests for hvt_fatty_decision.decide() — built from Raul's hand
review of real leads (2026-06-24). Each case encodes the lead's actual signals
and asserts the bot now routes where Raul did.

    python test_decisions.py
"""
from __future__ import annotations
import sys
from types import SimpleNamespace
from dataclasses import dataclass
from typing import Optional

from hvt_fatty_decision import (decide, DNC, HVT, VAULT, OCC_ALIVE, STAY, REVIEW,
                                _best_value, _est_net)


def P(**kw):
    base = dict(found=True, owner_name="", county="", property_address="",
                total_owed=None, has_lawsuit=False, assessed_value=None,
                market_value=None, zillow_zestimate=None, owner_status="alive",
                deceased_confirmed=False, occupancy="", is_vacant=False,
                is_vacant_land=False, is_entity=False)
    base.update(kw)
    return SimpleNamespace(**base)

def L(listed=False, site=None, kind=None):
    return {"listed": listed, "site": site, "kind": kind}

def T(current_due=None, paid=0, date=None, payer="", err=None):
    return {"current_due": current_due, "payment_recent_amount": paid,
            "last_payment_date": date, "payer_name": payer, "error": err}


@dataclass
class Case:
    label: str; expected: str
    parsed: object; listing: dict; tax: dict


CASES = [
    Case("Psillas — listed for sale (verified)", DNC,
         P(owner_name="PSILLAS NICHOLAS", assessed_value=120000, total_owed=4000),
         L(True, "HAR", "sale"), T(current_due=4000)),
    Case("Roman — taxes fully paid ($0 due)", DNC,
         P(owner_name="ROMAN JOSE", assessed_value=110000, total_owed=3000),
         L(False), T(current_due=0, paid=2917)),
    Case("Anderson — owes $125K of $420K, tax lawsuit, tiny payments", STAY,
         P(owner_name="ANDERSON JAMES", assessed_value=420000, total_owed=125000, has_lawsuit=True),
         L(False), T(current_due=125000, paid=700)),
    Case("Threadgill — deceased, vacant, $273K value, $10K owed", HVT,
         P(owner_name="THREADGILL WILLIAM", assessed_value=273000, total_owed=10000,
           deceased_confirmed=True, is_vacant=True),
         L(False), T(current_due=10000)),
    Case("Sanders — deceased, $175K value, $11K owed, lawsuit (heir occupied)", HVT,
         P(owner_name="SANDERS GRACE", assessed_value=175000, total_owed=11000,
           deceased_confirmed=True, has_lawsuit=True),
         L(False), T(current_due=11000, paid=300)),
    Case("Hilliard — $87K value, $12K owed -> thin margin", DNC,
         P(owner_name="HILLIARD ODIS", assessed_value=87000, total_owed=12000),
         L(False), T(current_due=12000)),
    Case("White — deceased, $90K value, $3K owed -> thin but deceased", OCC_ALIVE,
         P(owner_name="WHITE MARIE", assessed_value=90000, total_owed=3000, deceased_confirmed=True),
         L(False), T(current_due=3000, paid=600)),
    Case("Canterbury — $398K value, only $3,960 owed, alive", DNC,
         P(owner_name="CANTERBURY EARNEST", assessed_value=398000, total_owed=3960),
         L(False), T(current_due=3960, paid=500)),
    Case("Mendez — $79K value, $6,600 owed -> thin margin", DNC,
         P(owner_name="MENDEZ SYLVESTER", assessed_value=79000, total_owed=6600),
         L(False), T(current_due=6600, paid=1114)),
    Case("Rivera — $430K value, $6K owed, big recent payment, alive", DNC,
         P(owner_name="RIVERA JOSE", assessed_value=430000, total_owed=6000),
         L(False), T(current_due=6000, paid=3720)),
    Case("Lino — $439K value, $18K owed, 3yr behind, alive/paying", OCC_ALIVE,
         P(owner_name="LINO CLEOTILDE", assessed_value=439000, total_owed=18000),
         L(False), T(current_due=18000, paid=5952)),
    Case("Livingston — estate, $3K paid recently -> intent + oddity", VAULT,
         P(owner_name="LIVINGSTON MARIAN E ESTATE", assessed_value=140000, total_owed=9000),
         L(False), T(current_due=9000, paid=3000, payer="Dana Anderson")),
    Case("Willie Doris Trust — owes ~$10K, only <$1K paid -> still motivated", STAY,
         P(owner_name="WILLIE DORIS REV LD TRUST", assessed_value=110000, total_owed=10000),
         L(False), T(current_due=10000, paid=565)),
    Case("Hines — NOT listed (off-market), $45K owed of $400K, no payment", STAY,
         P(owner_name="HINES MYRTLE", assessed_value=400000, total_owed=45000),
         L(False), T(current_due=45000, paid=0)),
    Case("Listing undetermined + still owes -> not auto-killed (STAY)", STAY,
         P(owner_name="X", assessed_value=200000, total_owed=20000),
         L(None), T(current_due=20000, paid=0)),
    # ---- 2026-06-24 feedback: three fixes ----
    Case("Below $60K net + deceased + VACANT -> DNC (math wins, not Occ-Alive)", DNC,
         P(owner_name="THIN DECEASED", assessed_value=70000, total_owed=5000,
           deceased_confirmed=True, is_vacant=True),
         L(False), T(current_due=5000)),
    Case("Multi-property: $0 on one parcel but Researcher total $8K, no big payment -> still owes (HVT)", HVT,
         P(owner_name="MULTIPROP DECEASED", assessed_value=200000, total_owed=8000,
           deceased_confirmed=True, is_vacant=True),
         L(False), T(current_due=0, paid=0)),
    Case("Genuinely paid off: $0 due + payment covered the total -> DNC (paid)", DNC,
         P(owner_name="ROMAN PAID", assessed_value=110000, total_owed=3000),
         L(False), T(current_due=0, paid=2917)),
    Case("Misread value ($30K) below taxes owed ($45K) -> REVIEW (bad value)", REVIEW,
         P(owner_name="BADVALUE", assessed_value=30000, total_owed=45000),
         L(False), T(current_due=45000)),
    Case("Finkelstein — value misread as $11,784 (implausibly low) -> REVIEW", REVIEW,
         P(owner_name="FINKELSTEIN", assessed_value=11784, total_owed=9715),
         L(False), T(current_due=9715)),
]


def main() -> int:
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
    p = f = 0
    for c in CASES:
        d = decide(c.parsed, c.listing, c.tax)
        ok = d.category == c.expected
        print(f"  [{'PASS' if ok else 'FAIL'}] {c.label}")
        print(f"         expected={c.expected}  got={d.category} ({d.confidence}) — {d.reason}")
        p, f = (p+1, f) if ok else (p, f+1)
    # --- profit-math unit checks ---
    print("\n  Profit-math checks:")
    checks = [
        ("$100K value, $10K owed -> $71K net (Raul's example)",
         round(_est_net(100000, 10000)) == 71000),
        ("net < $60K target flags (87K val, 12K owed -> 56,780)",
         _est_net(87000, 12000) < 60000),
        ("value: far apart (100K vs 200K) -> average 150K",
         _best_value(P(assessed_value=100000, zillow_zestimate=200000)) == 150000),
        ("value: close (100K vs 110K) -> lower 100K",
         _best_value(P(assessed_value=100000, zillow_zestimate=110000)) == 100000),
        ("value: only assessed present -> that value",
         _best_value(P(assessed_value=95000)) == 95000),
    ]
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        p, f = (p+1, f) if ok else (p, f+1)
    print(f"\n  {p} passed, {f} failed of {len(CASES)+len(checks)}")
    return 0 if f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
