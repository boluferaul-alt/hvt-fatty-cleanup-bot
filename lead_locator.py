"""Locate a lead's county record when the researcher summary can't.

tax_watch reads the researcher note only to FIND the county record. When that
note is missing (12 leads on 9/16/26) or names no usable county, the lead used
to be blocked outright. Raul 9/18: "every lead, no excuses". So fall back to
the lead's own Lofty property address, and get the county from the US Census
geocoder (free, no key, first-party government data).

Locating data only. Tax figures still come from the live county lookup.
"""
from __future__ import annotations

import re
from typing import Optional

import requests

CENSUS_URL = ("https://geocoding.geo.census.gov/geocoder/geographies/"
              "onelineaddress")


def _s(v) -> str:
    return str(v or "").strip()


def _lead_detail(lead: dict, client=None) -> dict:
    """The full lead record. The /leads list omits leadPropertyList, and
    GET /leads/{id} nests everything under "lead"."""
    if lead.get("leadPropertyList") or client is None:
        return lead
    try:
        resp = client._request("GET", f"/leads/{lead.get('leadId')}")
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                data = data.get("lead") or data.get("data") or data
                if isinstance(data, dict):
                    return {**lead, **{k: v for k, v in data.items() if v not in (None, "")}}
    except Exception:
        pass
    return lead


_SUFFIX = (r"(?:ST|STREET|DR|DRIVE|RD|ROAD|AVE|AVENUE|LN|LANE|BLVD|CT|COURT|WAY|"
           r"CIR|CIRCLE|PL|PLACE|PKWY|TRL|TRAIL|HWY|LOOP|PATH|ROW|TER|XING)")


def _street_only(addr: str) -> str:
    """'5041 BROOKSIDE RD BROOKSIDE, TX 77581' -> '5041 BROOKSIDE RD'."""
    a = re.sub(r"\s+", " ", addr or "").strip(" ,")
    a = re.split(r"\s+Mailing Address", a, flags=re.I)[0]
    seg = a.split(",")[0].strip()
    m = re.match(rf"^(\d+[A-Z]?\s+.*\b{_SUFFIX}\b\.?)(.*)$", seg, re.I)
    if m:
        rest = m.group(2).strip()
        # Cut a trailing city word ("... RD BROOKSIDE") but keep a street
        # letter/number ("111 AVENUE F", "CR 103").
        if rest and (len(rest) <= 2 or re.match(r"^(?:CR|FM|UNIT|APT|#)?\s*\d", rest, re.I)):
            return seg
        return m.group(1).strip() if rest else seg
    return seg


def locate_from_notes(notes: list) -> dict:
    """County / property address / property id from ANY note, in the older
    free-text formats the summary parser doesn't recognise:
      "Property Address: 5041 BROOKSIDE RD BROOKSIDE, TX 77581 Mailing Address: ..."
      "Property Id: 2157-00-15700"   ".../esearch.brazoriacad.org/Property/View/254043"
      "County : Hood"
    Locating data only - never a tax figure."""
    out = {"address": "", "one_line": "", "property_id": "", "county": "", "geo_id": ""}
    for n in notes or []:
        t = re.sub(r"<[^>]+>", " ", str(n.get("content") or n.get("note") or ""))
        t = re.sub(r"\s+", " ", t.replace("&nbsp;", " "))
        if not out["address"]:
            m = re.search(r"Property Address\s*[:\-]\s*(\d[^|]{6,90}?)(?=\s+Mailing Address|\s+Property I[dD]|\s{2,}|$)", t, re.I)
            if m:
                out["one_line"] = re.split(r"\s+Mailing Address", m.group(1), flags=re.I)[0].strip(" ,")
                out["address"] = _street_only(out["one_line"])
        if not out["property_id"]:
            m = (re.search(r"Property I[dD]\s*[:\-]\s*([A-Z0-9][A-Z0-9\-]{2,30})", t)
                 or re.search(r"esearch\.[a-z]+cad\.org/Property/View/(\d+)", t, re.I))
            if m:
                out["property_id"] = m.group(1)
        if not out["geo_id"]:
            m = re.search(r"Geo(?:graphic)?\s*I[dD]\s*[:\-]\s*([0-9][0-9\-]{6,30})", t)
            if m:
                out["geo_id"] = m.group(1)
        if not out["county"]:
            m = re.search(r"\bCounty\s*:\s*([A-Za-z ]{3,20}?)(?:\s+County)?\s+(?:State|Owner|Property|Tax|\||$)", t)
            if m:
                out["county"] = m.group(1).strip()
            else:
                m = re.search(r"esearch\.([a-z]+)cad\.org", t, re.I)
                if m:
                    out["county"] = m.group(1)
    return out


def lead_property_address(lead: dict, client=None) -> tuple[str, str]:
    """(street, full one-line address) from the Lofty lead, or ("", "")."""
    lead = _lead_detail(lead, client)
    props = lead.get("leadPropertyList")
    for p in props or []:
        if not isinstance(p, dict):
            continue
        street = _s(p.get("streetAddress") or p.get("address") or p.get("street"))
        if not street:
            continue
        city = _s(p.get("city"))
        state = _s(p.get("state") or p.get("stateCode")) or "TX"
        zipc = _s(p.get("zipCode") or p.get("zip") or p.get("postalCode"))
        return street, ", ".join(x for x in (street, city, f"{state} {zipc}".strip()) if x)
    street = _s(lead.get("streetAddress"))
    if street:
        full = ", ".join(x for x in (street, _s(lead.get("city")),
                                     f"{_s(lead.get('state')) or 'TX'} {_s(lead.get('zipCode'))}".strip()) if x)
        return street, full
    return "", ""


def lead_record_county(lead: dict, client=None) -> str:
    """The county Lofty itself stores on the lead's property, if any."""
    lead = _lead_detail(lead, client)
    for p in lead.get("leadPropertyList") or []:
        if isinstance(p, dict) and _s(p.get("county")):
            return _s(p.get("county"))
    return ""


def county_for_address(one_line: str, timeout: int = 20) -> Optional[str]:
    """Texas county name for an address via the Census geocoder, or None."""
    if not one_line:
        return None
    try:
        r = requests.get(CENSUS_URL, params={
            "address": one_line, "benchmark": "Public_AR_Current",
            "vintage": "Current_Current", "format": "json"}, timeout=timeout)
        r.raise_for_status()
        matches = r.json().get("result", {}).get("addressMatches", [])
    except Exception:
        return None
    for m in matches:
        for c in (m.get("geographies", {}).get("Counties") or []):
            if c.get("STATE") == "48":            # Texas FIPS
                return re.sub(r"\s+County$", "", c.get("NAME", "")).upper()
    return None
