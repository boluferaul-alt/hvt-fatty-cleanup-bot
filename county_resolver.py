"""Discover the tax-search URL for an arbitrary TX county.

Cache hit (county already in playbook): return cached playbook.
Cache miss: ask Claude for the official county tax-office URL, verify it
loads, and find the owner-name search form on it. Save to the playbook.

Lifted from lofty-overdue-bot/src/county_resolver.py. Only change: imports
are flat (no .llm package prefix) since this bot's files are flat.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, TimeoutError as PWTimeout

from llm import LLM

log = logging.getLogger(__name__)


# Path to the on-disk playbook cache. Co-located with the rest of the bot
# under data/ so the same JSON file ships with the Docker image and the
# write-after-discovery path is one place.
DEFAULT_PLAYBOOK_PATH = Path(__file__).parent / "data" / "county_playbooks.json"


DISCOVER_SYSTEM = (
    "You know every Texas county's official tax-collector website. "
    "Given a county name, return the public URL of the property-tax account "
    "search page where someone can look up an account by owner name."
)


# Hand-verified URLs for counties where the LLM has been observed to
# hallucinate or get the URL subtly wrong (typos, dropped hyphens,
# pointing at the CAD appraisal site instead of the Tax A&C site).
# These are tried FIRST in candidates so discovery doesn't waste an
# LLM round-trip. Form selectors are still discovered by the LLM
# from the live page.
#
# Keys must match _county_key() output (uppercased, no " COUNTY" suffix).
KNOWN_COUNTY_URLS: dict[str, list[str]] = {
    "SAN JACINTO": [
        "https://actweb.acttax.com/act_webdev/sanjacinto/index.jsp",
    ],
    "BRAZORIA": [
        "https://actweb.acttax.com/act_webdev/brazoria/index.jsp",
    ],
    "LIBERTY": [
        "https://www.libertycountytax.com/property-search",
    ],
    "TAYLOR": [
        "https://actweb.acttax.com/act_webdev/taylor/index.jsp",
    ],
    "POLK": [
        "https://actweb.acttax.com/act_webdev/polk/index.jsp",
    ],
}


def load_playbooks(path: Path | str = DEFAULT_PLAYBOOK_PATH) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read playbooks at %s — starting empty", p)
        return {}


def save_playbooks(playbooks: dict[str, Any], path: Path | str = DEFAULT_PLAYBOOK_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(playbooks, indent=2, sort_keys=True), encoding="utf-8")


def _county_key(county: str) -> str:
    return (county or "").strip().upper().replace(" COUNTY", "").strip()


def get_cached_playbook(county: str, playbooks: dict[str, Any]) -> dict[str, Any] | None:
    """Look up a county in an in-memory playbook dict. None if missing."""
    if not county:
        return None
    key = _county_key(county)
    if key in playbooks:
        return playbooks[key]
    # Some legacy entries are stored with " COUNTY" suffix or full lower-case.
    for k, v in playbooks.items():
        if _county_key(k) == key:
            return v
    return None


async def discover_county_playbook(county: str, llm: LLM, browser: Browser) -> dict[str, Any]:
    """Build a playbook entry for a county we've never seen.

    Two-step:
      1. LLM proposes the tax-search URL (it has TX county tax sites in
         training data; for the long tail we fall through to a Google search).
      2. We open the URL with Playwright, dump the form HTML, and ask the
         LLM which input is the owner-name field and which control submits.
    """
    proposed = llm.ask_json(
        system=DISCOVER_SYSTEM,
        user=(
            f"County: {county}, TX.\n"
            "Return JSON with keys: "
            '{"search_url": "https://...", '
            '"backup_search_url": "https://..." | null, '
            '"notes": "anything I should know about this site"}.\n'
            "The search_url must land on a page with an owner-name search "
            "form — not a portal homepage."
        ),
    )
    if not proposed.get("search_url"):
        log.warning("LLM failed to propose a URL for county %s", county)
        return {"county": county, "error": "no_url"}

    context = await browser.new_context()
    page = await context.new_page()
    # Hand-verified URLs win over LLM-proposed ones. Tried first.
    known = KNOWN_COUNTY_URLS.get(_county_key(county), [])
    candidates: list[str] = list(known)
    if proposed["search_url"] not in candidates:
        candidates.append(proposed["search_url"])
    if proposed.get("backup_search_url") and proposed["backup_search_url"] not in candidates:
        candidates.append(proposed["backup_search_url"])

    for url in candidates:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except PWTimeout:
            log.warning("Timeout loading %s for %s", url, county)
            continue

        html = await page.content()
        # Trim — we just need the <form>s.
        snippet = _form_snippets(html)
        form = llm.ask_json(
            system=(
                "Given the HTML snippet of a Texas county tax-search page, identify "
                "the form for searching by owner name. Return JSON: "
                '{"owner_field_selector": "CSS selector for the input that takes the owner name", '
                '"submit_selector": "CSS selector for the submit button", '
                '"extra_steps": "natural-language description of any pre-clicks needed, or empty"}'
            ),
            user=f"County: {county}\nURL: {url}\nHTML:\n{snippet}",
        )
        if form.get("owner_field_selector") and form.get("submit_selector"):
            await context.close()
            return {
                "county": _county_key(county),
                "search_url": url,
                "owner_field_selector": form["owner_field_selector"],
                "submit_selector": form["submit_selector"],
                "extra_steps": form.get("extra_steps") or "",
                "notes": proposed.get("notes") or "",
            }

    await context.close()
    return {"county": _county_key(county), "error": "form_not_found", "tried": candidates}


def _form_snippets(html: str, max_chars: int = 8000) -> str:
    """Return only the <form>...</form> blocks of an HTML page, truncated."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    forms = soup.find_all("form")
    if not forms:
        # No <form>s — could be a JS app. Send the body text instead.
        text = soup.get_text(" ", strip=True)
        return text[:max_chars]
    combined = "\n\n".join(str(f) for f in forms)
    return combined[:max_chars]
