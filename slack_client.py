"""
Minimal Slack incoming-webhook poster.

Why this instead of the slack_sdk package: the webhook flow doesn't need
auth tokens or rate-limit handling — we just POST a JSON body to a URL.
Avoiding slack_sdk keeps the dep footprint tiny on Render.

If we later want a real slash command (/cleanup), we'll add a Flask
endpoint that verifies Slack's signing secret and switch from webhook
to chat.postMessage via bot token. For now, webhook is enough.
"""

from __future__ import annotations

import os
import json
from typing import Optional

import requests


def _money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"${v:,.0f}"


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[:n - 1] + "…"


def _line(r: dict) -> str:
    """One lead → one Slack line, tagged with its source pipeline."""
    src = (r.get("source_pipeline") or "?").upper()
    addr = _truncate(r.get("address") or "?", 36)
    return (
        f"`{src}` `{r['lead_id']}` {_truncate(r.get('name') or '?', 28)} — "
        f"{addr} — {_money(r.get('value'))} — "
        f"{_truncate(r.get('reason') or '', 120)}"
    )


def _section(title: str, rows: list[dict], cap: int = 20) -> list[dict]:
    """Build a titled section + divider for a list of rows (empty → nothing)."""
    if not rows:
        return []
    lines = [f"*{title}* ({len(rows)}):"]
    for r in rows[:cap]:
        lines.append(_line(r))
    if len(rows) > cap:
        lines.append(f"_…and {len(rows) - cap} more_")
    return [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": "\n".join(lines)[:3000]}},
        {"type": "divider"},
    ]


def build_summary_message(rows: list[dict], stats: dict) -> dict:
    """
    Build the Slack payload for the cleanup report. Report-only: leads are
    grouped by WHY they're flagged so Raul can pull the dead ones himself.

      🏷️ Listed on the market   (Zillow/Realtor, sale or rent)
      💵 Paid taxes recently     (≥ floor within the lookback window)
      ⚠️ Needs manual review     (undetermined / scrape failed / no summary)
      🟡 Partial payment (kept)  (heads-up, below floor — stays in pipeline)
      ✅ Clean                    (count only)

    Slack limits: 50 blocks max (we cap each list at 20 rows), 3000 chars
    per text field (we truncate).
    """
    when = stats.get("when") or ""
    pipeline_count = stats.get("pipeline_count")
    processed = stats.get("processed", len(rows))
    auto_move = stats.get("auto_move")

    mode = "report-only" if not auto_move else "auto-move"
    header = (
        f"*HVT · Fatty · Hot Occ Alive cleanup — {when}*\n"
        f"Processed *{processed}* lead{'s' if processed != 1 else ''}"
    )
    bits = []
    if pipeline_count is not None:
        bits.append(f"{pipeline_count} in the 3 pipelines")
    bits.append(mode)
    header += f" ({', '.join(bits)})."

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header}},
    ]

    # Honest completion line — verified-live vs BLOCKED. Surfaced before anything
    # else so a partial run can never read as complete.
    a = stats.get("audit") or {}
    if a:
        comp = (f"✅ *{a.get('verified', 0)}* verified live  ·  "
                f"⛔ *{a.get('blocked', 0)}* BLOCKED (not verified live)")
        if not stats.get("require_live_tax", True):
            comp += "  ·  _⚠ live-tax gate OFF (note-based)_"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": comp}})

    # Bucket rows by recommended destination.
    def bucket(cat): return [r for r in rows if r.get("decision") == cat]
    dnc   = bucket("DNC")
    hvt   = bucket("HVT")
    vault = bucket("VAULT")
    occ   = bucket("OCC_ALIVE")
    stay  = bucket("STAY")
    review= bucket("REVIEW")
    blocked = bucket("BLOCKED")

    tally_parts = []
    for label, b in (("DNC", dnc), ("HVT", hvt), ("VAULT", vault),
                     ("OCC-ALIVE", occ), ("STAY", stay), ("REVIEW", review)):
        if b:
            tally_parts.append(f"*{label}*: {len(b)}")
    below = [r for r in rows if r.get("below_target")]
    if below:
        tally_parts.insert(0, f"*💸 BELOW NET TARGET*: {len(below)}")
    if blocked:
        tally_parts.insert(0, f"*⛔ BLOCKED*: {len(blocked)}")
    blocks.append({"type": "section",
                   "text": {"type": "mrkdwn", "text": " · ".join(tally_parts) or "no leads"}})
    blocks.append({"type": "divider"})

    # BLOCKED first — these are NOT done. They got no recommendation because the
    # live county tax check couldn't be completed; each needs Edge/human.
    blocks += _section("⛔ BLOCKED — not verified live, NO recommendation", blocked)
    # The profit math is Raul's #1 filter — surface it next. His call to remove.
    blocks += _section("💸 Below $60K net target — YOUR CALL to remove", below)
    blocks += _section("🚫 Do Not Contact — listed / paid up / no motivation", dnc)
    blocks += _section("🎯 HVT — deceased + value + taxes owed", hvt)
    blocks += _section("🗄️ Vault — intent to pay / park it", vault)
    blocks += _section("🏠 Occupied-Alive — paying, low priority", occ)
    blocks += _section("⚠️ Review — bot couldn't decide", review)

    # Footer
    footer_bits = [f"_{len(stay)} still working (STAY)_"]
    if stats.get("duration_s"):
        footer_bits.append(f"_{stats['duration_s']:.0f}s elapsed_")
    if stats.get("dry_run"):
        footer_bits.append("_DRY_RUN — nothing moved or posted live_")
    elif not auto_move:
        footer_bits.append("_report-only — no leads were moved_")
    blocks.append({"type": "context",
                   "elements": [{"type": "mrkdwn",
                                 "text": " · ".join(footer_bits)}]})

    return {
        "blocks": blocks,
        "text": (f"Cleanup — {processed} processed: "
                 f"DNC {len(dnc)}, HVT {len(hvt)}, Vault {len(vault)}, "
                 f"Occ {len(occ)}, Review {len(review)}"),
    }


def post_to_slack(webhook_url: str, payload: dict) -> bool:
    """POST a message payload to Slack. Returns True if Slack accepted it."""
    if not webhook_url:
        return False
    try:
        resp = requests.post(webhook_url, json=payload, timeout=15)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def post_summary(rows: list[dict], stats: dict,
                 webhook_url: Optional[str] = None) -> bool:
    """High-level helper: build + post the cleanup summary."""
    url = webhook_url or os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        print("[slack] SLACK_WEBHOOK_URL not set — skipping Slack post.")
        return False
    payload = build_summary_message(rows, stats)
    ok = post_to_slack(url, payload)
    if ok:
        print(f"[slack] posted summary ({len(rows)} rows)")
    else:
        print("[slack] FAILED to post summary")
        # Dump the payload so a human can investigate from logs.
        print(f"[slack] payload preview: {json.dumps(payload)[:500]}")
    return ok


def post_error(message: str, webhook_url: Optional[str] = None) -> bool:
    """Post an error / health alert to Slack."""
    url = webhook_url or os.getenv("SLACK_WEBHOOK_URL", "").strip()
    if not url:
        return False
    payload = {
        "blocks": [
            {"type": "section",
             "text": {"type": "mrkdwn",
                      "text": f"🚨 *HVT+Fatty cleanup bot error*\n{message}"}},
        ],
        "text": f"HVT+Fatty cleanup bot error: {message[:100]}",
    }
    return post_to_slack(url, payload)
