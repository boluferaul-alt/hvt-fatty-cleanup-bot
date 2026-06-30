# Completion Contract — Definition of Done for the Dirty Deed bots

**The problem this fixes:** the bot was deciding leads off the researcher
note's `Tax Paid Last 12 Months` field instead of hitting the **live county
tax-collector site** — the only source of truth for "have taxes actually been
paid." It then wrote "verify live" and shipped, presenting unfinished work as
done. An LLM-driven bot always drifts to the cheapest path that *looks*
complete. Prose instructions ("verify live") don't stop it. **A code gate does.**

## The rule
A lead gets a recommendation **only** when its taxes are **verified live** —
the record must carry, from the live county page:

- `current_due` (the live balance — `0` is a valid verified answer)
- `tax_source_url` (the page we actually read)
- `tax_verified_at` (timestamp)

Missing any one → the lead is **BLOCKED** with a specific reason and gets **no
recommendation.** `decide()` is never called on a blocked record (`completion_contract.assert_decidable()` raises). The stale note is **never** the basis for a FATTY/HVT/VAULT/DNC call — it's kept only as labeled, unverified context.

## NEVER do
- ❌ Decide off the note's `Tax Paid Last 12 Months` / `How Much Paid`. Stale.
- ❌ Write "verify live" and ship it as a recommendation. That's a BLOCKED lead.
- ❌ Present a run as complete when leads were blocked. The audit line forbids it.
- ❌ Blind-sum same-surname parcels from a CAD owner search — filter to the
  owner of record first (Fregia: 23 parcels → only 2 are ours).

## The honest audit
Every run reports, before anything else:

```
COMPLETION: VERIFIED LIVE: 41  |  BLOCKED (not verified): 12  |  top reason: Bell County reCAPTCHA (7)
```

Blocked leads are listed **first** in Slack, with their reason. A clean-looking
spreadsheet can never bury them.

## The hard tradeoff (headless Render vs reCAPTCHA counties)
A headless Render browser **cannot** pass the reCAPTCHA on Bell / Taylor /
Caldwell tax sites. Those counties are **BLOCKED honestly** (reason names the
county) rather than faked from the note. They need either:
1. the **Edge-profile runner** (real browser profile, the path that already
   works locally), or
2. a **human** to check that county's site.

So the Render cron will produce real recommendations for the scriptable
counties (Liberty, Brazoria, Fort Bend, Galveston, Montgomery, Ector, San
Jacinto via ACT/Certified Payments) and a clean BLOCKED list for the captcha
ones — instead of silently guessing on all of them.

## Knobs (env)
| Var | Default | Effect |
|-----|---------|--------|
| `REQUIRE_LIVE_TAX` | `1` | Gate on. `0` = legacy note-based (emits the "gate OFF" warning). |
| `CAPTCHA_COUNTIES` | `BELL,TAYLOR,CALDWELL` | Pre-blocked with a clear reason (headless can't pass them). |
| `TAX_LOOKBACK_DAYS` | `365` | "Recent payment" window. |

## How to roll this onto another bot
1. `import completion_contract as cc`
2. Declare the bot's required artifacts: `cc.ArtifactSpec(key=..., fields=(...), source=..., verified=...)`.
3. Per unit of work: try to fill the artifact live; on failure `cc.block(record, reason)`.
4. Before deciding: `cc.assert_decidable(record, SPECS)` (or check `cc.is_decidable`).
5. Before delivery: `cc.audit(records)` → put `cc.audit_line(...)` at the top of the report.

The same five steps make any bot finish the job or say exactly why it couldn't.
