"""Given a county playbook + owner name, return their tax status.

The pipeline:
  1. Open the cached search URL.
  2. Fill the owner-name field with the playbook's selector, submit.
  3. Hand the results HTML to Claude — pick the right account or report no_match.
  4. Open the detail page (if any).
  5. Hand the detail HTML to Claude — extract total_due, payments_last_12mo.

Steps 3 + 5 use the LLM because result/detail pages vary too much per
county to template. Steps 1 + 2 are deterministic from the playbook.

Lifted from lofty-overdue-bot/src/tax_scraper.py. Only change: imports are
flat (no .llm / .models package prefix) since this bot's files are flat.
"""
from __future__ import annotations

import logging
from typing import Any

from playwright.async_api import Browser, TimeoutError as PWTimeout

from llm import LLM
from models import Payment, TaxRecord

log = logging.getLogger(__name__)

__all__ = ["Payment", "TaxRecord", "scrape_tax"]


RESULTS_SYSTEM = (
    "You read a Texas county tax-office search-results page and decide which "
    "account belongs to a named owner. Owner names can vary: 'JOHN SMITH' may "
    "appear as 'SMITH JOHN' or 'SMITH, JOHN' or with an ETAL/ETUX suffix. "
    "If multiple accounts match the same owner, prefer the one with the "
    "highest balance owed (that's the property we care about)."
)

DETAIL_SYSTEM = (
    "You read a Texas county tax-account detail page and extract payment "
    "information. Money values must be plain numbers (no $, no commas). "
    "Dates as YYYY-MM-DD when possible; null if unparseable."
)


async def scrape_tax(
    browser: Browser,
    playbook_entry: dict[str, Any],
    owner_name: str,
    llm: LLM,
) -> TaxRecord:
    if playbook_entry.get("error"):
        return TaxRecord(error=f"playbook_error:{playbook_entry['error']}")

    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()
    try:
        await page.goto(playbook_entry["search_url"], wait_until="domcontentloaded", timeout=25_000)
        await page.fill(playbook_entry["owner_field_selector"], owner_name)
        await page.click(playbook_entry["submit_selector"])
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except PWTimeout:
            pass  # Some sites stream; we'll work with what we have

        results_html = _trim_html(await page.content())
        chosen = llm.ask_json(
            system=RESULTS_SYSTEM,
            user=(
                f"Owner we're searching for: {owner_name}\n"
                f"Search results HTML:\n{results_html}\n\n"
                "Return JSON: "
                '{"action": "click" | "no_match" | "multiple_unclear", '
                '"click_selector": "CSS selector for the result link to click, if action=click", '
                '"reason": "short string"}'
            ),
        )
        action = chosen.get("action")
        if action == "no_match":
            return TaxRecord(found=False, raw_notes=chosen.get("reason", ""))
        if action != "click" or not chosen.get("click_selector"):
            return TaxRecord(found=False, error="results_unparseable", raw_notes=chosen.get("reason", ""))

        try:
            await page.click(chosen["click_selector"])
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except (PWTimeout, Exception) as e:  # noqa: BLE001
            log.warning("Detail navigation failed: %s", e)
            return TaxRecord(found=True, error="detail_nav_failed")

        detail_html = _trim_html(await page.content())
        extracted = llm.ask_json(
            system=DETAIL_SYSTEM,
            user=(
                f"Owner: {owner_name}\n"
                f"Detail page HTML:\n{detail_html}\n\n"
                "Return JSON: "
                '{"total_due": number | null, '
                '"paid_last_12_months": number | null, '
                '"payments": [{"date": "YYYY-MM-DD" | null, "amount": number}], '
                '"notes": "anything relevant — tax lawsuit number, deferred status, etc."}'
            ),
            max_tokens=1500,
        )
        payments = [
            Payment(date=p.get("date"), amount=_to_float(p.get("amount")))
            for p in (extracted.get("payments") or [])
        ]
        return TaxRecord(
            found=True,
            total_due=_to_float(extracted.get("total_due")),
            paid_last_12_months=_to_float(extracted.get("paid_last_12_months")),
            payments=payments,
            raw_notes=extracted.get("notes", ""),
        )
    except Exception as e:  # noqa: BLE001
        log.exception("tax scrape failed for %s", owner_name)
        return TaxRecord(error=f"exception:{type(e).__name__}:{e}")
    finally:
        await context.close()


def _trim_html(html: str, max_chars: int = 12000) -> str:
    """Strip <script>, <style>, <svg> and condense whitespace."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "svg", "noscript"]):
        tag.decompose()
    text = str(soup)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n<!-- truncated -->"
    return text


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None
