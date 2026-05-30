"""
Lofty API client for the cleanup bot.

Pattern lifted from the existing lofty-bot (boluferaul-alt/lofty-bot) +
extended with:
  - get_notes(lead_id): read existing notes on a lead so we can parse
    the Researcher Bot Summary Report that the original lofty-bot wrote.
  - list_stages(): try to enumerate pipeline stages so target stage IDs
    can be discovered at runtime instead of hardcoded.
  - move_to_stage(lead_id, stage_id): single best-effort move attempt
    (the existing bot's probe_move_stage tried 5 endpoint variants on
    every move — we already know which one Lofty's API expects, so we
    just try it and log the outcome).

WHY a separate client instead of importing from the existing bot: the
existing repo is `lofty-bot`, not a package. Vendoring the auth pattern
here keeps this service deployable as a standalone Render web service
without git-submodule shenanigans, and the surface area is tiny.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://api.lofty.com/v1.0"


@dataclass
class MoveAttempt:
    """One stage-move attempt — return value of move_to_stage()."""
    method: str
    url: str
    status: int
    ok: bool
    body: Any

    def __str__(self) -> str:
        tag = "OK" if self.ok else "FAIL"
        return f"[{tag}] {self.method} {self.url} -> HTTP {self.status}"


class LoftyClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = (api_key or os.getenv("LOFTY_API_KEY", "")).strip()
        if not self.api_key or self.api_key == "placeholder":
            raise RuntimeError("LOFTY_API_KEY missing or placeholder in env")
        self.headers = {
            "Authorization": f"token {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            # Lofty's WAF blocks python-requests' default UA.
            "User-Agent": "hvt-fatty-cleanup-bot/1.0 (+render)",
        }

    # ------------------------------------------------------------------
    # Low-level
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{BASE_URL}{path}"
        kwargs.setdefault("headers", self.headers)
        kwargs.setdefault("timeout", 30)
        resp = requests.request(method, url, **kwargs)
        if resp.status_code == 429:
            # Single 60s retry on rate limit.
            time.sleep(60)
            resp = requests.request(method, url, **kwargs)
        return resp

    @staticmethod
    def _body(resp: requests.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            return resp.text

    @staticmethod
    def _extract_list(body: Any) -> list:
        """Lofty wraps list payloads under different keys depending on endpoint."""
        if isinstance(body, list):
            return body
        if isinstance(body, dict):
            for key in ("leads", "list", "items", "results", "notes", "stages"):
                if isinstance(body.get(key), list):
                    return body[key]
            data = body.get("data")
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                for key in ("leads", "list", "items", "results", "notes", "stages"):
                    if isinstance(data.get(key), list):
                        return data[key]
        return []

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_me(self) -> dict:
        resp = self._request("GET", "/me")
        resp.raise_for_status()
        return resp.json()

    def list_leads_in_stages(self, stage_ids: list[int],
                             limit: int = 100,
                             max_pages: int = 500) -> dict[int, list[dict]]:
        """
        Single-pass variant of list_leads_in_stage that buckets leads for
        multiple target stages from one walk of the /leads endpoint.
        Returns {stage_id: [leads, ...]} for each requested stage_id.

        Lofty's /leads endpoint quirks (observed 2026-05-30):
          - `stageId=` query param is silently ignored; filter client-side.
          - `limit` is hard-capped at 100. limit>100 returns 0 (not error).
          - The endpoint surfaces ~35K leads in workspace order. Real
            pipeline-assigned leads are scattered throughout offset range.
            Must scan the full set to find every lead in a given stage.
        """
        if limit > 100:
            limit = 100
        targets = set(stage_ids)
        out: dict[int, list[dict]] = {sid: [] for sid in stage_ids}
        page = 0
        empty_streak = 0
        while page < max_pages:
            offset = page * limit
            resp = self._request("GET", "/leads", params={
                "offset": offset,
                "limit": limit,
            })
            if resp.status_code != 200:
                break
            batch = self._extract_list(resp.json())
            if not batch:
                empty_streak += 1
                if empty_streak >= 3:
                    break
                page += 1
                continue
            empty_streak = 0
            for ld in batch:
                sid = ld.get("stageId")
                if sid in targets:
                    out[sid].append(ld)
            if len(batch) < limit:
                break  # genuine last page
            page += 1
        return out

    def list_leads_in_stage(self, stage_id: int, limit: int = 100,
                            max_pages: int = 500) -> list[dict]:
        """
        Return every lead currently in `stage_id`. Paginates the global
        /leads endpoint and filters client-side by stageId.

        Lofty's /leads endpoint quirks (observed 2026-05-30):
          - The `stageId=` query param is silently ignored by the server,
            so we MUST filter client-side.
          - `limit` is hard-capped at 100. Passing limit>100 returns 0
            leads (not an error, just empty). Stay at 100.
          - The endpoint surfaces ~35K leads in workspace order. Real
            pipeline-assigned leads are NOT concentrated near the top;
            they're scattered throughout the offset range. Need to scan
            the full set to find every lead in a given stage.

        Defensive caps:
          - max_pages * limit = 50,000 (safely covers ~36K workspace)
          - bails early if a page comes back empty (true end of list)
          - bails early if API returns non-200
        """
        if limit > 100:
            limit = 100  # API caps at 100 — anything higher returns []
        all_leads: list[dict] = []
        page = 0
        empty_streak = 0
        while page < max_pages:
            offset = page * limit
            resp = self._request("GET", "/leads", params={
                "stageId": stage_id,   # server ignores, but harmless
                "offset": offset,
                "limit": limit,
            })
            if resp.status_code != 200:
                break
            batch = self._extract_list(resp.json())
            if not batch:
                # One empty page might just be a stage-less range; tolerate
                # a short streak of empties before giving up.
                empty_streak += 1
                if empty_streak >= 3:
                    break
                page += 1
                continue
            empty_streak = 0
            in_stage = [ld for ld in batch if ld.get("stageId") == stage_id]
            all_leads.extend(in_stage)
            if len(batch) < limit:
                break  # genuine last page
            page += 1
        return all_leads

    def get_lead(self, lead_id: int) -> Optional[dict]:
        """Fetch a single lead by ID. Returns None on 404."""
        resp = self._request("GET", f"/leads/{lead_id}")
        if resp.status_code != 200:
            return None
        body = resp.json()
        if isinstance(body, dict):
            for key in ("data", "lead"):
                node = body.get(key)
                if isinstance(node, dict) and (node.get("leadId") or node.get("id")):
                    return node
            if body.get("leadId") or body.get("id"):
                return body
        return body

    def get_notes(self, lead_id: int) -> list[dict]:
        """
        Read all notes on a lead. The Researcher Bot Summary Report we need
        to parse is one of these notes (posted by the existing lofty-bot).

        Tries the standard Lofty endpoints in order — the first one that
        returns a list wins. If none work we return [] and the caller has
        to fall back to whatever metadata is on the lead object itself.
        """
        endpoints = [
            ("GET", f"/leads/{lead_id}/notes"),
            ("GET", "/notes", {"leadId": lead_id, "limit": 100}),
            ("GET", f"/leads/{lead_id}", None),  # Some accounts embed notes in the lead.
        ]
        for ep in endpoints:
            method, path = ep[0], ep[1]
            params = ep[2] if len(ep) > 2 else None
            kwargs = {}
            if params:
                kwargs["params"] = params
            resp = self._request(method, path, **kwargs)
            if resp.status_code != 200:
                continue
            body = resp.json()
            # Notes might be top-level or embedded inside a lead object.
            notes = self._extract_list(body)
            if notes:
                return notes
            # Embedded path: GET /leads/{id} returned the whole lead;
            # look for a notes field on it.
            if isinstance(body, dict):
                for key in ("notes", "leadNotes"):
                    val = body.get(key)
                    if isinstance(val, list) and val:
                        return val
                data = body.get("data")
                if isinstance(data, dict):
                    for key in ("notes", "leadNotes"):
                        val = data.get(key)
                        if isinstance(val, list) and val:
                            return val
        return []

    def list_stages(self) -> list[dict]:
        """
        Try to enumerate every pipeline stage so the orchestrator can
        match names like 'HVT' / 'FATTY' / 'Vault Hot Occ Alive' to stage
        IDs at runtime. Returns [] if the API doesn't expose this.
        """
        for path in ("/stages", "/pipelines/stages", "/pipelineStages"):
            resp = self._request("GET", path)
            if resp.status_code != 200:
                continue
            stages = self._extract_list(resp.json())
            if stages:
                return stages
        return []

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def post_note(self, lead_id: int, content: str) -> bool:
        """
        Post a note to a lead. Returns True on success. Tries the flat
        /notes endpoint first (the one that works on the existing bot's
        API key); falls back to /leads/{id}/notes.
        """
        endpoints = [
            ("POST", "/notes", {"leadId": lead_id, "content": content}),
            ("POST", f"/leads/{lead_id}/notes", {"content": content}),
        ]
        for method, path, payload in endpoints:
            resp = self._request(method, path, json=payload)
            if resp.status_code in (200, 201, 204):
                body = self._body(resp)
                # Lofty sometimes returns 200 + {"data":"no change"} for
                # writes that silently no-op'd. Treat that as failure.
                if isinstance(body, dict):
                    data = body.get("data")
                    if isinstance(data, str) and data.lower() == "no change":
                        continue
                return True
        return False

    def move_to_stage(self, lead_id: int, stage_id: int) -> MoveAttempt:
        """
        Attempt to move a lead to a target stage.

        IMPORTANT: the existing lofty-bot found that stage-move writes
        silently fail under that API key's scope (returns 200 +
        {"data": "no change"}). If that's still the case, this returns
        ok=False and the caller falls back to RECOMMEND-only mode.

        If the API key has been upgraded since then, this might actually
        work — the bot will report ok=True in the Slack summary and the
        lead will be moved.
        """
        path = f"/leads/{lead_id}"
        payload = {"stageId": stage_id}
        try:
            resp = self._request("PUT", path, json=payload)
        except requests.RequestException as e:
            return MoveAttempt(method="PUT", url=f"{BASE_URL}{path}",
                               status=0, ok=False, body=str(e))
        body = self._body(resp)
        ok = resp.status_code in (200, 201, 204)
        if ok and isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, str) and data.lower() == "no change":
                ok = False
        return MoveAttempt(method="PUT", url=resp.url,
                           status=resp.status_code, ok=ok, body=body)


if __name__ == "__main__":
    # Quick smoke check.
    import sys
    try:
        c = LoftyClient()
        me = c.get_me()
        print(f"OK: authenticated as {me.get('firstName')} {me.get('lastName')}")
    except Exception as e:
        print(f"FAIL: {e}", file=sys.stderr)
        sys.exit(1)
