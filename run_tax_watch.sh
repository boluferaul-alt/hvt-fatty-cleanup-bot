#!/bin/sh
# Render cron entrypoint (Render passes dockerCommand without a shell, so the
# two steps live here). Refresh county rolls, then check every lead.
python -u fetch_bulk_rolls.py
exec python -u tax_watch.py --stages hvt,fatty,hot,nurture,ddoffer --lookback 14
