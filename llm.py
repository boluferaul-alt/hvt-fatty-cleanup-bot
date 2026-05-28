"""Anthropic Claude wrapper. Single chat-completion helper used by:
- county_resolver.py (find tax site URL for an unknown county)
- tax_scraper.py (interpret messy tax-history HTML)

Lifted from lofty-overdue-bot/src/llm.py.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from anthropic import Anthropic

log = logging.getLogger(__name__)

# Default to the latest Sonnet — cheap + fast + smart enough for the work here.
MODEL = "claude-sonnet-4-6"


class LLM:
    def __init__(self, api_key: str) -> None:
        self._client = Anthropic(api_key=api_key)

    def ask_json(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        model: str = MODEL,
    ) -> dict[str, Any]:
        """Run a prompt and parse a single JSON object out of the response.

        Claude is asked (via system prompt) to reply with raw JSON only.
        If parsing fails we log and return {} so callers can degrade.
        """
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system + "\n\nReply with a single JSON object and nothing else.",
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        ).strip()
        # Strip ```json fences if Claude adds them despite the instruction
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning("LLM JSON parse failed; raw response: %s", text[:400])
            return {}

    def ask_text(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        model: str = MODEL,
    ) -> str:
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            block.text for block in resp.content if getattr(block, "type", None) == "text"
        ).strip()
