"""Cameron County — LIVE tax lookup (replaces the delinquent-roll snapshot).

Built 2026-09-19. Every Cameron lead was blocked on 9/16 with "account not in
the CAMERON delinquent roll" — the roll is keyed on the 16-digit tax account,
but the researcher note carries the CAD Property Id (e.g. 45185), so nothing
ever matched. Two plain-HTTP steps, no CAPTCHA, no login:

  1. Property Id -> tax account: the CAD's TrueProdigy public API hands out an
     anonymous token, and property/search on `pid` returns `simpleGeo`, which
     is the tax office's 16-digit account (45185 -> 0399890010019000).
  2. Balance: the tax office's Hamer eTax site (camerontax.go2gov.net) account
     page shows current-year taxes due + delinquent taxes due "AS OF" today,
     penalties/interest/fees included.

LIMITATION: the payment-history page gives totals paid per tax YEAR, with no
dates and no payer names, so payments_available=False (same as Caldwell).
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

import county_adapters as ca
from county_adapters import TaxStatus, UA, REQUEST_DELAY_SEC

PRODIGY = "https://prod-container.trueprodigyapi.com"
ETAX = "https://camerontax.go2gov.net"
_MONEY = r"-?\$[\d,]+\.\d{2}"


def _m(s: str) -> float:
    s = s.replace("$", "").replace(",", "")
    return float(s)


def _text(page: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))


class CameronLiveAdapter:
    def __init__(self, county: str = "CAMERON", slug=None,
                 session: Optional[requests.Session] = None):
        self.county = county
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self._token = None

    # --- step 1: CAD Property Id -> 16-digit tax account -------------------
    def _prodigy_headers(self) -> dict:
        h = {"User-Agent": UA, "Content-Type": "application/json",
             "Origin": "https://cameron.prodigycad.com",
             "Referer": "https://cameron.prodigycad.com/"}
        if self._token is None:
            r = requests.post(f"{PRODIGY}/trueprodigy/cadpublic/auth/token",
                              headers=h, json={"office": "Cameron"}, timeout=30)
            r.raise_for_status()
            self._token = (r.json().get("user") or {}).get("token") or ""
        h["Authorization"] = self._token
        return h

    def _prodigy_search(self, criteria: dict) -> list[dict]:
        year = str(datetime.now().year)
        body = {"pYear": {"operator": "=", "value": year}, **criteria}
        r = requests.post(f"{PRODIGY}/public/property/search?page=1&pageSize=25",
                          headers=self._prodigy_headers(), json=body, timeout=30)
        time.sleep(REQUEST_DELAY_SEC)
        if r.status_code != 200:
            return []
        return r.json().get("results") or []

    def tax_account(self, account: str) -> str:
        acct = re.sub(r"\D", "", str(account))
        if len(acct) >= 12:            # already a tax account number
            return acct
        hits = self._prodigy_search({"pid": {"operator": "=", "value": acct}})
        return str(hits[0].get("simpleGeo") or "") if hits else ""

    def find_account(self, *, owner: str = "", address: str = "") -> list[dict]:
        # Name/address search on the CAD API isn't wired yet; Cameron leads
        # carry the Property Id, which lookup() resolves directly.
        return []

    # --- step 2: live balance ---------------------------------------------
    def lookup(self, account: str) -> TaxStatus:
        st = TaxStatus(county=self.county, account=str(account),
                       payments_available=False,
                       source_name="County Tax Office")
        try:
            acct = self.tax_account(account)
        except (requests.RequestException, ValueError) as e:
            st.error = f"CAD account lookup failed: {type(e).__name__}"
            return st
        if not acct:
            st.error = f"Property Id {account} not found on the Cameron CAD"
            return st
        st.source_url = f"{ETAX}/faces/accounts?account={acct}&view=search&mode=std"
        try:
            r = self.s.get(st.source_url, timeout=30)
            time.sleep(REQUEST_DELAY_SEC)
        except requests.RequestException as e:
            st.error = f"tax office request failed: {type(e).__name__}"
            return st
        t = _text(r.text)
        if r.status_code != 200 or "OWNERSHIP INFORMATION" not in t or acct not in t:
            st.error = f"no Cameron tax account page for {acct}"
            return st

        total = 0.0
        # Current-year rows: "2025 $base $pen $fees $total"
        cur = re.search(r"TAXES DUE - AS OF.*?(?=DELINQUENT TAXES DUE|$)", t)
        if cur:
            for row in re.finditer(rf"\b(\d{{4}}) ({_MONEY}) ({_MONEY}) ({_MONEY}) ({_MONEY})",
                                   cur.group(0)):
                total += _m(row.group(5))
        # Prior-year delinquent summary row: 4 amounts, last is the total.
        dq = re.search(rf"DELINQUENT TAXES DUE - AS OF.*?({_MONEY}) ({_MONEY}) ({_MONEY}) ({_MONEY})", t)
        if dq:
            total += _m(dq.group(4))
        st.total_due = round(total, 2)

        mo = re.search(r"Legal Description (\d{12,}) (.*?) (\d+ .*?, TX|P ?O BOX)", t)
        if mo:
            st.owner = mo.group(2).strip()[:120]
        st.account = acct
        st.verified = True
        st.verified_at = datetime.now(timezone.utc).isoformat()
        return st


ca.ACT_COUNTIES["CAMERON"] = (CameronLiveAdapter, None)
ca.UNSUPPORTED = [c for c in ca.UNSUPPORTED if c != "CAMERON"]
