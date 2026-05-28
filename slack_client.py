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


def build_summary_message(rows: list[dict], stats: dict) -> dict:
    """
    Build a Slack message payload for the post-cleanup summary report.

    Format:
      Header block — title + counts
      Divider
      Section per decision category — list of moves
      Divider
      Footer — run metadata

    Slack message limits:
      - 50 blocks max → we cap per-category lists at 20 leads each, with
        a "(+N more, see full log)" footer.
      - 3000 char limit per text field → we truncate long reasons.
    """
    when = stats.get("when") or ""
    pipeline_count = stats.get("pipeline_count")
    processed = stats.get("processed", len(rows))

    header = (
        f"*HVT + Fatty cleanup — {when}*\n"
        f"Processed *{processed}* lead{'s' if processed != 1 else ''}"
    )
    if pipeline_count is not None:
        header += f" (HVT + Fatty currently has {pipeline_count})"
    header += "."

    blocks: list[dict] = [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": header}},
    ]

    # Tally and group rows by decision category.
    by_decision: dict[str, list[dict]] = {}
    for r in rows:
        by_decision.setdefault(r["decision"], []).append(r)

    # Show tallies up front
    tally_parts: list[str] = []
    for cat in ("STAY", "VAULT", "FLAG"):
        if cat in by_decision:
            tally_parts.append(f"*{cat}*: {len(by_decision[cat])}")
    if tally_parts:
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": " · ".join(tally_parts)}})

    blocks.append({"type": "divider"})

    # Per-category lists (skip STAY — it's the boring majority)
    for cat in ("VAULT", "FLAG"):
        cat_rows = by_decision.get(cat) or []
        if not cat_rows:
            continue
        lines: list[str] = [f"*{cat}* ({len(cat_rows)}):"]
        for r in cat_rows[:20]:
            move_tag = "✅" if "ok" in (r.get("move_result") or "").lower() else "⚠️"
            addr = _truncate(r.get("address") or "?", 36)
            line = (
                f"{move_tag} `{r['lead_id']}` {_truncate(r['name'], 30)} — "
                f"{addr} — {_money(r.get('value'))} — "
                f"{_truncate(r['reason'], 120)}"
            )
            lines.append(line)
        if len(cat_rows) > 20:
            lines.append(f"_…and {len(cat_rows) - 20} more_")
        blocks.append({"type": "section",
                       "text": {"type": "mrkdwn",
                                "text": "\n".join(lines)[:3000]}})
        blocks.append({"type": "divider"})

    # Move-failure summary
    move_failed = [r for r in rows
                   if r["decision"] == "VAULT"
                   and "ok" not in (r.get("move_result") or "").lower()
                   and "dry" not in (r.get("move_result") or "").lower()
                   and "disabled" not in (r.get("move_result") or "").lower()]
    if move_failed:
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": (f"⚠️ *{len(move_failed)} VAULT move(s) FAILED via API* — "
                     f"Lofty API silently rejected the stage change. The "
                     f"recommendations above are correct but the leads are "
                     f"still in HVT/Fatty. Manual moves needed.")}})

    # Footer
    footer_bits = [f"_Run completed at {when}_"]
    if stats.get("duration_s"):
        footer_bits.append(f"_{stats['duration_s']:.0f}s elapsed_")
    if stats.get("dry_run"):
        footer_bits.append("_DRY_RUN mode — no actual moves attempted_")
    blocks.append({"type": "context",
                   "elements": [{"type": "mrkdwn",
                                 "text": " · ".join(footer_bits)}]})

    return {
        "blocks": blocks,
        # Fallback text for notifications and old Slack clients
        "text": (f"HVT+Fatty cleanup — {processed} processed: "
                 + " ".join(tally_parts)),
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
