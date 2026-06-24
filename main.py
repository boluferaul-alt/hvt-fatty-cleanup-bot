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
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from playwright.async_api import async_playwright

from lofty_client import LoftyClient
from note_parser import parse_lead_summary, ParsedSummary
from hvt_fatty_decision import (
    decide, Decision, render_row,
    DNC, HVT, VAULT, OCC_ALIVE, STAY, REVIEW, NO_MOVE_DECISIONS,
)

# How far back a tax payment counts as "recent" when summing payments. Raul's
# rule keys off "paid in the last year", so default to 365 days.
TAX_LOOKBACK_DAYS = int(os.getenv("TAX_LOOKBACK_DAYS", "365"))
import listing_check
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
STAGE_HOT_OCC_ALIVE = int(os.getenv("STAGE_HOT_OCC_ALIVE", "648757"))
STAGE_VAULT_HOT_OCC_ALIVE = int(os.getenv("STAGE_VAULT_HOT_OCC_ALIVE", "428742"))
STAGE_DO_NOT_CONTACT = int(os.getenv("STAGE_DO_NOT_CONTACT", "425581"))

# Where each recommended destination moves to (only used when AUTO_MOVE=1).
# STAY / REVIEW never move — they're judgment calls Raul finishes himself.
DEST_STAGE = {
    DNC: STAGE_DO_NOT_CONTACT,
    HVT: STAGE_HVT,
    VAULT: STAGE_VAULT_HOT_OCC_ALIVE,
    OCC_ALIVE: STAGE_HOT_OCC_ALIVE,
}

DRY_RUN = os.getenv("DRY_RUN", "0") == "1"
# Report-only by default: decide + post Slack, but never move a lead. Raul
# reviews the report and moves the leads himself.
AUTO_MOVE = os.getenv("AUTO_MOVE", "0") == "1"
CLEANUP_LIMIT = int(os.getenv("CLEANUP_LIMIT", "0"))
# The Researcher already wrote Total Taxes Owed + How Much Paid into each note,
# so we decide off that by default. The generic live county scraper is flaky
# (reCAPTCHA portals, selector drift) — only enable it once per-county handlers
# are solid.
USE_LIVE_TAX = os.getenv("USE_LIVE_TAX", "0") == "1"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()


def _llm_or_none() -> Optional[LLM]:
    if not ANTHROPIC_API_KEY:
        return None
    try:
        return LLM(ANTHROPIC_API_KEY)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] LLM init failed: {e}")
        return None


async def _listing_check_one(browser, address: str) -> dict:
    """Wrap listing_check.check_listing (Zillow + Realtor) with a safety net."""
    undetermined = {"listed": None, "site": None, "kind": None, "detail": ""}
    if not address:
        return undetermined
    try:
        return await listing_check.check_listing(browser, address)
    except Exception as e:  # noqa: BLE001
        print(f"      [listing err] {e}")
        return {**undetermined, "detail": f"error: {e}"}


def _parse_date(s: Optional[str]):
    """Parse a YYYY-MM-DD string to a date; None if unparseable."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


async def _tax_check_one(
    browser, parsed: ParsedSummary, llm: Optional[LLM],
    playbooks: dict,
) -> dict:
    """Run the county tax scrape and shape the result into the dict
    that decide() expects:
        current_balance, payment_recent_amount, payment_last_12mo_amount,
        last_payment_date, recent_window_days, ambiguous_recent, error
    """
    result = {
        "current_balance": None,
        "payment_recent_amount": None,
        "payment_last_12mo_amount": None,
        "last_payment_date": None,
        "recent_window_days": TAX_LOOKBACK_DAYS,
        "ambiguous_recent": False,
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

    # Sum only the payments that fall inside the recent window (e.g. 90 days).
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=TAX_LOOKBACK_DAYS)
    recent_sum = 0.0
    recent_dates: list[str] = []
    dated_payments = 0
    for p in (record.payments or []):
        d = _parse_date(p.date)
        if d is None:
            continue
        dated_payments += 1
        if d >= cutoff and isinstance(p.amount, (int, float)):
            recent_sum += p.amount
            recent_dates.append(p.date)

    result["payment_recent_amount"] = recent_sum

    # Most-recent payment date overall (prefer a dated recent one).
    all_dates = sorted([p.date for p in (record.payments or []) if p.date],
                       reverse=True)
    if recent_dates:
        result["last_payment_date"] = sorted(recent_dates, reverse=True)[0]
    elif all_dates:
        result["last_payment_date"] = all_dates[0]

    # The county reported a 12-month payment but gave us no dated payment
    # lines, so we can't confirm whether it landed inside the recent window.
    # Flag it for manual review instead of silently treating it as old.
    if recent_sum == 0 and dated_payments == 0 and (record.paid_last_12_months or 0) > 0:
        result["ambiguous_recent"] = True

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

    # If we couldn't even find a bot summary, skip listing + tax — they
    # need the property address / county.
    listing: dict = {"listed": None, "site": None, "kind": None, "detail": ""}
    tax_result: Optional[dict] = None
    if parsed.found:
        # 2. Listing check (best-effort; Render is often captcha-blocked -> None).
        try:
            listing = await _listing_check_one(browser, parsed.property_address)
        except Exception as e:  # noqa: BLE001
            print(f"      [listing err] {e}")
        # 3. Tax — baseline from the note (reliable for every county, never
        #    crashes). The Researcher wrote Total Taxes Owed + How Much Paid.
        tax_result = {
            "current_due": None,   # engine falls back to parsed.total_owed (comprehensive)
            "payment_recent_amount": parsed.paid_amount,
            "tax_paid_last_12mo": parsed.tax_paid_last_12mo,
            "error": None,
        }
        if USE_LIVE_TAX:           # optional live enrichment (off by default)
            try:
                live = await _tax_check_one(browser, parsed, llm, playbooks)
                if live and not live.get("error"):
                    if isinstance(live.get("current_balance"), (int, float)):
                        tax_result["current_due"] = live["current_balance"]
                    if live.get("payment_recent_amount"):
                        tax_result["payment_recent_amount"] = live["payment_recent_amount"]
            except Exception as e:  # noqa: BLE001
                print(f"      [tax live err] {e}")

    # 4. Decide.
    decision = decide(parsed, listing, tax_result)

    # 5. Move — route to the destination's stage (only if AUTO_MOVE on and not
    # a dry run). Ships report-only by default. STAY/REVIEW never move.
    move_result = ""
    target = DEST_STAGE.get(decision.category)
    if decision.category in NO_MOVE_DECISIONS or target is None:
        move_result = decision.category.lower()
    elif DRY_RUN:
        move_result = "dry-run"
    elif not AUTO_MOVE:
        move_result = "report-only"
    else:
        attempt = client.move_to_stage(lead_id, target)
        move_result = "ok" if attempt.ok else f"FAIL ({attempt.status})"

    print(f"      → {decision.category}  ({move_result})  — {decision.reason}")

    return render_row(
        lead, parsed, listing, tax_result, decision,
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
          f"STAGE_HOT_OCC_ALIVE={STAGE_HOT_OCC_ALIVE}  "
          f"VAULT={STAGE_VAULT_HOT_OCC_ALIVE}")
    print(f"  TAX_LOOKBACK_DAYS={TAX_LOOKBACK_DAYS}")
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
    print(f"\nListing leads in stages {STAGE_HVT} (HVT), {STAGE_FATTY} (Fatty), "
          f"{STAGE_HOT_OCC_ALIVE} (Hot Occ Alive) — single pass...")
    buckets = client.list_leads_in_stages(
        [STAGE_HVT, STAGE_FATTY, STAGE_HOT_OCC_ALIVE]
    )
    hvt_leads = buckets.get(STAGE_HVT, [])
    fatty_leads = buckets.get(STAGE_FATTY, [])
    hot_leads = buckets.get(STAGE_HOT_OCC_ALIVE, [])
    print(f"Found {len(hvt_leads)} HVT, {len(fatty_leads)} Fatty, "
          f"{len(hot_leads)} Hot Occ Alive lead(s).")

    # Tag each lead with its source pipeline for the report.
    tagged: list[tuple[str, dict]] = (
        [("HVT", ld) for ld in hvt_leads]
        + [("FATTY", ld) for ld in fatty_leads]
        + [("HOT", ld) for ld in hot_leads]
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
        "hot_count": len(hot_leads),
        "duration_s": elapsed,
        "tally": tally,
        "errors": len(errors),
        "dry_run": DRY_RUN,
        "auto_move": AUTO_MOVE,
    }

    print("\n" + "=" * 72)
    print("Run summary")
    print("=" * 72)
    for cat in (DNC, HVT, VAULT, OCC_ALIVE, STAY, REVIEW):
        if cat in tally:
            print(f"  {cat:12s} {tally[cat]}")
    if errors:
        print(f"\nErrors ({len(errors)}):")
        for lid, nm, reason in errors[:10]:
            print(f"  [{lid}] {nm}: {reason}")

    # Post to Slack — but NOT on a dry run (so test runs don't hit the team).
    if DRY_RUN:
        print("[slack] DRY_RUN — skipping Slack post.")
    else:
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
