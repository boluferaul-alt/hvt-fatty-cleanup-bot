"""CrmClient - tax_watch's lead source and note target once Lofty is gone.

Raul 2026-09-28: "starting this Saturday, from now on, we're going to go in [the new CRM]. I'm
canceling Lofty this week." The CRM (dirty-deed-db on Render) serves the same leads, stages and
researcher notes it used to mirror from Lofty, shaped like Lofty's payloads, so tax_watch's
locating and county-lookup code runs unchanged against it. Only the methods tax_watch calls exist.

leadId here is the CRM's own lead id, not Lofty's. `lofty_ids` maps back so the account cache
learned under Lofty ids keeps working.
"""
from __future__ import annotations

import os

import requests

# tax_watch stage key -> the stage name the CRM stores (the old Lofty pipeline names).
CRM_STAGES = {
    "hvt": "HVT",
    "fatty": "FATTY",
    "hot": "HOT Occupied/Alive",
    "nurture": "Nurture",
    "ddoffer": "DD Offer",
}


class _NoLofty:
    status_code = 404

    @staticmethod
    def json():
        return {}


class CrmClient:
    def __init__(self, url: str | None = None, key: str | None = None):
        self.url = (url or os.getenv("CRM_URL", "https://dirty-deed-db.onrender.com")).rstrip("/")
        self.key = (key or os.getenv("CRM_BOT_API_KEY", "")).strip()
        if not self.key:
            raise RuntimeError("CRM_BOT_API_KEY missing: set it to the CRM's BOT_API_KEY")
        self.s = requests.Session()
        self.s.headers.update({"X-API-Key": self.key})
        self.lofty_ids: dict[str, str] = {}

    def _get(self, path: str, **params):
        r = self.s.get(f"{self.url}{path}", params=params, timeout=120)
        r.raise_for_status()
        return r.json()

    def list_leads_in_stages(self, stage_names: list[str]) -> dict[str, list[dict]]:
        """{stage name: [lead, ...]} for the given CRM stage names."""
        out: dict[str, list[dict]] = {n: [] for n in stage_names}
        offset = 0
        while True:
            j = self._get("/api/bot/tax-watch/leads", stages=",".join(stage_names), limit=500, offset=offset)
            for lead in j["leads"]:
                if lead.get("loftyLeadId"):
                    self.lofty_ids[str(lead["leadId"])] = str(lead["loftyLeadId"])
                out.setdefault(lead["stage"], []).append(lead)
            offset += len(j["leads"])
            if not j["leads"] or offset >= j["total"]:
                return out

    def get_notes(self, lead_id) -> list[dict]:
        return self._get(f"/api/bot/tax-watch/notes/{int(lead_id)}")["notes"]

    def post_note(self, lead_id, content: str, pin: bool = False) -> bool:
        """The CRM pins the new tax note and hides the older weeks itself."""
        try:
            r = self.s.post(f"{self.url}/api/bot/tax-update", timeout=60,
                            json={"parcel_id": int(lead_id), "body": content})
            return r.status_code == 200 and bool(r.json().get("found"))
        except requests.RequestException:
            return False

    def set_note_pin(self, *_a, **_k) -> bool:
        return True       # the CRM already unpinned (and hid) the old tax notes when the new one landed

    def _request(self, *_a, **_k):
        return _NoLofty()  # lead_locator asks Lofty for lead detail; the CRM already sent it all
