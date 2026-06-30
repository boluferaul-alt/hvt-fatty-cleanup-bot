"""
completion_contract.py — shared "definition of done" for the Dirty Deed bots.

WHY THIS EXISTS
---------------
An LLM agent (and a bot that leans on one) always drifts toward the cheapest
path that *looks* finished. In the cleanup bot that meant: read the researcher
note's "Tax Paid Last 12 Months" field and decide off it, instead of hitting
the live county tax-collector site. The note is stale; the live county site is
the only source of truth for "have taxes actually been paid." Prose
instructions ("please verify live") do not stop the shortcut. A hard gate does.

THE CONTRACT
------------
A unit of work — a lead "record" (a plain dict) — is only DECIDABLE once every
required artifact is present AND proven live: each artifact must have its data
field(s) filled, a `source` (the URL/site it came from) and a `verified`
timestamp. Miss any one and the record is BLOCKED with a specific reason.

  - decide steps call assert_decidable() and refuse to run on a BLOCKED record,
    so a recommendation can never be produced off unverified data.
  - audit() returns an honest verified-vs-blocked tally so partial work can
    never be dressed up as complete in the report.

Every bot imports this, declares its required artifacts once, and runs the same
gate. Enforced in code, not in a prompt. To roll it onto another bot: define
that bot's ArtifactSpec list, call assert_decidable()/block() per unit of work,
and audit() before delivery.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

VERIFIED = "VERIFIED"
BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ArtifactSpec:
    """One required, live-verified artifact on a record.

    key:      short id, e.g. "tax".
    label:    human label for reports, e.g. "live county tax check".
    fields:   record keys whose value must be present (is not None). A value of
              0 counts as present — a $0 live balance is a real verified answer.
    source:   record key holding the live source (URL/site); must be truthy.
    verified: record key holding the verified-at timestamp; must be truthy.
    """
    key: str
    label: str
    fields: tuple[str, ...]
    source: str
    verified: str


def missing_reasons(record: dict, specs: Iterable[ArtifactSpec]) -> list[str]:
    """Why this record is not decidable. Empty list = fully verified."""
    reasons: list[str] = []
    for spec in specs:
        why: list[str] = []
        for f in spec.fields:
            if record.get(f) is None:
                why.append(f"no {f}")
        if not record.get(spec.source):
            why.append("no live source")
        if not record.get(spec.verified):
            why.append("not verified live")
        if why:
            reasons.append(f"{spec.label}: " + ", ".join(why))
    return reasons


def is_decidable(record: dict, specs: Iterable[ArtifactSpec]) -> bool:
    return not missing_reasons(record, specs)


class BlockedError(RuntimeError):
    """Raised when a decision is attempted on an unverified record."""


def assert_decidable(record: dict, specs: Iterable[ArtifactSpec]) -> None:
    """Raise BlockedError unless every required artifact is present + live."""
    reasons = missing_reasons(record, specs)
    if reasons:
        raise BlockedError("; ".join(reasons))


def block(record: dict, reason: str) -> dict:
    """Mark a record BLOCKED with a reason. It will never get a recommendation."""
    record["status"] = BLOCKED
    record["blocked_reason"] = reason
    return record


def mark_verified(record: dict) -> dict:
    record["status"] = VERIFIED
    record.setdefault("blocked_reason", "")
    return record


def audit(records: list[dict]) -> dict:
    """Honest completion tally — blocked work can never read as done.

    Returns total / verified / blocked counts, a reason histogram, a `complete`
    flag (no blocked), and pct_verified. Put this at the top of every report.
    """
    verified = [r for r in records if r.get("status") == VERIFIED]
    blocked = [r for r in records if r.get("status") == BLOCKED]
    reasons: dict[str, int] = {}
    for r in blocked:
        # Group by the first clause of the reason (the artifact + cause).
        key = (r.get("blocked_reason") or "unknown").split(";")[0].strip()
        reasons[key] = reasons.get(key, 0) + 1
    total = len(records)
    return {
        "total": total,
        "verified": len(verified),
        "blocked": len(blocked),
        "blocked_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "complete": len(blocked) == 0,
        "pct_verified": round(100 * len(verified) / total) if total else 0,
    }


def audit_line(a: dict) -> str:
    """One-line honest summary for a report header (ASCII-safe for any console)."""
    line = f"VERIFIED LIVE: {a['verified']}  |  BLOCKED (not verified): {a['blocked']}"
    if a["blocked_reasons"]:
        top = next(iter(a["blocked_reasons"].items()))
        line += f"  |  top reason: {top[0]} ({top[1]})"
    return line
