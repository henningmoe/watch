"""Report generation logic for Cermaq Watch."""

import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def compute_report_statistics(articles):
    total = len(articles)
    cermaq_count = sum(1 for a in articles if a.scope == "cermaq")

    tones = {"positiv": 0, "noytral": 0, "negativ": 0}
    regions = {"norge": 0, "chile": 0, "canada": 0, "global": 0}
    themes_count = {}
    sources = {}

    for a in articles:
        if a.tone in tones:
            tones[a.tone] += 1
        if a.region in regions:
            regions[a.region] += 1
        for theme in a.themes or []:
            themes_count[theme] = themes_count.get(theme, 0) + 1
        domain = a.source_domain or "ukjent"
        if domain not in sources:
            sources[domain] = {"name": a.source_name or domain, "count": 0, "cermaq_count": 0}
        sources[domain]["count"] += 1
        if a.scope == "cermaq":
            sources[domain]["cermaq_count"] += 1

    top_sources = sorted(sources.values(), key=lambda s: s["count"], reverse=True)[:5]
    top_themes = sorted(themes_count.items(), key=lambda t: t[1], reverse=True)[:5]

    return {
        "total_articles": total,
        "cermaq_count": cermaq_count,
        "industry_count": total - cermaq_count,
        "tones": tones,
        "regions": regions,
        "themes": themes_count,
        "top_themes": top_themes,
        "top_sources": top_sources,
    }


def generate_ai_report_content(articles, stats, period_start, period_end, lang, focus):
    import os
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    lang_instruction = {
        "no": "Skriv på norsk.",
        "en": "Write in English.",
        "es": "Escribe en español.",
        "ja": "日本語で書いてください。",
    }.get(lang, "Skriv på norsk.")

    article_context = ""
    for a in articles[:80]:
        title = (a.titles or {}).get(lang) or a.title or ""
        summary = ((a.summaries or {}).get(lang, "") or "")[:300]
        meta = f"[{a.scope}|{a.region}|{a.tone}|{','.join(a.themes or [])}]"
        article_context += f"\n{meta} {title}\n{summary}\n"

    period_str = (
        f"{period_start.strftime('%d. %B %Y')} til {period_end.strftime('%d. %B %Y')}"
    )

    system_prompt = f"""{lang_instruction}

Du er en medieanalytiker for Cermaq, en global laksoppdretter eid av Mitsubishi med
operasjoner i Norge, Chile og Canada.

Lag en strukturert rapport for perioden {period_str} basert på artiklene nedenfor.
Rapporten skal ha NØYAKTIG 7 seksjoner i JSON-format.

VIKTIG: Returner KUN gyldig JSON, ingen markdown eller forklaringer utenfor JSON.

Format:
{{
  "executive_summary": "string - 3-5 setninger - hovedhendelser, tone-trend, volum-trend",
  "cermaq_specific": {{
    "headline": "string",
    "narrative": "string - 2-4 avsnitt",
    "highlights": ["string"]
  }},
  "industry_overview": {{
    "headline": "string",
    "narrative": "string - 2-4 avsnitt",
    "highlights": ["string"]
  }},
  "theme_spotlight": [
    {{"theme": "string", "headline": "string", "discussion": "string"}}
  ],
  "events": [
    {{"name": "string", "description": "string"}}
  ],
  "forward_looking": {{
    "narrative": "string",
    "watchpoints": ["string"]
  }},
  "key_quotes": [
    {{"quote": "string (max 30 ord)", "source": "string", "context": "string"}}
  ]
}}

VEILEDNING:
- executive_summary: 3-5 setninger, det første en travel leder leser
- cermaq_specific: Direkte Cermaq-omtaler (scope=cermaq artikler)
- industry_overview: Konkurrenter, regulatorisk, marked, bransjetrends
- theme_spotlight: 2-3 viktigste tema (Sjø, Landbasert, Fôr, Fiskehelse, Teknologi, Digitalisering, Finans, Marked)
- events: Tom liste [] hvis ingen tydelige events/konferanser identifisert
- forward_looking: Hva ser ut til å komme? Hvilke saker bør følges?
- key_quotes: 3-5 viktige sitater eller hovedfunn, korte og slagkraftige
Vær konkret og nøytral. Bruk tall der relevant.

ARTIKLER ({len(articles)} totalt, {min(80, len(articles))} vist):
{article_context}

STATISTIKK: {json.dumps(stats, ensure_ascii=False)}
"""

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=4000,
        system=system_prompt,
        messages=[{"role": "user", "content": f"Lag rapport for {period_str}."}],
    )

    raw_text = response.content[0].text.strip()

    if raw_text.startswith("```"):
        parts = raw_text.split("```")
        raw_text = parts[1] if len(parts) > 1 else raw_text
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as e:
        log.error("Klarte ikke parse rapport-JSON: %s", e)
        return {
            "executive_summary": raw_text[:500],
            "cermaq_specific": {"headline": "", "narrative": "", "highlights": []},
            "industry_overview": {"headline": "", "narrative": "", "highlights": []},
            "theme_spotlight": [],
            "events": [],
            "forward_looking": {"narrative": "", "watchpoints": []},
            "key_quotes": [],
            "_parse_error": str(e),
        }


def generate_default_title(period_start, period_end, report_type, lang):
    if report_type == "weekly":
        week = period_start.isocalendar()[1]
        titles = {
            "no": f"Ukerapport uke {week}",
            "en": f"Weekly Report — Week {week}",
            "es": f"Informe Semanal — Semana {week}",
            "ja": f"週次レポート — 第{week}週",
        }
        return titles.get(lang, titles["no"])
    elif report_type == "monthly":
        months_no = ["januar", "februar", "mars", "april", "mai", "juni",
                     "juli", "august", "september", "oktober", "november", "desember"]
        if lang == "no":
            return f"Månedsrapport {months_no[period_start.month - 1]} {period_start.year}"
        return f"Monthly Report — {period_start.strftime('%B %Y')}"
    else:
        if lang == "no":
            return (f"Rapport {period_start.strftime('%d.%m')} "
                    f"– {period_end.strftime('%d.%m.%Y')}")
        return (f"Report {period_start.strftime('%Y-%m-%d')} "
                f"to {period_end.strftime('%Y-%m-%d')}")


def generate_report(
    period_start,
    period_end,
    lang="no",
    report_type="custom",
    title=None,
    focus="all",
    theme_filter=None,
    triggered_by="manual",
):
    from sqlalchemy import func
    from poc.db import SessionLocal, Article, Report

    log.info(
        "Genererer rapport: %s til %s, lang=%s, type=%s",
        period_start.date(), period_end.date(), lang, report_type,
    )

    with SessionLocal() as session:
        query = session.query(Article).filter(
            Article.classified_at.isnot(None),
            Article.scope != "irrelevant",
            func.coalesce(Article.published_at, Article.fetched_at) >= period_start,
            func.coalesce(Article.published_at, Article.fetched_at) < period_end,
        )

        if focus == "cermaq":
            query = query.filter(Article.scope == "cermaq")
        elif focus == "industry":
            query = query.filter(Article.scope != "cermaq")

        articles = query.order_by(
            func.coalesce(Article.published_at, Article.fetched_at).desc()
        ).all()

        if theme_filter:
            articles = [a for a in articles if a.themes and any(t in a.themes for t in theme_filter)]

        if not articles:
            log.warning("Ingen artikler i periode %s – %s", period_start.date(), period_end.date())
            return None

        log.info("Rapport-grunnlag: %d artikler", len(articles))

        stats = compute_report_statistics(articles)
        content = generate_ai_report_content(articles, stats, period_start, period_end, lang, focus)

        if not title:
            title = generate_default_title(period_start, period_end, report_type, lang)

        report = Report(
            report_type=report_type,
            lang=lang,
            period_start=period_start,
            period_end=period_end,
            title=title,
            content=content,
            article_ids=[a.id for a in articles],
            statistics=stats,
            generated_at=datetime.now(timezone.utc),
            triggered_by=triggered_by,
        )
        session.add(report)
        session.commit()
        session.refresh(report)

        log.info("Rapport lagret med id=%d", report.id)
        return report
