"""
HVT + Fatty cleanup bot — main orchestrator.

Workflow per run:
  1. Authenticate to Lofty (verify API key).
  2. List every lead in STAGE_HVT and STAGE_FATTY. Combine.
  3. For each lead (subject to CLEANUP_LIMIT):
       a. Fetch its notes; parse the latest Researcher Bot Summary.
       b. Check Zillow for an active MLS listing (Playwright).
       c. Scrape the county tax portal for current balance + last-12mo
          payments (Playwright + LLM HTML interpretation).
       d. Run hvt_fatty_decision.decide(...) → VAULT / FLAG / STAY.
       e. If VAULT and AUTO_MOVE and not DRY_RUN: PUT /leads/{id} with
          stageId = STAGE_VAULT_HOT_OCC_ALIVE.
  4. Post a structured summary to Slack.
  5. Return run stats.

Designed to be called either:
  - as a script:  `python main.py`
  - via the Flask wrapper: `POST /run`
"""

from __future__ import annotations

import asyncio
import gc
import os
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

from playwright.async_api import async_playwright

from lofty_client import LoftyClient
from note_parser import parse_lead_summary, ParsedSummary
from hvt_fatty_decision import (
    decide, Decision, render_row,
    VAULT, FLAG, STAY, NO_MOVE_DECISIONS,
)
import zillow_check
import tax_scraper
from county_resolver import (
    discover_county_playbook, get_cached_playbook,
    load_playbooks, save_playbooks,
)
from llm import LLM
import slack_client


# ----------------------------------------------------------------------
# Config — read once at import. The Flask /run handler can override DRY_RUN,
# AUTO_MOVE, CLEANUP_LIMIT per-request by reassigning module attributes.
# ----------------------------------------------------------------------

STAGE_HVT = int(os.getenv("STAGE_HVT", "606230"))
STAGE_FATTY = int(os.getenv("STAGE_FATTY", "606229"))
STAGE_VAULT_HOT_OCC_ALIVE = int(os.getenv("STAGE_VAULT_HOT_OCC_ALIVE", "428742"))

DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
AUTO_MOVE = os.getenv("AUTO_MOVE", "1") == "1"
CLEANUP_LIMIT = int(os.getenv("CLEANUP_LIMIT", "0"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()


def _llm_or_none() -> Optional[LLM]:
    if not ANTHROPIC_API_KEY:
        return None
    try:
        return LLM(ANTHROPIC_API_KEY)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] LLM init failed: {e}")
        return None


async def _zillow_check_one(browser, address: str) -> Optional[bool]:
    """Wrap zillow_check.is_listed_for_sale with safety net."""
    if not address:
        return None
    try:
        return await zillow_check.is_listed_for_sale(browser, address)
    except Exception as e:  # noqa: BLE001
        print(f"      [zillow err] {e}")
        return None


async def _tax_check_one(
    browser, parsed: ParsedSummary, llm: Optional[LLM],
    playbooks: dict,
) -> dict:
    """Run the county tax scrape and shape the result into the dict
    that decide() expects:
        current_balance, payment_last_12mo_amount, last_payment_date, error
    """
    result = {
        "current_balance": None,
        "payment_last_12mo_amount": None,
        "last_payment_date": None,
        "error": None,
    }

    if not parsed.county or not parsed.owner_name:
        result["error"] = "missing county or owner"
        return result

    if llm is None:
        result["error"] = "ANTHROPIC_API_KEY not configured"
        return result

    # Cache lookup, else discover.
    playbook = get_cached_playbook(parsed.county, playbooks)
    if playbook is None:
        try:
            playbook = await discover_county_playbook(parsed.county, llm, browser)
        except Exception as e:  # noqa: BLE001
            result["error"] = f"playbook discover failed: {e}"
            return result
        # Cache, even if discovery surfaced an error — saves re-trying.
        key = (parsed.county or "").strip().upper().replace(" COUNTY", "").strip()
        playbooks[key] = playbook
        try:
            save_playbooks(playbooks)
        except Exception as e:  # noqa: BLE001
            print(f"      [playbook save err] {e}")

    if playbook.get("error"):
        result["error"] = f"playbook:{playbook['error']}"
        return result

    # Scrape.
    try:
        record = await tax_scraper.scrape_tax(
            browser, playbook, parsed.owner_name, llm
        )
    except Exception as e:  # noqa: BLE001
        result["error"] = f"scrape exception: {e}"
        return result

    if record.error:
        result["error"] = record.error
        return result

    result["current_balance"] = record.total_due
    result["payment_last_12mo_amount"] = record.paid_last_12_months or 0
    # Pick the most recent payment date from the payments list.
    dates = [p.date for p in (record.payments or []) if p.date]
    if dates:
        try:
            dates.sort(reverse=True)
            result["last_payment_date"] = dates[0]
        except Exception:  # noqa: BLE001
            result["last_payment_date"] = dates[0] if dates else None
    return result


async def process_one_lead_async(
    client: LoftyClient, lead: dict, source_pipeline: str,
    browser, llm: Optional[LLM], playbooks: dict,
) -> dict:
    """Full per-lead workflow. Returns a row dict for the Slack table."""
    lead_id = lead.get("leadId") or lead.get("id")
    name = f"{lead.get('firstName', '')} {lead.get('lastName', '')}".strip()
    print(f"  · {lead_id} {name} [{source_pipeline}]")

    # 1. Notes + parse.
    notes = client.get_notes(lead_id)
    parsed = parse_lead_summary(notes)

    # If we couldn't even find a bot summary, skip Zillow + tax — they
    # need the property address / county.
    zillow_listed: Optional[bool] = None
    tax_result: Optional[dict] = None
    if parsed.found:
        # 2. Zillow check.
        zillow_listed = await _zillow_check_one(browser, parsed.property_address)
        # 3. Tax check.
        tax_result = await _tax_check_one(browser, parsed, llm, playbooks)

    # 4. Decide.
    decision = decide(parsed, zillow_listed, tax_result)

    # 5. Move (if VAULT and not DRY_RUN).
    move_result = ""
    if decision.category in NO_MOVE_DECISIONS:
        move_result = decision.category.lower()
    elif DRY_RUN:
        move_result = "dry-run"
    elif not AUTO_MOVE:
        move_result = "auto-move disabled"
    else:
        # VAULT → stage = STAGE_VAULT_HOT_OCC_ALIVE
        attempt = client.move_to_stage(lead_id, STAGE_VAULT_HOT_OCC_ALIVE)
        move_result = "ok" if attempt.ok else f"FAIL ({attempt.status})"

    print(f"      → {decision.category}  ({move_result})  — {decision.reason}")

    return render_row(
        lead, parsed, zillow_listed, tax_result, decision,
        move_result, source_pipeline=source_pipeline,
    )


async def run_cleanup_async() -> dict:
    """Async core — boots a single Playwright browser for the whole run."""
    started = time.time()
    started_iso = datetime.now(timezone.utc).isoformat()

    print("=" * 72)
    print(f"HVT+Fatty cleanup bot — starting {started_iso}")
    print(f"  DRY_RUN={int(DRY_RUN)}  AUTO_MOVE={int(AUTO_MOVE)}  "
          f"LIMIT={CLEANUP_LIMIT or 'all'}")
    print(f"  STAGE_HVT={STAGE_HVT}  STAGE_FATTY={STAGE_FATTY}  "
          f"VAULT={STAGE_VAULT_HOT_OCC_ALIVE}")
    print("=" * 72)

    client = LoftyClient()

    # Auth check.
    me = client.get_me()
    print(f"Authenticated as {me.get('firstName')} {me.get('lastName')} "
          f"(userId={me.get('id')})")

    # Pull leads from both source pipelines in a single API walk.
    # Lofty's /leads endpoint requires scanning the full workspace (~36K
    # leads) since the server-side stageId filter is ignored, so we
    # bucket both stages in one pass instead of two.
    print(f"\nListing leads in stages {STAGE_HVT} (HVT) and {STAGE_FATTY} (Fatty) — single pass...")
    buckets = client.list_leads_in_stages([STAGE_HVT, STAGE_FATTY])
    hvt_leads = buckets.get(STAGE_HVT, [])
    fatty_leads = buckets.get(STAGE_FATTY, [])
    print(f"Found {len(hvt_leads)} HVT lead(s) and {len(fatty_leads)} Fatty lead(s).")

    # Tag each lead with its source pipeline for the report.
    tagged: list[tuple[str, dict]] = (
        [("HVT", ld) for ld in hvt_leads]
        + [("FATTY", ld) for ld in fatty_leads]
    )
    pipeline_count = len(tagged)
    print(f"\nTotal to process: {pipeline_count} lead(s).")

    if CLEANUP_LIMIT > 0:
        tagged = tagged[:CLEANUP_LIMIT]
        print(f"Limiting this run to first {CLEANUP_LIMIT} lead(s).")

    # Load county playbook cache (read once, save back when modified).
    playbooks = load_playbooks()
    print(f"Loaded {len(playbooks)} cached county playbook(s).")

    llm = _llm_or_none()
    if llm is None:
        print("[warn] No ANTHROPIC_API_KEY — tax_scraper will return FLAG "
              "(\"LLM key missing\") for every lead.")

    rows: list[dict] = []
    errors: list[tuple[Any, str, str]] = []

    # Chromium leaks ~10-15MB per browser-context cycle even after close().
    # Render Starter = 512MB total. To avoid OOM on a 100+ lead run, we
    # restart the whole browser process every BROWSER_REFRESH_EVERY leads
    # and force a Python GC pass. Empirically OOM hits around lead ~30 at
    # 512MB without this; refresh every 15 keeps peak well under 350MB.
    BROWSER_REFRESH_EVERY = 15
    LOW_MEM_ARGS = [
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-sync",
        "--no-sandbox",
        "--no-zygote",
    ]

    async with async_playwright() as pw:
        browser = None
        try:
            for i, (source, lead) in enumerate(tagged, start=1):
                # Restart browser at start of run + every N leads.
                if browser is None or (i - 1) % BROWSER_REFRESH_EVERY == 0:
                    if browser is not None:
                        try:
                            await browser.close()
                        except Exception:
                            pass
                        gc.collect()
                        print(f"  [memory] browser restart (lead #{i})")
                    browser = await pw.chromium.launch(
                        headless=True, args=LOW_MEM_ARGS,
                    )

                try:
                    row = await process_one_lead_async(
                        client, lead, source, browser, llm, playbooks,
                    )
                    rows.append(row)
                except Exception as e:  # noqa: BLE001
                    traceback.print_exc()
                    lid = lead.get("leadId") or lead.get("id")
                    nm = f"{lead.get('firstName','')} {lead.get('lastName','')}".strip()
                    errors.append((lid, nm, str(e)))
                if i % 10 == 0:
                    print(f"  [{i}/{len(tagged)}] processed so far...")
        finally:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:
                    pass

    elapsed = time.time() - started

    # Tally.
    tally: dict[str, int] = {}
    for r in rows:
        tally[r["decision"]] = tally.get(r["decision"], 0) + 1

    stats = {
        "when": started_iso,
        "processed": len(rows),
        "pipeline_count": pipeline_count,
        "hvt_count": len(hvt_leads),
        "fatty_count": len(fatty_leads),
        "duration_s": elapsed,
        "tally": tally,
        "errors": len(errors),
        "dry_run": DRY_RUN,
        "auto_move": AUTO_MOVE,
    }

    print("\n" + "=" * 72)
    print("Run summary")
    print("=" * 72)
    for cat in (VAULT, FLAG, STAY):
        if cat in tally:
            print(f"  {cat:10s} {tally[cat]}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for lid, nm, reason in errors[:10]:
            print(f"  [{lid}] {nm}: {reason}")

    # Post to Slack.
    slack_client.post_summary(rows, stats)
    if errors:
        sample = errors[0]
        slack_client.post_error(
            f"{len(errors)} lead(s) errored during cleanup. "
            f"First: [{sample[0]}] {sample[1]}: {sample[2]}"
        )

    return stats


def run_cleanup() -> dict:
    """Sync entry point for app.py + main()."""
    return asyncio.run(run_cleanup_async())


def main() -> None:
    try:
        run_cleanup()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        slack_client.post_error(
            f"HVT+Fatty cleanup bot crashed: {type(e).__name__}: {e}"
        )
        raise


if __name__ == "__main__":
    main()
