"""AI-based article classification using the Claude API."""

import json
import logging
import os
import re

import anthropic
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_PROMPT = (
    "Du er en klassifiserer for Cermaq Watch — en medie-overvåkningsløsning "
    "for Cermaq, en global lakseoppdretter med virksomhet i Norge, Chile og "
    "Canada.\n\n"
    "Din oppgave er å klassifisere én artikkel langs seks akser:\n\n"
    "1. SCOPE — hvor relevant for Cermaq?\n"
    "   - 'cermaq': Artikkelen omtaler Cermaq direkte, eller eier (Mitsubishi\n"
    "     Corporation), datterselskap (True Arctic), eller nøkkelpersoner\n"
    "     (Steven Rafferty, Snorre Jonassen, Lise Bergan, David Kiemele,\n"
    "     Knut Ellekjær, Sven Eric Ellingsen, Terje Suul)\n"
    "   - 'industry': Artikkelen omtaler lakseoppdrettsbransjen, konkurrenter\n"
    "     (Mowi, SalMar, Grieg, Bakkafrost, Lerøy), regulering, marked,\n"
    "     teknologi eller andre temaer som er relevante for Cermaq selv om\n"
    "     Cermaq ikke nevnes\n"
    "   - 'irrelevant': Artikkelen handler om noe annet\n\n"
    "2. REGION — geografisk kontekst\n"
    "   - 'norge', 'chile', 'canada', 'global'\n\n"
    "3. TONE — sentiment for Cermaq eller bransjen\n"
    "   - 'positiv', 'noytral', 'negativ'\n\n"
    "4. CATEGORY — saksområde (én av disse)\n"
    "   - 'regulatorisk', 'marked', 'fiskehelse', 'miljo', 'drift', 'ma',\n"
    "     'politikk', 'annet'\n\n"
    "5. THEMES — tematiske underkategorier (velg 0-3 temaer)\n"
    "   - 'Sjø': Sjøbasert oppdrett — merder, lokaliteter, brønnbåter,\n"
    "     slakteri, havbasert oppdrett, eksponert havbruk, offshore-anlegg\n"
    "   - 'Landbasert': Land-baserte oppdrettsanlegg (RAS, gjennomstrømnings-\n"
    "     anlegg), settefiskanlegg, smolt-produksjon på land, post-smolt\n"
    "   - 'Fôr': Fôrproduksjon, fôrråvarer (soya, fiskemel, fiskeolje,\n"
    "     alternative proteiner), FCR, Cargill, BioMar, Skretting\n"
    "   - 'Fiskehelse': Sykdom (PD, ILA, AGD), lus, parasitter, vaksiner,\n"
    "     dødelighet, dyrevelferd, behandling, rensefisk, lusetelling\n"
    "   - 'Teknologi': Innovasjon i utstyr, undervannskamera, sensorer, AI/ML,\n"
    "     automatisering på anlegg, lasernoder, undervannsroboter\n"
    "   - 'Digitalisering': IT-systemer, dataplattformer, programvare, ERP,\n"
    "     digital transformasjon på selskapsnivå, IoT, cybersecurity, AI-strategi\n"
    "   - 'Finans': Finansielle resultater, kvartalsrapporter, børsmeldinger,\n"
    "     aksjekurs, M&A, investeringer, utbytte, valuta, råvarepriser\n\n"
    "   VIKTIG om themes:\n"
    "   - 'themes' er et array med 0-3 verdier (maks 3)\n"
    "   - Velg KUN tema som faktisk er hovedfokus i artikkelen\n"
    "   - Irrelevante artikler får alltid themes: []\n\n"
    "6. RELEVANCE — relevans-score 1-5\n"
    "   - 5: Direkte og viktig Cermaq-sak\n"
    "   - 4: Cermaq-omtale eller veldig relevant bransjenyhet\n"
    "   - 3: Relevant bransjenyhet uten Cermaq\n"
    "   - 2: Tangerer bransjen\n"
    "   - 1: Marginal relevans\n"
    "   - scope=irrelevant gir alltid relevance=1\n\n"
    "OUTPUT-FORMAT:\n"
    "Returner KUN gyldig JSON. Start med {{ og slutt med }}. Ingen prosa\n"
    "eller markdown.\n\n"
    "{{\n"
    '  "scope": "cermaq" | "industry" | "irrelevant",\n'
    '  "region": "norge" | "chile" | "canada" | "global",\n'
    '  "tone": "positiv" | "noytral" | "negativ",\n'
    '  "category": "regulatorisk" | "marked" | "fiskehelse" | "miljo" | "drift" | "ma" | "politikk" | "annet",\n'
    '  "themes": [],\n'
    '  "relevance": 1-5,\n'
    '  "titles": {{"no": "...", "en": "...", "es": "...", "ja": "..."}},\n'
    '  "summaries": {{"no": "2 setninger", "en": "2 sentences", "es": "2 oraciones", "ja": "2文"}}\n'
    "}}\n\n"
    "Tittel: {title}\n"
    "Innhold: {content_truncated}\n"
    "Kilde: {source_name}"
)

_DEFAULT = {
    "region": "global",
    "tone": "noytral",
    "scope": "industry",
    "category": "annet",
    "themes": [],
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

        # Ensure themes is a list of valid strings
        themes = result.get("themes")
        if not isinstance(themes, list):
            themes = []
        valid_themes = {"Sjø", "Landbasert", "Fôr", "Fiskehelse", "Teknologi", "Digitalisering", "Finans"}
        result["themes"] = [t for t in themes if t in valid_themes][:3]

        log.info(
            "Klassifiserte artikkel-id=%s: scope=%s, region=%s, tone=%s, themes=%s",
            article.get("id"),
            result.get("scope"),
            result.get("region"),
            result.get("tone"),
            result.get("themes"),
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
