# HVT / Fatty / Hot Occ Alive Cleanup Bot

A scheduled + on-demand bot that audits the **HVT**, **Fatty**, and
**Hot Occupied/Alive** pipelines in Lofty CRM. For each lead it reads the
Researcher Bot Summary note (value, taxes owed, tax-lawsuit, deceased/alive,
occupancy), checks whether the property is **actively listed** (Zillow +
Realtor.com, for sale OR for rent), scrapes the **county tax portal** for the
current balance + recent payments, and recommends one of **five
destinations** based on how much *motivation* is left.

**Decision model rebuilt 2026-06-24** from Raul's hand review of 467 leads —
"taxes were paid" is NOT an automatic removal. See `hvt_fatty_decision.py`.

**Report-only by default.** The bot recommends and posts a Slack report; Raul
reviews and moves leads himself. (Set `AUTO_MOVE=1` to auto-route each
recommendation to its stage.)

Companion to **hot-occ-alive-cleanup-bot** (same Render deployment style,
same Lofty + Slack scaffolding).

---

## What it does per lead

1. Pulls every lead in `606230` (HVT), `606229` (Fatty), `648757` (Hot Occ Alive).
2. Parses the Researcher Summary note: value, taxes owed, tax-lawsuit,
   deceased/alive, occupancy, owner.
3. `listing_check.check_listing` — Zillow + Realtor, **active** for-sale/for-rent
   only (off-market / sold / pending are rejected).
4. `tax_scraper` — county portal current balance + recent payments.
5. `decide(...)` → **DNC / HVT / VAULT / OCC_ALIVE / STAY / REVIEW** (+ confidence).
6. *(Only if `AUTO_MOVE=1`)* routes each recommendation to its stage
   (DNC→425581, HVT→606230, VAULT→428742, OCC_ALIVE→648757). STAY/REVIEW never move.
7. Posts a Slack report to `#dirtydeedbot` grouped by destination.

---

## Decision model (Raul's logic — priority order)

| # | Signal | Destination |
|---|---|---|
| 1 | Listed on the market (VERIFIED active) | **DNC** — realtors block deep discounts |
| 2 | Taxes fully paid / $0 due | **DNC** |
| 3 | Est. net `< MIN_NET_PROFIT` (`0.8·value − owed − $8K heirs − $7K attorney − 6%`) | **DNC** (or **OCC_ALIVE** if deceased) |
| 4 | Deceased owner + value ≥ `HVT_MIN_VALUE` + still owes | **HVT** (title/heir play) |
| 5 | Active tax lawsuit + still owes | **STAY** (strong motivation) |
| 6 | Estate / third-party payer + intent to pay | **VAULT** |
| 7 | Big recent payment + low owed/value ratio + alive | **DNC** · else **OCC_ALIVE** |
| 8 | Owed/value ratio `< DEAD_RATIO` (~2%) → DNC; `< LOW_RATIO` (~4%) → OCC_ALIVE | **DNC / OCC_ALIVE** |
| 9 | Small payments vs big balance owed | **STAY** |
| 10 | Missing tax/value data | **REVIEW** |

All thresholds (`SALE_FACTOR`, `HEIR_COST`, `ATTORNEY_COST`, `COMMISSION_PCT`,
`MIN_NET_PROFIT`, `HVT_MIN_VALUE`, `SUBSTANTIAL_PAYMENT`, `DEAD_RATIO`,
`LOW_RATIO`) are env-overridable — retune without a redeploy. `test_decisions.py`
encodes 15 of Raul's real leads as the regression spec.

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
| Hot Occ Alive (source) | 648757 |
| Vault Hot Occ Alive (destination if AUTO_MOVE=1) | 428742 |

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
| `STAGE_HOT_OCC_ALIVE` | no | `648757` | Hot Occ Alive source pipeline. |
| `STAGE_VAULT_HOT_OCC_ALIVE` | no | `428742` | REMOVE destination (only used if `AUTO_MOVE=1`). |
| `PAYMENT_FLAG_THRESHOLD` | no | `500` | USD; ≥ this paid within the window → REMOVE. |
| `TAX_LOOKBACK_DAYS` | no | `90` | Days back that count as a "recent" payment. |
| `DRY_RUN` | no | `0` | `1` = decide but don't move or post (still logs). |
| `AUTO_MOVE` | no | `0` | `0` = report-only (no moves). `1` = auto-vault REMOVE. |
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
| `listing_check.py` | Playwright listing check — Zillow + Realtor.com, sale + rent |
| `zillow_check.py` | Legacy Zillow-only check (superseded by listing_check) |
| `tax_scraper.py` | County-portal payment lookup |
| `county_resolver.py` | Discovers tax-search URL per TX county |
| `hvt_fatty_decision.py` | Decision tree (VAULT / FLAG / STAY) |
| `slack_client.py` | Posts Slack summary via incoming webhook |
| `data/county_playbooks.json` | Cached per-county form selectors |
| `render.yaml` | Render Blueprint — one-click deploy |
| `Dockerfile` | Docker image (Playwright base + Python deps) |
| `requirements.txt` | Python deps |
| `.env.example` | Template for local development |
