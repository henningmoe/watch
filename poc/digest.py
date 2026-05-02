"""Daily AI digest generation for Cermaq Watch — web-search powered."""

import json
import logging
import os
import re
from datetime import datetime, timezone

import markdown as md
from anthropic import Anthropic

log = logging.getLogger(__name__)

LANG_INSTRUCTIONS = {
    "no": "Skriv på norsk. Bruk norske begreper.",
    "en": "Write in English.",
    "es": "Escribe en español.",
    "ja": "日本語で書いてください。",
}

SECTION_TITLES = {
    "no": {
        "cermaq": "Cermaq-relaterte saker",
        "norge": "Bransjebilde Norge",
        "chile": "Bransjebilde Chile",
        "canada": "Bransjebilde Canada og globalt",
        "watch": "Hva å følge med på",
        "sources": "Kilder",
    },
    "en": {
        "cermaq": "Cermaq-related stories",
        "norge": "Industry Norway",
        "chile": "Industry Chile",
        "canada": "Industry Canada and global",
        "watch": "What to watch",
        "sources": "Sources",
    },
    "es": {
        "cermaq": "Noticias relacionadas con Cermaq",
        "norge": "Industria en Noruega",
        "chile": "Industria en Chile",
        "canada": "Industria en Canadá y global",
        "watch": "A seguir",
        "sources": "Fuentes",
    },
    "ja": {
        "cermaq": "Cermaq関連のニュース",
        "norge": "ノルウェー業界",
        "chile": "チリ業界",
        "canada": "カナダ・国際業界",
        "watch": "今後注視すべき点",
        "sources": "出典",
    },
}


def _extract_json(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0)
    raise ValueError(f"Kunne ikke finne JSON i: {text[:200]!r}")


def generate_digest(miniflux_articles: list[dict], lang: str = "no") -> dict | None:
    """Generate daily digest by letting Claude search the web actively.

    Uses miniflux_articles as context but supplements with live web_search
    calls. Returns dict with headline, body (HTML), metadata — or None on failure.
    """
    cermaq_count = sum(1 for a in miniflux_articles if a.get("scope") == "cermaq")

    miniflux_context = ""
    for a in miniflux_articles:
        summary = (a.get("summaries") or {}).get(lang) or (a.get("summaries") or {}).get("no", "")
        miniflux_context += (
            f"- [{a.get('source_name', 'Ukjent')}] {a.get('title', '')}\n"
        )
        if summary:
            miniflux_context += f"  {summary}\n"
        if a.get("url"):
            miniflux_context += f"  {a['url']}\n"
        miniflux_context += "\n"

    titles = SECTION_TITLES.get(lang, SECTION_TITLES["no"])
    lang_instr = LANG_INSTRUCTIONS.get(lang, LANG_INSTRUCTIONS["no"])

    system_prompt = (
        "Du er kommunikasjonssjef-assistent for Cermaq, et globalt "
        "lakseoppdrettsselskap eid av Mitsubishi Corporation, med drift "
        "i Norge (Nordland og Finnmark), Canada (British Columbia) og "
        "Chile (Los Lagos, Aysén, Magallanes).\n\n"
        "Du har tilgang til web_search-verktøyet og MÅ bruke det aktivt "
        "for å finne ferske Cermaq-relaterte saker fra hele nettet.\n\n"
        "SØKESTRATEGI — bruk web_search 4-6 ganger med varierte søk:\n"
        '- "Cermaq" (siste Cermaq-spesifikke nyheter)\n'
        '- "Cermaq Norway" eller "Cermaq Norge"\n'
        '- "Cermaq Chile"\n'
        '- "Cermaq Canada"\n'
        '- "Steven Rafferty Cermaq" (CEO-relaterte uttalelser)\n'
        '- "True Arctic Cermaq" (merkenavn)\n'
        "Eventuelt bredere bransjesøk hvis Cermaq-saker er få:\n"
        '- "norwegian salmon farming news"\n'
        '- "chilean salmon industry"\n'
        '- "salmon price Norway"\n\n'
        "VIKTIG: Bruk web_search MAKS 8 ganger totalt. "
        "Prioriter Cermaq-spesifikke søk først.\n\n"
        "OUTPUT-FORMAT:\n"
        "Returner KUN gyldig JSON, ingen markdown-wrapping, ingen kode-blokker.\n\n"
        '{{\n'
        '  "headline": "Én setning som fanger dagens viktigste sak",\n'
        '  "body": "Markdown-tekst med seksjoner",\n'
        '  "sources": ["url1", "url2"]\n'
        "}}\n\n"
        f"I \"body\", strukturer slik (hopp over tomme seksjoner):\n\n"
        f"### {titles['cermaq']}\n"
        "2-4 setninger om Cermaq-spesifikke saker. "
        "Inkluder lenker som [tittel](url) der det er naturlig.\n\n"
        f"### {titles['norge']}\n"
        "2-3 setninger om norsk lakseoppdrett-bransje.\n\n"
        f"### {titles['chile']}\n"
        "2-3 setninger om chilensk lakseoppdrett.\n\n"
        f"### {titles['canada']}\n"
        "2 setninger om kanadisk eller globalt bransjebilde.\n\n"
        f"### {titles['watch']}\n"
        "1-2 setninger om saker som bør overvåkes nærmere fremover.\n\n"
        'I "sources"-listen, samle alle URL-er du faktisk har basert '
        "påstander på (web_search og Miniflux).\n\n"
        f"{lang_instr}\n\n"
        "TONE: Kortfattet, profesjonelt, analytisk. En mediekonsulent "
        "som rapporterer til ledergruppen — ikke salgsorientert."
    )

    user_prompt = (
        f"Lag en daglig medie-briefing om Cermaq og lakseoppdrett-bransjen "
        f"basert på siste 24 timers nyheter.\n\n"
        f"Bruk web_search aktivt for å finne ferske Cermaq-saker.\n\n"
        f"Miniflux-artikler som kontekst "
        f"({len(miniflux_articles)} totalt, {cermaq_count} merket som Cermaq-spesifikke):\n\n"
        f"{miniflux_context}"
    )

    # Estimated cost per call: ~6 searches × $0.01 + ~5k tokens × $3/M + ~800 tokens × $15/M
    # ≈ $0.06 + $0.015 + $0.012 = ~$0.087 per language, ~$0.35 for all four
    log.info("Estimert digest-kostnad per språk: ~$0.09 (~0.85 kr)")

    try:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model="claude-sonnet-4-6-20250514",
            max_tokens=3000,
            temperature=0.3,
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 8,
                }
            ],
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        search_count = sum(
            1 for block in response.content
            if getattr(block, "type", "") == "server_tool_use"
            and getattr(block, "name", "") == "web_search"
        )
        log.info("Digest lang=%s brukte web_search %d ganger", lang, search_count)

        text_parts = [
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        ]
        full_text = "\n".join(text_parts)

        result = json.loads(_extract_json(full_text))

        body_html = md.markdown(result.get("body", ""), extensions=["nl2br"])

        sources = result.get("sources") or []
        if sources:
            items = "".join(
                f'<li><a href="{url}" target="_blank" rel="noopener">{url}</a></li>'
                for url in sources[:15]
            )
            body_html += f"<h3>{titles['sources']}</h3><ul>{items}</ul>"

        log.info("Digest klar lang=%s: %s", lang, result.get("headline", "")[:60])
        return {
            "headline": result.get("headline", ""),
            "body": body_html,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "lang": lang,
            "article_count": len(miniflux_articles),
            "cermaq_count": cermaq_count,
            "search_count": search_count,
            "sources": sources,
        }

    except Exception as exc:
        log.error("Digest-generering feilet for lang=%s: %s", lang, exc, exc_info=True)
        return None
