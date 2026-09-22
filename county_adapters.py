"""Live county tax lookups — deterministic adapters, one per portal engine.

WHY THIS EXISTS
---------------
Raul, 2026-07-19: "you can never count on the Lofty notes, you always have to
do your own county search every time — that's the whole point of the bot."

The original tax path drove Playwright + an LLM against a per-county "playbook"
of CSS selectors. That is fragile three ways at once (headless browser, selector
drift, LLM extraction) and on Render it failed often enough that the bot fell
back to researcher-note figures and reported them as verified. This module
replaces that with plain HTTP + explicit parsing for the portals we understand.

CONTRACT
--------
Every adapter returns a TaxStatus. If anything at all goes wrong the result is
`verified=False` with a human-readable `error`, and `total_due is None`. There
is NO path in this module that returns a number sourced from anywhere but the
live county page. A caller that receives verified=False must BLOCK the lead —
never substitute a note value.

COVERAGE (2026-08-01)
---------------------
ACT (actweb.acttax.com) — plain HTTP, no captcha, no browser:
    Galveston (`galveston`), Fort Bend (`fbc`), Montgomery (`montgomery`),
    Jefferson (`jefferson`)
Own adapters: Ector (ACT modern skin), Liberty (TaxSys), Caldwell (BIS CAD +
    ArcGIS, balance only), San Jacinto (Tyler ProxyT, balance only).
Still blocked, each for a specific reason — see the notes on the classes and on
UNSUPPORTED below:
    Brazoria  — Cloudflare guards the balance endpoint (search works)
    Bell, Taylor — reCAPTCHA on the CAD search (confirmed still present
                   2026-08-01); not to be defeated, needs a feed or a human
    Walker, Harris, Cameron — not yet probed
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# Be a good citizen — ACT's terms call out high-volume automated traffic.
REQUEST_DELAY_SEC = 1.5


@dataclass
class Payment:
    date: Optional[str]          # YYYY-MM-DD
    amount: float                # negative for reversals/transfers out
    tax_year: str
    kind: str                    # "Payment" | "Transfer"
    payer: str

    def days_ago(self, today: Optional[date] = None) -> Optional[int]:
        if not self.date:
            return None
        try:
            return ((today or datetime.now(timezone.utc).date())
                    - date.fromisoformat(self.date)).days
        except ValueError:
            return None


@dataclass
class TaxStatus:
    county: str
    verified: bool = False
    account: str = ""
    owner: str = ""
    site_address: str = ""
    total_due: Optional[float] = None
    payments: list[Payment] = field(default_factory=list)
    active_lawsuit: str = ""
    source_url: str = ""
    verified_at: str = ""
    error: Optional[str] = None
    # Some portals (e.g. the CAD tax table) publish the balance owed but NOT a
    # dated payment history with payer names. When False, callers must NOT say
    # "no payments on record" — we simply can't see them here.
    payments_available: bool = True
    # How to name the source in the posted note (defaults to the tax office).
    source_name: str = "County Tax Office"

    def real_payments(self) -> list[Payment]:
        """Actual money in, newest first. Excludes transfers and reversals."""
        return sorted([p for p in self.payments if p.kind == "Payment" and p.amount > 0],
                      key=lambda p: p.date or "", reverse=True)

    def last_payment(self) -> Optional[Payment]:
        r = self.real_payments()
        return r[0] if r else None

    def payments_within(self, days: int, today: Optional[date] = None) -> list[Payment]:
        out = []
        for p in self.real_payments():
            ago = p.days_ago(today)
            if ago is not None and ago <= days:
                out.append(p)
        return out


def _clean(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?s)<!--.*?-->", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def _rows(html: str) -> list[list[str]]:
    out = []
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", html):
        cells = [
            re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", "", c))
            .replace("&nbsp;", " ").replace("&amp;", "&").strip()
            for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)
        ]
        cells = [c for c in cells if c]
        if cells:
            out.append(cells)
    return out


def _txt(cell_html: str) -> str:
    """Visible text of one table cell."""
    t = re.sub(r"(?s)<[^>]+>", "", cell_html)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&#xF1;", "n").replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", t).strip()


def _money(s: str) -> Optional[float]:
    if s is None:
        return None
    neg = s.strip().startswith("(")
    m = re.search(r"[\d,]+\.\d{2}|\d[\d,]*", s.replace("$", ""))
    if not m:
        return None
    try:
        v = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return -v if neg else v


#: Page furniture that follows the last labelled field on some ACT skins.
#: "Active Lawsuits" is the final recognised label on the Ector layout, so
#: _field()'s "read to the next label, else to end of text" rule swallowed the
#: whole nav strip into the value and notes went out reading "Active lawsuit:
#: None Jurisdiction Information Register for a 2026 Certified Tax Statement by
#: E-Mail What-If Tax Calculator ...". Cut the value at any of these.
_BOILERPLATE = re.compile(
    r"\s*(?:Jurisdiction Information|Register for a|What-?If Tax Calculator|"
    r"Taxes Due Detail|Payment Information for|Add to Cart|"
    r"Account Is Fully Paid|Show levy detail|Print this page)\b.*", re.I | re.S)

_NO_LAWSUIT = {"", "none", "n/a", "na", "no", "no active lawsuits"}


def _clean_lawsuit(value: str) -> str:
    """The lawsuit reference alone, or "" when the county says there is none.

    Returns "" rather than the literal "None" so build_note() drops the clause
    instead of printing it.
    """
    v = _BOILERPLATE.sub("", (value or "")).strip(" .;,-")
    return "" if v.lower() in _NO_LAWSUIT else v


class ACTAdapter:
    """actweb.acttax.com — the ACT tax-collector platform.

    Account-number gotcha (cost an hour on 2026-07-19): `can` is the SHORT
    account number, which equals the Lofty note's "Property Id" — NOT the
    geo / "Long Account Number". Passing the geo returns "Invalid Account".
    """

    SEARCH_BY = {"owner": "3", "address": "6", "account": "4", "long_account": "5"}

    # Labels on the detail page, used as the boundary set so a field capture
    # stops at the next label instead of swallowing the rest of the page.
    _LABELS = (r"Account Number|Long Account Number|Address|Property Site Address|"
               r"Legal Description|Current Tax Levy|Current Amount Due|"
               r"Prior Year Amount Due|Total Amount Due|Last Payment Amount[^:]*|"
               r"Last Payer[^:]*|Last Payment Date[^:]*|Active Lawsuits|"
               r"Pending Internet Payments|Market Value|Land Value|"
               r"Improvement Value|Capped Value|Agricultural Value|Exemptions|"
               r"Exemption and Tax Rate Information")

    def __init__(self, county: str, slug: str, session: Optional[requests.Session] = None):
        self.county = county
        self.slug = slug
        self.base = f"https://actweb.acttax.com/act_webdev/{slug}"
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA, "Referer": f"{self.base}/index.jsp"})
        self._primed = False

    def _prime(self):
        if not self._primed:
            self.s.get(f"{self.base}/index.jsp", timeout=30)
            self._primed = True

    def _field(self, text: str, label: str) -> str:
        m = re.search(re.escape(label) + r":\s*(.*?)(?=\s+(?:" + self._LABELS + r")\b:|$)", text)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _total_due(text: str) -> Optional[float]:
        """Read the balance. The label is NOT identical across ACT counties:
        Galveston prints 'Total Amount Due:' but Fort Bend prints
        'Total Amount Due for 2025 & Prior Years:' — an exact-label match
        silently failed on every Fort Bend lead in the 2026-07-19 run.
        """
        m = re.search(r"Total Amount Due[^:$]{0,40}:\s*\$?([\d,]+\.\d{2})", text)
        if m:
            return _money(m.group(1))
        # Some skins only itemise current + prior year.
        cur = re.search(r"Current (?:Year )?(?:Amount )?Due[^:$]{0,20}:\s*\$?([\d,]+\.\d{2})", text)
        pri = re.search(r"Prior Year (?:Amount )?Due[^:$]{0,20}:\s*\$?([\d,]+\.\d{2})", text)
        if cur or pri:
            return (_money(cur.group(1)) or 0.0 if cur else 0.0) + \
                   (_money(pri.group(1)) or 0.0 if pri else 0.0)
        return None

    def find_account(self, *, owner: str = "", address: str = "",
                     long_account: str = "") -> list[dict]:
        """Search the portal. Returns [{account, owner, site_address, long_account}].

        long_account = the geo id without dashes; it pins ONE parcel where an
        address can't (63 condo units at 661 Bering Dr all share the address).
        """
        self._prime()
        if long_account:
            crit, by = re.sub(r"\D", "", long_account), self.SEARCH_BY["long_account"]
        elif owner:
            crit, by = owner, self.SEARCH_BY["owner"]
        elif address:
            crit, by = address, self.SEARCH_BY["address"]
        else:
            return []
        r = self.s.post(f"{self.base}/showlist.jsp",
                        data={"criteria": crit, "searchby": by,
                              "subcriteria": "", "submit": "Search"}, timeout=30)
        time.sleep(REQUEST_DELAY_SEC)
        out = []
        for cells in _rows(r.text):
            # Result rows start with a duplicated account number ("116680 116680")
            m = re.match(r"^(\d+)(?:\s+\1)?$", cells[0].strip())
            if m and len(cells) >= 4:
                out.append({"account": m.group(1), "owner": cells[1],
                            "site_address": cells[2],
                            "long_account": cells[-1] if cells[-1].isdigit() else ""})
        return out

    def lookup(self, account: str) -> TaxStatus:
        """Full live status for one account: balance + complete payment history."""
        st = TaxStatus(county=self.county, account=str(account))
        url = f"{self.base}/showdetail2.jsp?can={account}"
        st.source_url = url
        try:
            self._prime()
            r = self.s.get(f"{self.base}/showdetail2.jsp",
                           params={"can": account}, timeout=30)
            time.sleep(REQUEST_DELAY_SEC)
        except requests.RequestException as e:
            st.error = f"detail request failed: {type(e).__name__}"
            return st
        if r.status_code != 200:
            st.error = f"detail HTTP {r.status_code}"
            return st

        text = _clean(r.text)
        if "Invalid Account" in text or "does not exist" in text:
            st.error = (f"invalid account '{account}' — for ACT, `can` must be the SHORT "
                        f"account number (Lofty 'Property Id'), not the geo/long account")
            return st
        if "Account Number" not in text:
            st.error = "detail page did not render an account record"
            return st

        st.owner = self._field(text, "Address")
        st.site_address = self._field(text, "Property Site Address")
        st.active_lawsuit = _clean_lawsuit(self._field(text, "Active Lawsuits"))
        st.total_due = self._total_due(text)
        if st.total_due is None:
            st.error = "could not read the total-due figure from the live page"
            return st

        st.payments = self._payments(account)
        st.verified = True
        st.verified_at = datetime.now(timezone.utc).isoformat()
        return st

    def _payments(self, account: str) -> list[Payment]:
        """Full receipt history.

        CRITICAL: the detail page's "Last Payment Amount / Last Payer / Last
        Payment Date for Current Year Taxes" fields cover ONLY the current tax
        year. On delinquent back-tax accounts they read "Not Received" even when
        real payments exist — on 2026-07-19 that was all 10 of 10 test leads.
        Trusting them = 100% false negatives. This page is the real source.
        """
        try:
            r = self.s.get(f"{self.base}/reports/paymentinfo.jsp",
                           params={"can": account, "ownerno": "0"}, timeout=30)
            time.sleep(REQUEST_DELAY_SEC)
        except requests.RequestException:
            return []
        if r.status_code != 200:
            return []
        out = []
        for cells in _rows(r.text):
            if len(cells) >= 5 and re.match(r"^\d{4}-\d{2}-\d{2}$", cells[0]):
                amt = _money(cells[1])
                if amt is None:
                    continue
                out.append(Payment(date=cells[0], amount=amt, tax_year=cells[2],
                                   kind=cells[3].strip(), payer=cells[4].strip()))
        return out


class ACTModernAdapter(ACTAdapter):
    """The newer ACT skin, e.g. Ector: actweb.acttax.com/ectorcad/ectorcad/.

    Differences from the classic /act_webdev/ layout:
      * detail   -> account-details.jsp   (not showdetail2.jsp)
      * payments -> reports/payment-info.jsp  (hyphen; not paymentinfo.jsp)
      * search   -> search-result.jsp, and the results come back as a JSON blob
                    embedded in the page rather than an HTML table.
      * the search POST is rejected unless Origin/Accept/Accept-Language are
        sent — a bare requests POST silently returns an empty result set.

    Ector-specific gotcha: its researcher notes carry NO Property Id, so the
    account must be resolved by owner-name search and then cached.
    """

    _LABELS = (r"Account Number|Mailing Address|Property Site Address|"
               r"Legal Description|Jurisdictions Collected|"
               r"Active Lawsuits & Bankruptcies|Jurisdiction Information|"
               r"Current Year Levy|Current Year Due|Prior Year Due|"
               r"Total Amount Due|Show levy detail|Total Due")

    def __init__(self, county, slug, session=None):
        super().__init__(county, slug, session=session)
        self.base = f"https://actweb.acttax.com/{slug}/{slug}"
        self.s.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://actweb.acttax.com",
            "Referer": f"{self.base}/index.jsp",
        })

    def find_account(self, *, owner: str = "", address: str = "") -> list[dict]:
        self._prime()
        crit, stype = (owner, "owner") if owner else (address, "address")
        if not crit:
            return []
        r = self.s.post(f"{self.base}/search-result.jsp",
                        data={"criteria": crit, "search-type": stype,
                              "altCriteria": ""}, timeout=30)
        time.sleep(REQUEST_DELAY_SEC)
        m = re.search(r'var result\s*=\s*(\{.*?\});', r.text, re.S)
        if not m:
            return []
        import json as _json
        try:
            data = _json.loads(m.group(1)).get("data") or {}
        except ValueError:
            return []
        def _txt(v: str) -> str:
            # Fields carry raw markup: "FAMBRO MARY E<br>2201 VENTURA AVE<br>..."
            v = re.sub(r"(?i)<br\s*/?>", ", ", str(v or ""))
            v = re.sub(r"<[^>]+>", " ", v).replace("&nbsp;", " ").replace("&amp;", "&")
            return re.sub(r"\s+", " ", v).strip().strip(",").strip()

        out = []
        for a in data.get("accounts", []):
            # The site-address key is `address` — NOT `addr`. Getting this wrong
            # left site_address empty, which silently disabled multi-parcel
            # disambiguation and blocked every multi-parcel owner (Fambro owns
            # 8 Ector parcels) as "ambiguous".
            out.append({"account": a.get("can", ""),
                        "owner": _txt(a.get("owner")),
                        "site_address": _txt(a.get("address")),
                        "long_account": a.get("aprdistacc", "")})
        return out

    def lookup(self, account: str) -> TaxStatus:
        st = TaxStatus(county=self.county, account=str(account))
        st.source_url = f"{self.base}/account-details.jsp?can={account}"
        try:
            self._prime()
            r = self.s.get(f"{self.base}/account-details.jsp",
                           params={"can": account}, timeout=30)
            time.sleep(REQUEST_DELAY_SEC)
        except requests.RequestException as e:
            st.error = f"detail request failed: {type(e).__name__}"
            return st
        if r.status_code != 200:
            st.error = f"detail HTTP {r.status_code}"
            return st
        text = _clean(r.text)
        if "Account Number" not in text:
            st.error = f"no account record for '{account}'"
            return st
        st.owner = self._field(text, "Mailing Address")
        st.site_address = self._field(text, "Property Site Address")
        st.active_lawsuit = _clean_lawsuit(
            self._field(text, "Active Lawsuits & Bankruptcies"))
        st.total_due = _money(self._field(text, "Total Amount Due"))
        if st.total_due is None:
            st.error = "could not read 'Total Amount Due' from the live page"
            return st
        st.payments = self._payments(account)
        st.verified = True
        st.verified_at = datetime.now(timezone.utc).isoformat()
        return st

    def _payments(self, account: str) -> list[Payment]:
        try:
            r = self.s.get(f"{self.base}/reports/payment-info.jsp",
                           params={"can": account, "ownerno": "0"}, timeout=30)
            time.sleep(REQUEST_DELAY_SEC)
        except requests.RequestException:
            return []
        if r.status_code != 200:
            return []
        out = []
        for cells in _rows(r.text):
            if len(cells) >= 5 and re.match(r"^\d{4}-\d{2}-\d{2}$", cells[0]):
                amt = _money(cells[1])
                if amt is None:
                    continue
                out.append(Payment(date=cells[0], amount=amt, tax_year=cells[2],
                                   kind=cells[3].strip(), payer=cells[4].strip()))
        return out


class TaxSysAdapter:
    """The '/Accounts/AccountDetails' platform — Liberty (and Brazoria).

    Cracked 2026-07-19. Everything is a plain GET, no JS execution needed,
    despite the site looking like a single-page app:
      search -> /Search/Results?Query.SearchField=N&Query.SearchText=...
      detail -> /Accounts/AccountDetails?taxAccountNumber=N&DisplayYear=2
    `DisplayYear=2` is "All Years" and is what exposes the full receipt list;
    the default view only shows the current year.

    Brazoria runs the SAME software at tax.brazoriacountytx.gov but sits behind
    Cloudflare, so it is NOT registered here.

    Re-probed 2026-08-01, and the picture is narrower than "Brazoria is walled":
      * GET /Search/Results          -> 200. Owner search WORKS and returns the
                                        account, owner and situs address.
      * GET /Accounts/AccountDetails -> 403 with an encrypted challenge body,
                                        even after priming the session on the
                                        root + search (CF cookie present) and
                                        sending a same-origin Referer.
    So Cloudflare guards precisely the endpoint carrying the balance. Account
    RESOLUTION is solved for Brazoria; only the money is out of reach. If a
    licensed feed or bulk file ever supplies the balance, wire it to the search
    above. Do not try to defeat the challenge.
    """

    SEARCH_BY = {"account": "1", "owner": "2", "mailing": "3",
                 "address": "5", "cad": "6"}

    def __init__(self, county: str, base: str, session: Optional[requests.Session] = None):
        self.county = county
        self.base = base.rstrip("/")
        self.s = session or requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def find_account(self, *, owner: str = "", address: str = "") -> list[dict]:
        crit, by = (owner, "owner") if owner else (address, "address")
        if not crit:
            return []
        try:
            r = self.s.get(f"{self.base}/Search/Results", params={
                "Query.SearchField": self.SEARCH_BY[by],
                "Query.SearchText": crit,
                "Query.SearchAction": "",
                "Query.PropertyType": "",
                "Query.IncludeInactiveAccounts": "False",
                "Query.PayStatus": "Both",
            }, timeout=30)
            time.sleep(REQUEST_DELAY_SEC)
        except requests.RequestException:
            return []
        if r.status_code != 200:
            return []
        out = []
        for cells in _rows(r.text):
            # ACCOUNT NO. | OWNER NAME | PROPERTY LOCATION | LEGAL | TYPE | TOTAL DUE
            if len(cells) >= 6 and re.match(r"^\d{3,}$", cells[0].strip()):
                out.append({"account": cells[0].strip(), "owner": cells[1],
                            "site_address": cells[2], "long_account": ""})
        return out

    def lookup(self, account: str) -> TaxStatus:
        st = TaxStatus(county=self.county, account=str(account))
        st.source_url = (f"{self.base}/Accounts/AccountDetails"
                         f"?taxAccountNumber={account}&DisplayYear=2")
        try:
            r = self.s.get(f"{self.base}/Accounts/AccountDetails",
                           params={"taxAccountNumber": account,
                                   "DisplayYear": "2", "SortOrder": "1"}, timeout=30)
            time.sleep(REQUEST_DELAY_SEC)
        except requests.RequestException as e:
            st.error = f"detail request failed: {type(e).__name__}"
            return st
        if r.status_code == 403:
            st.error = "403 — site is behind a bot challenge; needs a licensed feed"
            return st
        if r.status_code != 200:
            st.error = f"detail HTTP {r.status_code}"
            return st

        text = _clean(r.text)
        m = re.search(r"Account:\s*" + re.escape(str(account)) + r"\s*Total Due\s*(\$[\d,]+\.\d{2})", text)
        if not m:
            m = re.search(r"Total Due\s*(\$[\d,]+\.\d{2})", text)
        if not m:
            st.error = f"could not read 'Total Due' for account {account}"
            return st
        st.total_due = _money(m.group(1))
        mo = re.search(r"Basic Information\s*Owner\s*(.*?)\s*Type\s", text)
        if mo:
            st.owner = mo.group(1).strip()[:120]
            st.site_address = st.owner
        st.payments = self._payments(r.text)
        st.verified = True
        st.verified_at = datetime.now(timezone.utc).isoformat()
        return st

    _PAY_RE = re.compile(
        r"Payment Date\s*\|\s*([\d]{1,2}/[\d]{1,2}/\d{4})\s*\|\s*"
        r"Payment Amount\s*\|\s*(-?\$[\d,]+\.\d{2})\s*\|\s*"
        r"Tax Year Paid\s*\|\s*(\d{4})\s*\|\s*"
        r"Payer\s*\|\s*([^|]{1,60}?)\s*\|", re.S)

    def _payments(self, html: str) -> list[Payment]:
        t = re.sub(r"(?s)<[^>]+>", "|", html)
        t = re.sub(r"(\|\s*)+", "|", t)
        t = re.sub(r"[ \t]+", " ", t)
        out = []
        for m in self._PAY_RE.finditer(t):
            mm, dd, yy = m.group(1).split("/")
            iso = f"{yy}-{int(mm):02d}-{int(dd):02d}"
            amt = _money(m.group(2))
            if amt is None:
                continue
            payer = m.group(4).strip()
            # Internal reallocations show up as a payer of "PAYMENT TRANSFER"
            # (and usually a negative amount) — not real money from a person.
            kind = "Transfer" if ("TRANSFER" in payer.upper() or amt < 0) else "Payment"
            out.append(Payment(date=iso, amount=amt, tax_year=m.group(3),
                               kind=kind, payer=payer))
        return out


class CaldwellCADAdapter:
    """Caldwell — esearch.caldwellcad.org (BIS CAD) + its ArcGIS feature service.

    Cracked 2026-07-21. Caldwell has no plain-HTTP tax-collector portal, but the
    CAD property page carries a full per-year, per-jurisdiction tax table with
    a 'Base Tax Due' / 'Amount Due' column — the live delinquent balance.

      * account = the CAD prop_id (e.g. 25449).
      * search  = the /Search route is session-walled (redirects to
        /Search/Expired), so we resolve owner/address -> prop_id through the
        public ArcGIS feature service instead (no session needed).
      * detail  = GET /Property/View/{prop_id}; sum the last column ('Amount
        Due') across every per-jurisdiction row for total owed.

    LIMITATION: the CAD table shows amounts (levied / paid / due) but NOT
    payment DATES or PAYER names, so payments_available=False. We can report the
    balance (still-owed vs paid-in-full) but not "paid $X on <date> by <payer>".
    """

    GIS = ("https://services.arcgis.com/rVxY74DxxIDrDbc0/ArcGIS/rest/services/"
           "CaldwellCADWebService/FeatureServer/0/query")
    BASE = "https://esearch.caldwellcad.org"

    def __init__(self, county: str = "CALDWELL", slug=None, session: Optional[requests.Session] = None):
        self.county = county
        # Other counties run the same eSearch software, and their property page
        # carries the same tax table (Taylor) or at least the real situs address
        # (Fort Bend). Pass the site as `slug` to point this adapter at one.
        if slug:
            self.BASE = str(slug).rstrip("/")
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA,
                               "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                               "Accept-Language": "en-US,en;q=0.9"})

    def _gis(self, where: str) -> list[dict]:
        try:
            r = self.s.get(self.GIS, params={
                "where": where, "outFields": "prop_id,file_as_name,situs_num,situs_street",
                "returnGeometry": "false", "resultRecordCount": "25", "f": "json"}, timeout=30)
            time.sleep(REQUEST_DELAY_SEC)
            feats = r.json().get("features", [])
        except (requests.RequestException, ValueError):
            return []
        out = []
        for f in feats:
            a = f.get("attributes", {})
            situs = " ".join(str(a.get(k) or "").strip() for k in ("situs_num", "situs_street")).strip()
            out.append({"account": str(a.get("prop_id") or ""),
                        "owner": str(a.get("file_as_name") or ""),
                        "site_address": situs, "long_account": ""})
        return [h for h in out if h["account"]]

    def find_account(self, *, owner: str = "", address: str = "") -> list[dict]:
        if owner:
            # last-name-first "SMITH JOHN"; match the leading token(s) generously
            key = re.sub(r"[',]", "", owner.upper()).strip()
            return self._gis(f"file_as_name LIKE '{key.split()[0]}%'")
        if address:
            m = re.match(r"\s*(\d+)\s+([A-Za-z0-9]+)", address)
            if m:
                return self._gis(f"situs_num = '{m.group(1)}' AND situs_street LIKE '{m.group(2).upper()}%'")
        return []

    def lookup(self, account: str) -> TaxStatus:
        st = TaxStatus(county=self.county, account=str(account), payments_available=False,
                       source_name="County Appraisal District tax roll")
        st.source_url = f"{self.BASE}/Property/View/{account}"
        try:
            r = self.s.get(st.source_url, timeout=30)
            time.sleep(REQUEST_DELAY_SEC)
        except requests.RequestException as e:
            st.error = f"detail request failed: {type(e).__name__}"
            return st
        if r.status_code != 200:
            st.error = f"detail HTTP {r.status_code}"
            return st
        if "Property Search" not in r.text and "Taxing" not in r.text:
            st.error = f"no CAD property page for account {account}"
            return st

        total = 0.0
        found_tax_rows = False
        for cells in _rows(r.text):
            # per-jurisdiction row: Year | Jurisdiction | Taxable | Base Tax |
            # Base Taxes Paid | Base Tax Due | Pen&Int | Atty | Amount Due (9 cols)
            if len(cells) >= 9 and re.match(r"^\d{4}$", cells[0]):
                found_tax_rows = True
                amt = _money(cells[-1])
                if amt:
                    total += amt
        if not found_tax_rows:
            st.error = f"no tax table on the CAD page for account {account}"
            return st
        st.total_due = round(total, 2)

        text = _clean(r.text)
        mo = re.search(r"(?:Owner(?: Name)?|file_as_name)\s*:?\s*(.*?)\s+(?:Mailing|Property|Legal|Agent|Situs)", text)
        if mo:
            st.owner = mo.group(1).strip()[:120]
        st.verified = True
        st.verified_at = datetime.now(timezone.utc).isoformat()
        return st


class TylerProxyTAdapter:
    """Tyler Technologies 'ProxyT' portal — San Jacinto (sjc-tax.us).

    Cracked 2026-08-01. The site looks like an AJAX SPA (Kendo UI templates,
    `#: OwnerName #` placeholders in the served HTML) but both halves we need
    are plain GETs:

      search -> /ProxyT/Search/Properties/?f=<text>&ty=<year>   (clean JSON)
      detail -> /Property-Detail/PropertyQuickRefID/<pqr>       (server-rendered)

    The detail page carries "Current Amount Due / Past Years Due / Total Due"
    as real text, so the balance needs no JS.

    TWO GOTCHAS, both load-bearing:

    1. `f=` is a FULL-TEXT search over name AND address AND parcel id. Searching
       "HOLCOMB" returns every property on *Holcomb St* — 25 hits, none of them
       owned by a Holcomb. find_account() therefore filters hits down to ones
       whose OwnerName actually contains the search key. Without that filter the
       caller disambiguates by address and silently locks onto a stranger's
       parcel, which is worse than blocking.

    2. PAYMENT HISTORY IS NOT AVAILABLE over plain HTTP. The "Payment History"
       tab is populated by a POST to /ProxyT/tax/AccountSummary that returns 500
       to an unprimed session. So `payments_available=False`, same honest mode
       as Caldwell: we publish the live balance and say nothing about payments,
       rather than implying "no payments on record".

    The portal rate-limits (HTTP 429) under rapid probing — keep the delay.
    """

    SEARCH_DELAY = 2.5

    def __init__(self, county: str, base: str, session: Optional[requests.Session] = None):
        self.county = county
        self.base = (base or "https://sjc-tax.us").rstrip("/")
        self.s = session or requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/html;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": self.base + "/",
        })
        #: certified tax roll year that actually returns rows, learned per run.
        self._year = ""

    # -- helpers ----------------------------------------------------------
    @staticmethod
    def _owner_key(owner: str) -> str:
        """Leading surname token — what a full-text hit must contain."""
        s = re.sub(r"[^A-Z ]", " ", (owner or "").upper())
        toks = [t for t in s.split() if len(t) > 2]
        return toks[0] if toks else ""

    def _search(self, text: str) -> list[dict]:
        if not text:
            return []
        # The search REQUIRES a tax year — `ty=` empty returns zero rows — and
        # the newest CERTIFIED roll lags the calendar. On 2026-08-01 the current
        # roll was still 2025 (Texas certifies the 2026 roll around October), so
        # a hardcoded "this year" silently returns nothing every spring/summer.
        # Walk back until a year answers, then remember it for this run.
        years = ([self._year] if self._year else
                 [str(datetime.now(timezone.utc).year - n) for n in range(0, 3)])
        for ty in years:
            try:
                r = self.s.get(f"{self.base}/ProxyT/Search/Properties/",
                               params={"f": text, "ty": ty}, timeout=45)
                time.sleep(self.SEARCH_DELAY)
            except requests.RequestException:
                return []
            if r.status_code != 200:
                continue
            try:
                rows = (r.json() or {}).get("ResultList") or []
            except ValueError:
                continue
            if rows:
                self._year = ty
                return rows
        return []

    # -- interface --------------------------------------------------------
    def find_account(self, *, owner: str = "", address: str = "") -> list[dict]:
        crit = owner or address
        hits = self._search(crit)
        if owner:
            # Drop street-name collisions (see gotcha 1).
            key = self._owner_key(owner)
            if key:
                hits = [h for h in hits
                        if key in (h.get("OwnerName") or "").upper()]
        out = []
        for h in hits:
            acct = (h.get("PropertyQuickRefID") or "").strip()
            if not acct:
                continue
            out.append({
                "account": acct,
                "owner": (h.get("OwnerName") or "").strip(),
                "site_address": (h.get("SitusAddress") or "").strip(),
                "long_account": (h.get("PropertyNumber") or "").strip(),
            })
        return out

    _DUE_RE = re.compile(
        r"Current Amount Due\s*\$?([\d,]+\.\d{2})\s*"
        r"Past Years Due\s*\$?([\d,]+\.\d{2})\s*"
        r"Total Due\s*\$?([\d,]+\.\d{2})", re.I)

    def lookup(self, account: str) -> TaxStatus:
        acct = str(account).strip()
        # Leave source_name at its default: build_note already prefixes the
        # county AND adds its own "payment history not published" caveat when
        # payments_available is False. Overriding it here produced
        # "San Jacinto San Jacinto County Tax Office" plus a doubled caveat.
        st = TaxStatus(county=self.county, account=acct,
                       payments_available=False)
        # A Lofty note may carry the dashed parcel number (0014-000-0770)
        # instead of the QuickRefID (R40257); resolve it via search.
        if not re.match(r"^[A-Z]\d+$", acct, re.I):
            hits = self._search(acct)
            if not hits:
                st.error = f"no San Jacinto account matches '{acct}'"
                return st
            if len(hits) > 1:
                st.error = f"'{acct}' matched {len(hits)} San Jacinto accounts"
                return st
            acct = (hits[0].get("PropertyQuickRefID") or "").strip()
            st.account = acct

        url = f"{self.base}/Property-Detail/PropertyQuickRefID/{acct}"
        st.source_url = url
        try:
            r = self.s.get(url, timeout=60)
            time.sleep(self.SEARCH_DELAY)
        except requests.RequestException as e:
            st.error = f"detail request failed: {type(e).__name__}"
            return st
        if r.status_code == 429:
            st.error = "429 — county portal rate-limited this run"
            return st
        if r.status_code != 200:
            st.error = f"detail HTTP {r.status_code}"
            return st

        text = _clean(r.text)
        m = self._DUE_RE.search(text)
        if not m:
            st.error = f"could not read 'Total Due' for account {acct}"
            return st
        st.total_due = _money(m.group(3))
        if st.total_due is None:
            st.error = f"unparseable Total Due for account {acct}"
            return st

        mo = re.search(r"Owner Name\s*(.*?)\s*Owner ID", text)
        if mo:
            st.owner = mo.group(1).strip()[:120]
        ms = re.search(r"Legal Description\s*(.*?)\s*Neighborhood", text)
        if ms:
            st.site_address = ms.group(1).strip()[:120]
        st.verified = True
        st.verified_at = datetime.now(timezone.utc).isoformat()
        return st


class PropertyTaxPaymentsAdapter:
    """*.propertytaxpayments.net — the portal Liberty moved to (2026-09).

    Cracked 2026-09-06 after the weekly run blocked all 31 Liberty leads. The
    OLD site (www.libertycountytax.com, TaxSysAdapter) is now a dead consent
    wall: it answers 200 with nothing but a "Do Not Sell or Share My Personal
    Information" CMP page, so the 'Total Due' regex could never match. The
    county did not break its data, it CHANGED VENDORS.

      * search  = GET /Search/Results?Query.SearchField=N&Query.SearchText=...
                  SearchField: 1=Account, 2=Owner Name, 5=Property Address.
                  Plain HTTP, no session cookie, no challenge.
      * balance = read straight off the results table. Columns are
                  [select, ACCOUNT NO., OWNER, PROPERTY LOCATION, LEGAL, TYPE,
                   TOTAL DUE] — so one request gives owner, situs AND balance.

    LIMITATION: /Accounts/AccountDetails (where the dated payment receipts with
    payer names live) IS behind a Cloudflare challenge — 403 "Just a moment...".
    So this county is balance-only, exactly like Caldwell and San Jacinto:
    payments_available=False. We can say "still owed $X" or "paid in full", but
    never "no payments on record" and never a payer name. Do NOT fall back to
    the researcher note to fill that gap.
    """

    def __init__(self, county: str, base: str, session: Optional[requests.Session] = None):
        self.county = county
        self.base = base.rstrip("/")
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA,
                               "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                               "Accept-Language": "en-US,en;q=0.9"})

    _ROW_RE = re.compile(r"(?s)<tr[^>]*>(.*?)</tr>")
    _CELL_RE = re.compile(r"(?s)<t[dh][^>]*>(.*?)</t[dh]>")

    last_fault = ""   # why the portal didn't give us a results page, if it didn't

    def _query(self, field: str, text: str) -> list[dict]:
        # 9/19 Render run: every Liberty lead came back "no account" while the
        # same accounts verified from the laptop. A refused request used to look
        # exactly like an empty result, so retry and remember what went wrong.
        r = None
        if getattr(self, "_refusals", 0) >= 4:
            # Portal has refused us repeatedly this run (e.g. it blocks the
            # server's IP). Fail fast with the same reason instead of spending
            # ~30s of retries on every remaining lead.
            return []
        self.last_fault = ""
        for wait in (0, 5, 20):
            time.sleep(wait)
            try:
                r = self.s.get(f"{self.base}/Search/Results",
                               params={"Query.SearchField": field,
                                       "Query.SearchText": text,
                                       "Query.IncludeInactiveAccounts": "False",
                                       "Query.PayStatus": "Both"}, timeout=30)
                time.sleep(REQUEST_DELAY_SEC)
            except requests.RequestException as e:
                self.last_fault = f"portal unreachable ({type(e).__name__})"
                r = None
                continue
            if r.status_code == 200 and "Search Results" in r.text:  # real portal page, even with 0 hits
                self.last_fault = ""
                self._refusals = 0
                break
            self.last_fault = (f"portal refused the request (HTTP {r.status_code}"
                               f"{', challenge page' if 'Just a moment' in r.text else ''})")
        if r is None or self.last_fault:
            self._refusals = getattr(self, "_refusals", 0) + 1
            return []
        tb = re.search(r"(?s)<tbody.*?</tbody>", r.text)
        if not tb:
            tb = re.search(r"(?s)<table.*?</table>", r.text)
        if not tb:
            return []
        out = []
        for row in self._ROW_RE.findall(tb.group(0)):
            cells = [_txt(c) for c in self._CELL_RE.findall(row)]
            # [select, account, owner, situs, legal, type, total due]
            if len(cells) < 7 or not re.match(r"^\d+$", cells[1] or ""):
                continue
            out.append({"account": cells[1], "owner": cells[2],
                        "site_address": cells[3], "long_account": "",
                        "_total_due": _money(cells[6])})
        return out

    def find_account(self, *, owner: str = "", address: str = "") -> list[dict]:
        if owner:
            key = re.sub(r"[',]", "", owner.upper()).strip()
            words = re.sub(r"[^A-Z0-9 ]", " ", key).split()
            # The portal wants "LAST FIRST". Notes often say "FIRST LAST", which
            # comes back as ~100 fuzzy matches and blocks the lead as ambiguous
            # (Leon Almanza, Brazoria, 9/19). Try both orders and keep only
            # owners carrying every word of the name.
            orders = [key] + ([" ".join([words[-1]] + words[:-1])] if len(words) > 1 else [])
            loose = []
            for q in orders:
                hits = self._query("2", q)
                exact = [h for h in hits
                         if set(words) <= set(re.sub(r"[^A-Z0-9 ]", " ", h["owner"].upper()).split())]
                if exact:
                    return exact
                loose = loose or hits
            if not loose and len(words) > 1:  # retry on surname alone
                loose = self._query("2", words[0])
            return loose
        if address:
            m = re.match(r"\s*(\d+\s+[A-Za-z0-9]+)", address)
            if m:
                return self._query("5", m.group(1))
        return []

    def lookup(self, account: str) -> TaxStatus:
        st = TaxStatus(county=self.county, account=str(account),
                       payments_available=False)
        st.source_url = (f"{self.base}/Search/Results?Query.SearchField=1"
                         f"&Query.SearchText={account}")
        hits = self._query("1", str(account))
        row = next((h for h in hits if h["account"] == str(account)), None)
        if row is None:
            st.error = (f"{self.last_fault} looking up account {account}" if self.last_fault
                        else f"no account {account} on the county portal")
            return st
        if row["_total_due"] is None:
            st.error = f"could not read 'Total Due' for account {account}"
            return st
        st.total_due = row["_total_due"]
        st.owner = row["owner"][:120]
        st.site_address = row["site_address"][:120]
        st.verified = True
        st.verified_at = datetime.now(timezone.utc).isoformat()
        return st


class HarrisTaxAdapter:
    """HARRIS — www.hctax.net. Cracked 2026-09-06.

    Harris was the single biggest hole in coverage: 317 of the 397 blocked
    leads on the 9/5 run. It was listed UNSUPPORTED, but it is NOT behind a
    bot challenge — the site answers plain HTTP and its own search is a JSON
    endpoint. Three calls:

      1. search  POST /Property/Actions/AccountsList
                 {colSearch: account|name|address, searchText, jtStartIndex,
                  jtPageSize, jtSorting}  -> JSON Records[{Account,Name,Address}]
      2. encrypt GET  /Property/AccountEncrypt?account=<13-digit account>
                 -> an opaque base64 token (the statement takes the TOKEN, not
                    the raw account number)
      3. detail  GET  /Property/TaxStatement?account=<token>

    The statement has TWO layouts and both must be handled:
      * delinquent -> a per-year table ending "Total Due >>> $8,876.76"
      * current    -> "Total Amount Due For <Month YYYY>: $0.00"
    Reading only one of them silently mis-reads half the book.

    LIMITATION: the receipts route does not expose dated payments with payer
    names, so payments_available=False — balance only, same as Caldwell /
    San Jacinto / Liberty. Never say "no payments on record" for Harris, and
    never fill the gap from the researcher note.
    """

    BASE = "https://www.hctax.net"
    WARM = "/Property/ViewStatementReceipts"

    def __init__(self, county: str = "HARRIS", slug=None,
                 session: Optional[requests.Session] = None):
        self.county = county
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA,
                               "Accept-Language": "en-US,en;q=0.9",
                               "Referer": self.BASE + self.WARM})
        self._warm = False

    def _warmup(self):
        if self._warm:
            return
        try:
            self.s.get(self.BASE + self.WARM, timeout=30)
            time.sleep(REQUEST_DELAY_SEC)
        except requests.RequestException:
            pass
        self._warm = True

    def _search(self, col: str, text: str) -> list[dict]:
        self._warmup()
        try:
            r = self.s.post(f"{self.BASE}/Property/Actions/AccountsList",
                            data={"colSearch": col, "searchText": text,
                                  "jtStartIndex": 0, "jtPageSize": 50,
                                  "jtSorting": "Name ASC"},
                            headers={"X-Requested-With": "XMLHttpRequest"},
                            timeout=30)
            time.sleep(REQUEST_DELAY_SEC)
            j = r.json()
        except (requests.RequestException, ValueError):
            return []
        if str(j.get("Result", "")).upper() != "OK":
            return []
        out = []
        for rec in (j.get("Records") or []):
            acct = str(rec.get("Account") or "").strip()
            if not acct:
                continue
            out.append({"account": acct,
                        "owner": str(rec.get("Name") or "").strip(),
                        "site_address": str(rec.get("Address") or "").strip(),
                        "long_account": ""})
        return out

    def find_account(self, *, owner: str = "", address: str = "") -> list[dict]:
        if owner:
            key = re.sub(r"[',]", "", owner.upper()).strip()
            hits = self._search("name", key)
            if not hits and " " in key:
                hits = self._search("name", key.split()[0])
            return hits
        if address:
            m = re.match(r"\s*(\d+\s+[A-Za-z0-9]+)", address)
            if m:
                return self._search("address", m.group(1))
        return []

    _DELINQ_RE = re.compile(r"Total Due\s*>>>\s*\|?\s*\$?([\d,]+\.\d{2})")
    _CURRENT_RE = re.compile(r"Total Amount Due For [A-Za-z]+ \d{4}:\s*\|?\s*\$?([\d,]+\.\d{2})")
    _OWNER_RE = re.compile(r"Assessed Owner\|Property Description\|[^|]*\|[^|]*\|([^|]+)\|")

    def lookup(self, account: str) -> TaxStatus:
        acct = re.sub(r"[^0-9]", "", str(account))
        st = TaxStatus(county=self.county, account=acct, payments_available=False)
        st.source_url = f"{self.BASE}{self.WARM}"
        self._warmup()
        try:
            e = self.s.get(f"{self.BASE}/Property/AccountEncrypt",
                           params={"account": acct}, timeout=30)
            time.sleep(REQUEST_DELAY_SEC)
        except requests.RequestException as ex:
            st.error = f"encrypt request failed: {type(ex).__name__}"
            return st
        token = (e.text or "").strip()
        if e.status_code != 200 or not token or "<" in token:
            st.error = f"no account {acct} on the county portal"
            return st
        try:
            r = self.s.get(f"{self.BASE}/Property/TaxStatement",
                           params={"account": token}, timeout=30)
            time.sleep(REQUEST_DELAY_SEC)
        except requests.RequestException as ex:
            st.error = f"detail request failed: {type(ex).__name__}"
            return st
        if r.status_code != 200:
            st.error = f"detail HTTP {r.status_code}"
            return st

        t = re.sub(r"(?s)<script.*?</script>", " ", r.text)
        t = re.sub(r"(?s)<style.*?</style>", " ", t)
        t = re.sub(r"(?s)<[^>]+>", "|", t)
        t = re.sub(r"(\|\s*)+", "|", t)
        t = t.replace("&amp;", "&").replace("&nbsp;", " ")

        m = self._DELINQ_RE.search(t) or self._CURRENT_RE.search(t)
        if not m:
            st.error = f"could not read 'Total Due' for account {acct}"
            return st
        st.total_due = _money(m.group(1))
        mo = self._OWNER_RE.search(t)
        if mo:
            st.owner = re.sub(r"\s+", " ", mo.group(1)).strip()[:120]
        ma = re.search(r"\|(\d+ [A-Z0-9][^|]{3,40})\|LT ", t)
        if ma:
            st.site_address = re.sub(r"\s+", " ", ma.group(1)).strip()[:120]
        st.verified = True
        st.verified_at = datetime.now(timezone.utc).isoformat()
        return st


# --- registry -------------------------------------------------------------
# county name (as it appears on the Lofty note) -> (adapter class, slug)
ACT_COUNTIES = {
    "GALVESTON":  (ACTAdapter, "galveston"),
    "FORT BEND":  (ACTAdapter, "fbc"),
    "MONTGOMERY": (ACTAdapter, "montgomery"),
    "JEFFERSON":  (ACTAdapter, "jefferson"),
    "ECTOR":      (ACTModernAdapter, "ectorcad"),
    "LIBERTY":    (PropertyTaxPaymentsAdapter, "https://liberty.propertytaxpayments.net"),
    "BRAZORIA":   (PropertyTaxPaymentsAdapter, "https://brazoria.propertytaxpayments.net"),
    "HARRIS":     (HarrisTaxAdapter, None),
    "CALDWELL":   (CaldwellCADAdapter, None),
    "SAN JACINTO": (TylerProxyTAdapter, "https://sjc-tax.us"),
}

# Counties with no live adapter yet — each needs one before the bot can verify
# them. Until then the bot MUST block them, never note-guess.
#
# BRAZORIA runs the same software as Liberty but is behind Cloudflare (403 to
# plain HTTP). Options are a licensed feed (TaxNetUSA/LienSuite), a bulk county
# file, or a runner on Raul's own Chrome — not defeating the challenge.
UNSUPPORTED = ["BELL", "TAYLOR", "WALKER", "CAMERON"]


def canonical_county(county: str) -> str:
    """Normalize a researcher-typed county name to the registry key.

    Researcher notes carry typos ("FFORT BEND" blocked a Fort Bend lead on
    9/16/26), so after the plain cleanup, snap anything within a close edit
    distance of a known county to that county.
    """
    import difflib
    key = re.sub(r"[^A-Z ]", " ", (county or "").upper())
    key = re.sub(r"\bCOUNTY\b|\bCO\b|\bTX\b|\bTEXAS\b", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    if not key or key in ACT_COUNTIES:
        return key
    known = list(ACT_COUNTIES) + list(UNSUPPORTED)
    squashed = {k.replace(" ", ""): k for k in known}
    if key.replace(" ", "") in squashed:
        return squashed[key.replace(" ", "")]
    close = difflib.get_close_matches(key, known, n=1, cutoff=0.85)
    return close[0] if close else key


def get_adapter(county: str, session: Optional[requests.Session] = None):
    """Return a live adapter for `county`, or None if we can't verify it yet."""
    key = canonical_county(county)
    entry = ACT_COUNTIES.get(key)
    if entry:
        cls, slug = entry
        return cls(key, slug, session=session)
    return None


def supported_counties() -> list[str]:
    return sorted(ACT_COUNTIES)


# --- appraisal-district (eSearch) fallback --------------------------------
# Counties whose CAD runs the same eSearch software as Caldwell. Used when the
# tax portal can't be resolved from the note: the CAD page is keyed by the
# note's own "Property Id", and it gives the SITUS ADDRESS (and for some
# counties the live tax table) — Raul 9/22: "when there's no account number you
# can search it by name, by property address, or by property ID."
ESEARCH_CAD = {
    "CALDWELL":  "https://esearch.caldwellcad.org",
    "TAYLOR":    "https://esearch.taylor-cad.org",
    "FORT BEND": "https://esearch.fbcad.org",
}


def cad_adapter(county: str, session: Optional[requests.Session] = None):
    """A CAD-backed adapter for `county`, or None. Its lookup() takes the CAD
    prop_id and returns a balance when that CAD publishes a tax table."""
    base = ESEARCH_CAD.get(canonical_county(county))
    return CaldwellCADAdapter(canonical_county(county), base, session=session) if base else None


def cad_situs(county: str, prop_id: str,
              session: Optional[requests.Session] = None) -> dict:
    """{'address','owner'} from the CAD property page, or {} — the note's
    Property Id is often the CAD id, and Lofty's address can be the owner's
    MAILING address (Davis Gail: Lofty said 661 Bering Dr Houston, the parcel
    is Evans Rd in Rosenberg)."""
    base = ESEARCH_CAD.get(canonical_county(county))
    pid = str(prop_id or "").strip()
    if not base or not pid:
        return {}
    s = session or requests.Session()
    out = {}
    for cand in dict.fromkeys([pid, re.sub(r"^[A-Za-z]+", "", pid)]):
        if not cand:
            continue
        try:
            r = s.get(f"{base}/Property/View/{cand}",
                      headers={"User-Agent": UA}, timeout=30)
            time.sleep(REQUEST_DELAY_SEC)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        text = _clean(r.text)
        ma = re.search(r"Situs Address:\s*(.*?)\s+(?:Map ID|Mapsco|Legal)", text)
        mo = re.search(r"Name:\s*(.*?)\s+(?:Agent|Mailing)", text)
        if ma and ma.group(1).strip():
            out = {"address": ma.group(1).strip()[:120],
                   "owner": (mo.group(1).strip()[:120] if mo else ""),
                   "account": cand}
            break
    return out
