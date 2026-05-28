"""
Flask wrapper around main.py for cloud deployment (Render).

Endpoints:
  GET  /          — health check
  POST /run       — trigger a cleanup run. Body (optional):
                    {"dry_run": true, "limit": 5}
                    Returns 202 immediately; the run continues in the
                    background. Poll /status for completion.
  GET  /status    — current run state + tail of captured stdout/stderr

Why background threads instead of sync: a cleanup over 100+ leads can
take 15+ minutes (Playwright + LLM round-trips per lead). Render and
most edge proxies cut idle HTTP at 60-100s. Returning 202 + polling
/status sidesteps the timeout entirely.

Why a single worker: the run state lives in process memory. Gunicorn is
pinned to --workers 1 and uses threads to keep /status responsive while
a run is in progress.
"""

from __future__ import annotations

import io
import os
import threading
import traceback
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone

from flask import Flask, jsonify, request

import main as bot

app = Flask(__name__)

_state_lock = threading.Lock()
_run_lock = threading.Lock()
_state: dict = {
    "status": "idle",        # idle | running | done | error
    "started_at": None,
    "finished_at": None,
    "duration_s": None,
    "stats": None,
    "error": None,
    "log_tail": "",
}


def _set_state(**updates) -> None:
    with _state_lock:
        _state.update(updates)


def _run_in_background(overrides: dict) -> None:
    buf = io.StringIO()
    _set_state(
        status="running",
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=None,
        duration_s=None,
        stats=None,
        error=None,
        log_tail="",
    )
    try:
        # Apply per-request overrides to the bot's module globals. These
        # only persist for the lifetime of this run because each /run
        # call resets them from env vars first.
        bot.DRY_RUN = bool(overrides.get("dry_run", bot.DRY_RUN))
        bot.AUTO_MOVE = bool(overrides.get("auto_move", bot.AUTO_MOVE))
        bot.CLEANUP_LIMIT = int(overrides.get("limit", bot.CLEANUP_LIMIT))
        with redirect_stdout(buf), redirect_stderr(buf):
            stats = bot.run_cleanup()
        _set_state(
            status="done",
            finished_at=datetime.now(timezone.utc).isoformat(),
            duration_s=stats.get("duration_s"),
            stats=stats,
            log_tail=buf.getvalue()[-20000:],
        )
    except Exception as e:
        traceback.print_exc(file=buf)
        _set_state(
            status="error",
            finished_at=datetime.now(timezone.utc).isoformat(),
            error=f"{type(e).__name__}: {e}",
            log_tail=buf.getvalue()[-20000:],
        )
    finally:
        _run_lock.release()


@app.get("/")
def health():
    return jsonify({
        "ok": True,
        "service": "hvt-fatty-cleanup-bot",
        "dry_run_default": bot.DRY_RUN,
        "auto_move_default": bot.AUTO_MOVE,
        "stage_hvt": bot.STAGE_HVT,
        "stage_fatty": bot.STAGE_FATTY,
        "stage_vault": bot.STAGE_VAULT_HOT_OCC_ALIVE,
    })


@app.post("/run")
def trigger_run():
    payload = request.get_json(silent=True) or {}
    overrides: dict = {}
    if "dry_run" in payload:
        overrides["dry_run"] = bool(payload["dry_run"])
    if "auto_move" in payload:
        overrides["auto_move"] = bool(payload["auto_move"])
    if "limit" in payload:
        try:
            limit = int(payload["limit"])
            if limit < 0 or limit > 10_000:
                return jsonify({"error": "limit must be 0-10000"}), 400
            overrides["limit"] = limit
        except (TypeError, ValueError):
            return jsonify({"error": "limit must be an integer"}), 400

    if not _run_lock.acquire(blocking=False):
        with _state_lock:
            current = dict(_state)
        return jsonify({"error": "a run is already in progress",
                        "state": current}), 409

    t = threading.Thread(target=_run_in_background, args=(overrides,),
                         daemon=True)
    t.start()
    with _state_lock:
        started_at = _state["started_at"]
    return jsonify({
        "ok": True,
        "started": True,
        "overrides": overrides,
        "started_at": started_at,
        "poll": "/status",
    }), 202


@app.get("/status")
def status():
    with _state_lock:
        return jsonify(dict(_state))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
