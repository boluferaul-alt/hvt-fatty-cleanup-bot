"""Is a property actively listed on the market — for SALE or for RENT?

Checks Zillow first, then Realtor.com. A property that is publicly listed
(sale or rent) means the owner is actively handling it, so it's no longer a
motivated-seller lead worth keeping in HVT / Fatty / Hot Occ Alive.

`check_listing(browser, address)` returns a dict:
    {
      "listed": True | False | None,   # True = active listing found
      "site":   "Zillow" | "Realtor.com" | None,
      "kind":   "sale" | "rent" | None,
      "detail": "<short human note>",
    }

`listed` semantics:
    True  — an active for-sale or for-rent listing was detected on a site.
    False — every site checked loaded and showed NO active listing.
    None  — couldn't determine on any site (captcha / blocked / error).

Both portals aggressively block scrapers, so we drive a real browser via
Playwright and treat "couldn't determine" as a soft None for manual review
rather than crashing the run. Supersedes the old zillow_check.py (Zillow,
for-sale only).
"""
from __future__ import annotations

import logging
import re

from playwright.async_api import Browser

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# Anti-bot interstitial fingerprints. If any of these show up we can't trust
# the page, so we return None (undetermined) for that site.
_BLOCK_MARKERS = (
    "press & hold",
    "press and hold",
    "are you a human",
    "pardon our interruption",
    "verify you are a human",
    "captcha",
    "unusual traffic",
    "access to this page has been denied",
)


def _normalize(address: str) -> str:
    return re.sub(r"\s+", " ", (address or "").strip())


def _slug(address: str) -> str:
    return _normalize(address).replace(" ", "-").replace(",", "")


def _blocked(body_text: str) -> bool:
    return any(m in body_text for m in _BLOCK_MARKERS)


async def _new_page(browser: Browser):
    context = await browser.new_context(
        user_agent=_UA,
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()
    return context, page


# Markers that PROVE the subject property is NOT an active listing. Checked
# first — they override a stray "for sale" string elsewhere on the page
# (nearby-listings rails, nav links, "homes for sale in <city>", etc.).
# This is the fix for the 2026-06 false positives (Nova, Romero, Hines,
# Kelley, Parr all read as "listed" when they were actually off-market).
_OFF_MARKET_MARKERS = (
    "off market", "off-market", "not currently for sale",
    "this home is not", "is not currently listed", "sold on", "last sold",
    "recently sold", "sold price", "pending", "under contract", "contingent",
    "no longer available", "this property is not for sale",
)
# Markers that confirm the SUBJECT page is an active listing (not a search
# rail). Off-market/sold pages do not carry these.
_ACTIVE_SALE_MARKERS = (
    "days on zillow", "listed by", "listing provided by", "listing by",
    "request a tour", "contact agent", "get pre-qualified",
    "this home is for sale",
)
_ACTIVE_RENT_MARKERS = ("for rent", "/mo", "request to apply", "contact property")


def _classify(body: str) -> dict:
    """Strict classifier: only call it listed if the SUBJECT property shows an
    active-listing fingerprint AND no off-market/sold/pending marker."""
    if any(m in body for m in _OFF_MARKET_MARKERS):
        return {"listed": False, "kind": None}
    sale = ("for sale" in body) and any(m in body for m in _ACTIVE_SALE_MARKERS)
    rent = ("for rent" in body) and ("/mo" in body or "request to apply" in body)
    if rent:
        return {"listed": True, "kind": "rent"}
    if sale:
        return {"listed": True, "kind": "sale"}
    return {"listed": False, "kind": None}


async def _check_zillow(browser: Browser, address: str) -> dict:
    """Return {'listed': T/F/None, 'kind': 'sale'|'rent'|None}."""
    query = _slug(address)
    context, page = await _new_page(browser)
    try:
        url = f"https://www.zillow.com/homes/{query}_rb/"
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        body = (await page.locator("body").inner_text()).lower()
        if _blocked(body):
            log.warning("Zillow blocked for %s", query)
            return {"listed": None, "kind": None}
        return _classify(body)
    except Exception as e:  # noqa: BLE001
        log.warning("Zillow check failed for %s: %s", query, e)
        return {"listed": None, "kind": None}
    finally:
        await context.close()


async def _check_realtor(browser: Browser, address: str) -> dict:
    """Return {'listed': T/F/None, 'kind': 'sale'|'rent'|None}.

    Realtor.com's address search lands on the property detail page when the
    address resolves. The detail page advertises status in plain text:
    "For Sale", "For Rent", "Off Market", "Sold".
    """
    query = _slug(address)
    context, page = await _new_page(browser)
    try:
        url = f"https://www.realtor.com/realestateandhomes-search/{query}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        body = (await page.locator("body").inner_text()).lower()
        if _blocked(body):
            log.warning("Realtor blocked for %s", query)
            return {"listed": None, "kind": None}
        return _classify(body)
    except Exception as e:  # noqa: BLE001
        log.warning("Realtor check failed for %s: %s", query, e)
        return {"listed": None, "kind": None}
    finally:
        await context.close()


async def check_listing(browser: Browser, address: str) -> dict:
    """Check Zillow then Realtor.com for any active for-sale/for-rent listing.

    Short-circuits the moment one site confirms a listing. If neither
    confirms but at least one site was undetermined (blocked/error), the
    overall result is None (manual review) rather than a false "not listed".
    """
    result = {"listed": None, "site": None, "kind": None, "detail": ""}
    if not address:
        result["detail"] = "no address"
        return result

    z = await _check_zillow(browser, address)
    if z["listed"] is True:
        return {"listed": True, "site": "Zillow", "kind": z["kind"],
                "detail": f"Zillow: active {z['kind']} listing"}

    r = await _check_realtor(browser, address)
    if r["listed"] is True:
        return {"listed": True, "site": "Realtor.com", "kind": r["kind"],
                "detail": f"Realtor.com: active {r['kind']} listing"}

    # Neither confirmed a listing.
    if z["listed"] is False and r["listed"] is False:
        return {"listed": False, "site": None, "kind": None,
                "detail": "Not listed on Zillow or Realtor.com"}

    # At least one site was undetermined → manual review.
    sites = []
    if z["listed"] is None:
        sites.append("Zillow")
    if r["listed"] is None:
        sites.append("Realtor.com")
    return {"listed": None, "site": None, "kind": None,
            "detail": f"Undetermined ({', '.join(sites) or 'unknown'} blocked/error)"}
