"""Generator step — calls claude-opus-4-7 with tool use to produce draft bulletin JSON."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import anthropic

from .schema import BulletinDraft, ValidationError, validate_draft

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "bulletin-generator.md"

_TOOLS: list[dict] = [
    {
        "name": "fetch_weekly_feed",
        "description": "Hent artikler og poster fra siste 7 dager.",
        "input_schema": {
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "ISO date YYYY-MM-DD"},
                "to_date":   {"type": "string", "description": "ISO date YYYY-MM-DD"},
            },
            "required": [],
        },
    },
    {
        "name": "fetch_market_data",
        "description": "Hent aksjekurser, laksepriser og valutakurser.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "fetch_calendar_next_week",
        "description": "Hent planlagte events og bransjehendelser for neste uke.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _extract_json(text: str) -> dict:
    """Strip markdown fences and parse JSON from model output."""
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text.strip(), flags=re.MULTILINE)
    # Find outermost { ... }
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in model output")
    return json.loads(text[start : end + 1])


def run_generator(
    weekly_feed: list[dict],
    market_data: dict,
    calendar_data: list[dict],
    utgave_nummer: int,
    *,
    api_key: str | None = None,
) -> BulletinDraft:
    """Run the generator prompt and return a validated BulletinDraft."""
    client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
    system = _load_system_prompt()

    # Data payloads returned when Claude calls each tool
    tool_responses: dict[str, Any] = {
        "fetch_weekly_feed": weekly_feed,
        "fetch_market_data": market_data,
        "fetch_calendar_next_week": calendar_data,
    }

    messages: list[dict] = [
        {
            "role": "user",
            "content": (
                f"Lag News Bulletin utgave {utgave_nummer}. "
                "Bruk verktøyene for å hente data, og returner deretter JSON-objektet."
            ),
        }
    ]

    # Agentic tool-use loop
    for _turn in range(8):
        log.info("Generator turn %d", _turn + 1)
        response = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=8000,
            temperature=0.3,
            system=system,
            tools=_TOOLS,
            messages=messages,
        )

        # Append assistant turn
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            # Extract text output and parse JSON
            text_parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
            raw_text = "\n".join(text_parts).strip()
            log.info("Generator produced %d chars", len(raw_text))
            try:
                data = _extract_json(raw_text)
            except (ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"Generator returned invalid JSON: {exc}\n\n{raw_text[:500]}") from exc

            try:
                return validate_draft(data)
            except ValidationError as exc:
                # Re-prompt once with the validation error
                if _turn == 0:
                    log.warning("Validation failed, re-prompting: %s", exc)
                    messages.append({
                        "role": "user",
                        "content": f"JSON-validering feilet: {exc}. Returner korrigert JSON.",
                    })
                    continue
                raise

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if getattr(block, "type", "") == "tool_use":
                    payload = tool_responses.get(block.name, {})
                    log.info("Tool call: %s", block.name)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(payload, ensure_ascii=False),
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        break

    raise RuntimeError("Generator did not produce output within turn limit")
