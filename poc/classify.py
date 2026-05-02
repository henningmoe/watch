"""AI-based article classification using the Claude API."""

import json
import logging
import os
import re

import anthropic
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_PROMPT = (
    "Du er media-analyst for Cermaq, et globalt lakseoppdrettsselskap eid "
    "av Mitsubishi Corporation, med drift i Norge (Nordland og Finnmark), "
    "Canada (British Columbia) og Chile (Los Lagos, Aysén, Magallanes).\n\n"
    "Klassifiser denne artikkelen og returner KUN gyldig JSON, ingenting "
    "annet. Ingen markdown, ingen code blocks, bare ren JSON.\n\n"
    "Tittel: {title}\n"
    "Innhold: {content_truncated}\n"
    "Kilde: {source_name}\n\n"
    "Returner JSON med disse feltene:\n"
    '- region: "norge" | "chile" | "canada" | "global"\n'
    '- tone: "positiv" | "noytral" | "kritisk"\n'
    '- scope: "cermaq" hvis Cermaq omtales direkte, "industry" ellers\n'
    '- category: "regulatorisk" | "marked" | "fiskehelse" | "miljo" | "drift" | "ma" | "politikk" | "annet"\n'
    "- relevance: heltall 1-5 hvor 5 er mest relevant for Cermaq\n"
    "- summary_no: 2 setninger på norsk som oppsummerer artikkelen"
)

_DEFAULT = {
    "region": "global",
    "tone": "noytral",
    "scope": "industry",
    "category": "annet",
    "relevance": 1,
}


def _extract_json(text: str) -> str:
    """Strip markdown fences and extract the first JSON object from *text*."""
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[len("```json"):]
    elif cleaned.startswith("```"):
        cleaned = cleaned[len("```"):]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        return match.group(0)
    raise ValueError(f"No JSON object found in response: {text[:200]!r}")


def classify(article: dict) -> dict:
    """Classify a single article; return enrichment dict with classification fields."""
    raw_html = article.get("content", "") or ""
    plain_text = BeautifulSoup(raw_html, "html.parser").get_text()
    content_truncated = plain_text[:1500]

    default = {**_DEFAULT, "summary_no": plain_text[:200]}

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=400,
            temperature=0,
            messages=[
                {
                    "role": "user",
                    "content": _PROMPT.format(
                        title=article.get("title", ""),
                        content_truncated=content_truncated,
                        source_name=article.get("source_name", ""),
                    ),
                }
            ],
        )
        text_block = next((b for b in response.content if b.type == "text"), None)
        if text_block is None:
            raise ValueError("No text block in response")

        raw_text = text_block.text
        result = json.loads(_extract_json(raw_text))
        log.info(
            "Klassifiserte artikkel-id=%s: scope=%s, region=%s, tone=%s",
            article.get("id"),
            result.get("scope"),
            result.get("region"),
            result.get("tone"),
        )
        return result
    except Exception as exc:
        raw_text = locals().get("raw_text", "")
        log.error(
            "Klassifisering feilet for id=%s: %s. Raw response: %s",
            article.get("id"),
            exc,
            raw_text[:500],
        )
        return default


if __name__ == "__main__":
    sample = {"title": "Test article", "url": "https://example.com", "content": ""}
    print(classify(sample))
