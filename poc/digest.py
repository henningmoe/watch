"""AI digest generation for Cermaq Watch — daily and weekly, web-search powered."""

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

REGION_CONTEXT: dict = {
    None: {
        "focus_label": "Globalt nyhetsbilde",
        "focus_instruction": (
            "Gi et balansert totalbilde av nyhetsbildet. "
            "Vekt artikler etter viktighet, ikke region."
        ),
        "key_topics": "",
        "relevant_events": [
            "Sjømatdagene Trondheim", "Hav Expo", "Aqua Nor",
            "Nor-Fishing", "Seafood Expo Global Barcelona",
            "Seafood Expo North America Boston",
            "Salmon Chile", "AquaSur Chile",
            "World Seafood Congress", "TEKMAR",
            "China Fisheries Seafood Expo",
            "Cermaq generalforsamling",
            "Mitsubishi resultatpresentasjon",
        ],
    },
    "norge": {
        "focus_label": "Norske forhold",
        "focus_instruction": (
            "Fokuser på norske forhold og vekt artikler fra norske kilder "
            "(E24, iLaks, Kyst.no, NRK, DN, Intrafish, LandbasedAQ, "
            "Fish Farming Expert) tyngst. Norske bransjespørsmål er "
            "hovedfokus."
        ),
        "key_topics": (
            "Norske fokusområder: trafikklys-system, Mattilsynet, "
            "Statsforvalteren, fjordlokaliteter, lakselus, MTB-tildeling, "
            "rømming, nye konsesjoner, postsmolt, Sjømatrådet."
        ),
        "relevant_events": [
            "Sjømatdagene Trondheim", "Hav Expo", "Aqua Nor",
            "Nor-Fishing", "TEKMAR", "North Atlantic Seafood Forum",
            "LandbasedAQ Konferansen",
            "Seafood Expo Global Barcelona",
            "Cermaq generalforsamling",
        ],
    },
    "chile": {
        "focus_label": "Chilenske forhold",
        "focus_instruction": (
            "Fokuser på chilenske forhold og vekt artikler fra chilenske "
            "kilder (Mundo Acuícola, AQUA.cl, Salmonexpert, Pulso, "
            "Diario Financiero) tyngst. Chilenske bransjespørsmål er "
            "hovedfokus."
        ),
        "key_topics": (
            "Chilenske fokusområder: SAG, Sernapesca, sykdomshåndtering "
            "(SRS, ISA), CORFO-konsesjoner, Magallanes-regionen, Aysén, "
            "Los Lagos, Patagonia, eksport til USA og Brasil, "
            "aquaculture law-revisjon."
        ),
        "relevant_events": [
            "Salmon Chile", "AquaSur Chile",
            "Seafood Expo Global Barcelona",
            "Seafood Expo North America Boston",
            "Cermaq generalforsamling",
        ],
    },
    "canada": {
        "focus_label": "Kanadiske forhold",
        "focus_instruction": (
            "Fokuser på kanadiske forhold og vekt artikler fra "
            "kanadiske/internasjonale kilder (SeaWestNews, IntraFish, "
            "CBC, Globe and Mail, Vancouver Sun, BC Salmon Farmers Assoc) "
            "tyngst. Spesielt vektlegge regulatoriske saker."
        ),
        "key_topics": (
            "Kanadiske fokusområder: First Nations-relasjoner, "
            "BC-regjeringens net-pen-policy, DFO-regulering, "
            "transition plan for åpne merder, Vancouver Island, "
            "Newfoundland og Labrador, ASF (Atlantic Salmon Federation), "
            "CFIA-godkjenninger."
        ),
        "relevant_events": [
            "Seafood Expo North America Boston",
            "Seafood Expo Global Barcelona",
            "Cermaq generalforsamling",
        ],
    },
    "global": {
        "focus_label": "Internasjonale saker",
        "focus_instruction": (
            "Fokuser på internasjonale saker, M&A i bransjen, "
            "globale markedsbevegelser, Mitsubishi-relaterte nyheter, "
            "EU- og USA-regulatorisk, eksport-data internasjonalt. "
            "Lokale saker fra Norge/Chile/Canada nedprioriteres."
        ),
        "key_topics": (
            "Globale fokusområder: Mitsubishi sjømat-segmentet, "
            "oppkjøp og fusjoner, internasjonale lakseprisene "
            "(Fish Pool, Nasdaq Salmon Index, Urner Barry), "
            "eksport-data globalt, EU-regulatorisk, USA-import, "
            "Asia-marked, Russland-saker, AI/teknologi-trender."
        ),
        "relevant_events": [
            "Seafood Expo Global Barcelona",
            "Seafood Expo North America Boston",
            "World Seafood Congress",
            "China Fisheries Seafood Expo",
            "Cermaq generalforsamling",
            "Mitsubishi resultatpresentasjon",
        ],
    },
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
    if not text:
        raise ValueError("Tom respons")

    text_stripped = text.strip()
    if text_stripped.startswith("{") and text_stripped.endswith("}"):
        return text_stripped

    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if match:
        return match.group(1)

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        return text[first_brace : last_brace + 1]

    raise ValueError(f"Kunne ikke finne JSON i: {text[:200]!r}")


def parse_digest_json(raw_text: str) -> dict:
    """Robust JSON-parsing with fallback strategies."""
    text = raw_text.strip()

    # Strip markdown fences
    if text.startswith("```"):
        parts = text.split("```", 2)
        if len(parts) >= 2:
            inner = parts[1]
            if inner.startswith("json"):
                inner = inner[4:]
            text = inner.strip()

    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Extract outermost {...}
    first = text.find("{")
    last = text.rfind("}")
    if first != -1 and last > first:
        block = text[first : last + 1]
        try:
            return json.loads(block)
        except json.JSONDecodeError:
            pass
        # Remove trailing commas
        cleaned = re.sub(r",(\s*[}\]])", r"\1", block)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

    raise ValueError(f"Klarte ikke parse digest-JSON. Første 500: {text[:500]!r}")


_EVENTS_CONTEXT = """
KJENTE BRANSJE-EVENTS (løft frem hvis 3+ artikler refererer til dem):
- Sjømatdagene Trondheim (januar)
- North Atlantic Seafood Forum / Bergen (mars)
- Hav Expo (april)
- Aqua Nor (august, annet hvert år)
- Nor-Fishing (august, annet hvert år)
- LandbasedAQ Konferansen
- TEKMAR (desember, Trondheim)
- Seafood Expo Global / Barcelona (april/mai)
- Seafood Expo North America / Boston (mars)
- Aquaculture America (februar/mars)
- Salmon Chile (mars/april)
- AquaSur Chile (oktober, annet hvert år)
- World Seafood Congress
- China Fisheries & Seafood Expo (oktober/november)
- Cermaq generalforsamling
- Mitsubishi resultatpresentasjoner
""".strip()


def generate_norwegian_master_digest(articles: list[dict]) -> dict | None:
    """Generate structured Norwegian digest with per-region sections."""
    article_context = ""
    for a in articles[:80]:
        title = a.get("title") or ""
        summary = (a.get("summaries") or {}).get("no", "")[:300]
        themes = ",".join(a.get("themes") or [])
        meta = f"[{a.get('scope')}|{a.get('region')}|{a.get('tone')}|{themes}]"
        src = a.get("source_name") or a.get("source_domain") or ""
        pub = a.get("published_at") or a.get("fetched_at") or ""
        if pub and len(pub) >= 10:
            pub = pub[:16].replace("T", " ")  # "2026-05-10 08:30"
        article_context += (
            f"\n{meta} [{pub}] {title}\n"
            f"{summary}\n"
            f"  URL: {a.get('url', '')}\n"
            f"  Kilde: {src}\n"
        )

    system_prompt = f"""Du skriver daglig medieoppsummering for \
kommunikasjonsteamet i Cermaq, som distribuerer den internt til \
ledelse og ansatte. Cermaq er global laksoppdretter (Norge, Chile, \
Canada) eid av Mitsubishi.

OPPGAVE: Lag en KORT, fokusert medieoppsummering basert på artikler \
fra SISTE 24 TIMER. Bruk web_search 1-2 ganger for ferske Cermaq-saker \
som ikke er i Miniflux-feeden.

KRITISK TIDSKRAV:
- Kun nyheter fra siste 24 timer
- Eldre saker IGNORERES selv om de er i artikkellisten
- Sjekk publiseringsdato [i hakeparantes] før du tar med en sak
- Hvis publiseringsdato mangler, kan saken inkluderes

VINKLING:
Skriv som om kommunikasjonsteamet kort orienterer kollegaer om \
mediebildet. Tone:
- "Cermaq omtales i dag i forbindelse med..."
- "Internasjonalt rapporterer..."
- "I Norge er det fokus på..."
Ikke skriv som journalist, men som intern formidler.

STRUKTUR — fire seksjoner:
1. "global" — Hovedseksjon. Cermaq-konsernet, Mitsubishi/M&A, \
internasjonale markedsbevegelser, store region-saker som er \
bemerkelsesverdige globalt.
2. "norge" — KUN hvis 2+ norske artikler fra siste 24t. \
Norske forhold: regulatorisk, lakselus, MTB, fjordlokaliteter. \
RETURNER null hvis < 2 norske artikler.
3. "chile" — KUN hvis 2+ chilenske artikler fra siste 24t. \
Chilenske forhold: SAG, Sernapesca, sykdom, eksport, regioner. \
RETURNER null hvis < 2 chilenske artikler.
4. "canada" — KUN hvis 2+ kanadiske artikler fra siste 24t. \
First Nations, BC net-pen-policy, DFO-regulering. \
RETURNER null hvis < 2 kanadiske artikler.

VIKTIG: IKKE fyll tomme seksjoner med tekst om at "ingen nyheter \
i dag". Returner bokstavelig talt JSON null.

PRIORITERING I "global":
1. NEGATIVE Cermaq-saker FØRST (utslipp, ulykker, regulatorisk kritikk, \
sykdom, miljøproblemer)
2. Positive/nøytrale Cermaq-saker
3. Mitsubishi/M&A
4. Generelle bransje-bevegelser (kort)

EVENT-LØFTING:
Hvis 3+ artikler fra siste 24t refererer til en av disse eventene, \
gi det ekstra omtale i relevant seksjon:
Sjømatdagene Trondheim, North Atlantic Seafood Forum, Hav Expo, \
Aqua Nor, Nor-Fishing, TEKMAR, Seafood Expo Global Barcelona, \
Seafood Expo North America Boston, Aquaculture America, Salmon Chile, \
AquaSur Chile, World Seafood Congress, China Fisheries Seafood Expo, \
Cermaq generalforsamling, Mitsubishi resultatpresentasjoner.

KILDE-ATTRIBUSJON:
- Etter HVER statement: kilde i klammer, eks. (E24), (iLaks)
- Bruk kildens HOVEDNAVN (iLaks, ikke ilaks.no)
- IKKE URL i body — kun i sources-array
- Flere kilder for samme statement: (E24, iLaks)

LENGDE OG TONE — VIKTIG:
- "global"-seksjon: 3-7 setninger
- Region-seksjoner: 3-6 setninger HVIS de har innhold (ellers null)
- Hold det KORT og presist
- Flytende prosa, ingen markdown, ingen lister
- Ikke gjenta samme info i flere seksjoner

OUTPUT — KUN gyldig JSON, ingen markdown-fences, ingen forklaring:
{{
  "headline": "Én setning som fanger dagens hovedtema",
  "sections": {{
    "global": "3-7 setninger. Negative Cermaq-saker først. Kilder i klammer.",
    "norge": "3-6 setninger ELLER null",
    "chile": "3-6 setninger ELLER null",
    "canada": "3-6 setninger ELLER null"
  }},
  "sources": [
    {{"name": "E24", "url": "https://e24.no/..."}},
    {{"name": "iLaks", "url": "https://ilaks.no/..."}}
  ],
  "events_mentioned": []
}}

EKSEMPEL på god "global"-seksjon:
"Cermaq omtales i dag i forbindelse med dieselutslippet ved \
Alta-fjorden, der Statsforvalteren ga selskapet alvorlig refs (E24). \
Internasjonalt rapporterer Mitsubishi om mer enn doblet fortjeneste \
fra Cermaq-segmentet til 180 millioner dollar i FY2025 (Reuters). \
Markedet preges av fallende laksepriser, med spotpris ned til \
EUR 6,96 per kilo (Kyst.no)."

EKSEMPEL på god "norge"-seksjon:
"Norske myndigheter strammer inn på lakselus-rapportering, og \
Mattilsynet har varslet utvidet tilsynsregime fra juni (iLaks). \
Cermaq vurderer nytt storslakteri i Finnmark etter Grieg-oppkjøpet, \
en beslutning som påvirker rundt 600 ansatte (E24, iLaks)."

ARTIKLER ({len(articles)} totalt, filtrert til siste 24 timer):
{article_context}"""

    user_prompt = (
        "Lag daglig medieoppsummering for Cermaq basert på artiklene over. "
        "Bruk web_search 1-2 ganger for ferske saker som ikke er i Miniflux. "
        "Hold det kort — maks 3-7 setninger per seksjon."
    )

    log.info("Genererer norsk master-digest for %d artikler", len(articles))
    try:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=6000,
            temperature=0.3,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        search_count = sum(
            1 for b in response.content
            if getattr(b, "type", "") == "server_tool_use"
            and getattr(b, "name", "") == "web_search"
        )
        raw_text = "".join(
            b.text for b in response.content if getattr(b, "type", "") == "text"
        )
        log.info(
            "Norsk digest AI-respons: %d tegn, web_search brukt %d ganger",
            len(raw_text), search_count,
        )

        content = parse_digest_json(raw_text.strip())

        # Normalise sources to list[{name, url}]
        raw_src = content.get("sources") or []
        sources = []
        for s in raw_src:
            if isinstance(s, dict):
                sources.append(s)
            elif isinstance(s, str) and s:
                sources.append({"name": s, "url": s})
        content["sources"] = sources
        content["search_count"] = search_count
        content["article_count"] = len(articles)
        content["generated_at"] = datetime.now(timezone.utc).isoformat()

        log.info("Norsk digest klar: %s", str(content.get("headline", ""))[:80])
        return content

    except Exception as exc:
        log.error("Norsk digest generering feilet: %s", exc, exc_info=True)
        return None


def translate_digest(norsk_content: dict, target_lang: str) -> dict | None:
    """Translate a Norwegian digest dict to target_lang (en, es, ja)."""
    lang_name = {"en": "English", "es": "Spanish", "ja": "Japanese"}.get(target_lang)
    if not lang_name:
        return None

    system_prompt = f"""You are a professional translator specializing in \
salmon aquaculture industry terminology.

TASK: Translate the following JSON document from Norwegian to {lang_name}.

CRITICAL RULES:
1. Translate ONLY: headline, sections.global, sections.norge, \
sections.chile, sections.canada
2. Keep source attributions in parentheses unchanged: (E24), (iLaks), (Kyst.no)
3. Keep section keys unchanged: global, norge, chile, canada
4. Keep "sources" array URLs unchanged; keep source "name" fields unchanged
5. Keep "events_mentioned" array; translate event names only if a common \
translation exists, otherwise keep Norwegian original
6. Preserve exact JSON structure and null values
7. Return ONLY valid JSON, no markdown fences, no explanation

INDUSTRY HINTS:
- lakseoppdrett → salmon farming / acuicultura de salmón / サケ養殖
- lakselus → salmon lice / piojos del salmón / サケジラミ
- rømming → escapes / escapes / 脱走
- Mattilsynet → Norwegian Food Safety Authority / Autoridad Noruega de Seguridad Alimentaria / ノルウェー食品安全機関
- Statsforvalteren → County Governor / Gobernador Regional / 県知事
- Sernapesca, SAG, DFO → keep as-is in all languages

{"Use proper Japanese punctuation (、and。) and write naturally for a business audience." if target_lang == "ja" else ""}

Return the translated JSON object."""

    user_prompt = (
        f"Translate this JSON to {lang_name}:\n\n"
        + json.dumps(norsk_content, ensure_ascii=False, indent=2)
    )

    log.info("Oversetter digest til %s", target_lang)
    try:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=6000,
            temperature=0.2,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        raw_text = "".join(
            b.text for b in response.content if getattr(b, "type", "") == "text"
        )
        translated = parse_digest_json(raw_text.strip())

        # Preserve metadata from original
        translated["generated_at"] = norsk_content.get("generated_at")
        translated["article_count"] = norsk_content.get("article_count", 0)
        translated["search_count"] = norsk_content.get("search_count", 0)

        log.info("Oversettelse til %s ferdig", target_lang)
        return translated

    except Exception as exc:
        log.error("Oversettelse til %s feilet: %s", target_lang, exc, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Legacy daily digest (kept for reference — new code uses generate_norwegian_master_digest)
# ---------------------------------------------------------------------------

def generate_digest(
    articles: list[dict],
    lang: str = "no",
    region: str | None = None,
) -> dict | None:
    """Generate daily digest focused on last 24 hours, optionally region-specific.

    Uses 2-3 web searches. Returns plain-text body (not markdown).
    region: None = all, 'norge', 'chile', 'canada', 'global'
    """
    cermaq_count = sum(1 for a in articles if a.get("scope") == "cermaq")

    article_context = ""
    for a in articles[:40]:
        summary = (a.get("summaries") or {}).get(lang) or (a.get("summaries") or {}).get("no", "")
        article_context += f"- [{a.get('source_name', 'Ukjent')}] {a.get('title', '')}\n"
        if summary:
            article_context += f"  {summary}\n"
        if a.get("url"):
            article_context += f"  {a['url']}\n"
        article_context += "\n"

    ctx = REGION_CONTEXT.get(region, REGION_CONTEXT[None])
    lang_instr = LANG_INSTRUCTIONS.get(lang, LANG_INSTRUCTIONS["no"])
    events_list = ", ".join(ctx["relevant_events"])

    system_prompt = f"""Du er en kommunikasjonsassistent for Cermaq, en \
global laksoppdretter (Norge, Chile, Canada) eid av Mitsubishi.

OPPGAVE: Lag en daglig medieoppdatering basert på artikler fra siste \
24 timer. Bruk web_search 2-3 ganger for å finne ferske Cermaq-saker \
som ikke er i Miniflux-feeden.

REGIONSFOKUS: {ctx['focus_label']}
{ctx['focus_instruction']}
{ctx['key_topics']}

PRIORITERING I BODY (rekkefølge er viktig):
1. NEGATIVE Cermaq-saker FØRST. Dette inkluderer: utslipp, ulykker, \
regulatorisk kritikk, misnøye, tap, avvik, fiskedød, sykdom, \
miljøproblemer.
2. Deretter Cermaq-spesifikke saker (positive eller nøytrale)
3. Deretter relevante bransjesaker
4. Til slutt eventuelle markedsdata (laksepris, valuta) hvis relevant

EVENT-LØFTING:
Hvis 3 eller flere artikler refererer til en bransje-event/messe/\
konferanse, gi den ekstra omtale i body.
Relevante events for denne regionen: {events_list}.
Hvis et event ikke er i listen men flere artikler nevner det, løft \
det også frem.

KILDE-ATTRIBUSJON:
- Etter HVER statement, oppgi kilde i klammer rett etter setningen
- Format: (Kildenavn). Eksempler: (E24), (iLaks), (Kyst.no), \
(Mundo Acuícola), (SeaWestNews)
- Bruk kildens HOVEDNAVN, ikke domene. (iLaks ikke ilaks.no)
- IKKE inkluder URL i body — kun i sources-array
- Hvis flere artikler underbygger samme statement: (E24, iLaks)
- Hvis fakta kommer fra web_search, oppgi navnet på kilden

LENGDE OG TONE:
- Body skal være 4-8 setninger som dekker hovedpunktene
- Direkte, konsist, profesjonelt språk
- Ingen markdown, ingen overskrifter inni body, ingen punktlister
- Skriv flytende prosa

OUTPUT-FORMAT:
Returner KUN gyldig JSON. Start med {{ og slutt med }}.
Ingen markdown-fences, ingen forklaring før eller etter.

{{
  "headline": "Én setning som fanger dagens hovedtema (uten kilde-tag)",
  "body": "4-8 setninger flytende prosa med kilde-tagger i klammer. Negative Cermaq-saker først.",
  "sources": [
    {{"name": "E24", "url": "https://e24.no/..."}},
    {{"name": "iLaks", "url": "https://ilaks.no/..."}}
  ],
  "events_mentioned": []
}}

events_mentioned: Array med navn på eventer løftet frem i body. \
Tom array [] hvis ingen.

{lang_instr}"""

    user_prompt = (
        f"Lag en daglig medieoppdatering for Cermaq, siste 24 timer. "
        f"Region: {ctx['focus_label']}.\n\n"
        f"Bruk web_search 2-3 ganger for ferske saker som ikke er i Miniflux.\n\n"
        f"Miniflux-artikler ({len(articles)} totalt, {cermaq_count} Cermaq-spesifikke):\n\n"
        f"{article_context}"
    )

    log.info("Genererer daglig digest lang=%s region=%s", lang, region or "all")

    try:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            temperature=0.3,
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 3,
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
        log.info("Daglig digest lang=%s region=%s brukte web_search %d ganger",
                 lang, region or "all", search_count)

        text_parts = [
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        ]
        full_text = "\n".join(text_parts)

        result = json.loads(_extract_json(full_text))

        # sources can be list of dicts or list of strings — normalise to list of dicts
        raw_sources = result.get("sources") or []
        sources = []
        for s in raw_sources:
            if isinstance(s, dict):
                sources.append(s)
            elif isinstance(s, str) and s:
                sources.append({"name": s, "url": s})

        log.info("Daglig digest klar lang=%s region=%s: %s",
                 lang, region or "all", result.get("headline", "")[:60])
        return {
            "headline": result.get("headline", ""),
            "body": result.get("body", ""),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "lang": lang,
            "region": region,
            "article_count": len(articles),
            "cermaq_count": cermaq_count,
            "search_count": search_count,
            "sources": sources,
            "events_mentioned": result.get("events_mentioned") or [],
        }

    except Exception as exc:
        log.error("Daglig digest feilet lang=%s region=%s: %s",
                  lang, region or "all", exc, exc_info=True)
        return None


def generate_weekly_digest(miniflux_articles: list[dict], lang: str = "no") -> dict | None:
    """Generate full weekly digest with markdown sections, 4-8 web searches."""
    cermaq_count = sum(1 for a in miniflux_articles if a.get("scope") == "cermaq")

    miniflux_context = ""
    for a in miniflux_articles:
        summary = (a.get("summaries") or {}).get(lang) or (a.get("summaries") or {}).get("no", "")
        miniflux_context += f"- [{a.get('source_name', 'Ukjent')}] {a.get('title', '')}\n"
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
        "SØKESTRATEGI — bruk web_search 4-8 ganger med varierte søk:\n"
        '- "Cermaq" (siste Cermaq-spesifikke nyheter)\n'
        '- "Cermaq Norway" eller "Cermaq Norge"\n'
        '- "Cermaq Chile"\n'
        '- "Cermaq Canada"\n'
        '- "Steven Rafferty Cermaq" (CEO-relaterte uttalelser)\n'
        '- "Mitsubishi Cermaq"\n'
        '- "True Arctic Cermaq" (merkenavn)\n'
        "Eventuelt bredere bransjesøk hvis Cermaq-saker er få:\n"
        '- "norwegian salmon farming news"\n'
        '- "chilean salmon industry"\n'
        '- "salmon price Norway"\n\n'
        "VIKTIG: Bruk web_search MAKS 8 ganger totalt. "
        "Prioriter Cermaq-spesifikke søk først.\n\n"
        "OUTPUT-FORMAT:\n"
        "Returner KUN gyldig JSON, ingen markdown-wrapping, ingen kode-blokker.\n\n"
        "{{\n"
        '  "headline": "Én setning som fanger ukens viktigste sak",\n'
        '  "body": "Markdown-tekst med seksjoner",\n'
        '  "sources": ["url1", "url2"]\n'
        "}}\n\n"
        f'I "body", strukturer slik (hopp over tomme seksjoner):\n\n'
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
        "som rapporterer til ledergruppen — ikke salgsorientert.\n\n"
        "---\n\n"
        "KRITISK: Returner BARE gyldig JSON. Start svaret med { og slutt "
        "med }. Ikke skriv noen prosa, overskrifter, eller introduksjoner "
        "før JSON-en. Hele svaret skal kunne parses av json.loads().\n\n"
        "Format:\n"
        "{\n"
        '  "headline": "...",\n'
        '  "body": "### Seksjon\\n...\\n\\n### Neste seksjon\\n...",\n'
        '  "sources": ["url1", "url2"]\n'
        "}\n\n"
        'body skal inneholde markdown-seksjonene, men SELVE SVARET skal være '
        "ren JSON. Ikke skriv 'Her er...' eller andre prosa-introduksjoner."
    )

    user_prompt = (
        f"Lag en ukentlig medie-briefing om Cermaq og lakseoppdrett-bransjen "
        f"basert på siste 7 dagers nyheter.\n\n"
        f"Bruk web_search aktivt for å finne ferske Cermaq-saker.\n\n"
        f"Miniflux-artikler som kontekst "
        f"({len(miniflux_articles)} totalt, {cermaq_count} merket som Cermaq-spesifikke):\n\n"
        f"{miniflux_context}"
    )

    log.info("Genererer ukentlig digest lang=%s", lang)

    try:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model="claude-sonnet-4-6",
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
        log.info("Ukentlig digest lang=%s brukte web_search %d ganger", lang, search_count)

        text_parts = [
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        ]
        full_text = "\n".join(text_parts)

        try:
            result = json.loads(_extract_json(full_text))
        except (ValueError, json.JSONDecodeError) as parse_err:
            log.warning(
                "Ukentlig digest JSON-parsing feilet, bruker rå tekst: %s", parse_err
            )
            result = {
                "headline": (full_text.split("\n")[0] or "Ukens oppsummering")[:120],
                "body": full_text,
                "sources": [],
            }

        body_html = md.markdown(result.get("body", ""), extensions=["nl2br"])

        sources = result.get("sources") or []
        if sources:
            items = "".join(
                f'<li><a href="{url}" target="_blank" rel="noopener">{url}</a></li>'
                for url in sources[:15]
            )
            body_html += f"<h3>{titles['sources']}</h3><ul>{items}</ul>"

        log.info("Ukentlig digest klar lang=%s: %s", lang, result.get("headline", "")[:60])
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
        log.error("Ukentlig digest feilet lang=%s: %s", lang, exc, exc_info=True)
        return None
