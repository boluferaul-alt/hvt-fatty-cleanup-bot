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
    paid_amount: Optional[float] = None         # From "How Much Paid: $X"
    property_id: str = ""                        # CAD/Property Id (for county scraping)
    owner_status_basis: str = ""                 # how owner alive/deceased was decided


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


def _normalize_note(s: str) -> str:
    """Lofty notes are HTML (<br>, <p>, &nbsp;) with everything on one line.
    Convert tags to newlines so line-anchored field regexes don't over-capture."""
    if not s:
        return ""
    s = re.sub(r"<\s*br\s*/?\s*>", "\n", s, flags=re.I)
    s = re.sub(r"</?\s*p\s*>", "\n", s, flags=re.I)
    s = re.sub(r"</?\s*div\s*>", "\n", s, flags=re.I)
    s = s.replace("&nbsp;", " ").replace("&amp;", "&").replace("\xa0", " ")
    s = re.sub(r"<[^>]+>", " ", s)        # strip any remaining tags
    return s


def _amount_after(text: str, *labels: str) -> Optional[float]:
    """Grab the dollar amount immediately after a label (tight — avoids
    sweeping up a later field's number on glued one-line notes)."""
    for lab in labels:
        m = re.search(lab + r"\s*:?\s*\$?\s*([\d,]+(?:\.\d+)?)", text, re.I)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
    return None


# --- Owner-of-record alive/deceased detector (Raul's 2026-06-24 rules) ---
def _owner_first(owner: str, note: str) -> str:
    o = owner or ""
    if not o:
        m = re.search(r"Owner(?: Name)?:\s*([A-Za-z][A-Za-z ,.&]+)", note, re.I)
        o = m.group(1) if m else ""
    toks = [w for w in re.findall(r"[A-Za-z]+", o.upper())
            if len(w) > 1 and w not in ("ETAL", "ETUX", "ESTATE", "EST", "THE",
                                        "LLC", "TRUST", "LIFE", "LF", "JR", "SR")]
    return toks[1] if len(toks) >= 2 else (toks[0] if toks else "")


def owner_status(note: str, owner: str, vacant: Optional[bool]) -> tuple[str, str]:
    """Return (status, why) for the OWNER OF RECORD only — ignore dead
    co-owners/heirs. Priority order is Raul's. Falls back to the vacancy
    tiebreaker (vacant -> deceased/HVT, occupied -> alive) when ambiguous."""
    t = note
    fn = _owner_first(owner, t)
    if re.search(r"ALIVE\s*\(\s*owner\b", t, re.I):
        return "alive", 'explicit "ALIVE (owner)" line'
    if fn and re.search(r"\d\s*y\.?o\.?\s*LIVING", t, re.I):
        m = re.search(re.escape(fn) + r"[^\n]{0,55}\d{1,3}\s*y\.?o\.?\s*(LIVING|DECEASED)", t, re.I)
        if m:
            return ("deceased" if m.group(1).upper() == "DECEASED" else "alive"), "owner age tag"
    if fn:  # owner appears in the Alive list (notes glue words: "AliveJAMES...")
        am = re.search(r"Alive", t, re.I)
        if am:
            after = t[am.end():]
            end = len(after)
            for mk in (r"Bot Deceased", r"DECEASED STATUS", r"Possible associate", r"\bDeceased\b"):
                mm = re.search(mk, after, re.I)
                if mm:
                    end = min(end, mm.start())
            if re.search(re.escape(fn), after[:min(end, 700)], re.I):
                return "alive", "owner in Alive list"
    if re.search(r"\bEST(ATE)?\b", owner or "", re.I):
        return "deceased", "owner name contains ESTATE"
    if re.search(r"(?:CONFIRMED|DECEASED)\s*\(\s*owner\b", t, re.I):
        return "deceased", 'explicit "(owner) DECEASED" line'
    if re.search(r"Bot Deceased Finding:\s*CONFIRMED", t, re.I):
        return "deceased", "Bot Deceased Finding CONFIRMED"
    if re.search(r"no deceased confirmed|RESCUE\s*[—-]\s*no deceased", t, re.I):
        return "alive", "rescue / no deceased"
    # Ambiguous -> occupancy tiebreaker
    if vacant is True:
        return "deceased", "inferred: vacant + ambiguous"
    if vacant is False:
        return "alive", "inferred: occupied + ambiguous"
    return "unconfirmed", "no clear signal"


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

    text = _normalize_note(note_content)

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
    out.total_owed = _amount_after(
        text, r"Total\s*Taxes?\s*Owed", r"Total\s*Due", r"Total\s*Owed")
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

    # ---- Valuation (tight $-after-label to avoid grabbing a later field) ----
    out.assessed_value = _amount_after(
        text, r"Appraised\s*Value", r"Assessed\s*Value", r"County\s*Assessed")
    out.zillow_zestimate = _amount_after(
        text, r"Zillow\s*Z?est(?:imate)?\.?", r"Zillow\s*EST")
    out.market_value = _amount_after(text, r"Market\s*Value")

    # ---- Owner status is decided AFTER occupancy below (it uses the
    #      vacant/occupied tiebreaker for ambiguous notes). ----

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

    # ---- Owner of record alive/deceased (owner-only; uses vacancy tiebreak) ----
    out.owner_status, out.owner_status_basis = owner_status(text, out.owner_name, out.is_vacant)
    out.deceased_confirmed = (out.owner_status == "deceased")

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

    # ---- Tax paid last 12 months + how much (Researcher already scraped this) ----
    paid = _field(text, r"Tax\s*Paid\s*Last\s*12\s*Months?:\s*([^\n]+)")
    if paid:
        low = paid.strip().lower()
        if low.startswith("yes"):
            out.tax_paid_last_12mo = True
        elif low.startswith("no"):
            out.tax_paid_last_12mo = False
    out.paid_amount = _amount_after(text, r"How\s*Much\s*Paid")

    # ---- Property / CAD id (for county tax lookups) ----
    out.property_id = _field(
        text,
        r"Property\s*Id\s*:?\s*([0-9][0-9.\-]*)",
        r"CAD\s*/?\s*Property\s*ID\s*:?\s*([0-9][0-9.\-]*)",
    )

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
