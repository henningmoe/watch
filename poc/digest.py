"""Daily AI digest generation for Cermaq Watch."""

import json
import logging
import os
import re
from datetime import datetime, timezone

import anthropic
import markdown as md

log = logging.getLogger(__name__)

lang_label = {
    "no": "norsk",
    "en": "engelsk",
    "es": "spansk",
    "ja": "japansk",
}

section_titles = {
    "no": {
        "cermaq": "Cermaq-relaterte saker",
        "norge": "Bransjebilde Norge",
        "chile": "Bransjebilde Chile",
        "canada": "Bransjebilde Canada og globalt",
        "watch": "Hva å følge med på",
    },
    "en": {
        "cermaq": "Cermaq-related stories",
        "norge": "Industry Norway",
        "chile": "Industry Chile",
        "canada": "Industry Canada and global",
        "watch": "What to watch",
    },
    "es": {
        "cermaq": "Noticias relacionadas con Cermaq",
        "norge": "Industria en Noruega",
        "chile": "Industria en Chile",
        "canada": "Industria en Canadá y global",
        "watch": "A seguir",
    },
    "ja": {
        "cermaq": "Cermaq関連のニュース",
        "norge": "ノルウェー業界",
        "chile": "チリ業界",
        "canada": "カナダ・国際業界",
        "watch": "今後注視すべき点",
    },
}

_SYSTEM_PROMPT = (
    "Du er kommunikasjonssjef-assistent for Cermaq, et globalt "
    "lakseoppdrettsselskap eid av Mitsubishi Corporation.\n\n"
    "Du lager korte daglige medie-briefinger til ledergruppen basert "
    "på artikler hentet fra norske, chilenske, kanadiske og internasjonale kilder.\n\n"
    "Skriv kortfattet, profesjonelt, og analytisk. Tone: en mediekonsulent "
    "som rapporterer til ledergruppen. Ikke salgsorientert eller skrytende.\n\n"
    "Returner KUN gyldig JSON, ingen markdown-wrapping."
)


def _extract_json(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        return match.group(0)
    raise ValueError(f"No JSON object found in response: {text[:200]!r}")


def generate_digest(articles: list[dict], lang: str = "no") -> dict | None:
    """Generate a short AI summary of the last 24 hours of articles.

    Returns dict with headline, body (HTML), generated_at, lang,
    article_count, cermaq_count — or None on failure.
    """
    article_count = len(articles)
    cermaq_count = sum(1 for a in articles if a.get("scope") == "cermaq")

    articles_text = ""
    for a in articles:
        summaries = a.get("summaries") or {}
        summary = summaries.get(lang) or summaries.get("no", "")
        articles_text += (
            f"[{a.get('source_name', '')} | {a.get('published_at', '')} | "
            f"scope={a.get('scope', '')} | tone={a.get('tone', '')} | "
            f"region={a.get('region', '')}]\n"
            f"{a.get('title', '')}\n"
            f"{summary}\n"
            "---\n"
        )

    titles = section_titles.get(lang, section_titles["no"])
    label = lang_label.get(lang, lang)

    user_prompt = (
        f"Lag en daglig oppsummering ({label}-språk) av Cermaq og bransjedekning "
        f"siste 24 timer, basert på artikkellisten under.\n\n"
        f"Strukturen skal være markdown med disse seksjonene (hopp over seksjoner "
        f"som ikke har innhold):\n\n"
        f"**Hovedoverskrift** (én setning som fanger dagens viktigste sak)\n\n"
        f"### {titles['cermaq']}\n"
        f"2-3 setninger som oppsummerer Cermaq-omtaler. "
        f"Kun hvis det finnes scope=cermaq-saker.\n\n"
        f"### {titles['norge']}\n"
        f"2-3 setninger om norsk lakseoppdrett-bransje. "
        f"Kun hvis norske bransjeartikler finnes.\n\n"
        f"### {titles['chile']}\n"
        f"2-3 setninger om chilensk lakseoppdrett. "
        f"Kun hvis chilenske artikler finnes.\n\n"
        f"### {titles['canada']}\n"
        f"2-3 setninger om kanadisk eller globalt bransjebilde. "
        f"Kun hvis relevant.\n\n"
        f"### {titles['watch']}\n"
        f"1-2 setninger om saker som kan utvikle seg og bør overvåkes nærmere.\n\n"
        f"Skriv på {label}.\n\n"
        f"Artikler ({article_count} totalt, {cermaq_count} Cermaq-saker):\n\n"
        f"{articles_text}\n"
        f"Returner JSON med to felt:\n"
        f"- headline: hovedoverskrift som streng\n"
        f"- body: hele markdown-teksten med seksjoner (uten hoved-overskrift)"
    )

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model="claude-sonnet-4-6-20250514",
            max_tokens=1000,
            temperature=0.3,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text_block = next((b for b in response.content if b.type == "text"), None)
        if text_block is None:
            raise ValueError("No text block in response")

        raw_text = text_block.text
        result = json.loads(_extract_json(raw_text))
        body_html = md.markdown(result.get("body", ""), extensions=["extra"])

        log.info("Digest generert lang=%s headline=%s", lang, result.get("headline", "")[:60])
        return {
            "headline": result.get("headline", ""),
            "body": body_html,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "lang": lang,
            "article_count": article_count,
            "cermaq_count": cermaq_count,
        }
    except Exception as exc:
        log.error("Digest-generering feilet for lang=%s: %s", lang, exc)
        return None
