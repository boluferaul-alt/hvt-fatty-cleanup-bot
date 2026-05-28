"""
Decision tree for the HVT + Fatty weekly cleanup bot.

Inputs (collected by main.py):
  - parsed Researcher Bot Summary (note_parser.ParsedSummary, or None)
  - Zillow listing status: True / False / None  (None = undetermined)
  - Tax check result: dict with keys
        current_balance: float | None
        payment_last_12mo_amount: float | None
        last_payment_date: str | None
        error: str | None     (set if the scrape failed end-to-end)

Output: Decision(category, reason)
  category ∈ {VAULT, FLAG, STAY}

Rules (priority order — first match wins):
  1. Listed on Zillow True              → VAULT  ("Owner is selling on MLS")
  2. Payment ≥ PAYMENT_VAULT_THRESHOLD  → VAULT  ("Heat cooling: paid $X on YYYY-MM-DD")
  3. Payment > 0 but below threshold    → FLAG   ("Small payment $X, manual review")
  4. Zillow returned None + tax OK      → FLAG   ("Zillow undetermined, manual review")
  5. Tax scrape failed                  → FLAG   ("Tax check failed for county Y")
  6. No Researcher Bot Summary          → FLAG   ("Missing bot summary")
  7. All clear                          → STAY
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional


# Category constants
VAULT = "VAULT"
FLAG = "FLAG"
STAY = "STAY"

# Threshold: at/above this paid in last 12mo → VAULT, below → FLAG. Env-overridable.
PAYMENT_VAULT_THRESHOLD = float(os.getenv("PAYMENT_VAULT_THRESHOLD", "1000"))

NO_MOVE_DECISIONS = {STAY, FLAG}


@dataclass
class Decision:
    category: str       # VAULT | FLAG | STAY
    reason: str

    def __str__(self) -> str:
        return f"{self.category}: {self.reason}"


def _money(v: Optional[float]) -> str:
    if v is None:
        return "$?"
    return f"${v:,.0f}"


def decide(
    parsed: Optional[Any],
    zillow_listed: Optional[bool],
    tax_result: Optional[dict],
    *,
    payment_threshold: Optional[float] = None,
) -> Decision:
    """Apply the priority-ordered rules.

    Args:
        parsed: note_parser.ParsedSummary or None. If None, or .found is False,
                rule 6 fires (missing summary).
        zillow_listed: result of zillow_check.is_listed_for_sale.
                True = listed (rule 1 fires), False = not listed,
                None = undetermined (rule 4 candidate).
        tax_result: dict with current_balance / payment_last_12mo_amount /
                last_payment_date / error. None = scrape never ran.
        payment_threshold: override PAYMENT_VAULT_THRESHOLD for tests.
    """
    threshold = payment_threshold if payment_threshold is not None else PAYMENT_VAULT_THRESHOLD

    # ------------------------------------------------------------------
    # Rule 1 — Listed on Zillow (highest priority signal)
    # ------------------------------------------------------------------
    if zillow_listed is True:
        return Decision(
            category=VAULT,
            reason="Owner is selling on MLS (Zillow shows active listing).",
        )

    # ------------------------------------------------------------------
    # Rule 2 / 3 — recent tax payment
    # ------------------------------------------------------------------
    if tax_result and not tax_result.get("error"):
        paid = tax_result.get("payment_last_12mo_amount")
        last_date = tax_result.get("last_payment_date") or "unknown"
        if isinstance(paid, (int, float)) and paid >= threshold:
            return Decision(
                category=VAULT,
                reason=f"Heat cooling: paid {_money(paid)} on {last_date}.",
            )
        if isinstance(paid, (int, float)) and paid > 0:
            return Decision(
                category=FLAG,
                reason=(f"Small payment {_money(paid)} on {last_date}, "
                        f"below ${threshold:,.0f} threshold — manual review."),
            )

    # ------------------------------------------------------------------
    # Rule 5 — tax scrape failed entirely
    # ------------------------------------------------------------------
    if tax_result is None or tax_result.get("error"):
        county_hint = ""
        if parsed is not None and getattr(parsed, "county", ""):
            county_hint = f" for {parsed.county}"
        err = (tax_result or {}).get("error") or "scrape did not run"
        return Decision(
            category=FLAG,
            reason=f"Tax check failed{county_hint} ({err}).",
        )

    # ------------------------------------------------------------------
    # Rule 6 — missing bot summary on the lead
    # ------------------------------------------------------------------
    if parsed is None or not getattr(parsed, "found", False):
        return Decision(
            category=FLAG,
            reason="Missing bot summary, can't address-lookup.",
        )

    # ------------------------------------------------------------------
    # Rule 4 — Zillow undetermined (after tax checks have ruled out
    # the higher-priority VAULT signals). Tax check returned OK.
    # ------------------------------------------------------------------
    if zillow_listed is None:
        return Decision(
            category=FLAG,
            reason="Zillow undetermined (captcha / no result), manual review.",
        )

    # ------------------------------------------------------------------
    # Rule 7 — All clear, leave in pipeline
    # ------------------------------------------------------------------
    return Decision(
        category=STAY,
        reason="No listing, no recent payment — keep working it.",
    )


def render_row(
    lead: dict,
    parsed: Optional[Any],
    zillow_listed: Optional[bool],
    tax_result: Optional[dict],
    decision: Decision,
    move_result: str,
    source_pipeline: str = "",
) -> dict:
    """Shape per-lead data for the Slack table."""
    name = ""
    if lead:
        name = f"{lead.get('firstName', '')} {lead.get('lastName', '')}".strip()
    if not name and parsed is not None:
        name = getattr(parsed, "owner_name", "") or ""

    address = ""
    if parsed is not None:
        address = getattr(parsed, "property_address", "") or ""

    value = None
    if parsed is not None:
        for v in (getattr(parsed, "assessed_value", None),
                  getattr(parsed, "market_value", None),
                  getattr(parsed, "zillow_zestimate", None)):
            if v and v > 0:
                value = v
                break

    return {
        "lead_id": (lead or {}).get("leadId") or (lead or {}).get("id"),
        "name": name,
        "address": address,
        "county": getattr(parsed, "county", "") if parsed is not None else "",
        "owed": getattr(parsed, "total_owed", None) if parsed is not None else None,
        "value": value,
        "source_pipeline": source_pipeline,
        "zillow_listed": zillow_listed,
        "tax_paid_12mo": (tax_result or {}).get("payment_last_12mo_amount"),
        "tax_last_payment_date": (tax_result or {}).get("last_payment_date"),
        "tax_error": (tax_result or {}).get("error"),
        "decision": decision.category,
        "reason": decision.reason,
        "move_result": move_result,
    }
