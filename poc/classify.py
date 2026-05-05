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
    "   - 'Sjø': Sjøbasert oppdrett, åpne merder, sjølokaliteter, lokasjoner\n"
    "     i sjø, lakseslipp, rømming fra sjøanlegg, lus i sjø, miljøforhold sjø,\n"
    "     biomasse i sjø, slakting, brønnbåter, alle aspekter av oppdrett som\n"
    "     foregår i sjø\n"
    "   - 'Landbasert': Landbasert oppdrett, RAS-anlegg, smoltanlegg,\n"
    "     settefiskproduksjon, postsmolt-anlegg på land, hatcheries, klekkerier\n"
    "   - 'Fôr': Fôrproduksjon, fôringredienser, soya, fiskemel, fiskeolje,\n"
    "     bærekraftig fôr, fôrutnyttelse, FCR (fôrfaktor), nye fôringredienser,\n"
    "     fôrleverandører (Skretting, Cargill EWOS, BioMar)\n"
    "   - 'Fiskehelse': Fiskesykdommer, lakselus (som biologisk problem), ILA,\n"
    "     PD, AGD, vaksiner, fiskevelferd, dødelighet, sykdomsutbrudd,\n"
    "     veterinærarbeid, smoltkvalitet\n"
    "   - 'Teknologi': Innovasjon innen oppdrettsutstyr OG produksjonskonsepter:\n"
    "     * Fysisk utstyr: undervannskamera, sensorer, AI-drevet utstyr\n"
    "       (Stingray-laser, fôringsystemer), undervannsroboter, droner,\n"
    "       sonar, foringbåter\n"
    "     * Oppdrettskonsepter: RAS-teknologi, semilukkede anlegg, lukkede\n"
    "       flytende anlegg (Egget-konseptet), hybrid-anlegg, offshore-anlegg,\n"
    "       postsmolt-teknologi, nye merdkonsepter\n"
    "     * Tekniske leverandører til bransjen: AKVA Group, ScaleAQ, Steinsvik\n"
    "   - 'Digitalisering': AI/ML-algoritmer, software, data-plattformer:\n"
    "     * AI/ML for bildegjenkjenning, prediktiv analyse, optimalisering\n"
    "     * Digitale plattformer, ERP-systemer, CRM, dashboards\n"
    "     * Big data, IoT-datastrømmer, datavarehus\n"
    "     * Automatisering av beslutningsprosesser\n"
    "     * Digital sporbarhet, blockchain\n"
    "     * AI-assistenter, ChatGPT-bruk i bransjen\n"
    "     SKILLE fra Teknologi: Digitalisering = software/algoritmer/data.\n"
    "     Teknologi = fysisk utstyr og oppdrettskonsepter.\n"
    "     AI-systemer er typisk DIGITALISERING (selv om de bruker fysisk utstyr\n"
    "     som kameraer). Hvis hovedfokus er det fysiske utstyret, kan Teknologi\n"
    "     også inkluderes.\n"
    "   - 'Finans': Selskapsfinans og markedspriser (input-side):\n"
    "     * Kvartalsrapporter, årsrapporter, EBITDA, omsetning, resultat\n"
    "     * Aksjekurs, M&A, oppkjøp, fusjoner, utbytte\n"
    "     * Lakseprisen (spot, Fish Pool Index, NASDAQ Salmon Index)\n"
    "     * Valutakurser, råvarepriser (soya, fiskemel - input til fôr)\n"
    "     * Investeringer, capex-beslutninger, finansiering\n"
    "   - 'Marked': Salg, markedsføring og forbrukerdynamikk (output-side):\n"
    "     * Eksport-volum og -tall (Norge eksporterte X tonn)\n"
    "     * Retail-priser, kampanjer i butikkkjeder\n"
    "     * Konkurrentkampanjer (Mowi-merker, SalMar Aurora)\n"
    "     * Varemerker (True Arctic, premium-segmenter)\n"
    "     * Forbruker-trender, kategori-utvikling (sushi-grade, økologisk)\n"
    "     * Foodservice, restaurantbransjen\n"
    "     * ASC, MSC, GlobalGAP-sertifiseringer\n"
    "     * B2B-avtaler, distribusjonskanaler\n"
    "     * Markedsføring i nye land/markeder\n\n"
    "   EKSEMPLER PÅ GOD KLASSIFISERING:\n"
    "   'Mitsubishi reclassifies Cermaq after capital review' → themes: ['Finans']\n"
    "   'Hevder flytende lukket postsmolt gir kostnadsfordel mot RAS' → ['Teknologi','Sjø','Landbasert']\n"
    "   'KI kan gjere det lettare å fange pukkellaks i elvane' → ['Digitalisering']\n"
    "   'Norge eksporterte rekordmengde laks til Japan i april' → ['Marked']\n"
    "   'True Arctic vinner pris under Sushi Award' → ['Marked']\n"
    "   'Cermaq Chile får ASC-sertifisering på alle anlegg' → ['Marked','Sjø']\n"
    "   'Stingray-laser vist effektiv mot lakselus' → ['Teknologi','Fiskehelse']\n"
    "   'AI-drevet fôringsystem reduserer fôrfaktor 8%' → ['Digitalisering','Fôr']\n\n"
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
        valid_themes = {"Sjø", "Landbasert", "Fôr", "Fiskehelse", "Teknologi", "Digitalisering", "Finans", "Marked"}
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
