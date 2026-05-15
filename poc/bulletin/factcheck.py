"""Factcheck step — second AI call that verifies draft bulletin against source pool."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from pathlib import Path

import anthropic

from .schema import BulletinDraft, BulletinVerified, ValidationError, validate_verified

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent.parent.parent / "prompts" / "bulletin-factcheck.md"


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _extract_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\s*```$", "", text.strip(), flags=re.MULTILINE)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in factcheck output")
    return json.loads(text[start : end + 1])


def _draft_to_dict(draft: BulletinDraft) -> dict:
    return dataclasses.asdict(draft)


def run_factcheck(
    draft: BulletinDraft,
    source_pool: list[dict],
    *,
    api_key: str | None = None,
) -> BulletinVerified:
    """Run the factcheck prompt and return a validated BulletinVerified."""
    client = anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])
    system = _load_system_prompt()

    user_content = json.dumps(
        {
            "draft_json": _draft_to_dict(draft),
            "source_pool": source_pool,
        },
        ensure_ascii=False,
        indent=2,
    )

    log.info("Running factcheck on draft (utgave %d)", draft.utgave_nummer)
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=10000,
        temperature=0.1,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )

    text_parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    raw_text = "\n".join(text_parts).strip()
    log.info("Factcheck response: %d chars", len(raw_text))

    try:
        data = _extract_json(raw_text)
    except (ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Factcheck returned invalid JSON: {exc}\n\n{raw_text[:500]}") from exc

    try:
        verified = validate_verified(data)
    except ValidationError as exc:
        raise RuntimeError(f"Factcheck output failed schema validation: {exc}") from exc

    fc = verified._factcheck_report
    if fc:
        log.info(
            "Factcheck report — verified: %d, corrected: %d, removed: %d, issues: %d",
            fc.claims_verified, fc.claims_corrected, fc.claims_removed, len(fc.issues),
        )
        for issue in fc.issues:
            log.warning("Factcheck issue [%s]: %s → %s", issue.location, issue.issue, issue.action)

        if any(i.issue == "insufficient" for i in fc.issues):
            log.warning("Factcheck marked draft as INSUFFICIENT — proceeding anyway (internal use)")

    return verified
