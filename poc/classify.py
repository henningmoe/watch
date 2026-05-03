"""AI-based article classification using the Claude API."""

import json
import logging
import os
import re

import anthropic
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_PROMPT = (
    "Du er media-analyst for Cermaq, et globalt lakseoppdrettsselskap "
    "eid av Mitsubishi Corporation, med drift i Norge (Nordland og "
    "Finnmark), Canada (British Columbia) og Chile (Los Lagos, Aysén, "
    "Magallanes).\n\n"
    "Klassifiser denne artikkelen og returner KUN gyldig JSON, "
    "ingenting annet. Ingen markdown, ingen code blocks, bare ren JSON.\n\n"
    "REGLER FOR scope-FELTET (kritisk viktig):\n\n"
    'scope = "cermaq" KUN når én av disse er sant:\n'
    "- Artikkelen nevner ordet \"Cermaq\" eksplisitt\n"
    "- Artikkelen nevner Cermaq-merker: True Arctic, Salmon Saver, "
    "Cermaq Norway, Cermaq Canada, Cermaq Chile, Cermaq Group\n"
    "- Artikkelen nevner Cermaq-ledere ved navn: Steven Rafferty (CEO), "
    "Snorre Jonassen, Lise Bergan, David Kiemele, Knut Ellekjær, "
    "Sven Eric Ellingsen, Terje Suul\n\n"
    'scope = "industry" når artikkelen handler om:\n'
    "- Lakseoppdrett, fiskehelse, akvakultur generelt\n"
    "- Sjømat-marked, eksport, lakseprisen\n"
    "- Konkurrenter (Mowi, SalMar, Lerøy, Grieg, AquaChile, MultiX, "
    "Bakkafrost, Atlantic Sapphire)\n"
    "- Oppdrettsregulering, konsesjoner, miljøkrav, fiskehelsekrav\n"
    "- Akvakultur-teknologi, RAS-anlegg, landbasert oppdrett\n"
    "- ...uten at Cermaq nevnes direkte\n\n"
    'scope = "irrelevant" hvis artikkelen IKKE handler om sjømat, '
    "laks, oppdrett eller relaterte næringstema. Dette inkluderer:\n"
    "- Generelle nyheter, kriminalitet, terror, ulykker\n"
    "- Sport, kultur, underholdning, lokale arrangementer\n"
    "- Politikk og lokalsaker uten næringskobling til oppdrett\n"
    "- Vær, trafikk, samfunnssaker, samferdsel\n"
    "- Kraftforsyning eller industri uten direkte oppdrett-kobling\n"
    "- Tekniske eller administrative meldinger fra fylkeskommuner\n\n"
    "VIKTIG: Bare det at en artikkel kommer fra norsk kilde eller "
    "nevner et norsk geografisk område er IKKE nok for scope=cermaq. "
    "Cermaq må faktisk nevnes ved navn.\n\n"
    "REGLER FOR ANDRE FELT:\n\n"
    '- region: "norge" | "chile" | "canada" | "global"\n'
    '- tone: "positiv" | "noytral" | "negativ"\n'
    '  * positiv: artikkelen er positiv for Cermaq eller bransjen\n'
    '  * noytral: faktabasert, ingen tydelig vinkling\n'
    '  * negativ: kritisk eller negativ omtale\n'
    '- category: "regulatorisk" | "marked" | "fiskehelse" | "miljo" | '
    '"drift" | "ma" | "politikk" | "annet"\n'
    "- relevance: 1-5 hvor 5 er mest relevant for Cermaq\n"
    "  * scope=irrelevant gir alltid relevance=1\n"
    "  * scope=industry typisk relevance=2-3\n"
    "  * scope=cermaq typisk relevance=4-5\n\n"
    "Tittel: {title}\n"
    "Innhold: {content_truncated}\n"
    "Kilde: {source_name}\n\n"
    "Returner JSON med:\n"
    "- region, tone, scope, category, relevance\n"
    "- titles: dict med fire nøkler (oversett tittelen naturlig; behold egennavn "
    "som Cermaq, stedsnavn og personalnavn uforandret):\n"
    '    "no": tittel på norsk\n'
    '    "en": title in English\n'
    '    "es": título en español\n'
    '    "ja": 日本語のタイトル\n'
    "- summaries: dict med fire nøkler:\n"
    '    "no": 2 setninger på norsk\n'
    '    "en": 2 sentences in English\n'
    '    "es": 2 oraciones en español\n'
    '    "ja": 日本語の2文'
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

    _orig_title = article.get("title", "")
    default = {
        **_DEFAULT,
        "titles": {"no": _orig_title, "en": _orig_title, "es": _orig_title, "ja": _orig_title},
        "summaries": {
            "no": plain_text[:200],
            "en": plain_text[:200],
            "es": plain_text[:200],
            "ja": plain_text[:200],
        },
    }

    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1300,
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
