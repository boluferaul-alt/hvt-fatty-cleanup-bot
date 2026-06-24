"""
Decision engine for the HVT / Fatty / Hot Occ Alive cleanup bot.

Rebuilt 2026-06-24 from Raul's hand review of 467 leads. The old logic was
"recent tax payment OR listed -> remove." Raul's real logic is about how much
MOTIVATION is left, judged from several signals together, and routes to FIVE
destinations — not two.

DESTINATIONS (recommendation):
  DNC        Do Not Contact  — dead lead (listed / fully paid / no motivation / margin too thin)
  HVT        High Value Target — deceased owner + value + taxes owed (title/heir play)
  VAULT      Park it — some intent to pay + an oddity (estate, a third party paying)
  OCC_ALIVE  Occupied/Alive — alive & paying, low priority, keep in the system
  STAY       Keep working it in the current pipeline (still motivated)
  REVIEW     Can't decide automatically — hand to Raul with the data laid out

KEY RULES LEARNED FROM RAUL:
  1. Listed on the market (VERIFIED active) -> DNC. Realtors block deep discounts.
  2. Taxes fully paid / $0 due -> DNC (automatic).
  3. Profit math gate: net ~= SALE_FACTOR*value - owed - HEIR - ATTORNEY - COMMISSION%.
     If net < MIN_NET -> too thin. Deceased/heir-occupied -> OCC_ALIVE, else DNC.
  4. Deceased owner + decent value + still owes -> HVT (even if a relative paid
     some taxes — that often signals a title/heir issue, which is the play).
  5. Active tax lawsuit + still owes -> STAY. Strong motivation; never auto-kill.
  6. Substantial recent payment: low owed/value ratio + alive -> DNC (no pain);
     otherwise -> OCC_ALIVE (paying but keep in system).
  7. Very low owed/value ratio + alive, no big payment -> DNC; mildly low -> OCC_ALIVE.
  8. Small payments against a big balance still owed -> STAY ("at that pace they
     never catch up — still motivated").
  9. Estate / third-party payer + intent to pay -> VAULT.

Signals come from note_parser.ParsedSummary (assessed_value, total_owed,
has_lawsuit, owner_status/deceased_confirmed, occupancy/is_vacant, is_entity,
owner_name) + the live tax_result (current_due, payment_recent_amount, payer).
When the data needed for a confident call is missing, we return REVIEW.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Optional


# Destination constants
DNC = "DNC"
HVT = "HVT"
VAULT = "VAULT"
OCC_ALIVE = "OCC_ALIVE"
STAY = "STAY"
REVIEW = "REVIEW"

# Tunable thresholds (env-overridable, no redeploy needed)
SALE_FACTOR        = float(os.getenv("SALE_FACTOR", "0.80"))      # likely sale price vs assessed
HEIR_COST          = float(os.getenv("HEIR_COST", "8000"))
ATTORNEY_COST      = float(os.getenv("ATTORNEY_COST", "7000"))
COMMISSION_PCT     = float(os.getenv("COMMISSION_PCT", "0.06"))
MIN_NET_PROFIT     = float(os.getenv("MIN_NET_PROFIT", "50000"))  # Raul's floor
HVT_MIN_VALUE      = float(os.getenv("HVT_MIN_VALUE", "80000"))
SUBSTANTIAL_PAYMENT= float(os.getenv("SUBSTANTIAL_PAYMENT", "1000"))
DEAD_RATIO         = float(os.getenv("DEAD_RATIO", "0.02"))       # owed/value below this = no motivation
LOW_RATIO          = float(os.getenv("LOW_RATIO", "0.04"))        # below this = limited motivation

# Destinations the bot will not auto-move (even with AUTO_MOVE=1) — judgment calls
NO_MOVE_DECISIONS = {STAY, REVIEW}


@dataclass
class Decision:
    category: str            # one of the destination constants
    reason: str
    confidence: str = "MED"  # HIGH (rule-clear) | MED (heuristic) | LOW (thin data)
    flag_type: str = ""      # listed | tax_paid | deceased | thin_margin | low_motivation | lawsuit | partial | ""

    def __str__(self) -> str:
        return f"{self.category}: {self.reason}"


def _money(v: Optional[float]) -> str:
    return "$?" if v is None else f"${v:,.0f}"


def _best_value(parsed) -> Optional[float]:
    for v in (getattr(parsed, "assessed_value", None),
              getattr(parsed, "market_value", None),
              getattr(parsed, "zillow_zestimate", None)):
        if v and v > 0:
            return float(v)
    return None


def _is_deceased(parsed) -> bool:
    return bool(getattr(parsed, "deceased_confirmed", False)
                or (getattr(parsed, "owner_status", "") or "").lower() == "deceased")


def _is_vacant(parsed) -> bool:
    return bool(getattr(parsed, "is_vacant", False)
                or getattr(parsed, "is_vacant_land", False)
                or (getattr(parsed, "occupancy", "") or "").upper() == "VACANT")


def _est_net(value: Optional[float], owed: float) -> Optional[float]:
    if value is None:
        return None
    sale = SALE_FACTOR * value
    return sale - owed - HEIR_COST - ATTORNEY_COST - COMMISSION_PCT * sale


def decide(
    parsed: Optional[Any],
    listing: Optional[dict],
    tax_result: Optional[dict],
) -> Decision:
    """Apply Raul's priority-ordered framework. See module docstring."""
    listing = listing or {}
    tax_result = tax_result or {}

    # ---- gather signals ----
    value = _best_value(parsed) if parsed is not None else None
    owed_note = getattr(parsed, "total_owed", None) if parsed is not None else None
    current_due = tax_result.get("current_due")
    owed = current_due if isinstance(current_due, (int, float)) else owed_note
    paid = tax_result.get("payment_recent_amount")
    lawsuit = bool(getattr(parsed, "has_lawsuit", False)) if parsed is not None else False
    deceased = _is_deceased(parsed) if parsed is not None else False
    vacant = _is_vacant(parsed) if parsed is not None else False
    owner = (getattr(parsed, "owner_name", "") if parsed is not None else "") or ""
    is_estate = bool(re.search(r"\b(estate|est|trust|life est|lf est)\b", owner, re.I))
    payer = (tax_result.get("payer_name") or "").strip()
    ratio = (owed / value) if (value and owed is not None) else None
    net = _est_net(value, owed) if owed is not None else None

    # ------------------------------------------------------------------
    # 1. Listed on the market (VERIFIED active) -> DNC
    # ------------------------------------------------------------------
    if listing.get("listed") is True:
        site = listing.get("site") or "the market"
        kind = listing.get("kind") or "sale"
        return Decision(DNC, f"Listed for {kind} on {site} — realtors block our deep discounts.",
                        "HIGH", "listed")

    # ------------------------------------------------------------------
    # 2. Taxes fully paid / nothing owed -> DNC
    # ------------------------------------------------------------------
    if owed is not None and owed <= 0:
        return Decision(DNC, "All taxes paid in full ($0 due) — no motivation.", "HIGH", "tax_paid")

    # ------------------------------------------------------------------
    # If we can't even read taxes owed, we can't reason -> REVIEW
    # ------------------------------------------------------------------
    if owed is None:
        if tax_result.get("error"):
            return Decision(REVIEW, f"Tax check failed ({tax_result['error']}).", "LOW")
        if parsed is None or not getattr(parsed, "found", False):
            return Decision(REVIEW, "No Researcher Summary — can't look up taxes.", "LOW")
        return Decision(REVIEW, "Couldn't read taxes owed — verify manually.", "LOW")

    # ------------------------------------------------------------------
    # 3. Profit-math gate — too thin after heirs/attorney/closing
    # ------------------------------------------------------------------
    if net is not None and net < MIN_NET_PROFIT:
        if deceased or vacant:
            return Decision(OCC_ALIVE,
                f"Thin spread (est. net {_money(net)} < {_money(MIN_NET_PROFIT)}) but deceased/heir case — keep low-priority.",
                "MED", "thin_margin")
        return Decision(DNC,
            f"Spread too thin — est. net {_money(net)} after heirs/attorney/6% (target {_money(MIN_NET_PROFIT)}).",
            "MED", "thin_margin")

    # ------------------------------------------------------------------
    # 4. Deceased owner + value + still owes -> HVT (title/heir play)
    # ------------------------------------------------------------------
    if deceased and value is not None and value >= HVT_MIN_VALUE and owed > 0:
        extra = " (taxes paid by a third party — likely title/heir issue)" if (paid and paid > 0) else ""
        vtag = "vacant " if vacant else ""
        return Decision(HVT,
            f"Deceased owner + {vtag}{_money(value)} value, {_money(owed)} owed — prime HVT{extra}.",
            "HIGH", "deceased")

    # ------------------------------------------------------------------
    # 5. Active tax lawsuit + still owes -> STAY (strong motivation)
    # ------------------------------------------------------------------
    if lawsuit and owed > 0:
        return Decision(STAY,
            f"Active tax lawsuit + {_money(owed)} owed — still motivated, keep working.",
            "HIGH", "lawsuit")

    # ------------------------------------------------------------------
    # 9. Estate / third-party payer + intent to pay -> VAULT
    # ------------------------------------------------------------------
    if (is_estate or (payer and owner and payer.split()[0].upper() not in owner.upper())) \
            and isinstance(paid, (int, float)) and paid >= SUBSTANTIAL_PAYMENT:
        return Decision(VAULT,
            f"Intent to pay ({_money(paid)} recently) but estate/third-party payer — park in Vault.",
            "MED", "tax_paid")

    # ------------------------------------------------------------------
    # 6. Substantial recent payment
    # ------------------------------------------------------------------
    if isinstance(paid, (int, float)) and paid >= SUBSTANTIAL_PAYMENT:
        if ratio is not None and ratio < LOW_RATIO:
            return Decision(DNC,
                f"Paid {_money(paid)} recently; only {_money(owed)} owed vs {_money(value)} value — low motivation.",
                "MED", "tax_paid")
        return Decision(OCC_ALIVE,
            f"Paid {_money(paid)} recently and looks active — low priority, keep in Occupied/Alive.",
            "MED", "tax_paid")

    # ------------------------------------------------------------------
    # 7. Low owed/value ratio (no big payment) — limited motivation
    # ------------------------------------------------------------------
    if ratio is not None and ratio < DEAD_RATIO:
        return Decision(DNC,
            f"Only {_money(owed)} owed vs {_money(value)} value (ratio {ratio:.1%}) — little motivation.",
            "MED", "low_motivation")
    if ratio is not None and ratio < LOW_RATIO:
        return Decision(OCC_ALIVE,
            f"Low taxes-owed-to-value ratio ({ratio:.1%}) — limited motivation, keep low-priority.",
            "MED", "low_motivation")

    # ------------------------------------------------------------------
    # 8. Default: still owes meaningfully, no disqualifier -> STAY
    # ------------------------------------------------------------------
    if isinstance(paid, (int, float)) and paid > 0:
        return Decision(STAY,
            f"Only {_money(paid)} paid against {_money(owed)} owed — small payments, still motivated.",
            "MED", "partial")
    return Decision(STAY,
        f"{_money(owed)} owed, not listed, no recent payoff — motivation remains, keep working.",
        "MED")


def render_row(
    lead: dict,
    parsed: Optional[Any],
    listing: Optional[dict],
    tax_result: Optional[dict],
    decision: Decision,
    move_result: str,
    source_pipeline: str = "",
) -> dict:
    """Shape per-lead data for the Slack table / spreadsheet."""
    name = ""
    if lead:
        name = f"{lead.get('firstName', '')} {lead.get('lastName', '')}".strip()
    if not name and parsed is not None:
        name = getattr(parsed, "owner_name", "") or ""
    listing = listing or {}
    return {
        "lead_id": (lead or {}).get("leadId") or (lead or {}).get("id"),
        "name": name,
        "address": getattr(parsed, "property_address", "") if parsed is not None else "",
        "county": getattr(parsed, "county", "") if parsed is not None else "",
        "owed": getattr(parsed, "total_owed", None) if parsed is not None else None,
        "value": _best_value(parsed) if parsed is not None else None,
        "lawsuit": bool(getattr(parsed, "has_lawsuit", False)) if parsed is not None else False,
        "deceased": _is_deceased(parsed) if parsed is not None else False,
        "vacant": _is_vacant(parsed) if parsed is not None else False,
        "source_pipeline": source_pipeline,
        "listed": listing.get("listed"),
        "listing_site": listing.get("site"),
        "listing_kind": listing.get("kind"),
        "current_due": (tax_result or {}).get("current_due"),
        "tax_paid_recent": (tax_result or {}).get("payment_recent_amount"),
        "tax_last_payment_date": (tax_result or {}).get("last_payment_date"),
        "tax_error": (tax_result or {}).get("error"),
        "decision": decision.category,
        "confidence": decision.confidence,
        "flag_type": decision.flag_type,
        "reason": decision.reason,
        "move_result": move_result,
    }
