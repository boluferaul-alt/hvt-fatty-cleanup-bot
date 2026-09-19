"""Bulk-file tax adapters for the counties with no live HTTP portal.

Brazoria, Bell, Taylor and Cameron all block the live per-account lookup
(Cloudflare on Brazoria's detail endpoint, reCAPTCHA on the BIS eSearch sites),
but every one of them publishes its delinquent roll for free on the county or
CAD website. So instead of scraping a walled portal we read the county's own
published file.

WHAT THIS IS AND IS NOT
-----------------------
This is still FIRST-PARTY COUNTY DATA — it is not a researcher note, and the
"never trust the note" rule is intact. But it is a periodic SNAPSHOT, not a
live read, so it must never claim to be live:

  * payments_available = False. These rolls carry a balance, not a dated
    receipt history with payer names, so callers must not say "no payments on
    record" — we simply cannot see them.
  * source_name carries the file date, so a posted note reads
    "per Bell County delinquent roll, file dated 2026-07-17" and nobody
    mistakes it for a portal check.

Refresh cadence published by the counties:
  Brazoria  weekly, generated Fridays, posted the following Monday
  Taylor    roughly weekly
  Bell      periodic
  Cameron   weekly + monthly

Run fetch_bulk_rolls.py to (re)download. If a county's file is missing this
adapter returns unverified with a reason, so the lead lands in the normal
BLOCKED bucket rather than silently reporting a stale or empty balance.

Registered into county_adapters.ADAPTERS at import time; consumers just
`import bulk_rolls` once.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from datetime import datetime, timezone
from typing import Optional

import county_adapters as ca
from county_adapters import TaxStatus

HERE = os.path.dirname(os.path.abspath(__file__))
BULK = os.path.join(HERE, "state", "bulk")


def _money(v) -> Optional[float]:
    if v is None:
        return None
    s = str(v).replace("$", "").replace(",", "").strip()
    if not s or s in {"-", "None"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[.,#]", " ", (s or "").upper())).strip()


def _addr_key(addr: str) -> tuple:
    m = re.match(r"(\d+)\s+(?:[NSEW]\s+)?([A-Z0-9]+)", _norm(addr))
    return (m.group(1), m.group(2)) if m else ()


def _newest(pattern: str) -> Optional[str]:
    """Newest file in state/bulk matching `pattern` (regex), or None."""
    if not os.path.isdir(BULK):
        return None
    hits = [f for f in os.listdir(BULK) if re.search(pattern, f, re.I)]
    if not hits:
        return None
    hits.sort(key=lambda f: os.path.getmtime(os.path.join(BULK, f)), reverse=True)
    return os.path.join(BULK, hits[0])


def _csv_rows(path):
    """Yield dict rows from a CSV, keyed by its header row.

    The county publishes the delinquent roll as CSV as well as xlsx; the CSV
    repeats the header name "EXEMPTIONS" twice, which DictReader collapses to
    the last one. Nothing here reads it, so that collision is harmless.
    """
    import csv
    with io.open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
        for row in csv.DictReader(fh):
            yield row


def _xlsx_rows(path_or_bytes, sheet: Optional[str] = None):
    """Yield dict rows from an xlsx, keyed by its header row."""
    import openpyxl
    wb = openpyxl.load_workbook(path_or_bytes, read_only=True, data_only=True)
    ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb[wb.sheetnames[0]]
    header = None
    for row in ws.iter_rows(values_only=True):
        if header is None:
            header = [str(c).strip() if c is not None else "" for c in row]
            continue
        yield dict(zip(header, row))


class _BulkAdapter:
    """Shared behaviour: load once, serve account/owner/address lookups."""

    COUNTY = ""
    SOURCE_LABEL = ""
    _cache: dict | None = None
    _file_date: str = ""
    _loaded_from: str = ""

    def __init__(self, county: str = "", slug=None, session=None):
        self.county = county or self.COUNTY
        if type(self)._cache is None:
            type(self)._cache = self._load()

    # -- subclasses implement -------------------------------------------
    def _load(self) -> dict:
        raise NotImplementedError

    # -- shared ----------------------------------------------------------
    @property
    def source_name(self) -> str:
        d = type(self)._file_date or "unknown date"
        return f"{self.SOURCE_LABEL}, file dated {d}"

    def _blocked(self, reason: str) -> TaxStatus:
        return TaxStatus(county=self.county, verified=False, error=reason,
                         payments_available=False, source_name=self.source_name)

    def find_account(self, *, owner: str = "", address: str = "") -> list[dict]:
        rows = type(self)._cache or {}
        out = []
        if owner:
            key = _norm(owner)
            lead = key.split()[0] if key.split() else ""
            for acct, r in rows.items():
                o = _norm(r.get("owner", ""))
                if not o or not lead:
                    continue
                if o.startswith(key[:14]) or (lead and o.startswith(lead)):
                    out.append({"account": acct, "owner": r.get("owner", ""),
                                "site_address": r.get("site_address", ""),
                                "long_account": ""})
        elif address:
            want = _addr_key(address)
            if want:
                for acct, r in rows.items():
                    if _addr_key(r.get("site_address", "")) == want:
                        out.append({"account": acct, "owner": r.get("owner", ""),
                                    "site_address": r.get("site_address", ""),
                                    "long_account": ""})
        return out[:25]

    def lookup(self, account: str) -> TaxStatus:
        rows = type(self)._cache
        if rows is None:
            return self._blocked(f"{self.county} bulk roll not loaded")
        if not rows:
            return self._blocked(
                f"{self.county} bulk roll file missing — run fetch_bulk_rolls.py")
        acct = str(account or "").strip()
        r = rows.get(acct) or rows.get(acct.lstrip("0")) or rows.get(acct.upper())
        if r is None:
            # a delinquent roll only lists people who OWE. Not being in it is
            # meaningful, but it is not proof of $0 — the account may simply not
            # be in this file's scope. Report it as unverified, not paid-in-full.
            return self._blocked(
                f"account {acct} not in the {self.county} delinquent roll "
                f"(dated {type(self)._file_date}) — not proof of $0")
        st = TaxStatus(
            county=self.county, verified=True, account=acct,
            owner=r.get("owner", ""), site_address=r.get("site_address", ""),
            total_due=r.get("total_due"),
            active_lawsuit=r.get("lawsuit", ""),
            payments_available=False,
            source_name=self.source_name,
            source_url=type(self)._loaded_from,
            verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        return st


class BellRollAdapter(_BulkAdapter):
    """Bell — BellCAD_Delinquent_Roll_Condensed_*.xlsx (bellcad.org/data-portal).

    Sheet `Public_Delinquent_Data`, one row per delinquent property:
      prop_id | Base_Tax_Due | years_owed | situs_address | Ownr_Name | DataDate
    Carries owner AND situs, so owner/address resolution works like a portal.
    `years_owed` is a range string ("2016-2021") — handy for how deep the hole is.
    """

    COUNTY = "BELL"
    SOURCE_LABEL = "Bell CAD published delinquent roll"

    def _load(self) -> dict:
        path = _newest(r"BellCAD.*Delinquent.*\.xlsx$")
        if not path:
            return {}
        type(self)._loaded_from = path
        out = {}
        for row in _xlsx_rows(path, "Public_Delinquent_Data"):
            acct = str(row.get("prop_id") or "").strip()
            if not acct or acct.lower() == "none":
                continue
            due = _money(row.get("Base_Tax_Due"))
            addr = " ".join(str(row.get(k) or "").strip()
                            for k in ("situs_address", "situs_city")
                            if str(row.get(k) or "").lower() != "none").strip()
            out[acct] = {"owner": str(row.get("Ownr_Name") or "").strip(),
                         "site_address": addr, "total_due": due,
                         "years_owed": str(row.get("years_owed") or "").strip(),
                         "lawsuit": ""}
            if not type(self)._file_date:
                d = str(row.get("DataDate") or "")[:10]
                if d:
                    type(self)._file_date = d
        if not type(self)._file_date:
            type(self)._file_date = datetime.fromtimestamp(
                os.path.getmtime(path)).strftime("%Y-%m-%d")
        return out


class CameronRollAdapter(_BulkAdapter):
    """Cameron — Txtrwt5000_*.zip (cameroncountytx.gov tax office reports).

    Sheet `Txtrwt5000`:
      ACCOUNT | ASDVALUE | DELDUE | CURDUE | TOTALDUE | FRSTYRDELQ | CAUSE

    LIMITATION: this file carries NO owner name and NO situs address, so
    find_account() cannot resolve by owner — the lead's account number has to
    come from the researcher summary. Leads without one stay blocked. CAUSE is
    the tax-suit cause number when the county has sued.
    """

    COUNTY = "CAMERON"
    SOURCE_LABEL = "Cameron County delinquent tax file"

    def _load(self) -> dict:
        path = _newest(r"Txtrwt5000.*\.zip$")
        if not path:
            return {}
        type(self)._loaded_from = path
        type(self)._file_date = datetime.fromtimestamp(
            os.path.getmtime(path)).strftime("%Y-%m-%d")
        m = re.search(r"(\d{4})(\d{2})(\d{2})", os.path.basename(path))
        if m:
            type(self)._file_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        out = {}
        with zipfile.ZipFile(path) as zf:
            member = next((i.filename for i in zf.infolist()
                           if i.filename.lower().endswith(".xlsx")), None)
            if not member:
                return {}
            data = zf.read(member)
        for row in _xlsx_rows(io.BytesIO(data)):
            acct = str(row.get("ACCOUNT") or "").strip()
            if not acct:
                continue
            cause = str(row.get("CAUSE") or "").strip()
            out[acct] = {"owner": "", "site_address": "",
                         "total_due": _money(row.get("TOTALDUE")),
                         "assessed_value": _money(row.get("ASDVALUE")),
                         "first_year_delinquent": str(row.get("FRSTYRDELQ") or "").strip(),
                         "lawsuit": cause if cause and cause != "0" else ""}
        return out


class BrazoriaRollAdapter(_BulkAdapter):
    """Brazoria — TaxRoll_Brazoria_Excel.xlsx (county tax office, weekly).

    The county regenerates this every Friday and posts it Monday. It lives on a
    public OneDrive share whose anonymous download endpoints all 401 to a
    script, so the file has to be saved manually into state/bulk/ once a week
    (or by a signed-in sync). Until it is present this returns blocked, never a
    guess.

    IMPORTANT, straight off the county's own page: "Amount due does not include
    penalties, interest and collection fees." So this balance reads LOW versus
    what a payoff actually costs. Any note built from it has to say so.
    """

    COUNTY = "BRAZORIA"
    SOURCE_LABEL = ("Brazoria County delinquent tax roll "
                    "(excl. penalties/interest/fees)")

    def _load(self) -> dict:
        # The county posts the roll as CSV and as xlsx; accept either.
        path = _newest(r"TaxRoll_Brazoria.*\.(xlsx|csv)$")
        if not path:
            return {}
        type(self)._loaded_from = path
        type(self)._file_date = datetime.fromtimestamp(
            os.path.getmtime(path)).strftime("%Y-%m-%d")
        rows = (_csv_rows(path) if path.lower().endswith(".csv")
                else _xlsx_rows(path))
        out = {}
        for row in rows:
            keys = {k.lower().replace(" ", "").replace("_", ""): k
                    for k in row if k}

            def pick(*names):
                for n in names:
                    if n in keys:
                        return row[keys[n]]
                return None

            acct = str(pick("account", "accountno", "acct", "accountnumber",
                            "propertyid", "taxaccountnumber") or "").strip()
            if not acct:
                continue
            # TOTAL AMOUNT DUE is the full delinquent balance; AMOUNT DUE is the
            # current-year slice. Both columns exist in the CSV, so ask for the
            # total FIRST — the old order silently understated multi-year debt.
            total = _money(pick("totalamountdue", "amountdue", "totaldue",
                                "balance"))
            # ADDRESS 1 / CITY are the OWNER'S MAILING address. The situs lives
            # in STREET NUMBER + STREET NAME and must be built from those, or a
            # parcel gets matched on where its owner gets mail.
            situs = str(pick("propertyaddress", "situs", "situsaddress",
                             "location") or "").strip()
            if not situs:
                num = str(pick("streetnumber") or "").strip().lstrip("0")
                street = str(pick("streetname") or "").strip()
                situs = f"{num} {street}".strip()
            out[acct] = {
                "owner": str(pick("owner", "ownername", "name",
                                  "name1") or "").strip(),
                "site_address": situs,
                "total_due": total,
                "lawsuit": str(pick("suitnumber", "suit") or "").strip(),
            }
        return out



class TaylorRollAdapter(_BulkAdapter):
    """Taylor - TaylorCAD_CollData_Delinquent_*.zip (taylor-cad.org/data-downloads).

    Fixed-width `coll_dat.txt`, 1459-byte records, per the county's published
    "True Automation Collection Transfer File Layout 8.1.0.x" spec
    (taylor-cad.org/wp-content/uploads/2023/12/CollectionTransferFileLayout_8_1_0_x.pdf,
    also mirrored by Bell CAD - the two counties run the same export).

    Offsets below are 1-indexed inclusive, straight from that spec. They are
    NOT inferred: summing base_mno_due + base_ins_due per entity/tax-year
    reproduces the county's own `coll_tot.txt` control totals on 452/452 keys,
    grand total $4,664,110.57 to the cent (verified 2026-08-27). An earlier
    attempt to guess the offsets landed on stmnt_id (794-805) and matched only
    35% - which is exactly why the file sat unparsed rather than guessed.

    ONE ROW PER BILL, not per property: a parcel has a separate record for each
    taxing entity (city/county/school) and each delinquent year. The balance is
    the SUM of every row sharing a prop_id.

    LIMITATION: the layout carries the OWNER'S MAILING address (addr_line1..3),
    never a situs address, so a parcel cannot be disambiguated by property
    address the way the ACT counties can. Owner name and legal description are
    all that is available.
    """

    COUNTY = "TAYLOR"
    SOURCE_LABEL = "Taylor CAD published delinquent roll"

    # (start, end) 1-indexed inclusive, from the spec's Data Layout table
    F_PROP_ID = (1, 12)
    F_OWNER = (80, 149)
    F_ADDR1 = (150, 209)
    F_LEGAL = (486, 740)
    F_ENTITY_CD = (785, 789)
    F_TAX_YR = (790, 793)
    F_MNO_DUE = (893, 908)
    F_INS_DUE = (909, 924)
    F_SUIT = (935, 984)
    F_DEFER_BEGIN = (1188, 1212)
    F_DEFER_END = (1213, 1237)
    RECLEN = 1459

    @staticmethod
    def _f(rec: str, span) -> str:
        return rec[span[0] - 1:span[1]]

    def _load(self) -> dict:
        path = _newest(r"TaylorCAD.*Delinquent.*\.zip$")
        if not path:
            return {}
        type(self)._loaded_from = path
        type(self)._file_date = datetime.fromtimestamp(
            os.path.getmtime(path)).strftime("%Y-%m-%d")
        m = re.search(r"(\d{2})([A-Za-z]{3})(\d{2})", os.path.basename(path))
        if m:
            try:
                d = datetime.strptime(f"{m.group(1)}{m.group(2)}{m.group(3)}", "%d%b%y")
                type(self)._file_date = d.strftime("%Y-%m-%d")
            except ValueError:
                pass

        with zipfile.ZipFile(path) as zf:
            member = next((i.filename for i in zf.infolist()
                           if i.filename.lower().endswith("coll_dat.txt")), None)
            if not member:
                return {}
            raw = zf.read(member)

        out: dict = {}
        step = self.RECLEN
        for off in range(0, len(raw) - step + 1, step):
            rec = raw[off:off + step].decode("ascii", "replace")
            acct = self._f(rec, self.F_PROP_ID).strip().lstrip("0") or "0"
            try:
                due = (int(self._f(rec, self.F_MNO_DUE))
                       + int(self._f(rec, self.F_INS_DUE))) / 100
            except ValueError:
                continue
            e = out.get(acct)
            if e is None:
                suit = self._f(rec, self.F_SUIT).strip()
                defer = self._f(rec, self.F_DEFER_BEGIN).strip()
                e = out[acct] = {
                    "owner": self._f(rec, self.F_OWNER).strip(),
                    # mailing address, NOT situs - see class docstring
                    "site_address": "",
                    "mailing_address": self._f(rec, self.F_ADDR1).strip(),
                    "legal_desc": self._f(rec, self.F_LEGAL).strip(),
                    "total_due": 0.0,
                    "lawsuit": suit,
                    "deferral": defer,
                    "years": set(),
                }
            e["total_due"] = round(e["total_due"] + due, 2)
            yr = self._f(rec, self.F_TAX_YR).strip()
            if yr:
                e["years"].add(yr)
            if not e["lawsuit"]:
                e["lawsuit"] = self._f(rec, self.F_SUIT).strip()

        for e in out.values():
            e["first_year_delinquent"] = min(e["years"]) if e["years"] else ""
            e.pop("years", None)
        return out


# Taylor's spec was located and applied 2026-08-27 (the layout PDF is public on
# taylor-cad.org/data-downloads, mirrored at bellcad.org), so the offsets are
# now read from the document instead of guessed, and validated against the
# county's own control totals. See TaylorRollAdapter.

# BRAZORIA moved to the live portal 2026-09-06: brazoria.propertytaxpayments.net
# is the same vendor Liberty migrated to, and it answers plain HTTP. The live
# balance INCLUDES penalties/interest/fees, which this roll file explicitly
# excludes — Leon Almanza read $4,005.89 from the roll vs $6,750.59 live — and
# the portal also returns the situs address, which resolves the owner-name
# ambiguity that blocked 6 Brazoria leads. The roll adapter is kept below for
# reference/fallback but is no longer registered.
# CAMERON moved to a live lookup 2026-09-19 (cameron_live.py): the roll is
# keyed on the 16-digit tax account but notes carry the CAD Property Id, so
# every Cameron lead missed. The roll adapter stays for reference only.
BULK_COUNTIES = {
    "BELL": (BellRollAdapter, None),
    "TAYLOR": (TaylorRollAdapter, None),
}

# The live registry is ACT_COUNTIES and get_adapter() reads it, so registering
# there is all it takes for tax_watch / vault_tax_report to pick these up.
ca.ACT_COUNTIES.update(BULK_COUNTIES)
ca.UNSUPPORTED = [c for c in ca.UNSUPPORTED if c not in BULK_COUNTIES]
