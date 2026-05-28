# HVT + Fatty Cleanup Bot

A scheduled + on-demand bot that audits the **HVT** and **Fatty** pipelines
in Lofty CRM. For each lead, it reads the Researcher Bot Summary note,
checks whether the property is currently listed on **Zillow**, scrapes
the **county tax portal** to see if the owner has made a recent payment,
and decides whether the lead should be **vaulted**, **flagged** for manual
review, or left **as-is**.

Companion to **hot-occ-alive-cleanup-bot** (same Render deployment style,
same Lofty + Slack scaffolding) and lifts the Zillow + tax-portal modules
from **lofty-overdue-bot** (running daily in prod for weeks).

---

## What it does per lead

1. Pulls every lead currently in stageId `606230` (HVT) and `606229` (Fatty).
2. Fetches each lead's notes via Lofty API.
3. Picks the most recent Researcher Bot Summary note and parses out:
   property address, county, owner, taxes owed, appraised value.
4. Runs `zillow_check.is_listed_for_sale(address)` — Playwright-driven,
   returns True / False / None (None = couldn't determine).
5. Runs `tax_scraper` against the property's county portal — returns the
   current balance, the amount paid in the last 12 months, and the date
   of the last payment.
6. Applies the decision tree (below) → VAULT / FLAG / STAY.
7. If decision is VAULT, moves the lead via Lofty API to
   `STAGE_VAULT_HOT_OCC_ALIVE` (428742) — same vault destination as the
   existing hot-occ-alive cleanup bot.
8. Posts a structured Slack summary with per-category sections to
   `#dirtydeedbot`.

---

## Decision tree

| Priority | Signal | Outcome |
|---|---|---|
| 1 | Listed on Zillow (True) | **VAULT** — "Owner is selling on MLS" |
| 2 | Tax payment in last 12 months ≥ $1,000 | **VAULT** — "Heat cooling: paid $X on YYYY-MM-DD" |
| 3 | Tax payment in last 12 months > $0 but < $1,000 | **FLAG** — "Small payment $X, manual review" |
| 4 | Zillow check returned None (couldn't determine) AND tax check OK | **FLAG** — "Zillow undetermined, manual review" |
| 5 | Tax scrape failed entirely | **FLAG** — "Tax check failed for county Y" |
| 6 | No Researcher Bot Summary note on lead | **FLAG** — "Missing bot summary, can't address-lookup" |
| 7 | Everything OK, no listing, no payment | **STAY** — leave in HVT or Fatty |

See `hvt_fatty_decision.py` for the code. The $1,000 threshold is exposed
as `PAYMENT_VAULT_THRESHOLD` (env-overridable).

---

## Deployment to Render

### One-click via Blueprint

This repo includes a `render.yaml` Blueprint that auto-creates:
- A **web service** (Docker — Playwright needs chromium baked in) for
  `/run`-on-demand.
- A **cron job** (Sunday 23:00 UTC = 6pm Central weekly run).

Steps:

1. Push this repo to GitHub.
2. In Render, click **New → Blueprint**.
3. Connect this repo. Render reads `render.yaml`.
4. You'll be prompted for **two secret env vars**:
   - `LOFTY_API_KEY` — copy from the existing lofty-bot or
     hot-occ-alive-cleanup-bot Render service.
   - `SLACK_WEBHOOK_URL` — same `#dirtydeedbot` webhook the other
     cleanup bots use.
5. Click **Apply**.

Within a few minutes:
- `https://hvt-fatty-cleanup-bot.onrender.com/` (health check)
- A cron job in your dashboard, scheduled weekly.

### Stage IDs — pre-populated

All stage IDs are hardcoded in `render.yaml`:

| Pipeline | stageId |
|---|---|
| HVT (source) | 606230 |
| Fatty (source) | 606229 |
| Vault Hot Occ Alive (destination for VAULT) | 428742 |

If Lofty re-numbers a stage, update the env var in Render's UI — no
redeploy needed.

---

## Slack setup (incoming webhook)

Reuse the existing `#dirtydeedbot` incoming webhook used by
`hot-occ-alive-cleanup-bot`. Paste it into Render as `SLACK_WEBHOOK_URL`.

---

## Triggering a run

### Manual (HTTP)

```bash
# Health check
curl https://hvt-fatty-cleanup-bot.onrender.com/

# Trigger a run (uses env-var defaults)
curl -X POST https://hvt-fatty-cleanup-bot.onrender.com/run

# Dry run — decide but don't move
curl -X POST https://hvt-fatty-cleanup-bot.onrender.com/run \
     -H "Content-Type: application/json" \
     -d '{"dry_run": true}'

# Process only first 5 leads (testing)
curl -X POST https://hvt-fatty-cleanup-bot.onrender.com/run \
     -H "Content-Type: application/json" \
     -d '{"limit": 5}'

# Poll status (every 30s)
curl https://hvt-fatty-cleanup-bot.onrender.com/status
```

### Scheduled

The cron job runs `python main.py` every Sunday at 23:00 UTC.

---

## Env vars

| Name | Required | Default | What it does |
|---|---|---|---|
| `LOFTY_API_KEY` | yes | — | Lofty API key (copy from existing lofty-bot). |
| `SLACK_WEBHOOK_URL` | yes | — | Slack incoming webhook for summary reports. |
| `ANTHROPIC_API_KEY` | optional | — | Used by tax_scraper for HTML interpretation. If empty, tax scrape returns FLAG ("LLM key missing"). |
| `STAGE_HVT` | no | `606230` | HVT source pipeline. |
| `STAGE_FATTY` | no | `606229` | Fatty source pipeline. |
| `STAGE_VAULT_HOT_OCC_ALIVE` | no | `428742` | VAULT destination. |
| `PAYMENT_VAULT_THRESHOLD` | no | `1000` | USD; ≥ this paid in last 12mo → VAULT. |
| `DRY_RUN` | no | `0` | `1` = decide but don't move or post (still logs). |
| `AUTO_MOVE` | no | `1` | `0` = decide + post Slack, but skip API moves. |
| `CLEANUP_LIMIT` | no | `0` | Max leads per run. `0` = all. |

---

## Local development

```bash
cd ~/Desktop
git clone https://github.com/boluferaul-alt/hvt-fatty-cleanup-bot.git
cd hvt-fatty-cleanup-bot

pip install -r requirements.txt
playwright install chromium --with-deps

cp .env.example .env
# Edit .env — paste LOFTY_API_KEY + SLACK_WEBHOOK_URL + ANTHROPIC_API_KEY

# Smoke test the Lofty auth
python lofty_client.py
# Expected: OK: authenticated as Raul Bolufe

# Run regression tests on the decision tree
python test_decisions.py

# Run a 5-lead dry-run end-to-end
DRY_RUN=1 CLEANUP_LIMIT=5 python main.py
```

---

## Files

| File | Purpose |
|---|---|
| `app.py` | Flask wrapper — HTTP entrypoint for Render web service |
| `main.py` | Orchestrator — top-to-bottom cleanup run |
| `lofty_client.py` | Lofty API client (auth, list leads, read notes, move) |
| `note_parser.py` | Parses Researcher Bot Summary notes |
| `zillow_check.py` | Playwright-driven MLS-listing check |
| `tax_scraper.py` | County-portal payment lookup |
| `county_resolver.py` | Discovers tax-search URL per TX county |
| `hvt_fatty_decision.py` | Decision tree (VAULT / FLAG / STAY) |
| `slack_client.py` | Posts Slack summary via incoming webhook |
| `data/county_playbooks.json` | Cached per-county form selectors |
| `render.yaml` | Render Blueprint — one-click deploy |
| `Dockerfile` | Docker image (Playwright base + Python deps) |
| `requirements.txt` | Python deps |
| `.env.example` | Template for local development |
