"""
Parse the Researcher Bot Summary Report notes that the existing lofty-bot
writes onto every researched lead.

The note format (defined in lofty-bot's main.build_summary_report) looks like:

    🤖 BOT RESEARCH COMPLETE — RECOMMENDED: Bot-Research-Needed-Hot
    Status: Ready for SmartSkip
    Bot run: 2026-04-24 15:51
    ---
    SUMMARY REPORT
    County: Taylor County
    Owner: Cora Lambert
    Property: 1241 S JEFFERSON DR
    Type: REAL | Unknown sqft | Built Unknown
    Mailing Address: ABILENE, TX (different)

    TAX STATUS:
    - Total Due: $5,161
    - Years Behind: 8 years
    Tax Lawsuit: Cause 13236-D       <-- only present if there's a lawsuit

    VALUATION:
    - Assessed Value: $406,547 (from CAD/tax office)
    - Zillow Zestimate: Not found
    - Zillow Status: Unknown

    URLs:
    ...

    OWNER STATUS: Cora Lambert — DECEASED (2017-01-06)
    Source: Houston Chronicle obituary

    HEIRS IDENTIFIED (if deceased):
    - Cassandra Lambert (daughter, 42)

    HEAT SIGNALS: out-of-state-mail, multi-property

    PIPELINE DECISION: → Bot-Research-Needed-Hot
    <justification text>

Some leads have an abbreviated bot summary (from Von's manual researcher
runs) — those use slightly different headers like "Summary Report:" and
include fields like "Occupancy: VACANT" / "Less Then 7 Heirs: YES" /
"ALIVE". The parser handles both formats.

Output: a ParsedSummary dataclass with everything the decision tree needs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# Researcher Bot Summary notes always start with one of these markers.
# When multiple notes match, we use the most recent one.
SUMMARY_MARKERS = (
    "BOT RESEARCH COMPLETE",      # Lofty-bot (Raul's automated runs)
    "SUMMARY REPORT",             # Von's manual reports
    "Summary Report:",            # Von's casing variant
    "RESEARCH SUMMARY",           # Some older format
)


@dataclass
class ParsedSummary:
    """Everything we need from a Researcher Bot Summary note."""
    # Identification
    raw_text: str = ""
    found: bool = False
    note_date: str = ""           # ISO date of the note, if available

    # Property facts
    county: str = ""
    owner_name: str = ""
    property_address: str = ""
    mailing_address: str = ""
    mailing_different: Optional[bool] = None   # True/False/None
    property_type: str = ""        # "REAL" | "LAND" | "MH" | "SFR" | ...

    # Tax
    total_owed: Optional[float] = None
    years_behind: Optional[int] = None
    tax_lawsuit: str = ""          # "Cause 13236-D" or empty
    has_lawsuit: bool = False

    # Valuation
    assessed_value: Optional[float] = None
    zillow_zestimate: Optional[float] = None
    market_value: Optional[float] = None   # Some reports show market value separately

    # Owner status
    owner_status: str = ""         # "alive" | "deceased" | "unconfirmed"
    deceased_confirmed: bool = False

    # Occupancy
    occupancy: str = ""            # "OCCUPIED" | "VACANT" | "" (unknown)
    is_vacant: Optional[bool] = None
    is_vacant_land: bool = False   # Type=LAND specifically

    # Heirs / family
    heirs: list[str] = field(default_factory=list)
    homestead: Optional[bool] = None

    # Entity
    is_entity: bool = False        # True if owner name looks like LLC/Corp/Trust

    # Recommendation the bot already made (when present)
    bot_recommendation: str = ""   # "Bot-Research-Needed-Hot" / "Bot-DNC" / etc.
    bot_status: str = ""           # "Ready for SmartSkip" / "Do Not Contact"

    # Recent activity signals
    tax_paid_last_12mo: Optional[bool] = None   # From "Tax Paid Last 12 Months: YES/NO"


# ----------------------------------------------------------------------
# Field extractors — each one tolerates the variations between formats
# ----------------------------------------------------------------------

_MONEY_RE = re.compile(r"\$[\d,]+(?:\.\d+)?|[\d,]+\.\d+|[\d,]+")


def _parse_money(s: str) -> Optional[float]:
    if not s:
        return None
    m = _MONEY_RE.search(s)
    if not m:
        return None
    raw = m.group(0).replace("$", "").replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_years(s: str) -> Optional[int]:
    if not s:
        return None
    m = re.search(r"\d+", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


_ENTITY_TOKENS = (
    " LLC", " L.L.C", " INC", " INC.", " CORP", " CORPORATION",
    " TRUST", " ESTATE", " FOUNDATION", " LTD", " LP", " LLP",
    " COMPANY", " HOLDINGS", " PROPERTIES", " ENTERPRISES",
    " PARTNERS", " GROUP",
)


def _looks_like_entity(name: str) -> bool:
    upper = (name or "").upper()
    return any(tok in upper for tok in _ENTITY_TOKENS)


def _field(text: str, *patterns: str) -> str:
    """Find the first regex match across all patterns. Returns the captured group or ''."""
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE | re.MULTILINE)
        if m:
            return (m.group(1) if m.groups() else m.group(0)).strip()
    return ""


def parse_summary(note_content: str) -> ParsedSummary:
    """
    Parse a single note's content into a structured ParsedSummary.

    If the note doesn't contain a recognizable summary marker, `found` is
    False and only `raw_text` is populated.
    """
    out = ParsedSummary(raw_text=note_content or "")

    if not note_content:
        return out

    # Has this note got a Researcher Bot Summary at all?
    if not any(marker in note_content for marker in SUMMARY_MARKERS):
        return out
    out.found = True

    text = note_content

    # ---- Bot recommendation + status (top of note) ----
    out.bot_recommendation = _field(
        text,
        r"RECOMMENDED:\s*(\S+)",
        r"Pipeline Decision[:\s→\-]+(\S+)",
        r"PIPELINE DECISION[:\s→\-]+(\S+)",
    )
    out.bot_status = _field(
        text,
        r"Status:\s*(.+?)$",
    )

    # ---- County / owner / address ----
    out.county = _field(
        text,
        r"County:\s*(.+?)$",
        r"County Name:\s*(.+?)$",
    )
    out.owner_name = _field(
        text,
        r"Owner(?:\s*Name)?:\s*(.+?)$",
    )
    out.is_entity = _looks_like_entity(out.owner_name)

    out.property_address = _field(
        text,
        r"Property(?:\s*Address)?:\s*(.+?)$",
    )
    mailing_line = _field(
        text,
        r"Mailing(?:\s*Address)?:\s*(.+?)$",
    )
    out.mailing_address = mailing_line
    if mailing_line:
        # "Mailing: ... (different)" or "(same as property)"
        low = mailing_line.lower()
        if "different" in low or "out-of-state" in low or "(different" in low:
            out.mailing_different = True
        elif "same" in low or "same as property" in low:
            out.mailing_different = False

    out.property_type = _field(
        text,
        r"Property Type:\s*(.+?)$",
        r"Type:\s*([^|]+?)(?:\s*\||\s*$)",
    ).upper()

    # Vacant land detection
    pt = out.property_type
    if pt in ("LAND", "VACANT LAND") or "VACANT LAND" in text.upper():
        out.is_vacant_land = True

    # ---- Tax ----
    out.total_owed = _parse_money(_field(
        text,
        r"Total\s*(?:Due|Owed|Taxes\s+Owed):\s*([^\n]+)",
    ))
    out.years_behind = _parse_years(_field(
        text,
        r"Years\s*Behind(?:\s*Tax)?:\s*([^\n]+)",
        r"Total\s*Back\s*Years(?:\s*Tax)?:\s*([^\n]+)",
        r"Years:\s*(\d+)",
    ))

    lawsuit_str = _field(
        text,
        r"Tax\s*Lawsuit[s]?:\s*([^\n]+)",
    )
    if lawsuit_str:
        # Treat "NONE" / "N/A" / "NO" / blank as no lawsuit; anything else
        # (Cause numbers, case IDs) as a real lawsuit.
        low = lawsuit_str.strip().lower()
        if low not in ("none", "n/a", "no", "null", ""):
            out.tax_lawsuit = lawsuit_str.strip()
            out.has_lawsuit = True

    # ---- Valuation ----
    out.assessed_value = _parse_money(_field(
        text,
        r"Assessed\s*Value:\s*([^\n]+)",
        r"County\s*Assessed:\s*([^\n]+)",
        r"Appraised\s*Value:\s*([^\n]+)",
    ))
    out.zillow_zestimate = _parse_money(_field(
        text,
        r"Zillow\s*Z?estimate:\s*([^\n]+)",
        r"Zillow\s*EST:\s*([^\n]+)",
        r"Zillow\s*Est:\s*([^\n]+)",
    ))
    out.market_value = _parse_money(_field(
        text,
        r"Market\s*Value:\s*([^\n]+)",
    ))

    # ---- Owner status ----
    # Patterns vary: "OWNER STATUS: Name — DECEASED", "Deceased: Yes",
    # bare "DECEASED" / "Alive" lines, "Confirmed Deceased - ...".
    status_block = _field(
        text,
        r"OWNER\s*STATUS:\s*([^\n]+(?:\n[^\n]+)?)",
    )
    if status_block:
        low = status_block.lower()
        if "deceased" in low:
            out.owner_status = "deceased"
            out.deceased_confirmed = True
        elif "alive" in low:
            out.owner_status = "alive"
        else:
            out.owner_status = "unconfirmed"
    else:
        # Look for standalone markers.
        upper = text.upper()
        if re.search(r"\bDECEASED\b", upper) and "NOT DECEASED" not in upper:
            out.owner_status = "deceased"
            out.deceased_confirmed = True
        elif re.search(r"\bALIVE\b", upper):
            out.owner_status = "alive"
        else:
            out.owner_status = "unconfirmed"

    # Explicit "Deceased: Yes / No / Unknown" line wins over standalone marker.
    deceased_line = _field(text, r"Deceased:\s*([^\n]+)")
    if deceased_line:
        low = deceased_line.strip().lower()
        if low.startswith("yes") or low.startswith("confirmed"):
            out.owner_status = "deceased"
            out.deceased_confirmed = True
        elif low.startswith("no"):
            out.owner_status = "alive"
            out.deceased_confirmed = False
        # "Unconfirmed" / "Unknown" — leave whatever standalone markers picked.

    # ---- Occupancy ----
    occ = _field(
        text,
        r"Occupancy:\s*([^\n]+)",
        r"Vacant:\s*([^\n]+)",   # Von's format sometimes has "Vacant: Occupied"
    )
    if occ:
        low = occ.strip().lower()
        if "vacant" in low and "possible vacant" not in low:
            out.occupancy = "VACANT"
            out.is_vacant = True
        elif "occupied" in low or low in ("yes", "y"):
            out.occupancy = "OCCUPIED"
            out.is_vacant = False
        elif "possible vacant" in low:
            out.occupancy = "POSSIBLE_VACANT"
            out.is_vacant = None
        elif low.startswith("no"):
            out.occupancy = "VACANT"
            out.is_vacant = True

    # ---- Heirs ----
    heirs_section = _field(
        text,
        r"HEIRS\s*IDENTIFIED[^\n]*\n((?:[-•]\s*[^\n]+\n?)+)",
    )
    if heirs_section:
        for line in heirs_section.splitlines():
            line = line.strip()
            if line.startswith(("-", "•", "*")):
                name = line.lstrip("-•* ").split("(")[0].strip()
                if name:
                    out.heirs.append(name)

    # ---- Homestead ----
    hs = _field(text, r"Homestead:\s*([^\n]+)")
    if hs:
        low = hs.strip().lower()
        if low.startswith("yes") or low == "y":
            out.homestead = True
        elif low.startswith("no") or low == "n":
            out.homestead = False

    # ---- Tax paid last 12 months ----
    paid = _field(text, r"Tax\s*Paid\s*Last\s*12\s*Months?:\s*([^\n]+)")
    if paid:
        low = paid.strip().lower()
        if low.startswith("yes"):
            out.tax_paid_last_12mo = True
        elif low.startswith("no"):
            out.tax_paid_last_12mo = False

    return out


def pick_latest_summary(notes: list[dict]) -> Optional[dict]:
    """
    From a list of Lofty note objects, return the most recent one that
    contains a Researcher Bot Summary. Notes that aren't summaries are
    skipped. Notes are sorted by createdAt / updatedAt descending.

    Returns the raw note dict (not the parsed content) so the caller can
    pull metadata + the content separately.
    """
    candidates: list[tuple[str, dict]] = []
    for n in notes or []:
        content = n.get("content") or n.get("body") or n.get("text") or ""
        if not isinstance(content, str):
            continue
        if not any(marker in content for marker in SUMMARY_MARKERS):
            continue
        ts = (n.get("createdAt") or n.get("updatedAt")
              or n.get("created_at") or n.get("updated_at") or "")
        candidates.append((str(ts), n))

    if not candidates:
        return None
    # Lofty timestamps are ISO-ish; lexicographic sort works for ISO 8601.
    candidates.sort(key=lambda t: t[0], reverse=True)
    return candidates[0][1]


def parse_lead_summary(notes: list[dict]) -> ParsedSummary:
    """Convenience: pick the latest summary note and parse it. Returns an
    empty ParsedSummary with found=False if no summary note exists."""
    note = pick_latest_summary(notes)
    if not note:
        return ParsedSummary()
    content = note.get("content") or note.get("body") or note.get("text") or ""
    parsed = parse_summary(content)
    parsed.note_date = str(
        note.get("createdAt") or note.get("updatedAt") or ""
    )
    return parsed
