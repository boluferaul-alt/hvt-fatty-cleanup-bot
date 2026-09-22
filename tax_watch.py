"""tax_watch — autonomous live-county tax watcher that writes notes onto Lofty.

WHAT IT DOES, EVERY RUN
-----------------------
For every lead in the target pipelines:
  1. Read the lead's researcher note — for LOCATING DATA ONLY (county, account
     number, owner name, property address). Never for tax figures.
  2. Hit that county's tax portal LIVE and read the real balance + the full
     payment receipt history.
  3. If a payment posted inside the lookback window, write a note on the Lofty
     lead: how much, by who, and the balance still owed.
  4. If the county can't be verified, record it as BLOCKED. Never guess.

Raul's rule (2026-07-19): "you can never count on the Lofty notes, you always
have to do your own county search every time — that's the whole point of the
bot." There is no note-fallback path in this file.

WHAT "SELF-IMPROVING" MEANS HERE
--------------------------------
Deliberately narrow, because a bot that infers is a bot that lies:
  * ACCOUNT LEARNING — if the note's Property Id doesn't resolve, it searches
    the portal by owner name then property address, and CACHES the account it
    found. Next run goes straight there. Bad/missing note ids self-heal once.
  * COVERAGE LEDGER — every run appends per-county verified/blocked counts and
    the top failure reason to state/coverage.json, so gaps and regressions are
    visible instead of silent. This is what tells you which adapter to build
    next.
  * IDEMPOTENCE — a payment already noted is never re-noted, so it can run as
    often as you like without spamming the lead.
It never learns a dollar figure. Amounts come from the county, that run, or the
lead is blocked.

USAGE
    python tax_watch.py --stages hvt,fatty,hot,nurture,ddoffer --lookback 14
    python tax_watch.py --stages hvt --limit 25 --dry-run
"""
from __future__ import annotations

import argparse
import re
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from county_adapters import (get_adapter, supported_counties, canonical_county,
                             UNSUPPORTED, ESEARCH_CAD, cad_adapter, cad_situs)
import bulk_rolls  # noqa: F401  — registers Bell/Taylor bulk-roll adapters
import cameron_live  # noqa: F401  — registers the live Cameron adapter
from lofty_client import LoftyClient
from note_parser import parse_lead_summary, ParsedSummary
from lead_locator import (lead_property_address, county_for_address,
                          locate_from_notes, lead_record_county)

STATE_DIR = Path(__file__).parent / "state"
ACCOUNT_CACHE = STATE_DIR / "account_cache.json"
POSTED_STATE = STATE_DIR / "posted_payments.json"
COVERAGE_LOG = STATE_DIR / "coverage.json"

# Lofty exposes no /stages endpoint (every variant 404s), but each lead carries
# both `stageId` and `stage`, so the ids below came from walking the workspace
# once on 2026-08-06 and collecting the distinct pairs. Full map for reference:
#   -3 New Leads · 425572 Warm/Hot Wholesale · 425579 Closed · 425581 Do Not
#   Contact · 426286 Nurture · 428742 Vault Hot Occ Alive · 504150 Wrong Number
#   · 506784 ColdWholesale · 603344 Research Needed HOT · 606229 FATTY ·
#   606230 HVT · 606231 Occupied/Alive · 606232 Good Conv in Prog. · 606233 DD
#   Offer · 606234 Vault HVT/Fatty · 606236 Inventory · 648757 HOT Occupied/Alive
STAGES = {
    "hvt": int(os.getenv("STAGE_HVT", "606230")),
    "fatty": int(os.getenv("STAGE_FATTY", "606229")),
    "hot": int(os.getenv("STAGE_HOT_OCC_ALIVE", "648757")),
    # Raul 2026-08-06, standing order ("from now on, always and forever"):
    # every Saturday run covers Nurture too, not just the three hot pipelines.
    "nurture": int(os.getenv("STAGE_NURTURE", "426286")),
    # Raul 2026-08-23, standing order: DD Offer joins the weekly run "from now
    # on". Leads move Nurture -> DD Offer as he works them, and a lead that
    # crossed that line mid-week used to drop out of the scan entirely (Mary
    # Griffin, 8/23) — an offer in flight is exactly when a surprise tax
    # payment matters most.
    "ddoffer": int(os.getenv("STAGE_DD_OFFER", "606233")),
}

# Payments below this are rounding/fee noise, not a signal worth a note on the
# lead (Auzston's ledger carries a $1.81 line alongside his real $599.63).
MIN_PAYMENT = float(os.getenv("MIN_PAYMENT_TO_NOTE", "50"))

# How often to restate "taxes still not paid" on a lead that hasn't changed.
# The job runs twice weekly; without this every lead would collect ~8 identical
# notes a month and bury the real signal. A NEW payment always posts at once.
RESTATE_DAYS = int(os.getenv("TAX_RESTATE_DAYS", "30"))

# Statuses
VERIFIED_PAID = "verified_paid"        # live read + a new payment -> note posted
VERIFIED_UNPAID = "verified_unpaid"    # live read, still delinquent -> note posted
VERIFIED_NOPAY = "verified_no_change"  # live read, nothing new to say -> skipped
BLOCKED = "blocked"                    # could not verify live -> human/adapter needed


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path: Path, obj):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=1), encoding="utf-8")


def payment_key(p) -> str:
    """Stable identity for one payment, so we never post it twice."""
    return f"{p.date}|{p.amount:.2f}|{p.payer.strip().upper()}"


def _fmt_when(iso: Optional[str]) -> str:
    try:
        y, m, d = iso.split("-")
        return f"{int(m)}/{int(d)}/{y[2:]}"
    except (AttributeError, ValueError):
        return iso or "recent"


def payment_fragment(p) -> str:
    """The exact substring build_note() writes for a payment.

    Used to detect an already-posted payment by reading the lead's own Lofty
    notes. Render cron containers have an EPHEMERAL filesystem, so the local
    state/ cache does not survive between runs — without this the bot would
    re-post the same payment every week. Lofty is the durable store.
    """
    return f"${p.amount:,.2f} on {_fmt_when(p.date)} by {p.payer.strip()}"


def already_noted(notes: list, p) -> bool:
    frag = payment_fragment(p)
    for n in notes or []:
        body = str(n.get("content") or n.get("note") or n.get("body") or "")
        if frag in re.sub(r"<[^>]+>", "", body):
            return True
    return False


TAX_NOTE_TAG = "TAX UPDATE"


def last_tax_note_age_days(notes: list, today: Optional[object] = None) -> Optional[int]:
    """Days since we last wrote a TAX UPDATE note on this lead, or None.

    Stops the every-run restatement of "still not paid" from burying the lead
    in identical notes. A NEW payment ignores this and posts immediately.
    """
    from datetime import date as _date, datetime as _dt
    today = today or datetime.now(timezone.utc).date()
    best = None
    for n in notes or []:
        body = re.sub(r"<[^>]+>", "", str(n.get("content") or n.get("note") or ""))
        if TAX_NOTE_TAG not in body:
            continue
        m = re.search(TAX_NOTE_TAG + r"\s*(\d{1,2})/(\d{1,2})/(\d{2})", body)
        if not m:
            continue
        try:
            d = _date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        age = (today - d).days
        if best is None or age < best:
            best = age
    return best


def _tax_notes(notes: list) -> list:
    """Every TAX UPDATE note on the lead, newest-dated first.

    Sorted on the date INSIDE the note text, not the create time, because that
    is the date the note itself advertises to Raul.
    """
    from datetime import date as _date
    out = []
    for n in notes or []:
        body = re.sub(r"<[^>]+>", "", str(n.get("content") or n.get("note") or ""))
        if TAX_NOTE_TAG not in body:
            continue
        m = re.search(TAX_NOTE_TAG + r"\s*(\d{1,2})/(\d{1,2})/(\d{2})", body)
        if not m:
            continue
        try:
            d = _date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            continue
        nid = n.get("noteId") or n.get("id")
        if nid is None:
            continue
        out.append({"id": nid, "date": d, "body": body,
                    "pinned": str(n.get("isPin")) == "True"})
    out.sort(key=lambda t: t["date"], reverse=True)
    return out


def refresh_in_place(client, lead_id, notes: list, note: str) -> bool:
    """Rewrite the newest TAX UPDATE note with today's figures, keep it pinned,
    and unpin any older tax note still pinned over it.

    Raul 2026-08-27: a silent skip is indistinguishable from "never checked" —
    166 of 328 leads on the 8/22 run were verified live and showed nothing for
    it. Refreshing the existing note keeps one always-current note per lead
    instead of either a duplicate pile or silence.

    The unpin half also repairs the pre-2026-08-15 ampersand bug, where a note
    containing "&" failed to pin and left a STALE note pinned on top (Lee Laney
    showed "still not paid $4,267.95" for two weeks after $571.03 was paid).
    """
    tax = _tax_notes(notes)
    if not tax:
        return False
    newest = tax[0]
    if not client.set_note_pin(int(lead_id), newest["id"], note, True):
        return False
    for old in tax[1:]:
        if old["pinned"]:
            client.set_note_pin(int(lead_id), old["id"], old["body"], False)
    return True


def build_note(status, payments, today_str: str) -> str:
    """The note Raul asked for.

    Two shapes (Raul 2026-07-20 — he wants a note even when nothing was paid,
    so acquisitions always sees current tax state on the lead):
      * money moved   -> how much, by who, and the balance still owed
      * nothing moved -> "TAXES STILL NOT PAID" + balance + last payment on file
    """
    src = f" Source: {status.county.title()} {getattr(status, 'source_name', 'County Tax Office')}, live check."
    lawsuit = ""
    if status.active_lawsuit and status.active_lawsuit.strip().lower() not in ("none", "n/a", ""):
        lawsuit = f" Active lawsuit: {status.active_lawsuit.strip()}."

    if payments:
        newest = payments[0]
        note = (f"{TAX_NOTE_TAG} {today_str} - TAXES PAID: {payment_fragment(newest)}. "
                f"Balance still owed: ${status.total_due:,.2f}.")
        if len(payments) >= 3:
            total = sum(p.amount for p in payments)
            note += (f" HEADS UP: active payment plan - {len(payments)} payments totaling "
                     f"${total:,.2f} in the window (owner is paying it down).")
        return note + lawsuit + src

    # Nothing paid in the window.
    if status.total_due is not None and status.total_due <= 0:
        # Balance cleared with no payment inside the window — someone settled it
        # earlier. Still worth saying loudly: it kills the tax-pressure angle.
        note = f"{TAX_NOTE_TAG} {today_str} - TAXES PAID IN FULL - $0 balance due."
    else:
        note = (f"{TAX_NOTE_TAG} {today_str} - TAXES STILL NOT PAID. "
                f"Balance owed: ${status.total_due:,.2f}.")
    last = status.last_payment()
    if last:
        note += f" Last payment on file: {payment_fragment(last)}."
    elif getattr(status, "payments_available", True):
        note += " No payments on record."
    else:
        # County publishes the balance but not a dated payment history.
        note += " (Payment history not published by this county.)"
    return note + lawsuit + src


def resolve_and_lookup(adapter, parsed, cache: dict, lead_id: str):
    """Return (TaxStatus, account, how, reason).

    Tries account candidates in order and STOPS at the first that verifies
    live. The note's Property Id is only ONE candidate — for some ACT counties
    (Fort Bend, Montgomery) the tax `can` is the geo-id-minus-dashes, not the
    Property Id, so a failed direct lookup must fall through to an owner /
    address SEARCH, which returns the county's own correct account. That search
    also disambiguates multi-parcel owners by the note's property address.

    If nothing verifies, TaxStatus is None and `reason` explains why (blocked,
    never note-guessed).
    """
    last_err = None

    def _try(account, how):
        nonlocal last_err
        st = adapter.lookup(account)
        if st.verified:
            return st, account, how, ""
        last_err = st.error
        return None

    # 1. cached account (learned on a prior run)
    cached = cache.get(str(lead_id))
    if cached:
        r = _try(cached, "cache")
        if r:
            return r

    # 2. the note's Property Id (correct for Galveston/Ector; wrong for FB/Mont)
    if parsed.property_id:
        r = _try(parsed.property_id.strip(), "note_property_id")
        if r:
            return r

    # 2b. geo id / long account (ACT counties): pins one parcel even when many
    #     share an owner or a street address (condos).
    if hasattr(adapter, "SEARCH_BY"):
        geo_cands = [getattr(parsed, "geo_id", "") or ""]
        if "-" in (parsed.property_id or ""):
            geo_cands.append(parsed.property_id)
        for g in dict.fromkeys(c for c in geo_cands if re.sub(r"\D", "", c)):
            digits = re.sub(r"\D", "", g)
            # Fort Bend's account IS the geo id sans dashes; Montgomery's is
            # "00" + those digits (0021570015700 for 2157-00-15700).
            for acct in (digits, "00" + digits):
                try:
                    r = _try(acct, "geo_id_direct")
                except Exception:
                    r = None
                if r:
                    return r
            try:
                hits = adapter.find_account(long_account=g)
            except Exception:
                hits = []
            if len(hits) == 1:
                r = _try(hits[0]["account"], "portal_search_geo_id")
                if r:
                    return r

    # 3. search the portal by owner, then by address; disambiguate on address.
    #    Try the cleaned owner key first (drops ESTATE/C-O/etc.), then the raw
    #    owner as a fallback.
    owner_keys = [k for k in dict.fromkeys(
        [_clean_owner(parsed.owner_name), (parsed.owner_name or "").strip()]) if k]
    search_plan = [("owner", {"owner": k}) for k in owner_keys]
    search_plan.append(("address", {"address": parsed.property_address}))
    # No owner on the note: try the lead's own name, but ONLY accept a hit
    # whose site address matches the note's property address - the lead may
    # be an heir, so a bare name hit is never trusted.
    lead_key = _clean_owner(getattr(parsed, "lead_name", ""))
    if not owner_keys and lead_key and parsed.property_address:
        search_plan.append(("lead_name", {"owner": lead_key}))
    ambiguous = ""
    for field_name, kwargs in search_plan:
        if not list(kwargs.values())[0]:
            continue
        try:
            hits = adapter.find_account(**kwargs)
        except Exception:
            continue
        if not hits:
            continue
        if field_name == "lead_name":
            hits = _match_by_address(hits, parsed.property_address)
            if len(hits) != 1:
                continue
        if len(hits) > 1:
            narrowed = _match_by_address(hits, parsed.property_address)
            if len(narrowed) > 1 or (not narrowed and field_name == "address"):
                # Several parcels share the address (condos, split lots):
                # keep the one whose portal owner carries the owner's / lead's
                # surname. Only ever narrows an address match, never widens.
                by_name = _match_by_name(narrowed or hits,
                                         [parsed.owner_name, getattr(parsed, "lead_name", "")])
                if len(by_name) == 1:
                    narrowed = by_name
            same_place = len({_addr_key(h.get("site_address", ""))
                              for h in narrowed}) == 1
            if 2 <= len(narrowed) <= 4 and same_place:
                # Several tax accounts at the SAME street address (land + mobile
                # home, lot + improvement): that's one property billed in
                # pieces. Check every account and report the combined balance,
                # listing each account so nothing is hidden.
                combo = _combine_accounts(adapter, [h["account"] for h in narrowed])
                if combo is not None:
                    return combo, combo.account, f"portal_search_{field_name}_multi", ""
            if len(narrowed) != 1:
                # Don't give up on an ambiguous owner search - the address
                # search further down the plan often pins the one parcel.
                ambiguous = ambiguous or (
                    f"ambiguous_{field_name}_{len(hits)}_matches"
                    f"{'' if not narrowed else f'_{len(narrowed)}_after_address'}")
                continue
            hits = narrowed
        r = _try(hits[0]["account"], f"portal_search_{field_name}")
        if r:
            return r

    # 4. Appraisal-district fallback, keyed by the note's Property Id.
    #    The CAD knows the real SITUS address (Lofty's can be the owner's
    #    mailing address) and some CADs publish the tax table outright.
    #    Raul 9/22: no account number is not a dead end — name, property
    #    address and property ID are all ways in.
    county = canonical_county(getattr(adapter, "county", "") or "")
    pid = (parsed.property_id or "").strip()
    if pid and county in ESEARCH_CAD:
        situs = cad_situs(county, pid)
        if situs.get("address"):
            # Street-only, because the tax portal's address search wants
            # "661 EVANS" or "EVANS", not "Evans RD, Rosenberg, TX 77471".
            street = re.sub(r",.*$", "", situs["address"]).strip()
            # Try every way in and keep whichever pins ONE parcel — a street
            # search that returns 63 hits must not stop us from trying the
            # CAD's owner name (Gail Davis, Fort Bend).
            for kwargs in ({"address": street},
                           {"owner": _clean_owner(situs.get("owner", ""))},
                           {"owner": _clean_owner(parsed.owner_name)}):
                if not list(kwargs.values())[0]:
                    continue
                try:
                    hits = adapter.find_account(**kwargs)
                except Exception:
                    hits = []
                if not hits:
                    continue
                narrowed = _match_by_address(hits, situs["address"])
                if len(narrowed) != 1:
                    by_name = _match_by_name(narrowed or hits,
                                             [situs.get("owner", ""), parsed.owner_name])
                    if len(by_name) == 1:
                        narrowed = by_name
                if len(narrowed) == 1:
                    r = _try(narrowed[0]["account"], "cad_situs_then_portal")
                    if r:
                        return r
        cad = cad_adapter(county)
        if cad is not None:
            st = cad.lookup(situs.get("account") or pid)
            if st.verified:
                return st, st.account, "cad_tax_table", ""

    if ambiguous:
        return None, "", "portal_search", ambiguous
    reason = (f"live county check failed: {last_err}" if last_err
              else "no account found on the county portal")
    return None, "", "", reason


def _combine_accounts(adapter, accounts: list[str]):
    """Live-check every account; None unless ALL verify (never a partial sum)."""
    import copy
    sts = []
    for acct in dict.fromkeys(accounts):
        st = adapter.lookup(acct)
        if not st.verified:
            return None
        sts.append(st)
    combo = copy.copy(sts[0])
    combo.total_due = round(sum(s.total_due or 0.0 for s in sts), 2)
    combo.payments = [p for s in sts for p in s.payments]
    combo.payments_available = all(s.payments_available for s in sts)
    combo.account = "+".join(s.account for s in sts)
    parts = "; ".join(f"acct {s.account} ${(s.total_due or 0):,.2f}" for s in sts)
    combo.source_name = (f"{getattr(sts[0], 'source_name', 'County Tax Office')} "
                         f"({len(sts)} tax accounts at this address: {parts})")
    return combo


def _clean_owner(owner: str) -> str:
    """A portal-search-friendly owner key.

    County portals want "LAST FIRST" and choke on the estate/trust boilerplate
    the note carries ("MATLOCK, THOMAS O ESTATE OF C/O MELINDA S ..." found
    nothing on Montgomery). Cut at the first noise token and keep the leading
    2-3 name words.
    """
    s = re.sub(r"[,]", " ", (owner or "").upper())
    s = re.split(r"\b(?:ESTATE|EST|C/O|ETAL|ET AL|ETUX|ET UX|ET VIR|LIFE EST|"
                 r"LF EST|TRUST|TR|HEIRS?|DECEASED|DEC'?D|JR|SR|III|II)\b", s)[0]
    # Ector notes append the tax attorneys to the owner ("FIELDS MARSHALL/
    # MICHELE GREENE-ROY BELL ATTYS"); everything past the slash is not the
    # owner. And "&" has no word boundary, so the pattern above never cut it —
    # "COX SADIE & COX MICHAEL" went to the portal whole and found nothing.
    s = s.split("/")[0].split("&")[0]
    s = re.sub(r"\s+", " ", s).strip()
    toks = s.split()
    return " ".join(toks[:3])


def _addr_key(addr: str) -> tuple:
    """(house number, first street token) — enough to tell parcels apart."""
    s = re.sub(r"[.,#]", " ", (addr or "").upper())
    s = re.sub(r"\s+", " ", s).strip()
    m = re.match(r"(\d+)\s+(?:[NSEW]\s+)?([A-Z0-9]+)", s)
    return (m.group(1), m.group(2)) if m else ()


_NAME_NOISE = {"ESTATE", "EST", "ETAL", "ETUX", "ETVIR", "LLC", "INC", "LTD", "LP",
               "THE", "AND", "OF", "TRUST", "TR", "HEIRS", "JR", "SR", "III", "C/O",
               "LIFE", "DECEASED", "UNKNOWN"}


def _name_tokens(name: str) -> list[str]:
    toks = re.findall(r"[A-Z]{3,}", (name or "").upper())
    return [t for t in toks if t not in _NAME_NOISE]


def _match_by_name(hits: list[dict], names: list[str]) -> list[dict]:
    """Hits whose portal owner contains a significant name token from the
    owner / lead name ("TERRELL" picks TERRELL JOHN EDWARD & JEAN ESTATE over
    ROMERO CASSANDRA at the same street address)."""
    toks = {t for n in names for t in _name_tokens(n)[:2]}
    if not toks:
        return []
    return [h for h in hits
            if toks & set(re.findall(r"[A-Z]{3,}", str(h.get("owner", "")).upper()))]


def _norm_addr(a: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[.,#]", " ", (a or "").upper())).strip()


_ADDR_NOISE = {"TX", "TEXAS", "N", "S", "E", "W", "NE", "NW", "SE", "SW",
               "ST", "STREET", "DR", "DRIVE", "RD", "ROAD", "LN", "LANE",
               "AVE", "AVENUE", "BLVD", "CT", "COURT", "CIR", "CIRCLE",
               "WAY", "PL", "PLACE", "TRL", "TRAIL", "PKWY", "HWY"}


def _street_core(addr: str) -> set:
    """The distinguishing words of a street address.

    Drops the house number, ZIP, state and the street-type/direction words, so
    "0 W LITTLE YORK RD HOUSTON TX 77088" and the county's "W LITTLE YORK RD
    77088" both reduce to {LITTLE, YORK}.
    """
    s = _norm_addr(addr)
    s = re.sub(r"^\d+\s+", "", s)                 # house number (incl. "0")
    s = re.sub(r"\b\d{5}(?:-\d{4})?\b", " ", s)   # ZIP
    return {t for t in s.split() if t not in _ADDR_NOISE and not t.isdigit()}


def _match_by_address(hits: list[dict], property_address: str) -> list[dict]:
    want = _addr_key(property_address)
    if want:
        exact = [h for h in hits if _addr_key(h.get("site_address", "")) == want]
        if exact or want[0] != "0":
            return exact
        # House number "0" is the note's placeholder for vacant land; the
        # county lists the same parcel with no number at all ("0 SPRING TOWN
        # DR" vs "SPRING TOWN DR 77388"), which blocked 6 Harris leads on
        # 9/19. Fall through to street-name matching.
    # No usable house number (vacant land, rural: "COUNTY ROAD 661 OFF").
    # Keep hits whose street words are all present in the note's address.
    core = _street_core(property_address)
    if not core:
        return []
    out = [h for h in hits
           if _street_core(h.get("site_address", "")) and
           _street_core(h.get("site_address", "")) <= core]
    if len(out) > 1:
        # The note has no real house number (vacant land), so prefer the
        # parcels the county also lists without one: "EVANS RD" is Gail Davis's
        # vacant tract, "1007 EVANS RD" is somebody's house on the same street.
        bare = [h for h in out if not _addr_key(h.get("site_address", ""))]
        if bare:
            return bare
    if out:
        return out
    exact = _norm_addr(property_address)
    return [h for h in hits if _norm_addr(h.get("site_address", "")) == exact]


def process(client, lead, stage_name, lookback, dry_run, cache, posted, today_str,
            only: set | None = None, restate_days: int = RESTATE_DAYS):
    lead_id = lead.get("leadId") or lead.get("id")
    name = f"{lead.get('firstName','')} {lead.get('lastName','')}".strip()
    row = {"lead_id": lead_id, "name": name, "pipeline": stage_name,
           "county": "", "status": BLOCKED, "reason": "", "note": ""}

    # Notes come from the CSV when we have them, else one API call.
    notes = lead.get("_notes") if lead.get("_notes") is not None else client.get_notes(lead_id)
    parsed = parse_lead_summary(notes)
    if not parsed.found or get_adapter(parsed.county) is None:
        # No summary, or a county we can't map: locate from the lead's own
        # Lofty property address instead of blocking (Raul 9/18: no excuses).
        # Owner name is deliberately NOT taken from the lead - the lead is
        # often an heir, and an owner search on an heir's name could lock onto
        # someone else's parcel. Address search only.
        # Sources, best first: older free-text notes ("Property Address: ...",
        # "Property Id: ...", CAD links), the county Lofty stores on the lead's
        # property, the lead's own address, and the Census geocoder last.
        from_notes = locate_from_notes(notes)
        street, one_line = lead_property_address(lead, client)
        street = from_notes["address"] or street
        one_line = from_notes["one_line"] or one_line
        county = ""
        for cand in (from_notes["county"], lead_record_county(lead, client)):
            if cand and get_adapter(cand) is not None:
                county = cand
                break
        if not county:
            county = county_for_address(one_line) or from_notes["county"] or ""
        if county and (not parsed.found or not parsed.county
                       or get_adapter(county) is not None):
            if not parsed.found:
                parsed = ParsedSummary(found=True)
            parsed.county = county
            if not parsed.property_address:
                parsed.property_address = street
            if not parsed.property_id:
                parsed.property_id = from_notes["property_id"]
            parsed.geo_id = from_notes["geo_id"]
            row["located_by"] = "lofty_notes/address"
        elif not parsed.found:
            row["reason"] = ("no researcher summary and no property address or "
                             "county anywhere on the Lofty lead — can't locate it")
            return row
    parsed.lead_name = name
    row["county"] = canonical_county(parsed.county)
    if only and row["county"] not in only:
        row["skipped"] = True
        return row

    adapter = get_adapter(parsed.county)
    if adapter is None:
        row["reason"] = (f"no live adapter for {row['county'] or 'unknown county'} — "
                         f"cannot verify; adapter needed")
        return row

    status, account, how, reason = resolve_and_lookup(adapter, parsed, cache, lead_id)
    if status is None or not status.verified:
        # The note's county can simply be wrong — Gerald Harris's note said
        # Liberty, but 701 Pauline Rd, Cleveland is in SAN JACINTO, where the
        # parcel was waiting under his own name (9/19). When the stated county
        # has nothing, ask the geocoder where the address really is and try
        # that county once before blocking.
        addr_line = (parsed.property_address or "")
        if not addr_line:
            _, addr_line = lead_property_address(lead, client)
        alt = canonical_county(county_for_address(addr_line) or "") if addr_line else ""
        alt_adapter = get_adapter(alt) if alt and alt != row["county"] else None
        if alt_adapter is not None:
            alt_parsed = parsed
            alt_parsed.county = alt
            status, account, how, reason = resolve_and_lookup(
                alt_adapter, alt_parsed, cache, lead_id)
            if status is not None and status.verified:
                row["county"], adapter = alt, alt_adapter
                how = f"{how}_in_{alt.lower()}_not_noted_county"
        if status is None or not status.verified:
            row["reason"] = reason
            return row

    cache[str(lead_id)] = account          # learned/confirmed
    row["account"], row["how_found"] = account, how
    row["balance"] = status.total_due

    # Notes from a CSV export are a SNAPSHOT — they can't contain a note
    # posted after the export, so a duplicate check against them would miss it
    # and we'd post twice. Always refresh from the API before deciding.
    if lead.get("_notes") is not None:
        notes = client.get_notes(lead_id)

    recent = status.payments_within(lookback)
    seen = set(posted.get(str(lead_id), []))
    fresh = [p for p in recent
             if p.amount >= MIN_PAYMENT
             and payment_key(p) not in seen and not already_noted(notes, p)]

    if not fresh:
        # Raul 2026-07-20: still write a note when nothing was paid, so
        # acquisitions sees live tax state on every lead. But restating it
        # every run would bury the lead in identical notes, so only restate
        # once per `restate_days`. A NEW payment ignores this and posts now.
        #
        # Raul 2026-09-22, overriding both of the above: post a NEW dated note
        # EVERY run, even when nothing changed. Refreshing the old note in
        # place kept Lofty's original timestamp, so the lead showed "Aug 8"
        # beside a note whose text said 9/19 — and "if we don't see an update,
        # we have to go and manually check, and that defeats the purpose."
        # A fresh note per run is the receipt that the check happened.
        row["status"] = VERIFIED_UNPAID

    note = build_note(status, fresh, today_str)
    row["note"] = note
    outcome = VERIFIED_PAID if fresh else VERIFIED_UNPAID
    if dry_run:
        row["status"] = outcome
        row["reason"] = "DRY RUN - note not posted"
        return row

    # One note per run, not one per attempt: a catch-up run or a re-run the
    # same day would otherwise stack identical notes on the lead.
    body = re.sub(r"<[^>]+>", "", note).strip()
    for n in notes or []:
        if re.sub(r"<[^>]+>", "", str(n.get("content") or "")).strip() == body:
            row["status"] = VERIFIED_NOPAY
            row["reason"] = "identical note already posted today"
            return row

    if client.post_note(int(lead_id), note, pin=True):
        if fresh:
            posted[str(lead_id)] = list(seen | {payment_key(p) for p in fresh})
        # A new note is now the current one, so drop the pin on every older tax
        # note; otherwise a stale "still not paid" can stay pinned beside it.
        for old in _tax_notes(notes):
            if old["pinned"]:
                client.set_note_pin(int(lead_id), old["id"], old["body"], False)
        row["status"] = outcome
        row["reason"] = "note posted"
    else:
        row["status"] = BLOCKED
        row["reason"] = "live data OK but Lofty post_note failed"
    return row


def leads_from_csv(path: str) -> list[dict]:
    """Lead LIST from a Lofty CSV export. IDs only — notes still come from the API.

    Why: listing a pipeline over the API means walking the whole ~35K-lead
    workspace (350+ calls, ~10 min, and it dropped the connection mid-run on
    2026-07-19). A pipeline CSV export names those leads instantly.

    Why not the CSV's Note 1-10 columns: Lofty's exporter STRIPS the HTML, so
    the summary collapses into one unpunctuated run
    ("County: GALVESTONOwner: NORTON HERMAN% Ownership: 100.0%...") and the
    field boundaries the parser needs are gone — county parsed as the entire
    note body on 153/154 leads. The API returns the same note with its <p>/<br>
    structure intact. So: CSV for WHICH leads, API for WHAT'S ON them.
    """
    import csv as _csv
    out = []
    with open(path, encoding="utf-8-sig") as fh:
        for d in _csv.DictReader(fh):
            lead_id = (d.get("Lead Id") or "").strip().strip("`")
            if not lead_id:
                continue
            out.append({
                "leadId": lead_id,
                "firstName": (d.get("First Name") or "").strip(),
                "lastName": (d.get("Last Name") or "").strip(),
            })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stages", default="hvt")
    ap.add_argument("--csv", default="", help="Lofty pipeline CSV export (skips the slow API scan)")
    ap.add_argument("--counties", default="",
                    help="only process these counties, e.g. GALVESTON,ECTOR")
    ap.add_argument("--lookback", type=int, default=int(os.getenv("TAX_LOOKBACK_DAYS", "14")))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--restate-days", type=int, default=RESTATE_DAYS,
                    help="how often to restate an unchanged 'still not paid' note")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    only = {c.strip().upper() for c in a.counties.split(",") if c.strip()}

    now = datetime.now(timezone.utc)
    today_str = f"{now.month}/{now.day}/{now.strftime('%y')}"

    want = [s.strip().lower() for s in a.stages.split(",") if s.strip()]
    # A stage name we don't recognise used to be dropped silently, so a typo (or
    # a renamed env var) would quietly run FEWER pipelines than asked for and
    # still report success. Raul's standing order is that every Saturday run
    # covers hvt+fatty+hot+nurture, so a missing pipeline has to be loud.
    unknown = [s for s in want if s not in STAGES]
    if unknown:
        sys.exit(f"unknown stage(s) {unknown}; choose from {list(STAGES)}. "
                 f"Refusing to run a partial pipeline set.")
    stage_ids = {STAGES[s]: s for s in want}
    if not stage_ids:
        sys.exit(f"no valid stages in {a.stages!r}; choose from {list(STAGES)}")

    print(f"tax_watch — lookback {a.lookback}d | stages {list(stage_ids.values())} | "
          f"{'DRY RUN' if a.dry_run else 'LIVE (will post notes)'}")
    print(f"live adapters: {', '.join(supported_counties())}")
    print(f"no adapter yet (will BLOCK): {', '.join(UNSUPPORTED)}\n")

    client = LoftyClient()
    cache = _load(ACCOUNT_CACHE, {})
    posted = _load(POSTED_STATE, {})

    if a.csv:
        buckets = {0: leads_from_csv(a.csv)}
        stage_ids = {0: Path(a.csv).stem[:24]}
        print(f"lead source: {a.csv} ({len(buckets[0])} leads)")
    else:
        buckets = client.list_leads_in_stages(list(stage_ids))

    rows = []
    done_ids = set()
    for sid, leads in buckets.items():
        stage_name = stage_ids[sid]
        kept = 0
        for lead in leads:
            lid = lead.get("leadId")
            if lid is not None and lid in done_ids:
                continue          # never check/post the same lead twice in one run
            done_ids.add(lid)
            if a.limit and kept >= a.limit:
                break
            try:
                row = process(client, lead, stage_name, a.lookback, a.dry_run,
                              cache, posted, today_str, only=only,
                              restate_days=a.restate_days)
            except Exception as e:  # noqa: BLE001
                row = {"lead_id": lead.get("leadId"), "name": "", "pipeline": stage_name,
                       "county": "", "status": BLOCKED, "reason": f"exception: {e}"}
            if row.get("skipped"):
                continue          # filtered-out county: not part of this run at all
            kept += 1
            rows.append(row)
            flag = {VERIFIED_PAID: "PAID ", VERIFIED_UNPAID: "UNPAID",
                    VERIFIED_NOPAY: "skip ", BLOCKED: "BLOCK"}[row["status"]]
            print(f"  [{flag}] {str(row['name'])[:26]:28s} {row['county'][:10]:11s} {row['reason'][:66]}")

    _save(ACCOUNT_CACHE, cache)
    if not a.dry_run:
        _save(POSTED_STATE, posted)

    # ---- coverage ledger: what worked, what didn't, per county ----
    by_county = defaultdict(Counter)
    reasons = defaultdict(Counter)
    for r in rows:
        by_county[r["county"] or "UNKNOWN"][r["status"]] += 1
        if r["status"] == BLOCKED:
            reasons[r["county"] or "UNKNOWN"][r["reason"][:80]] += 1
    counts = Counter(r["status"] for r in rows)
    entry = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "stages": list(stage_ids.values()),
        "lookback_days": a.lookback,
        "dry_run": a.dry_run,
        "totals": dict(counts),
        "by_county": {c: dict(v) for c, v in by_county.items()},
        "top_block_reason": {c: v.most_common(1)[0] for c, v in reasons.items() if v},
    }
    log = _load(COVERAGE_LOG, [])
    log.append(entry)
    _save(COVERAGE_LOG, log[-60:])

    # Per-lead ledger, so "which leads didn't get a note this week" is a lookup
    # instead of a guess (Raul 9/18: Nurture leads were missing the 9/12 note).
    import csv
    lead_csv = STATE_DIR / "last_run_leads.csv"
    with open(lead_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["lead_id", "name", "pipeline", "county", "status", "reason",
                    "balance", "account", "lofty_link"])
        for r in rows:
            w.writerow([r.get("lead_id"), r.get("name"), r.get("pipeline"),
                        r.get("county"), r.get("status"), r.get("reason"),
                        r.get("balance", ""), r.get("account", ""),
                        f"https://crm.lofty.com/admin/home/detail?leadId={r.get('lead_id')}&type=all"])

    posted = counts[VERIFIED_PAID] + counts[VERIFIED_UNPAID]
    print(f"\n{'='*72}")
    print(f"TOTAL {len(rows)} | NOTES POSTED {posted} "
          f"({counts[VERIFIED_PAID]} paid, {counts[VERIFIED_UNPAID]} still-not-paid) | "
          f"unchanged/skipped {counts[VERIFIED_NOPAY]} | BLOCKED {counts[BLOCKED]}")
    print("\nper-county coverage:")
    for c, v in sorted(by_county.items(), key=lambda kv: -sum(kv[1].values())):
        ver = v[VERIFIED_PAID] + v[VERIFIED_UNPAID] + v[VERIFIED_NOPAY]
        tot = sum(v.values())
        print(f"  {c[:16]:18s} verified {ver:3d}/{tot:<3d}  blocked {v[BLOCKED]:3d}"
              + (f"   <- {reasons[c].most_common(1)[0][0][:44]}" if reasons[c] else ""))
    print(f"\ncoverage ledger -> {COVERAGE_LOG}")
    if counts[BLOCKED]:
        print("BLOCKED leads were NOT guessed from notes. Build the adapter or verify by hand.")

    if not a.dry_run and os.getenv("SLACK_WEBHOOK_URL", "").strip():
        post_slack_summary(rows, counts, by_county, stage_ids, today_str)


def post_slack_summary(rows, counts, by_county, stage_ids, today_str):
    """Weekly report to Slack - the run is unattended on Render, so this is
    how Raul sees what it did."""
    from slack_client import post_to_slack

    def link(r):
        return (f"<https://crm.lofty.com/admin/home/detail?leadId={r['lead_id']}&type=all"
                f"|{str(r['name'])[:30] or r['lead_id']}>")

    paid = [r for r in rows if r["status"] == VERIFIED_PAID]
    blocked = [r for r in rows if r["status"] == BLOCKED]
    lines = [f"*Lofty tax watch {today_str}* - {len(rows)} leads "
             f"({', '.join(stage_ids.values())})",
             f"Notes posted: *{counts[VERIFIED_PAID]} paid*, {counts[VERIFIED_UNPAID]} still not paid | "
             f"refreshed {counts[VERIFIED_NOPAY]} | *blocked {counts[BLOCKED]}*"]
    if paid:
        lines.append("\n*TAXES PAID:*")
        lines += [f"- {link(r)} ({r['county'].title()}, {r['pipeline']}): {r.get('note','')[23:140]}"
                  for r in paid]
    if blocked:
        lines.append("\n*BLOCKED - not verified, fix by hand:*")
        lines += [f"- {link(r)} ({r['county'].title() or 'no county'}, {r['pipeline']}): {r['reason'][:80]}"
                  for r in blocked[:60]]
    lines.append("\n*By county (verified/total):* " + ", ".join(
        f"{c.title()} {v[VERIFIED_PAID] + v[VERIFIED_UNPAID] + v[VERIFIED_NOPAY]}/{sum(v.values())}"
        for c, v in sorted(by_county.items(), key=lambda kv: -sum(kv[1].values()))))
    text = "\n".join(lines)
    ok = post_to_slack(os.getenv("SLACK_WEBHOOK_URL").strip(), {"text": text[:39000]})
    print(f"[slack] summary {'posted' if ok else 'FAILED'}")


if __name__ == "__main__":
    main()
