"""Is a property currently listed for sale on Zillow?

Zillow aggressively blocks scrapers, so we drive a real browser via
Playwright. We treat "couldn't determine" as a soft state so the daily
digest can flag it as 'manual review' rather than crashing the run.

Lifted from lofty-overdue-bot/src/zillow_check.py — already standalone.
"""
from __future__ import annotations

import logging
import re

from playwright.async_api import Browser, async_playwright

log = logging.getLogger(__name__)


def _normalize(address: str) -> str:
    return re.sub(r"\s+", " ", address.strip())


async def is_listed_for_sale(browser: Browser, address: str) -> bool | None:
    """Return True if listed, False if not, None if undetermined.

    We search the address in Zillow's search bar and look for the
    "For sale" or "Listed by" tags on the resulting card. Anything else
    (off-market, sold, no result) counts as not-listed.
    """
    if not address:
        return None
    query = _normalize(address)
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
    )
    page = await context.new_page()
    try:
        url = f"https://www.zillow.com/homes/{query.replace(' ', '-').replace(',', '')}_rb/"
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # If Zillow shows a captcha or 'press and hold' page, we bail.
        body_text = (await page.locator("body").inner_text()).lower()
        if "press & hold" in body_text or "are you a human" in body_text:
            log.warning("Zillow captcha for %s", query)
            return None
        if "for sale" in body_text and ("listed by" in body_text or "$" in body_text):
            # Heuristic: a hit page with a sale price and "for sale" tag.
            # Off-market pages say "Off market" / "Zestimate" without "For sale".
            return True
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("Zillow check failed for %s: %s", query, e)
        return None
    finally:
        await context.close()


async def check_many(addresses: list[str]) -> dict[str, bool | None]:
    out: dict[str, bool | None] = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            for addr in addresses:
                out[addr] = await is_listed_for_sale(browser, addr)
        finally:
            await browser.close()
    return out


async def check_one(address: str) -> bool | None:
    """Single-shot convenience wrapper. Launches its own browser.

    Returns True/False/None per is_listed_for_sale().
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            return await is_listed_for_sale(browser, address)
        finally:
            await browser.close()
