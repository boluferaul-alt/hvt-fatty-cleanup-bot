"""Tests for completion_contract — the gate that forces live tax verification.

Run: python test_contract.py   (no network / API keys needed)
"""
from __future__ import annotations

import completion_contract as cc

TAX = cc.ArtifactSpec(
    key="tax",
    label="live county tax check",
    fields=("current_due",),
    source="tax_source_url",
    verified="tax_verified_at",
)
SPECS = [TAX]


def _t(name, cond):
    print(("PASS" if cond else "FAIL") + "  " + name)
    return bool(cond)


def main() -> None:
    ok = True

    # 1. Fully verified record is decidable.
    verified = {"current_due": 19407, "tax_source_url": "https://libertycountytax.com/acct/25583",
                "tax_verified_at": "2026-06-30T15:00:00Z"}
    ok &= _t("verified record is decidable", cc.is_decidable(verified, SPECS))

    # 2. A $0 live balance still counts as verified (0 is a real answer).
    paid = {"current_due": 0, "tax_source_url": "https://x", "tax_verified_at": "now"}
    ok &= _t("$0 live balance is decidable (0 is a real answer)", cc.is_decidable(paid, SPECS))

    # 3. Note-only record (no live source / timestamp) is BLOCKED — the shortcut.
    note_only = {"current_due": 19407}  # came from the researcher note, never verified live
    ok &= _t("note-only record is NOT decidable", not cc.is_decidable(note_only, SPECS))
    reasons = cc.missing_reasons(note_only, SPECS)
    ok &= _t("note-only reason names the live source + verification", any("no live source" in r for r in reasons))

    # 4. assert_decidable raises on a blocked record.
    raised = False
    try:
        cc.assert_decidable(note_only, SPECS)
    except cc.BlockedError:
        raised = True
    ok &= _t("assert_decidable raises BlockedError on unverified record", raised)

    # 5. Missing the balance is blocked even with a source.
    no_bal = {"tax_source_url": "https://x", "tax_verified_at": "now"}
    ok &= _t("missing current_due is blocked", not cc.is_decidable(no_bal, SPECS))

    # 6. audit() never lets blocked work read as complete.
    records = [
        cc.mark_verified(dict(verified)),
        cc.mark_verified(dict(paid)),
        cc.block({}, "live county tax check: Bell County uses reCAPTCHA — needs Edge/human"),
        cc.block({}, "live county tax check: no county on the lead"),
        cc.block({}, "live county tax check: Bell County uses reCAPTCHA — needs Edge/human"),
    ]
    a = cc.audit(records)
    ok &= _t("audit counts 2 verified", a["verified"] == 2)
    ok &= _t("audit counts 3 blocked", a["blocked"] == 3)
    ok &= _t("audit is NOT complete when any blocked", a["complete"] is False)
    ok &= _t("audit groups reasons (reCAPTCHA twice)", a["blocked_reasons"].get(
        "live county tax check: Bell County uses reCAPTCHA — needs Edge/human") == 2)
    print("\n" + ("ALL PASS" if ok else "SOME FAILED"))
    print("audit_line ->", cc.audit_line(a))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
