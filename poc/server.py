"""Flask server — serves HTML frontend and JSON API for Cermaq Watch."""

import logging
import os
import queue
import re
import hashlib
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request

try:
    from sqlalchemy import text as _sa_text
except ImportError:
    _sa_text = None

from poc.classify import classify
from poc.fetch import fetch_miniflux, _fetch_og_image
from poc.db import (
    init_db, ensure_columns, migrate_id_to_bigint, migrate_add_themes,
    migrate_add_digest_region,
    migrate_add_finance_digests, migrate_add_salmon_prices, migrate_add_calendar_events,
    migrate_add_reports, migrate_add_module_status,
    is_db_available, SessionLocal,
    Source, Article, Digest, Alert, WeeklyDigest, FinanceDigest, SalmonPrice, CalendarEvent,
    Report, ModuleStatus,
    extract_domain, get_or_create_source,
)
from poc.translations import TRANSLATIONS, t as _t, theme_label as _theme_label

log = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates")

_THEMES_LIST = ["Sjø", "Landbasert", "Fôr", "Fiskehelse", "Teknologi", "Digitalisering", "Finans", "Marked"]
_REGIONS_LIST = ["norge", "chile", "canada", "global"]


@app.context_processor
def inject_sidebar_counts():
    """Sidebar counts (theme/region) available in all templates."""
    from flask import request as _req

    active_region = _req.args.get("region", "")
    active_theme = _req.args.get("theme", "")

    theme_counts = {t: 0 for t in _THEMES_LIST}
    region_counts = {r: 0 for r in _REGIONS_LIST}
    total_classified = 0

    if is_db_available():
        try:
            with SessionLocal() as session:
                rows = (
                    session.query(Article.region, Article.themes)
                    .filter(
                        Article.classified_at.isnot(None),
                        Article.scope != "irrelevant",
                    )
                    .all()
                )
                total_classified = len(rows)
                for row in rows:
                    reg = row.region or "global"
                    if reg in region_counts:
                        region_counts[reg] += 1
                    else:
                        region_counts["global"] += 1
                    for t in (row.themes or []):
                        if t in theme_counts:
                            theme_counts[t] += 1
        except Exception as exc:
            log.warning("inject_sidebar_counts feilet: %s", exc)

    _lang = _req.args.get("lang", "no")
    if _lang not in TRANSLATIONS:
        _lang = "no"

    return dict(
        theme_counts=theme_counts,
        region_counts=region_counts,
        total_classified=total_classified,
        active_region=active_region,
        active_theme=active_theme,
        # i18n helpers
        t=lambda key, **kw: _t(key, _lang, **kw),
        theme_label=lambda theme: _theme_label(theme, _lang),
        translations=TRANSLATIONS.get(_lang, {}),
    )


# Articles keyed by ID; never shrinks (accumulates over time)
_articles: dict = {}
_articles_lock = threading.Lock()

# Fetch state
_fetch_error: str | None = None
_fetched_at: float | None = None
_CACHE_TTL = 30 * 60  # seconds between Miniflux fetches

# Classification queue and counters
_classify_queue: queue.Queue = queue.Queue()
_classified_count = 0
_total_count = 0
_counters_lock = threading.Lock()

_CLASSIFY_RATE = 1.0  # minimum seconds between classify calls

# Digest cache (daily)
_digest_cache: dict = {}  # lang -> digest dict
_digest_lock = threading.Lock()

# Weekly digest cache
_weekly_digest_cache: dict = {}  # lang -> weekly digest dict
_weekly_digest_lock = threading.Lock()

_UI_TEXTS: dict = {
    "no": {
        # legacy / existing
        "digest_title": "Dagens oppsummering",
        "digest_pending": "Dagens oppsummering genereres…",
        "based_on": "Basert på",
        "articles": "artikler",
        "generated_at": "Generert",
        "searches_used": "Web-søk brukt:",
        "nav_dashboard": "Dagsoversikt",
        # page
        "page_title": "Nyheter",
        "page_subtitle": "Daglig oversikt over Cermaq og bransjedekning",
        # stat cards
        "stat_articles": "Artikler",
        "stat_articles_subtitle": "{n} klassifisert",
        "stat_cermaq": "Cermaq-omtaler",
        "stat_cermaq_subtitle": "direkte Cermaq-saker",
        "stat_negative": "Negativ omtale",
        "stat_negative_subtitle": "negativ omtale",
        "stat_sources": "Aktive kilder",
        # digest card
        "digest_label": "Dagens oppsummering",
        "digest_generated": "Generert {time}",
        "digest_loading": "Dagens oppsummering genereres...",
        "digest_quiet": "Stille mediedøgn",
        "digest_few_articles": "Få relevante artikler siste 24 timer.",
        # filter card
        "filter_title": "Filtrer artikler",
        "filter_reset": "Tilbakestill alle",
        "filter_scope": "Omtale-nivå",
        "filter_country": "Land og region",
        "filter_tone": "Tone",
        "filter_time": "Tidsperiode",
        "filter_sort": "Sortér",
        "filter_all": "Alle",
        # scope values
        "scope_cermaq": "Cermaq direkte",
        "scope_industry_in_regions": "Bransje i Cermaq-områder",
        "scope_industry_general": "Bransje generelt",
        # tone values
        "tone_positive": "Positiv",
        "tone_neutral": "Nøytral",
        "tone_negative": "Negativ",
        # country values
        "country_norway": "Norge",
        "country_chile": "Chile",
        "country_canada": "Canada",
        "country_global": "Globalt",
        # time period values
        "time_today": "I dag",
        "time_7days": "Siste 7 dager",
        "time_30days": "Siste 30 dager",
        # sort values
        "sort_newest": "Nyeste først",
        "sort_oldest": "Eldste først",
        "sort_relevance": "Relevans",
        # result header / articles
        "showing_articles": "Viser {n} av {total} artikler",
        "btn_export": "Eksporter",
        "btn_create_alert": "Lag varsel",
        "time_ago_minutes": "{n} min siden",
        "time_ago_hours": "{n} t siden",
        "time_ago_days": "{n} dager siden",
        "load_more": "Last flere artikler",
        "search_placeholder": "Søk i artikler, kilder, personer…",
        # source filter
        "filter_source": "Kilde",
        "source_news": "Nyheter",
        "source_some": "SoMe",
        # nav
        "nav_alerts": "Varsling",
        "nav_overview": "Oversikt",
        "nav_news": "Nyheter",
        "nav_search": "Søk",
        "nav_reports": "Rapporter",
        "nav_competitors": "Konkurrenter",
        "nav_settings": "Innstillinger",
        "nav_sources": "Kilder",
        # reports page
        "report_weekly_title": "Ukentlig rapport",
        "report_weekly_subtitle": "Mediedekning siste 7 dager",
        "report_ai_summary": "AI-skrevet ukentlig sammendrag",
        "report_ai_loading": "Genererer sammendrag…",
        "report_top_articles": "Topp 10 mest relevante saker",
        "report_tone_dist": "Tone-fordeling",
        "report_period": "Periode",
        "report_export": "Skriv ut / Eksporter",
        "report_coming_soon": "Kommer snart",
        "report_monthly": "Månedlig",
        "report_crisis": "Krise",
        "report_theme": "Tema-dypdykk",
        "report_positiv": "Positiv",
        "report_noytral": "Nøytral",
        "report_negativ": "Negativ",
        # weekly digest page
        "daily_update": "Dagens AI-oppdatering",
        "nav_weekly": "Uken oppsummert",
        "weekly_summary": "Uken oppsummert",
        "see_weekly": "Se ukens oppsummering →",
        "weekly_not_generated": "Ingen ukentlig oppsummering generert ennå. Kjør den fra admin-siden.",
    },
    "en": {
        # legacy / existing
        "digest_title": "Daily summary",
        "digest_pending": "Daily summary being generated…",
        "based_on": "Based on",
        "articles": "articles",
        "generated_at": "Generated",
        "searches_used": "Web searches used:",
        "nav_dashboard": "Dashboard",
        # page
        "page_title": "News",
        "page_subtitle": "Daily overview of Cermaq and industry coverage",
        # stat cards
        "stat_articles": "Articles",
        "stat_articles_subtitle": "{n} classified",
        "stat_cermaq": "Cermaq mentions",
        "stat_cermaq_subtitle": "direct Cermaq stories",
        "stat_negative": "Negative mentions",
        "stat_negative_subtitle": "negative coverage",
        "stat_sources": "Active sources",
        # digest card
        "digest_label": "Today's summary",
        "digest_generated": "Generated {time}",
        "digest_loading": "Today's summary is being generated...",
        "digest_quiet": "Quiet news day",
        "digest_few_articles": "Few relevant articles in the last 24 hours.",
        # filter card
        "filter_title": "Filter articles",
        "filter_reset": "Reset all",
        "filter_scope": "Mention level",
        "filter_country": "Country and region",
        "filter_tone": "Tone",
        "filter_time": "Time period",
        "filter_sort": "Sort",
        "filter_all": "All",
        # scope values
        "scope_cermaq": "Cermaq direct",
        "scope_industry_in_regions": "Industry in Cermaq regions",
        "scope_industry_general": "General industry",
        # tone values
        "tone_positive": "Positive",
        "tone_neutral": "Neutral",
        "tone_negative": "Negative",
        # country values
        "country_norway": "Norway",
        "country_chile": "Chile",
        "country_canada": "Canada",
        "country_global": "Global",
        # time period values
        "time_today": "Today",
        "time_7days": "Last 7 days",
        "time_30days": "Last 30 days",
        # sort values
        "sort_newest": "Newest first",
        "sort_oldest": "Oldest first",
        "sort_relevance": "Relevance",
        # result header / articles
        "showing_articles": "Showing {n} of {total} articles",
        "btn_export": "Export",
        "btn_create_alert": "Create alert",
        "time_ago_minutes": "{n} min ago",
        "time_ago_hours": "{n} h ago",
        "time_ago_days": "{n} days ago",
        "load_more": "Load more articles",
        "search_placeholder": "Search articles, sources, people…",
        # source filter
        "filter_source": "Source",
        "source_news": "News",
        "source_some": "SoMe",
        # nav
        "nav_alerts": "Alerts",
        "nav_overview": "Overview",
        "nav_news": "News",
        "nav_search": "Search",
        "nav_reports": "Reports",
        "nav_competitors": "Competitors",
        "nav_settings": "Settings",
        "nav_sources": "Sources",
        # reports page
        "report_weekly_title": "Weekly report",
        "report_weekly_subtitle": "Media coverage last 7 days",
        "report_ai_summary": "AI-written weekly summary",
        "report_ai_loading": "Generating summary…",
        "report_top_articles": "Top 10 most relevant stories",
        "report_tone_dist": "Tone distribution",
        "report_period": "Period",
        "report_export": "Print / Export",
        "report_coming_soon": "Coming soon",
        "report_monthly": "Monthly",
        "report_crisis": "Crisis",
        "report_theme": "Theme deep-dive",
        "report_positiv": "Positive",
        "report_noytral": "Neutral",
        "report_negativ": "Negative",
        # weekly digest page
        "daily_update": "Today's AI update",
        "nav_weekly": "Week in review",
        "weekly_summary": "Week in review",
        "see_weekly": "View weekly summary →",
        "weekly_not_generated": "No weekly summary generated yet. Run it from the admin page.",
    },
    "es": {
        # legacy / existing
        "digest_title": "Resumen del día",
        "digest_pending": "Resumen del día generándose…",
        "based_on": "Basado en",
        "articles": "artículos",
        "generated_at": "Generado",
        "searches_used": "Búsquedas web usadas:",
        "nav_dashboard": "Panel",
        # page
        "page_title": "Noticias",
        "page_subtitle": "Resumen diario de Cermaq y cobertura del sector",
        # stat cards
        "stat_articles": "Artículos",
        "stat_articles_subtitle": "{n} clasificados",
        "stat_cermaq": "Menciones de Cermaq",
        "stat_cermaq_subtitle": "noticias directas de Cermaq",
        "stat_negative": "Menciones negativas",
        "stat_negative_subtitle": "cobertura negativa",
        "stat_sources": "Fuentes activas",
        # digest card
        "digest_label": "Resumen del día",
        "digest_generated": "Generado {time}",
        "digest_loading": "Se está generando el resumen del día...",
        "digest_quiet": "Día de noticias tranquilo",
        "digest_few_articles": "Pocos artículos relevantes en las últimas 24 horas.",
        # filter card
        "filter_title": "Filtrar artículos",
        "filter_reset": "Restablecer todo",
        "filter_scope": "Nivel de mención",
        "filter_country": "País y región",
        "filter_tone": "Tono",
        "filter_time": "Periodo",
        "filter_sort": "Ordenar",
        "filter_all": "Todos",
        # scope values
        "scope_cermaq": "Cermaq directo",
        "scope_industry_in_regions": "Industria en regiones Cermaq",
        "scope_industry_general": "Industria general",
        # tone values
        "tone_positive": "Positivo",
        "tone_neutral": "Neutral",
        "tone_negative": "Negativo",
        # country values
        "country_norway": "Noruega",
        "country_chile": "Chile",
        "country_canada": "Canadá",
        "country_global": "Global",
        # time period values
        "time_today": "Hoy",
        "time_7days": "Últimos 7 días",
        "time_30days": "Últimos 30 días",
        # sort values
        "sort_newest": "Más reciente primero",
        "sort_oldest": "Más antiguo primero",
        "sort_relevance": "Relevancia",
        # result header / articles
        "showing_articles": "Mostrando {n} de {total} artículos",
        "btn_export": "Exportar",
        "btn_create_alert": "Crear alerta",
        "time_ago_minutes": "hace {n} min",
        "time_ago_hours": "hace {n} h",
        "time_ago_days": "hace {n} días",
        "load_more": "Cargar más artículos",
        "search_placeholder": "Buscar artículos, fuentes, personas…",
        # source filter
        "filter_source": "Fuente",
        "source_news": "Noticias",
        "source_some": "SoMe",
        # nav
        "nav_alerts": "Alertas",
        "nav_overview": "Resumen",
        "nav_news": "Noticias",
        "nav_search": "Buscar",
        "nav_reports": "Informes",
        "nav_competitors": "Competidores",
        "nav_settings": "Configuración",
        "nav_sources": "Fuentes",
        # reports page
        "report_weekly_title": "Informe semanal",
        "report_weekly_subtitle": "Cobertura mediática últimos 7 días",
        "report_ai_summary": "Resumen semanal escrito por IA",
        "report_ai_loading": "Generando resumen…",
        "report_top_articles": "Top 10 historias más relevantes",
        "report_tone_dist": "Distribución de tono",
        "report_period": "Período",
        "report_export": "Imprimir / Exportar",
        "report_coming_soon": "Próximamente",
        "report_monthly": "Mensual",
        "report_crisis": "Crisis",
        "report_theme": "Análisis temático",
        "report_positiv": "Positivo",
        "report_noytral": "Neutro",
        "report_negativ": "Negativo",
        # weekly digest page
        "daily_update": "Actualización IA del día",
        "nav_weekly": "Resumen semanal",
        "weekly_summary": "Resumen semanal",
        "see_weekly": "Ver resumen semanal →",
        "weekly_not_generated": "No hay resumen semanal generado aún. Ejecútalo desde el panel de administración.",
    },
    "ja": {
        # legacy / existing
        "digest_title": "本日のまとめ",
        "digest_pending": "本日のまとめを生成中…",
        "based_on": "対象記事",
        "articles": "件",
        "generated_at": "生成日時",
        "searches_used": "ウェブ検索使用回数:",
        "nav_dashboard": "ダッシュボード",
        # page
        "page_title": "ニュース",
        "page_subtitle": "Cermaqと業界カバレッジの毎日の概要",
        # stat cards
        "stat_articles": "記事",
        "stat_articles_subtitle": "{n}件分類済み",
        "stat_cermaq": "Cermaqの言及",
        "stat_cermaq_subtitle": "Cermaq直接記事",
        "stat_negative": "ネガティブな言及",
        "stat_negative_subtitle": "ネガティブな報道",
        "stat_sources": "アクティブなソース",
        # digest card
        "digest_label": "本日のまとめ",
        "digest_generated": "生成日時 {time}",
        "digest_loading": "本日のまとめを生成中...",
        "digest_quiet": "静かな報道日",
        "digest_few_articles": "過去24時間で関連記事はわずかです。",
        # filter card
        "filter_title": "記事をフィルター",
        "filter_reset": "すべてリセット",
        "filter_scope": "言及レベル",
        "filter_country": "国と地域",
        "filter_tone": "トーン",
        "filter_time": "期間",
        "filter_sort": "並び替え",
        "filter_all": "すべて",
        # scope values
        "scope_cermaq": "Cermaq直接",
        "scope_industry_in_regions": "Cermaq地域の業界",
        "scope_industry_general": "一般業界",
        # tone values
        "tone_positive": "ポジティブ",
        "tone_neutral": "ニュートラル",
        "tone_negative": "ネガティブ",
        # country values
        "country_norway": "ノルウェー",
        "country_chile": "チリ",
        "country_canada": "カナダ",
        "country_global": "グローバル",
        # time period values
        "time_today": "今日",
        "time_7days": "過去7日間",
        "time_30days": "過去30日間",
        # sort values
        "sort_newest": "新しい順",
        "sort_oldest": "古い順",
        "sort_relevance": "関連性",
        # result header / articles
        "showing_articles": "{total}件中{n}件を表示",
        "btn_export": "エクスポート",
        "btn_create_alert": "アラート作成",
        "time_ago_minutes": "{n}分前",
        "time_ago_hours": "{n}時間前",
        "time_ago_days": "{n}日前",
        "load_more": "さらに記事を読み込む",
        "search_placeholder": "記事、ソース、人物を検索…",
        # source filter
        "filter_source": "ソース",
        "source_news": "ニュース",
        "source_some": "SoMe",
        # nav
        "nav_alerts": "アラート",
        "nav_overview": "概要",
        "nav_news": "ニュース",
        "nav_search": "検索",
        "nav_reports": "レポート",
        "nav_competitors": "競合他社",
        "nav_settings": "設定",
        "nav_sources": "ソース",
        # reports page
        "report_weekly_title": "週次レポート",
        "report_weekly_subtitle": "過去7日間のメディアカバレッジ",
        "report_ai_summary": "AI作成の週次サマリー",
        "report_ai_loading": "サマリーを生成中…",
        "report_top_articles": "最も関連性の高いトップ10記事",
        "report_tone_dist": "トーン分布",
        "report_period": "期間",
        "report_export": "印刷 / エクスポート",
        "report_coming_soon": "近日公開",
        "report_monthly": "月次",
        "report_crisis": "クライシス",
        "report_theme": "テーマ分析",
        "report_positiv": "ポジティブ",
        "report_noytral": "ニュートラル",
        "report_negativ": "ネガティブ",
        # weekly digest page
        "daily_update": "本日のAIアップデート",
        "nav_weekly": "週間まとめ",
        "weekly_summary": "週間まとめ",
        "see_weekly": "週間まとめを見る →",
        "weekly_not_generated": "まだ週間サマリーが生成されていません。管理画面から実行してください。",
    },
}


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _article_db_to_dict(a) -> dict:
    return {
        "id": a.id,
        "title": a.title,
        "url": a.url,
        "source_name": a.source_name,
        "source_domain": a.source_domain,
        "fetch_method": a.fetch_method,
        "published_at": a.published_at.isoformat() if a.published_at else None,
        "scope": a.scope,
        "region": a.region,
        "tone": a.tone,
        "category": a.category,
        "relevance": a.relevance,
        "summaries": a.summaries or {},
        "titles": a.titles or {},
        "themes": a.themes or [],
        "image_url": a.image_url,
        "content": a.content,
        "classified_at": a.classified_at.isoformat() if a.classified_at else None,
    }


def _load_from_db() -> None:
    if not is_db_available():
        log.info("Ingen Postgres — bruker tom in-memory cache")
        return

    try:
        with SessionLocal() as session:
            articles = session.query(Article).all()
            with _articles_lock:
                for a in articles:
                    _articles[a.id] = _article_db_to_dict(a)
            log.info("Lastet %d artikler fra Postgres", len(articles))

            for lang in ("no", "en", "es", "ja"):
                latest = (
                    session.query(Digest)
                    .filter(Digest.lang == lang)
                    .order_by(Digest.generated_at.desc())
                    .first()
                )
                if latest:
                    if latest.content:
                        cached = latest.content
                    else:
                        # Legacy rows without content JSON
                        cached = {
                            "headline": latest.headline or "",
                            "body": latest.body or "",
                            "sections": {"global": latest.body or ""},
                            "sources": latest.web_search_urls or [],
                            "article_count": latest.article_count or 0,
                            "search_count": latest.search_count or 0,
                            "generated_at": (
                                latest.generated_at.isoformat()
                                if latest.generated_at else None
                            ),
                        }
                    with _digest_lock:
                        _digest_cache[lang] = cached
            log.info("Lastet %d digester fra Postgres", len(_digest_cache))

            for lang in ("no", "en", "es", "ja"):
                latest_weekly = (
                    session.query(WeeklyDigest)
                    .filter_by(lang=lang)
                    .order_by(WeeklyDigest.generated_at.desc())
                    .first()
                )
                if latest_weekly:
                    with _weekly_digest_lock:
                        _weekly_digest_cache[lang] = {
                            "headline": latest_weekly.headline,
                            "body": latest_weekly.body,
                            "sources": latest_weekly.sources or [],
                            "article_count": latest_weekly.article_count or 0,
                            "cermaq_count": latest_weekly.cermaq_count or 0,
                            "search_count": latest_weekly.search_count or 0,
                            "generated_at": latest_weekly.generated_at.isoformat(),
                            "lang": lang,
                        }
            log.info("Lastet %d ukentlige digester fra Postgres", len(_weekly_digest_cache))
    except Exception as exc:
        log.error("Feil ved lasting fra Postgres: %s", exc)


def _persist_article(article: dict) -> None:
    if not is_db_available():
        return

    try:
        with SessionLocal() as session:
            existing = session.get(Article, article["id"])

            domain = article.get("source_domain") or extract_domain(article.get("url", ""))

            source_id = None
            if domain:
                source = get_or_create_source(
                    session, domain=domain, name=article.get("source_name", domain)
                )
                source_id = source.id

            published_at = None
            if article.get("published_at"):
                try:
                    published_at = datetime.fromisoformat(
                        article["published_at"].replace("Z", "+00:00")
                    )
                except Exception:
                    pass

            classified_at = None
            if article.get("classified_at"):
                try:
                    classified_at = datetime.fromisoformat(article["classified_at"])
                except Exception:
                    pass
            elif "scope" in article:
                classified_at = datetime.now(timezone.utc)

            kwargs = {
                "id": article["id"],
                "title": article.get("title"),
                "url": article.get("url"),
                "source_id": source_id,
                "source_domain": domain,
                "source_name": article.get("source_name"),
                "fetch_method": article.get("fetch_method", "miniflux"),
                "published_at": published_at,
                "scope": article.get("scope"),
                "region": article.get("region"),
                "tone": article.get("tone"),
                "category": article.get("category"),
                "relevance": article.get("relevance"),
                "themes": article.get("themes") or [],
                "summaries": article.get("summaries"),
                "titles": article.get("titles"),
                "image_url": article.get("image_url"),
                "content": article.get("content"),
                "classified_at": classified_at,
                "updated_at": datetime.now(timezone.utc),
            }

            if existing:
                for k, v in kwargs.items():
                    setattr(existing, k, v)
            else:
                session.add(Article(**kwargs))

            session.commit()
    except Exception as exc:
        log.error("Persist-feil for artikkel %s: %s", article.get("id"), exc)


def _persist_weekly_digest(lang: str, digest: dict) -> None:
    if not is_db_available():
        return

    try:
        with SessionLocal() as session:
            d = WeeklyDigest(
                lang=lang,
                headline=digest.get("headline", ""),
                body=digest.get("body", ""),
                sources=digest.get("sources", []),
                article_count=digest.get("article_count", 0),
                cermaq_count=digest.get("cermaq_count", 0),
                search_count=digest.get("search_count", 0),
            )
            session.add(d)
            session.commit()
            log.info("Ukentlig digest persistert for %s", lang)
    except Exception as exc:
        log.error("Ukentlig digest-persist-feil for %s: %s", lang, exc)


def _save_digest(lang: str, content: dict) -> None:
    """Save a structured digest to DB and update in-memory cache."""
    if is_db_available():
        try:
            with SessionLocal() as session:
                d = Digest(
                    lang=lang,
                    content=content,
                    generated_at=datetime.now(timezone.utc),
                )
                session.add(d)
                session.commit()
            log.info("Digest lagret: lang=%s", lang)
        except Exception as exc:
            log.error("Digest-lagring feilet lang=%s: %s", lang, exc)
    with _digest_lock:
        _digest_cache[lang] = content


# ---------------------------------------------------------------------------
# Startup: initialise DB and load existing data
# ---------------------------------------------------------------------------

init_db()
ensure_columns()
migrate_id_to_bigint()
migrate_add_themes()
migrate_add_digest_region()
migrate_add_finance_digests()
migrate_add_salmon_prices()
migrate_add_calendar_events()
migrate_add_reports()
migrate_add_module_status()
_load_from_db()


def update_module_status(
    module_key: str,
    status: str,
    message: str = "",
    data_summary: dict | None = None,
) -> None:
    """Upsert module run status in DB and log it."""
    log.info("ModuleStatus [%s] → %s  %s", module_key, status, message)
    if not is_db_available():
        return
    try:
        now = datetime.now(timezone.utc)
        with SessionLocal() as session:
            row = session.query(ModuleStatus).filter_by(module_key=module_key).first()
            if row is None:
                row = ModuleStatus(module_key=module_key)
                session.add(row)
            row.status = status
            row.last_message = message
            if status not in ("idle", "running"):
                row.last_run_at = now
            if data_summary is not None:
                row.data_summary = data_summary
            row.updated_at = now
            session.commit()
    except Exception as exc:
        log.warning("update_module_status feilet: %s", exc)


def seed_calendar_events() -> None:
    """Seed the calendar with known industry events for 2026-2027 (idempotent)."""
    if not is_db_available():
        return
    from sqlalchemy import text as _text

    _SEED: list[dict] = [
        # --- Q1 2026 results (typical release windows) ---
        {"title": "Mowi Q1 2026 kvartalsrapport", "event_date": "2026-05-07T06:00:00+00:00", "event_type": "q-report", "company": "Mowi"},
        {"title": "Lerøy Q1 2026 kvartalsrapport", "event_date": "2026-05-13T06:00:00+00:00", "event_type": "q-report", "company": "Lerøy"},
        {"title": "SalMar Q1 2026 kvartalsrapport", "event_date": "2026-05-14T06:00:00+00:00", "event_type": "q-report", "company": "SalMar"},
        {"title": "Grieg Seafood Q1 2026 kvartalsrapport", "event_date": "2026-05-20T06:00:00+00:00", "event_type": "q-report", "company": "Grieg Seafood"},
        {"title": "Bakkafrost Q1 2026 kvartalsrapport", "event_date": "2026-05-28T06:00:00+00:00", "event_type": "q-report", "company": "Bakkafrost"},
        # --- Q2 2026 results ---
        {"title": "Lerøy Q2 2026 kvartalsrapport", "event_date": "2026-08-19T06:00:00+00:00", "event_type": "q-report", "company": "Lerøy"},
        {"title": "Mowi Q2 2026 kvartalsrapport", "event_date": "2026-08-20T06:00:00+00:00", "event_type": "q-report", "company": "Mowi"},
        {"title": "Bakkafrost Q2 2026 kvartalsrapport", "event_date": "2026-08-25T06:00:00+00:00", "event_type": "q-report", "company": "Bakkafrost"},
        {"title": "Grieg Seafood Q2 2026 kvartalsrapport", "event_date": "2026-08-26T06:00:00+00:00", "event_type": "q-report", "company": "Grieg Seafood"},
        {"title": "SalMar Q2 2026 kvartalsrapport", "event_date": "2026-08-27T06:00:00+00:00", "event_type": "q-report", "company": "SalMar"},
        # --- Q3 2026 results ---
        {"title": "Mowi Q3 2026 kvartalsrapport", "event_date": "2026-11-05T06:00:00+00:00", "event_type": "q-report", "company": "Mowi"},
        {"title": "Lerøy Q3 2026 kvartalsrapport", "event_date": "2026-11-10T06:00:00+00:00", "event_type": "q-report", "company": "Lerøy"},
        {"title": "SalMar Q3 2026 kvartalsrapport", "event_date": "2026-11-12T06:00:00+00:00", "event_type": "q-report", "company": "SalMar"},
        {"title": "Bakkafrost Q3 2026 kvartalsrapport", "event_date": "2026-11-17T06:00:00+00:00", "event_type": "q-report", "company": "Bakkafrost"},
        {"title": "Grieg Seafood Q3 2026 kvartalsrapport", "event_date": "2026-11-18T06:00:00+00:00", "event_type": "q-report", "company": "Grieg Seafood"},
        # --- Norges Bank rentebeslutninger 2026 ---
        {"title": "Norges Bank rentebeslutning", "event_date": "2026-01-22T10:00:00+01:00", "event_type": "regulatory", "company": "Norges Bank"},
        {"title": "Norges Bank rentebeslutning", "event_date": "2026-03-26T10:00:00+01:00", "event_type": "regulatory", "company": "Norges Bank"},
        {"title": "Norges Bank rentebeslutning", "event_date": "2026-05-07T10:00:00+02:00", "event_type": "regulatory", "company": "Norges Bank"},
        {"title": "Norges Bank rentebeslutning", "event_date": "2026-06-18T10:00:00+02:00", "event_type": "regulatory", "company": "Norges Bank"},
        {"title": "Norges Bank rentebeslutning", "event_date": "2026-08-20T10:00:00+02:00", "event_type": "regulatory", "company": "Norges Bank"},
        {"title": "Norges Bank rentebeslutning", "event_date": "2026-09-17T10:00:00+02:00", "event_type": "regulatory", "company": "Norges Bank"},
        {"title": "Norges Bank rentebeslutning", "event_date": "2026-10-29T10:00:00+01:00", "event_type": "regulatory", "company": "Norges Bank"},
        {"title": "Norges Bank rentebeslutning", "event_date": "2026-12-17T10:00:00+01:00", "event_type": "regulatory", "company": "Norges Bank"},
        # --- Norske helligdager 2026 ---
        {"title": "Skjærtorsdag", "event_date": "2026-04-02T00:00:00+02:00", "event_type": "holiday"},
        {"title": "Langfredag", "event_date": "2026-04-03T00:00:00+02:00", "event_type": "holiday"},
        {"title": "1. påskedag", "event_date": "2026-04-05T00:00:00+02:00", "event_type": "holiday"},
        {"title": "2. påskedag", "event_date": "2026-04-06T00:00:00+02:00", "event_type": "holiday"},
        {"title": "Arbeidernes dag", "event_date": "2026-05-01T00:00:00+02:00", "event_type": "holiday"},
        {"title": "Kristi himmelfartsdag", "event_date": "2026-05-14T00:00:00+02:00", "event_type": "holiday"},
        {"title": "Grunnlovsdagen", "event_date": "2026-05-17T00:00:00+02:00", "event_type": "holiday"},
        {"title": "1. pinsedag", "event_date": "2026-05-24T00:00:00+02:00", "event_type": "holiday"},
        {"title": "2. pinsedag", "event_date": "2026-05-25T00:00:00+02:00", "event_type": "holiday"},
        {"title": "1. juledag", "event_date": "2026-12-25T00:00:00+01:00", "event_type": "holiday"},
        {"title": "2. juledag", "event_date": "2026-12-26T00:00:00+01:00", "event_type": "holiday"},
        # --- Bransjehendelser ---
        {"title": "Sjømatdagene 2027", "event_date": "2027-01-26T09:00:00+01:00", "event_type": "industry-event", "description": "Norges største sjømatkonferanse, Tromsø"},
        {"title": "AquaNor 2027", "event_date": "2027-08-19T09:00:00+02:00", "event_type": "industry-event", "description": "Verdens største havbruksmesse, Trondheim. Annet hvert år."},
        {"title": "Seafood Expo Global 2026", "event_date": "2026-04-21T09:00:00+02:00", "event_type": "industry-event", "description": "Barcelona, verdens største sjømatmesse"},
        {"title": "Fish International 2026", "event_date": "2026-02-16T09:00:00+01:00", "event_type": "industry-event", "description": "Bremen, internasjonal sjømatmesse"},
        # --- Cermaq ---
        {"title": "Cermaq generalforsamling 2026", "event_date": "2026-04-30T10:00:00+02:00", "event_type": "agm", "company": "Cermaq"},
    ]

    try:
        with SessionLocal() as session:
            existing_titles = {
                r[0]
                for r in session.execute(
                    _text("SELECT title FROM calendar_events")
                ).fetchall()
            }
            added = 0
            for ev in _SEED:
                if ev["title"] in existing_titles:
                    continue
                dt = datetime.fromisoformat(ev["event_date"])
                row = CalendarEvent(
                    title=ev["title"],
                    description=ev.get("description"),
                    event_date=dt,
                    event_type=ev.get("event_type"),
                    company=ev.get("company"),
                )
                session.add(row)
                added += 1
            if added:
                session.commit()
                log.info("seed_calendar_events: la til %d hendelser", added)
    except Exception as exc:
        log.warning("seed_calendar_events feilet: %s", exc)


seed_calendar_events()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _parse_dt(iso: str | None) -> datetime:
    if not iso:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Background thread 1 — fetch
# ---------------------------------------------------------------------------

def _fetch_loop() -> None:
    """Runs forever: fetch Miniflux, enqueue new articles, sleep 30 min."""
    global _fetch_error, _fetched_at

    while True:
        log.info("Fetching articles from Miniflux...")
        try:
            articles = fetch_miniflux()

            with _articles_lock:
                existing_ids = set(_articles.keys())

            new_articles = [a for a in articles if a["id"] not in existing_ids]
            log.info("Fetch complete: %d total, %d new", len(articles), len(new_articles))

            with _articles_lock:
                for a in new_articles:
                    _articles[a["id"]] = a
            with _counters_lock:
                global _total_count
                _total_count = len(_articles)

            for a in new_articles:
                _classify_queue.put(a["id"])

            with _articles_lock:
                _fetched_at = time.monotonic()
                _fetch_error = None

        except Exception as exc:
            log.error("Fetch failed: %s", exc)
            with _articles_lock:
                _fetch_error = str(exc)
                _fetched_at = time.monotonic()

        time.sleep(_CACHE_TTL)


# ---------------------------------------------------------------------------
# Source detection helper
# ---------------------------------------------------------------------------

def _detect_source_from_url(url: str) -> tuple[str | None, str | None]:
    """Return (source_name, source_domain) inferred from URL, or (None, None)."""
    u = (url or "").lower()
    if "linkedin.com" in u:
        return "LinkedIn", "linkedin.com"
    if "twitter.com" in u or "x.com/" in u:
        return "X", "x.com"
    if "facebook.com" in u:
        return "Facebook", "facebook.com"
    if "ilaks.no" in u:
        return "iLaks", "ilaks.no"
    if "intrafish" in u:
        return "IntraFish", "intrafish.no"
    if "salmonbusiness" in u:
        return "SalmonBusiness", "salmonbusiness.com"
    if "fishfarmingexpert" in u:
        return "Fish Farming Expert", "fishfarmingexpert.com"
    if "undercurrentnews" in u:
        return "Undercurrent News", "undercurrentnews.com"
    if "seafoodsource" in u:
        return "SeafoodSource", "seafoodsource.com"
    if "elciudadano" in u:
        return "El Ciudadano", "elciudadano.com"
    if "loslagosnoticias" in u:
        return "Los Lagos Noticias", "loslagosnoticias.cl"
    if "altaposten" in u:
        return "Altaposten", "altaposten.no"
    if "nrk.no" in u:
        return "NRK", "nrk.no"
    if "e24.no" in u:
        return "E24", "e24.no"
    if "dn.no" in u:
        return "Dagens Næringsliv", "dn.no"
    if "aftenposten" in u:
        return "Aftenposten", "aftenposten.no"
    return None, None


def _fetch_article_metadata(url: str) -> dict:
    """Fetch title, description, image_url and text content from a URL via OG metadata."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Cermaq Watch"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")

        title_match = (
            re.search(r"<meta[^>]*property=['\"]og:title['\"][^>]*content=['\"]([^'\"]+)", html)
            or re.search(r"<title>([^<]+)</title>", html)
        )
        title = title_match.group(1).strip() if title_match else ""

        desc_match = re.search(
            r"<meta[^>]*property=['\"]og:description['\"][^>]*content=['\"]([^'\"]+)", html
        )
        description = desc_match.group(1).strip() if desc_match else ""

        image_match = re.search(
            r"<meta[^>]*property=['\"]og:image['\"][^>]*content=['\"]([^'\"]+)", html
        )
        image_url = image_match.group(1).strip() if image_match else None

        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()[:5000]

        return {"title": title, "description": description, "image_url": image_url,
                "content": (description + "\n\n" + text).strip()}
    except Exception as exc:
        log.warning("Kunne ikke hente metadata for %s: %s", url, exc)
        return {"title": "", "description": "", "image_url": None, "content": ""}


def _ingest_urls_as_articles(urls: list) -> int:
    """Add a list of URLs as new articles and queue them for classification."""
    if not urls:
        return 0
    added = 0
    for url in urls:
        if not url or not url.startswith("http"):
            continue
        if is_db_available():
            try:
                with SessionLocal() as session:
                    if session.query(Article).filter_by(url=url).first():
                        continue
            except Exception:
                pass

        article_id = int(hashlib.md5(url.encode()).hexdigest()[:8], 16)
        with _articles_lock:
            if article_id in _articles:
                continue

        domain = urlparse(url).netloc.replace("www.", "")
        source_name, source_domain = _detect_source_from_url(url)
        if not source_name:
            source_name = domain.split(".")[0]
            source_domain = domain

        article = {
            "id": article_id,
            "title": "",
            "url": url,
            "source_name": source_name,
            "source_domain": source_domain,
            "fetch_method": "websearch",
            "published_at": None,
            "content": "",
            "image_url": None,
        }
        with _articles_lock:
            _articles[article_id] = article
        _classify_queue.put(article_id)
        added += 1

    log.info("Web-søk: la til %d nye artikler i klassifiseringskøen", added)
    return added


# ---------------------------------------------------------------------------
# Background thread 2 — classify
# ---------------------------------------------------------------------------

def _classify_loop() -> None:
    """Runs forever: pop IDs from queue, classify, update cache, persist."""
    global _classified_count

    while True:
        article_id = _classify_queue.get()  # blocks until work arrives
        try:
            with _articles_lock:
                article = _articles.get(article_id)

            if article is None or "scope" in article:
                _classify_queue.task_done()
                continue

            # Auto-detect source from URL before classification
            detected_name, detected_domain = _detect_source_from_url(article.get("url"))
            if detected_name:
                with _articles_lock:
                    if article_id in _articles:
                        _articles[article_id]["source_name"] = detected_name
                        _articles[article_id]["source_domain"] = detected_domain
                article = dict(article)
                article["source_name"] = detected_name
                article["source_domain"] = detected_domain

            # Fetch metadata for articles without title/content (e.g. from websearch)
            if not article.get("title") or not article.get("content"):
                meta = _fetch_article_metadata(article["url"])
                updates = {}
                if meta.get("title"):
                    updates["title"] = meta["title"]
                    article["title"] = meta["title"]
                if meta.get("content"):
                    updates["content"] = meta["content"]
                    article["content"] = meta["content"]
                if meta.get("image_url") and not article.get("image_url"):
                    updates["image_url"] = meta["image_url"]
                    article["image_url"] = meta["image_url"]
                if updates:
                    with _articles_lock:
                        if article_id in _articles:
                            _articles[article_id].update(updates)
            if not article.get("title"):
                article["title"] = f"Artikkel fra {article.get('source_name', 'web')}"
                with _articles_lock:
                    if article_id in _articles:
                        _articles[article_id]["title"] = article["title"]

            enrichment = classify(article)

            with _articles_lock:
                if article_id in _articles:
                    _articles[article_id].update(enrichment)

            with _counters_lock:
                _classified_count += 1

            # Fetch OG image for relevant articles that have no image from RSS
            if enrichment.get("scope") in ("cermaq", "industry") and not article.get("image_url"):
                og_image = _fetch_og_image(article["url"])
                if og_image:
                    with _articles_lock:
                        if article_id in _articles:
                            _articles[article_id]["image_url"] = og_image
                    log.info("OG-image hentet for id=%s", article_id)

            # Persist to Postgres (non-blocking; errors logged but not re-raised)
            with _articles_lock:
                persisted = dict(_articles.get(article_id, {}))
            _persist_article(persisted)

        except Exception as exc:
            log.error("classify loop error for id=%s: %s", article_id, exc)
        finally:
            _classify_queue.task_done()

        time.sleep(_CLASSIFY_RATE)


# ---------------------------------------------------------------------------
# Digest helpers
# ---------------------------------------------------------------------------

def _generate_news_digest() -> None:
    """Generate 1 Norwegian master digest + 3 translations (4 digests total)."""
    from poc.digest import generate_norwegian_master_digest, translate_digest
    from sqlalchemy import func as _func

    log.info("Starter digest-generering")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    if is_db_available():
        try:
            with SessionLocal() as session:
                rows = session.query(Article).filter(
                    Article.classified_at.isnot(None),
                    Article.scope != "irrelevant",
                    _func.coalesce(Article.published_at, Article.fetched_at) >= cutoff,
                ).order_by(
                    Article.relevance.desc(),
                    _func.coalesce(Article.published_at, Article.fetched_at).desc(),
                ).limit(60).all()
                articles = [_article_db_to_dict(a) for a in rows]
        except Exception as exc:
            log.error("_generate_news_digest DB-feil: %s", exc)
            with _articles_lock:
                articles = [
                    a for a in _articles.values()
                    if a.get("scope") in ("cermaq", "industry")
                    and _parse_dt(a.get("published_at")) >= cutoff
                ][:60]
    else:
        with _articles_lock:
            articles = [
                a for a in _articles.values()
                if a.get("scope") in ("cermaq", "industry")
                and _parse_dt(a.get("published_at")) >= cutoff
            ][:60]

    if len(articles) < 3:
        log.warning("For få artikler (%d) for digest — hopper over", len(articles))
        return

    log.info("Digest grunnlag: %d artikler", len(articles))

    # Step 1: Norwegian master
    norsk_content = generate_norwegian_master_digest(articles)
    if not norsk_content:
        log.error("Norsk master-digest generering feilet")
        return

    _save_digest("no", norsk_content)

    # Step 2: Translations
    for target_lang in ("en", "es", "ja"):
        try:
            translated = translate_digest(norsk_content, target_lang)
            if translated:
                _save_digest(target_lang, translated)
        except Exception as exc:
            log.error("Oversettelse til %s feilet: %s", target_lang, exc)

    # Ingest source URLs as articles
    all_source_urls = {
        src.get("url") if isinstance(src, dict) else src
        for src in norsk_content.get("sources", [])
        if (src.get("url") if isinstance(src, dict) else src)
    }
    if all_source_urls:
        added = _ingest_urls_as_articles(list(all_source_urls))
        log.info("Digest la til %d nye artikler fra web-søk", added)

    log.info("Digest-generering ferdig")


def _next_digest_time() -> datetime:
    """Return next scheduled digest time: 08:00 or 12:00 Oslo time."""
    oslo_tz = ZoneInfo("Europe/Oslo")
    now = datetime.now(oslo_tz)
    for hour in (8, 12):
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > now:
            return candidate
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=8, minute=0, second=0, microsecond=0)


def _digest_scheduler() -> None:
    """Regenerate news digest at 08:00 and 12:00 Oslo time."""
    oslo_tz = ZoneInfo("Europe/Oslo")
    while True:
        try:
            target = _next_digest_time()
            wait = max(0, (target - datetime.now(oslo_tz)).total_seconds())
            log.info("Neste digest: %s (om %.1f timer)", target.isoformat(), wait / 3600)
            time.sleep(wait)
            _generate_news_digest()
        except Exception as exc:
            log.error("Digest-scheduler-feil: %s", exc)
            time.sleep(3600)


def _initial_digest() -> None:
    """Generate digest once at startup if cache is empty."""
    time.sleep(60)
    with _digest_lock:
        has_digest = len(_digest_cache) > 0
    if not has_digest:
        _generate_news_digest()


def get_digest(lang: str = "no") -> dict | None:
    with _digest_lock:
        return _digest_cache.get(lang)


# ---------------------------------------------------------------------------
# Finance digest
# ---------------------------------------------------------------------------

_finance_digest_cache: dict = {}  # lang -> {'content': ..., 'generated_at': ..., 'rates': ..., 'stocks': ...}
_finance_digest_lock = threading.Lock()


def _send_slack_notification(message: str) -> None:
    """Post a message to Slack via incoming webhook if SLACK_WEBHOOK_URL is set."""
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return
    try:
        import requests as _req
        _req.post(url, json={"text": message}, timeout=5)
        log.info("Slack-varsling sendt")
    except Exception as exc:
        log.warning("Slack-varsling feilet: %s", exc)


def _build_finance_context() -> dict:
    """Collect all live finance data for enriched digest prompt. Returns context dict."""
    from poc.finance import fetch_norges_bank_rates, fetch_all_stocks, fetch_commodities

    rates = fetch_norges_bank_rates() or {}
    stocks = fetch_all_stocks() or {}
    commodities = fetch_commodities() or {}

    # Latest salmon prices from DB (most recent record per source)
    salmon: dict = {}
    if is_db_available():
        try:
            with SessionLocal() as session:
                for src in ("fish_pool", "nasdaq"):
                    row = (
                        session.query(SalmonPrice)
                        .filter_by(source=src)
                        .order_by(SalmonPrice.fetched_at.desc())
                        .first()
                    )
                    if row:
                        salmon[src] = row.price_data
        except Exception as exc:
            log.warning("_build_finance_context: salmon DB feil: %s", exc)

    # Recent Cermaq + industry articles (last 48 h)
    recent_articles: list = []
    if is_db_available():
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
            from sqlalchemy import func as _func
            with SessionLocal() as session:
                rows = (
                    session.query(Article)
                    .filter(
                        Article.classified_at.isnot(None),
                        Article.scope.in_(("cermaq", "industry")),
                        _func.coalesce(Article.published_at, Article.fetched_at) >= cutoff,
                    )
                    .order_by(
                        Article.relevance.desc(),
                        _func.coalesce(Article.published_at, Article.fetched_at).desc(),
                    )
                    .limit(12)
                    .all()
                )
                for a in rows:
                    summary = (a.summaries or {}).get("no") or (a.summaries or {}).get("en") or ""
                    recent_articles.append({
                        "title": a.title,
                        "scope": a.scope,
                        "tone": a.tone,
                        "source": a.source_name,
                        "summary": summary[:200],
                    })
        except Exception as exc:
            log.warning("_build_finance_context: article DB feil: %s", exc)

    return {
        "rates": rates,
        "stocks": stocks,
        "commodities": commodities,
        "salmon": salmon,
        "articles": recent_articles,
    }


def _format_finance_context_no(ctx: dict) -> str:
    """Format finance context as Norwegian text block for AI prompt."""
    parts = []

    if ctx.get("rates"):
        lines = [
            f"  {cur}: {v['rate']} NOK (endring: {v.get('change_pct', 'N/A')}%)"
            for cur, v in ctx["rates"].items()
        ]
        parts.append("Valutakurser (NOK):\n" + "\n".join(lines))

    if ctx.get("salmon"):
        salmon_lines = []
        fp = ctx["salmon"].get("fish_pool")
        if fp and fp.get("forward_prices"):
            p0 = fp["forward_prices"][0]
            salmon_lines.append(f"  Fish Pool nærmeste termin ({p0.get('period','')}): {p0.get('price','?')} NOK/kg")
        ns = ctx["salmon"].get("nasdaq")
        if ns and ns.get("spot_price"):
            salmon_lines.append(f"  Nasdaq Salmon spot: {ns['spot_price']} NOK/kg")
        if salmon_lines:
            parts.append("Lakseprisindekser:\n" + "\n".join(salmon_lines))

    if ctx.get("commodities"):
        lines = [
            f"  {name}: {v.get('price','?')} {v.get('commodity_currency','')} (1d: {v.get('change_1d','?')}%)"
            for name, v in ctx["commodities"].items()
        ]
        parts.append("Råvarer (fôrinput):\n" + "\n".join(lines))

    if ctx.get("stocks"):
        lines = [
            f"  {name}: {v.get('price','?')} NOK (1d: {v.get('change_1d','?')}%, 30d: {v.get('change_30d','?')}%)"
            for name, v in ctx["stocks"].items()
        ]
        parts.append("Konkurrentaksjer:\n" + "\n".join(lines))

    if ctx.get("articles"):
        news_lines = []
        for a in ctx["articles"][:8]:
            scope_tag = "[CERMAQ]" if a["scope"] == "cermaq" else "[Bransje]"
            tone_tag = f"[{a.get('tone','?')}]"
            news_lines.append(f"  {scope_tag}{tone_tag} {a['title'][:120]}")
        parts.append("Siste nyheter (48t):\n" + "\n".join(news_lines))

    return "\n\n".join(parts)


def _generate_finance_digest_all() -> None:
    """Fetch all finance data and generate enriched AI digest for all languages."""
    import anthropic as _anthropic

    ctx = _build_finance_context()
    rates = ctx["rates"]
    stocks = ctx["stocks"]

    if not rates and not stocks:
        log.warning("Finance digest: ingen data tilgjengelig")
        return

    context_no = _format_finance_context_no(ctx)

    system_instruction = (
        "Du er senioranalytiker for Cermaq ASA, et globalt lakseoppdrettsselskap eid av Mitsubishi "
        "Corporation med virksomhet i Norge, Chile og Canada. Du skriver en daglig markedsbrief til "
        "Cermaqs ledergruppe. Analysen skal:\n"
        "1. Binde sammen laksepris, valuta, råvarer og bransjenytt til ett helhetlig bilde\n"
        "2. Konkretisere konsekvenser for Cermaqs tre regioner (Norge, Chile, Canada)\n"
        "3. Fremheve det viktigste Cermaq må følge med på de neste 24 timene\n"
        "Vær presis, analytisk og handlingsorientert. IKKE gjenta tallene fra dataene — tolke dem. "
        "Maks 6 setninger."
    )

    prompts = {
        "no": f"{system_instruction}\n\nMarkedsdata:\n{context_no}",
        "en": (
            "You are a senior analyst for Cermaq ASA, a global salmon farming company owned by Mitsubishi "
            "Corporation with operations in Norway, Chile and Canada. Write a daily market brief (max 6 sentences) "
            "for Cermaq's leadership team that: 1) synthesises salmon price, FX, feed commodities and industry news; "
            "2) spells out consequences for Cermaq's three regions; 3) highlights the single most important "
            "development to monitor in the next 24 hours. Do NOT repeat the numbers — interpret them.\n\n"
            f"Market data:\n{context_no}"
        ),
        "es": (
            "Eres analista senior de Cermaq ASA, una empresa global de acuicultura de salmón propiedad de "
            "Mitsubishi Corporation con operaciones en Noruega, Chile y Canadá. Escribe un briefing diario de "
            "mercado (máx. 6 oraciones) para el equipo directivo de Cermaq que: 1) sintetice precio del salmón, "
            "divisas, materias primas y noticias del sector; 2) explique consecuencias para las tres regiones de "
            "Cermaq; 3) destaque el desarrollo más importante a vigilar en las próximas 24 horas. NO repitas los "
            "números — interprételos.\n\n"
            f"Datos de mercado:\n{context_no}"
        ),
        "ja": (
            "あなたはCermaq ASAのシニアアナリストです。CermaqはMitsubishi Corporationが所有するグローバルなサーモン養殖企業で、"
            "ノルウェー、チリ、カナダで事業を展開しています。Cermaqの経営陣向けに日次市場ブリーフ（最大6文）を作成してください。"
            "内容：1）サーモン価格・為替・飼料原料・業界ニュースを統合、2）Cermaqの3地域への影響を明示、"
            "3）今後24時間で最も重要な動向を強調。数字を繰り返すのではなく、解釈してください。\n\n"
            f"市場データ:\n{context_no}"
        ),
    }

    try:
        client = _anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    except Exception as exc:
        log.error("Finance digest: Anthropic-klient feilet: %s", exc)
        return

    now_iso = datetime.now(timezone.utc).isoformat()
    for lang, prompt in prompts.items():
        try:
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=600,
                temperature=0.2,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
            entry = {
                "content": text,
                "generated_at": now_iso,
                "rates": rates,
                "stocks": stocks,
                "lang": lang,
            }
            with _finance_digest_lock:
                _finance_digest_cache[lang] = entry

            if is_db_available():
                try:
                    with SessionLocal() as session:
                        fd = FinanceDigest(
                            lang=lang,
                            content=text,
                            generated_at=datetime.now(timezone.utc),
                            rates_snapshot=rates,
                            stocks_snapshot=stocks,
                        )
                        session.add(fd)
                        session.commit()
                except Exception as db_exc:
                    log.warning("Finance digest DB-persist feilet lang=%s: %s", lang, db_exc)

            log.info("Finance digest klar lang=%s", lang)
        except Exception as exc:
            log.error("Finance digest feilet lang=%s: %s", lang, exc)


# ---------------------------------------------------------------------------
# Salmon price scheduler
# ---------------------------------------------------------------------------

def _fetch_and_persist_salmon_prices() -> None:
    """Fetch all salmon price indices, persist to DB, then run anomaly detection."""
    from poc.finance import fetch_all_salmon_prices, fetch_norges_bank_rates, detect_anomalies
    prices = fetch_all_salmon_prices()
    if not is_db_available():
        return
    now = datetime.now(timezone.utc)
    for source_key, data in prices.items():
        if data is None:
            continue
        try:
            with SessionLocal() as session:
                row = SalmonPrice(source=source_key, price_data=data, fetched_at=now)
                session.add(row)
                session.commit()
            log.info("SalmonPrice persistert: source=%s", source_key)
        except Exception as exc:
            log.warning("SalmonPrice persist feilet source=%s: %s", source_key, exc)

    # Anomaly detection after each price fetch
    try:
        latest_rates = fetch_norges_bank_rates()
        latest_salmon = prices.get("nasdaq")

        # Fetch 30-day salmon history for sigma calculation
        history_salmon = []
        if is_db_available():
            cutoff30 = now - timedelta(days=30)
            with SessionLocal() as session:
                rows = (
                    session.query(SalmonPrice)
                    .filter(SalmonPrice.source == "nasdaq", SalmonPrice.fetched_at >= cutoff30)
                    .order_by(SalmonPrice.fetched_at)
                    .all()
                )
                history_salmon = [r.price_data for r in rows if r.price_data]

        anomalies = detect_anomalies(
            latest_rates=latest_rates,
            latest_salmon=latest_salmon,
            history_salmon=history_salmon,
        )
        for anomaly in anomalies:
            if anomaly.get("severity") in ("high", "medium"):
                msg = (
                    f"⚠️ *Cermaq Watch Avvik*: {anomaly['description']} "
                    f"(z={anomaly.get('z_score', 'N/A')}, verdi={anomaly['value']})"
                )
                log.warning("ANOMALI DETEKTERT: %s", anomaly["description"])
                _send_slack_notification(msg)
    except Exception as exc:
        log.warning("Anomali-deteksjon feilet: %s", exc)


def _next_salmon_fetch_time() -> datetime:
    """Return next scheduled salmon price fetch time.

    Fish Pool publishes Tuesdays 15:00 CET — we poll at 16:00.
    Nasdaq Salmon publishes Fridays — we poll at 17:00.
    Fall back to next weekday at 16:00 if neither applies.
    """
    oslo = ZoneInfo("Europe/Oslo")
    now = datetime.now(oslo)
    candidates = []
    for days_ahead in range(8):
        candidate = (now + timedelta(days=days_ahead)).replace(
            minute=0, second=0, microsecond=0
        )
        weekday = candidate.weekday()  # 0=Mon, 1=Tue, …, 4=Fri
        if weekday == 1:  # Tuesday
            t = candidate.replace(hour=16)
        elif weekday == 4:  # Friday
            t = candidate.replace(hour=17)
        else:
            continue
        if t > now:
            candidates.append(t)
    if candidates:
        return min(candidates)
    # Fallback: 24 hours from now
    return now + timedelta(hours=24)


def _salmon_price_scheduler() -> None:
    """Fetch salmon prices on Tuesday 16:00 and Friday 17:00 Oslo time."""
    # Run once shortly after startup so the UI has data immediately
    time.sleep(90)
    _fetch_and_persist_salmon_prices()

    while True:
        try:
            target = _next_salmon_fetch_time()
            wait = (target - datetime.now(ZoneInfo("Europe/Oslo"))).total_seconds()
            log.info("Neste lakseprisfetch: %s (om %.1f timer)", target.isoformat(), wait / 3600)
            time.sleep(max(wait, 60))
            _fetch_and_persist_salmon_prices()
        except Exception as exc:
            log.error("Salmon-price-scheduler-feil: %s", exc)
            time.sleep(3600)


# ---------------------------------------------------------------------------
# Backfill via Anthropic web search
# ---------------------------------------------------------------------------

def _run_backfill(days: int, max_articles: int) -> None:
    """Search for Cermaq articles via web search and add them to the pipeline."""
    import re
    import json
    import hashlib
    from urllib.parse import urlparse
    import anthropic as _anthropic

    client = _anthropic.Anthropic()

    queries = [
        ("site:ilaks.no Cermaq", "iLaks", "ilaks.no"),
        ("site:intrafish.no Cermaq", "IntraFish", "intrafish.no"),
        ("site:salmonbusiness.com Cermaq", "SalmonBusiness", "salmonbusiness.com"),
        ("site:fishfarmingexpert.com Cermaq", "Fish Farming Expert", "fishfarmingexpert.com"),
        ("Cermaq Norway news 2026", "various", None),
        ("Cermaq Chile noticias 2026", "various", None),
    ]

    found_urls: set = set()
    new_articles: list = []

    for query, source_hint, _domain in queries:
        if len(new_articles) >= max_articles:
            break
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=4000,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
                messages=[{
                    "role": "user",
                    "content": (
                        f"Search the web for: {query}\n\n"
                        f"Find Cermaq-related articles published in the last {days} days. "
                        f"Return ONLY a JSON array:\n"
                        f'[{{"url": "...", "title": "...", "published_at": "YYYY-MM-DD", "summary": "brief summary"}}]\n\n'
                        f"No prose, just JSON. Maximum 10 articles."
                    ),
                }],
            )
            for block in response.content:
                if getattr(block, "type", "") == "text":
                    text = block.text.strip()
                    match = re.search(r"\[\s*\{.*?\}\s*\]", text, re.DOTALL)
                    if match:
                        try:
                            articles = json.loads(match.group(0))
                            for art in articles:
                                url = art.get("url", "")
                                if url and url not in found_urls:
                                    found_urls.add(url)
                                    new_articles.append({**art, "source_hint": source_hint})
                        except json.JSONDecodeError:
                            log.warning("Backfill: JSON-parse feilet for søk '%s'", query)
        except Exception as exc:
            log.error("Backfill-søk feilet for '%s': %s", query, exc)

    log.info("Backfill fant %d unike artikler", len(new_articles))

    added = 0
    for art in new_articles[:max_articles]:
        url = art.get("url", "")
        if not url:
            continue

        if is_db_available():
            try:
                with SessionLocal() as session:
                    if session.query(Article).filter_by(url=url).first():
                        continue
            except Exception:
                pass

        article_id = int(hashlib.md5(url.encode()).hexdigest()[:8], 16)
        domain = urlparse(url).netloc.replace("www.", "")

        article = {
            "id": article_id,
            "title": art.get("title", ""),
            "url": url,
            "source_name": art.get("source_hint", domain.split(".")[0]),
            "source_domain": domain,
            "fetch_method": "backfill",
            "published_at": art.get("published_at"),
            "content": art.get("summary", ""),
            "image_url": None,
        }

        with _articles_lock:
            if article_id not in _articles:
                _articles[article_id] = article
                _classify_queue.put(article_id)
                added += 1

    log.info("Backfill: %d nye artikler lagt til klassifiseringskøen", added)


# ---------------------------------------------------------------------------
# Background thread 3 — OG-image backfill (runs once at startup)
# ---------------------------------------------------------------------------

def _backfill_og_images() -> None:
    """Wait for the first fetch+classify pass, then fill missing OG images."""
    deadline = time.monotonic() + 300
    while _fetched_at is None and time.monotonic() < deadline:
        time.sleep(2)
    if _fetched_at is None:
        log.warning("Backfill: timeout waiting for first fetch, skipping")
        return
    time.sleep(15)

    with _articles_lock:
        targets = [
            (a["id"], a["url"]) for a in _articles.values()
            if a.get("scope") in ("cermaq", "industry") and not a.get("image_url")
        ]

    if not targets:
        log.info("Backfill: ingen artikler trenger OG-image")
        return

    log.info("Backfiller OG-image for %d artikler", len(targets))
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda t: _fetch_og_image(t[1]), targets))

    success = 0
    with _articles_lock:
        for (article_id, _), og_image in zip(targets, results):
            if og_image and article_id in _articles:
                _articles[article_id]["image_url"] = og_image
                success += 1
    log.info("Backfill ferdig: %d av %d fikk bilde", success, len(targets))


# ---------------------------------------------------------------------------
# Startup: kick off background threads
# ---------------------------------------------------------------------------

def _start_background_threads() -> None:
    for target, name in [
        (_fetch_loop, "fetch-thread"),
        (_classify_loop, "classify-thread"),
        (_backfill_og_images, "og-backfill-thread"),
        (_digest_scheduler, "digest-scheduler-thread"),
        (_initial_digest, "digest-initial-thread"),
        # (_salmon_price_scheduler, "salmon-price-thread"),  # paused — trigger manually via admin
    ]:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        log.info("Started %s", name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_articles() -> tuple[list[dict], str | None]:
    global _fetched_at, _fetch_error

    if _fetched_at is None:
        deadline = time.monotonic() + 5
        while _fetched_at is None and time.monotonic() < deadline:
            time.sleep(0.1)

    with _articles_lock:
        snapshot = list(_articles.values())
        error = _fetch_error

    return snapshot, error


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        months = [
            "januar", "februar", "mars", "april", "mai", "juni",
            "juli", "august", "september", "oktober", "november", "desember",
        ]
        return f"{dt.day}. {months[dt.month - 1]} {dt.year}, {dt.strftime('%H:%M')}"
    except ValueError:
        return iso


def _sort_key(article: dict):
    rel = -(article.get("relevance") or 1)
    pub = article.get("published_at") or ""
    try:
        ts = -datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
    except Exception:
        ts = 0.0
    return (rel, ts)


def _check_admin_token() -> bool:
    token = request.args.get("token")
    return token == os.environ.get("ADMIN_TOKEN") and token is not None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.before_request
def _log_request():
    log.info("%s %s", request.method, request.path)


def _today_no() -> str:
    dt = datetime.now()
    days = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
    months = [
        "januar", "februar", "mars", "april", "mai", "juni",
        "juli", "august", "september", "oktober", "november", "desember",
    ]
    return f"{days[dt.weekday()]} {dt.day}. {months[dt.month - 1]} {dt.year}"


@app.template_filter("fmt_dt_friendly")
def fmt_dt_friendly(iso_str: str | None, lang: str = "no") -> str:
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        oslo = dt.astimezone(ZoneInfo("Europe/Oslo"))
        if lang == "ja":
            return oslo.strftime("%Y年%m月%d日 %H:%M")
        elif lang == "es":
            return oslo.strftime("%d/%m/%Y %H:%M")
        elif lang == "en":
            return oslo.strftime("%b %d, %Y %H:%M")
        else:
            return oslo.strftime("%d. %b %Y %H:%M")
    except Exception:
        return iso_str or ""


@app.route("/")
def index():
    lang = request.args.get("lang", "no")
    if lang not in ("no", "en", "es", "ja"):
        lang = "no"

    theme = request.args.get("theme", "").strip() or None
    region = request.args.get("region", "").strip() or None
    log.info("[index] theme=%r, region=%r, lang=%r", theme, region, lang)

    ui_text = _UI_TEXTS.get(lang, _UI_TEXTS["no"])

    error = None

    if is_db_available():
        try:
            with SessionLocal() as session:
                q = session.query(Article).filter(
                    Article.classified_at.isnot(None),
                    Article.scope != "irrelevant",
                )
                if region:
                    q = q.filter(Article.region == region)
                q = q.order_by(Article.published_at.desc())
                db_rows = q.all()
                log.info("[index] etter SQL-filter: %d artikler", len(db_rows))
                if theme:
                    before = len(db_rows)
                    db_rows = [a for a in db_rows if a.themes and theme in a.themes]
                    log.info("[index] theme-filter '%s': %d -> %d artikler", theme, before, len(db_rows))
                articles = [_article_db_to_dict(a) for a in db_rows]
        except Exception as exc:
            log.error("[index] DB-feil: %s", exc)
            articles, error = _get_articles()
            articles = [a for a in articles if a.get("classified_at") and a.get("scope") != "irrelevant"]
            if region:
                articles = [a for a in articles if a.get("region") == region]
            if theme:
                before = len(articles)
                articles = [a for a in articles if a.get("themes") and theme in a.get("themes")]
                log.info("[index] theme-filter (mem) '%s': %d -> %d artikler", theme, before, len(articles))
    else:
        articles, error = _get_articles()
        articles = [a for a in articles if a.get("classified_at") and a.get("scope") != "irrelevant"]
        if region:
            articles = [a for a in articles if a.get("region") == region]
        if theme:
            before = len(articles)
            articles = [a for a in articles if a.get("themes") and theme in a.get("themes")]
            log.info("[index] theme-filter (mem) '%s': %d -> %d artikler", theme, before, len(articles))

    articles_sorted = sorted(articles, key=_sort_key)
    formatted = [
        {**a, "published_at_iso": a.get("published_at") or "", "published_at": _fmt_dt(a.get("published_at"))}
        for a in articles_sorted
    ]

    for article in formatted:
        if "summaries" not in article:
            fallback = article.get("summary_no", "") or ""
            article["summaries"] = {"no": fallback, "en": fallback, "es": fallback, "ja": fallback}
        else:
            s = article["summaries"]
            fallback = s.get("no", "") or s.get("en", "") or ""
            article["summaries"] = {
                "no": s.get("no", "") or fallback,
                "en": s.get("en", "") or s.get("no", "") or fallback,
                "es": s.get("es", "") or s.get("no", "") or fallback,
                "ja": s.get("ja", "") or s.get("en", "") or fallback,
            }

    generated_at = _fmt_dt(datetime.now(timezone.utc).isoformat())

    with _counters_lock:
        classified = _classified_count
        total = _total_count or len(articles)

    return render_template(
        "index.html",
        articles=formatted,
        error=error,
        generated_at=generated_at,
        today=_today_no(),
        cache_count=total,
        classified_count=classified,
        total_count=total,
        queue_size=_classify_queue.qsize(),
        lang=lang,
        ui_text=ui_text,
    )


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------

@app.route("/admin/status")
def admin_status():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401

    with _articles_lock:
        total = len(_articles)
        scopes = {}
        for a in _articles.values():
            scope = a.get("scope", "unclassified")
            scopes[scope] = scopes.get(scope, 0) + 1
        classified = sum(1 for a in _articles.values() if "scope" in a)

    digest_status = {}
    with _digest_lock:
        for lang, d in _digest_cache.items():
            digest_status[lang] = {
                "headline": (d.get("headline") or "")[:80],
                "generated_at": d.get("generated_at"),
                "search_count": d.get("search_count", 0),
            }

    return jsonify({
        "articles": {"total": total, "classified": classified, "by_scope": scopes},
        "digests": digest_status,
        "postgres_available": is_db_available(),
        "queue_size": _classify_queue.qsize(),
    })


@app.route("/admin/regenerate-digest")
def admin_regenerate_digest():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401

    threading.Thread(target=_generate_news_digest, daemon=True).start()
    return jsonify({"status": "Digest-generering startet"})


@app.route("/admin/reclassify")
def admin_reclassify():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401

    since_str = request.args.get("since")
    do_all = request.args.get("all") == "true"
    missing_only = request.args.get("missing") == "true"

    targets = []
    with _articles_lock:
        for article_id, article in _articles.items():
            if do_all:
                targets.append(article_id)
            elif missing_only and "scope" not in article:
                targets.append(article_id)
            elif since_str:
                try:
                    cutoff = datetime.fromisoformat(since_str).replace(tzinfo=timezone.utc)
                    classified_at_str = article.get("classified_at")
                    if not classified_at_str:
                        targets.append(article_id)
                    else:
                        classified_at = datetime.fromisoformat(classified_at_str)
                        if classified_at < cutoff:
                            targets.append(article_id)
                except ValueError:
                    return jsonify({"error": "Ugyldig dato. Bruk ISO-format f.eks. 2026-05-01"}), 400

    # Strip classification fields so classify loop re-processes them
    with _articles_lock:
        for article_id in targets:
            if article_id in _articles:
                for key in ("scope", "region", "tone", "category", "relevance", "summaries", "classified_at"):
                    _articles[article_id].pop(key, None)

    for article_id in targets:
        _classify_queue.put(article_id)

    log.info("Reklassifiserer %d artikler", len(targets))
    return jsonify({
        "queued": len(targets),
        "criteria": {"since": since_str, "all": do_all, "missing_only": missing_only},
    })


@app.route("/admin/reclassify-all", methods=["POST"])
def admin_reclassify_all():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401

    def _run_reclassify():
        from poc.classify import classify as _classify
        from sqlalchemy import desc, func

        if not is_db_available():
            log.warning("Reklassifisering: ingen database tilgjengelig")
            return

        try:
            with SessionLocal() as session:
                articles = session.query(Article).order_by(
                    desc(func.coalesce(Article.published_at, Article.fetched_at))
                ).all()
                total = len(articles)
                log.info("Starter reklassifisering av %d artikler", total)

                for a in articles:
                    a.classified_at = None
                session.commit()

            for i, db_art in enumerate(articles):
                try:
                    art_dict = {
                        "id": db_art.id,
                        "title": db_art.title or "",
                        "url": db_art.url or "",
                        "content": db_art.content or "",
                        "source_name": db_art.source_name or "",
                    }
                    result = _classify(art_dict)

                    with SessionLocal() as session:
                        a = session.get(Article, db_art.id)
                        if a:
                            a.scope = result.get("scope")
                            a.region = result.get("region")
                            a.tone = result.get("tone")
                            a.category = result.get("category")
                            a.themes = result.get("themes", [])
                            a.relevance = result.get("relevance")
                            a.summaries = result.get("summaries") or a.summaries
                            a.titles = result.get("titles") or a.titles
                            a.classified_at = datetime.now(timezone.utc)
                            session.commit()

                    with _articles_lock:
                        if db_art.id in _articles:
                            _articles[db_art.id].update({
                                "scope": result.get("scope"),
                                "region": result.get("region"),
                                "tone": result.get("tone"),
                                "category": result.get("category"),
                                "themes": result.get("themes", []),
                                "relevance": result.get("relevance"),
                                "classified_at": datetime.now(timezone.utc).isoformat(),
                            })

                    if (i + 1) % 10 == 0:
                        log.info("Reklassifisert %d/%d artikler", i + 1, total)

                    time.sleep(_CLASSIFY_RATE)

                except Exception as exc:
                    log.error("Reklassifisering feilet for id=%s: %s", db_art.id, exc)

            log.info("Reklassifisering ferdig: %d artikler", total)
        except Exception as exc:
            log.exception("Reklassifisering feilet: %s", exc)

    threading.Thread(target=_run_reclassify, daemon=True).start()
    return jsonify({
        "status": "Reklassifisering startet",
        "note": "Tar 5-10 minutter. Nyeste artikler reklassifiseres først.",
    })


@app.route("/admin/reclassify-backfill", methods=["POST"])
def admin_reclassify_backfill():
    """Reklassifiser artikler fra siste N dager."""
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401

    try:
        days = int(request.form.get("days") or request.args.get("days") or 3)
    except (ValueError, TypeError):
        return jsonify({"error": "days må være et tall"}), 400

    if days < 1 or days > 90:
        return jsonify({"error": "days må være mellom 1 og 90"}), 400

    if not is_db_available():
        return jsonify({"error": "Ingen database tilgjengelig"}), 503

    from sqlalchemy import func as _func

    window_start = datetime.now(timezone.utc) - timedelta(days=days)

    with SessionLocal() as session:
        article_count = session.query(Article).filter(
            _func.coalesce(Article.published_at, Article.fetched_at) >= window_start
        ).count()

    estimated_minutes = round(article_count * _CLASSIFY_RATE / 60, 1)
    estimated_cost = round(article_count * 0.025, 2)

    def _run():
        from poc.classify import classify as _classify
        from sqlalchemy import func as _func2

        window = datetime.now(timezone.utc) - timedelta(days=days)
        update_module_status("reclassify_backfill", "running", f"0/? ferdig (siste {days} dager)")

        try:
            with SessionLocal() as session:
                articles = session.query(Article).filter(
                    _func2.coalesce(Article.published_at, Article.fetched_at) >= window
                ).order_by(
                    _func2.coalesce(Article.published_at, Article.fetched_at).desc()
                ).all()
                total = len(articles)
                log.info("Backfill-reklassifisering: %d artikler fra siste %d dager", total, days)
                for a in articles:
                    a.classified_at = None
                session.commit()

            processed = 0
            failed = 0
            for db_art in articles:
                try:
                    art_dict = {
                        "id": db_art.id,
                        "title": db_art.title or "",
                        "url": db_art.url or "",
                        "content": db_art.content or "",
                        "source_name": db_art.source_name or "",
                    }
                    result = _classify(art_dict)

                    with SessionLocal() as session:
                        a = session.get(Article, db_art.id)
                        if a:
                            a.scope = result.get("scope")
                            a.region = result.get("region")
                            a.tone = result.get("tone")
                            a.category = result.get("category")
                            a.themes = result.get("themes", [])
                            a.relevance = result.get("relevance")
                            a.summaries = result.get("summaries") or a.summaries
                            a.titles = result.get("titles") or a.titles
                            a.classified_at = datetime.now(timezone.utc)
                            session.commit()

                    with _articles_lock:
                        if db_art.id in _articles:
                            _articles[db_art.id].update({
                                "scope": result.get("scope"),
                                "region": result.get("region"),
                                "tone": result.get("tone"),
                                "category": result.get("category"),
                                "themes": result.get("themes", []),
                                "relevance": result.get("relevance"),
                                "classified_at": datetime.now(timezone.utc).isoformat(),
                            })

                    processed += 1
                    if processed % 10 == 0:
                        log.info("Backfill: %d/%d reklassifisert", processed, total)
                        update_module_status(
                            "reclassify_backfill", "running",
                            f"{processed}/{total} ferdig",
                            {"days": days, "processed": processed, "total": total},
                        )

                    time.sleep(_CLASSIFY_RATE)

                except Exception as exc:
                    failed += 1
                    log.error("Backfill reklassifisering feilet id=%s: %s", db_art.id, exc)

            summary = f"Backfill ferdig: {processed} reklassifisert, {failed} feilet (siste {days} dager)"
            log.info(summary)
            update_module_status(
                "reclassify_backfill",
                "ok" if failed < max(1, total // 2) else "error",
                summary,
                {"days": days, "processed": processed, "failed": failed, "total": total},
            )

        except Exception as exc:
            log.exception("Backfill-reklassifisering feilet: %s", exc)
            update_module_status("reclassify_backfill", "error", str(exc))

    threading.Thread(target=_run, daemon=True, name="reclassify-backfill-thread").start()
    return jsonify({
        "status": "started",
        "days": days,
        "article_count": article_count,
        "estimated_minutes": estimated_minutes,
        "estimated_cost_usd": estimated_cost,
        "message": (
            f"Reklassifisering startet for {article_count} artikler. "
            f"Estimert tid: {estimated_minutes} min, kostnad: ~${estimated_cost}"
        ),
    })


@app.route("/admin/migrate-tone")
def admin_migrate_tone():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401

    if not is_db_available():
        return jsonify({"error": "Database utilgjengelig"}), 503

    if _sa_text is None:
        return jsonify({"error": "SQLAlchemy text not available"}), 500

    with SessionLocal() as session:
        result = session.execute(
            _sa_text("UPDATE articles SET tone='negativ' WHERE tone='kritisk'")
        )
        session.commit()
        affected = result.rowcount

    with _articles_lock:
        for a in _articles.values():
            if a.get("tone") == "kritisk":
                a["tone"] = "negativ"

    log.info("Tone-migrering: %d artikler oppdatert fra 'kritisk' til 'negativ'", affected)
    return jsonify({"migrated": affected, "status": "ok"})


@app.route("/admin/fix-source-names")
def admin_fix_source_names():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401
    if not is_db_available():
        return jsonify({"error": "Database utilgjengelig"}), 503

    fixed = 0
    with SessionLocal() as session:
        articles = session.query(Article).all()
        for a in articles:
            url = (a.url or "").lower()
            name = (a.source_name or "").lower()
            if "linkedin.com" in url and "linkedin" not in name:
                a.source_name = "LinkedIn"
                a.source_domain = "linkedin.com"
                fixed += 1
            elif "twitter.com" in url or "x.com" in url:
                if "twitter" not in name and "x.com" not in name:
                    a.source_name = "X / Twitter"
                    a.source_domain = "x.com"
                    fixed += 1
            elif "facebook.com" in url and "facebook" not in name:
                a.source_name = "Facebook"
                a.source_domain = "facebook.com"
                fixed += 1
        session.commit()

    with _articles_lock:
        for art in list(_articles.values()):
            url = (art.get("url") or "").lower()
            name = (art.get("source_name") or "").lower()
            if "linkedin.com" in url and "linkedin" not in name:
                art["source_name"] = "LinkedIn"
            elif ("twitter.com" in url or "x.com" in url) and "twitter" not in name:
                art["source_name"] = "X / Twitter"
            elif "facebook.com" in url and "facebook" not in name:
                art["source_name"] = "Facebook"

    return jsonify({"fixed": fixed, "status": "ok"})


@app.route("/admin/search-articles")
def admin_search_articles():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401
    if not is_db_available():
        return jsonify({"error": "Database utilgjengelig"}), 503

    q = request.args.get("q", "")
    with SessionLocal() as session:
        from sqlalchemy import text as _text
        rows = session.execute(
            _text("SELECT id, title, source_name, url FROM articles WHERE url ILIKE :q OR title ILIKE :q LIMIT 20"),
            {"q": f"%{q}%"}
        ).fetchall()
    return jsonify([{"id": r[0], "title": r[1], "source_name": r[2], "url": r[3]} for r in rows])


@app.route("/admin/")
def admin_page():
    return render_template("admin.html")


@app.route("/admin/backfill", methods=["POST"])
def admin_backfill():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401

    days = int(request.args.get("days", 7))
    max_articles = int(request.args.get("max", 30))

    threading.Thread(target=_run_backfill, args=(days, max_articles), daemon=True).start()

    return jsonify({
        "status": "Backfill started",
        "days": days,
        "max_articles": max_articles,
        "note": "Sjekk Railway-logg eller hovedsiden for fremdrift",
    })


@app.route("/admin/add-article", methods=["POST"])
def admin_add_article():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json() or {}
    url = (data.get("url") or "").strip()

    if not url or not url.startswith("http"):
        return jsonify({"error": "Valid URL required"}), 400

    if is_db_available():
        try:
            with SessionLocal() as session:
                existing = session.query(Article).filter_by(url=url).first()
                if existing:
                    return jsonify({"error": "Article already exists", "id": existing.id}), 409
        except Exception:
            pass

    meta = _fetch_article_metadata(url)
    if not meta["title"] and not meta["content"]:
        return jsonify({"error": f"Could not fetch URL"}), 500

    article_id = int(hashlib.md5(url.encode()).hexdigest()[:8], 16)
    domain = urlparse(url).netloc.replace("www.", "")
    title = meta["title"] or url

    article = {
        "id": article_id,
        "title": title,
        "url": url,
        "source_name": data.get("source_name") or domain.split(".")[0],
        "source_domain": domain,
        "fetch_method": "manual",
        "published_at": data.get("published_at"),
        "content": meta["content"],
        "image_url": meta["image_url"],
    }

    with _articles_lock:
        _articles[article_id] = article
    _classify_queue.put(article_id)

    return jsonify({
        "id": article_id,
        "title": title,
        "url": url,
        "source": article["source_name"],
        "status": "Added and queued for classification",
    })


@app.route("/admin/run-websearch", methods=["POST"])
def admin_run_websearch():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401

    def _run():
        try:
            import anthropic as _anthropic
            import json as _json
            client = _anthropic.Anthropic()
            queries = [
                "Cermaq",
                "Cermaq Norway",
                "Cermaq Chile",
                "Cermaq Canada",
                "Steven Rafferty Cermaq",
                "Mitsubishi Cermaq",
                "True Arctic Cermaq",
            ]
            all_urls: set = set()
            for query in queries:
                try:
                    resp = client.messages.create(
                        model="claude-sonnet-4-6",
                        max_tokens=2000,
                        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}],
                        messages=[{"role": "user", "content": (
                            f"Search the web for: {query}\n\n"
                            f"Find recent articles about Cermaq from the last 7 days. "
                            f"Return ONLY a JSON array of URLs:\n"
                            f'["url1", "url2", ...]\n\nNo prose, just JSON. Maximum 10 URLs.'
                        )}],
                    )
                    for block in resp.content:
                        if getattr(block, "type", "") == "text":
                            m = re.search(r'\[\s*"[^"]+"(?:\s*,\s*"[^"]+")*\s*\]', block.text)
                            if m:
                                try:
                                    all_urls.update(_json.loads(m.group(0)))
                                except _json.JSONDecodeError:
                                    pass
                except Exception as exc:
                    log.error("Web-søk feilet for '%s': %s", query, exc)

            added = _ingest_urls_as_articles(list(all_urls))
            log.info("Manuelt web-søk: fant %d URL-er, la til %d nye artikler", len(all_urls), added)
        except Exception as exc:
            log.error("Web-søk-trigger feilet: %s", exc)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "Web-søk startet", "note": "Sjekk Railway-logg eller hovedsiden om 1–3 minutter"})


# ---------------------------------------------------------------------------
# Admin module triggers
# ---------------------------------------------------------------------------

def _run_async(module_key: str, target_fn, *args) -> None:
    """Run target_fn in a daemon thread, updating ModuleStatus before and after."""
    def _wrapper():
        update_module_status(module_key, "running", "Startet")
        try:
            target_fn(*args)
            update_module_status(module_key, "ok", "Fullført")
        except Exception as exc:
            update_module_status(module_key, "error", str(exc))
    threading.Thread(target=_wrapper, daemon=True, name=f"trigger-{module_key}").start()


@app.route("/admin/trigger/news-digest", methods=["POST"])
def admin_trigger_news_digest():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401
    _run_async("news_digest", _generate_news_digest)
    return jsonify({"status": "startet", "module": "news_digest"})


@app.route("/admin/trigger/finance-digest", methods=["POST"])
def admin_trigger_finance_digest():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401
    _run_async("finance_digest", _generate_finance_digest_all)
    return jsonify({"status": "startet", "module": "finance_digest"})


@app.route("/admin/trigger/salmon-prices", methods=["POST"])
def admin_trigger_salmon_prices():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401
    _run_async("salmon_prices", _fetch_and_persist_salmon_prices)
    return jsonify({"status": "startet", "module": "salmon_prices"})


def _fetch_rates_and_stocks() -> None:
    """Refresh exchange rates and stock quotes into the finance cache."""
    from poc.finance import fetch_norges_bank_rates, fetch_all_stocks, fetch_commodities
    rates = fetch_norges_bank_rates() or {}
    stocks = fetch_all_stocks() or {}
    commodities = fetch_commodities() or {}
    entry = {
        "rates": rates,
        "stocks": stocks,
        "commodities": commodities,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    with _finance_digest_lock:
        _finance_digest_cache["_rates_stocks"] = entry
    log.info("Rates+stocks refreshet: %d kurser, %d aksjer", len(rates), len(stocks))


@app.route("/admin/trigger/rates-stocks", methods=["POST"])
def admin_trigger_rates_stocks():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401
    _run_async("rates_stocks", _fetch_rates_and_stocks)
    return jsonify({"status": "startet", "module": "rates_stocks"})


@app.route("/admin/trigger/calendar", methods=["POST"])
def admin_trigger_calendar():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401
    _run_async("calendar", seed_calendar_events)
    return jsonify({"status": "startet", "module": "calendar"})


@app.route("/admin/module-status")
def admin_module_status():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401
    modules = {}
    if is_db_available():
        try:
            with SessionLocal() as session:
                rows = session.query(ModuleStatus).all()
                for row in rows:
                    modules[row.module_key] = {
                        "status": row.status,
                        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
                        "last_message": row.last_message,
                        "data_summary": row.data_summary,
                        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                    }
        except Exception as exc:
            log.warning("admin_module_status feilet: %s", exc)
    return jsonify({"modules": modules})


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.route("/api/feed")
def api_feed():
    articles, error = _get_articles()
    with _counters_lock:
        classified = _classified_count
        total = _total_count or len(articles)
    return jsonify({
        "articles": articles,
        "count": len(articles),
        "classified": classified,
        "total": total,
        "error": error,
    })


@app.route("/api/articles")
def api_articles():
    scope = request.args.get("scope")
    region = request.args.get("region")
    tone = request.args.get("tone")
    theme = request.args.get("theme")
    source_domain = request.args.get("source")
    since = request.args.get("since")
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))

    if not is_db_available():
        with _articles_lock:
            results = [
                a for a in _articles.values()
                if a.get("classified_at") and a.get("scope") != "irrelevant"
            ]
        if scope:
            results = [a for a in results if a.get("scope") == scope]
        if region:
            results = [a for a in results if a.get("region") == region]
        if tone:
            results = [a for a in results if a.get("tone") == tone]
        if theme:
            results = [a for a in results if theme in (a.get("themes") or [])]
        return jsonify({
            "results": results[offset:offset + limit],
            "total": len(results),
            "limit": limit,
            "offset": offset,
        })

    with SessionLocal() as session:
        q = session.query(Article).filter(
            Article.classified_at.isnot(None),
            Article.scope != "irrelevant",
        )
        if scope:
            q = q.filter(Article.scope == scope)
        if region:
            q = q.filter(Article.region == region)
        if tone:
            q = q.filter(Article.tone == tone)
        if source_domain:
            q = q.filter(Article.source_domain == source_domain)
        if since:
            try:
                cutoff = datetime.fromisoformat(since)
                q = q.filter(Article.published_at >= cutoff)
            except Exception:
                pass
        all_articles = q.order_by(Article.published_at.desc()).all()
        if theme:
            all_articles = [a for a in all_articles if a.themes and theme in a.themes]
        total = len(all_articles)
        paginated = all_articles[offset:offset + limit]
        return jsonify({
            "results": [_article_db_to_dict(a) for a in paginated],
            "total": total,
            "limit": limit,
            "offset": offset,
        })


@app.route("/api/articles/search")
def api_articles_search():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "q parameter required"}), 400

    since = request.args.get("since")
    limit = min(int(request.args.get("limit", 50)), 200)

    if not is_db_available():
        return jsonify({"results": [], "query": query, "total": 0})

    theme = request.args.get("theme")

    with SessionLocal() as session:
        from sqlalchemy import or_
        q_filter = or_(
            Article.title.ilike(f"%{query}%"),
            Article.content.ilike(f"%{query}%"),
        )
        q = session.query(Article).filter(
            q_filter,
            Article.classified_at.isnot(None),
            Article.scope != "irrelevant",
        )
        if since:
            try:
                cutoff = datetime.fromisoformat(since)
                q = q.filter(Article.published_at >= cutoff)
            except Exception:
                pass
        articles = q.order_by(Article.published_at.desc()).limit(limit).all()
        if theme:
            articles = [a for a in articles if a.themes and theme in a.themes]
        return jsonify({
            "results": [_article_db_to_dict(a) for a in articles],
            "query": query,
            "total": len(articles),
        })


@app.route("/api/digest")
def api_digest():
    lang = request.args.get("lang", "no")
    digest = get_digest(lang)
    if not digest:
        # Fallback to Norwegian
        digest = get_digest("no")
    if not digest:
        return jsonify({"content": None, "generated_at": None, "lang": lang})
    return jsonify({
        "content": digest,
        "generated_at": digest.get("generated_at"),
        "lang": lang,
    })


@app.route("/api/sources")
def api_sources():
    if not is_db_available():
        return jsonify({"results": []})

    with SessionLocal() as session:
        sources = session.query(Source).all()
        return jsonify({
            "results": [
                {"domain": s.domain, "name": s.name, "type": s.type, "region": s.region}
                for s in sources
            ]
        })


@app.route("/api/stats")
def api_stats():
    with _articles_lock:
        total = len(_articles)
        by_scope = {}
        by_region = {}
        for a in _articles.values():
            scope = a.get("scope", "unclassified")
            region = a.get("region", "unknown")
            by_scope[scope] = by_scope.get(scope, 0) + 1
            by_region[region] = by_region.get(region, 0) + 1

    return jsonify({
        "articles": {"total": total, "by_scope": by_scope, "by_region": by_region}
    })


# ---------------------------------------------------------------------------
# Alert endpoints (Task 7)
# ---------------------------------------------------------------------------

@app.route("/alerts")
def alerts_page():
    lang = request.args.get("lang", "no")
    if lang not in ("no", "en", "es", "ja"):
        lang = "no"
    ui_text = _UI_TEXTS.get(lang, _UI_TEXTS["no"])

    if not is_db_available():
        return render_template(
            "alerts.html",
            alerts=[],
            db_unavailable=True,
            lang=lang,
            ui_text=ui_text,
        )

    with SessionLocal() as session:
        alerts = session.query(Alert).filter_by(is_active=True).order_by(Alert.created_at.desc()).all()
        alerts_data = [
            {
                "id": a.id,
                "name": a.name,
                "description": a.description,
                "frequency": a.frequency,
                "email": a.email,
                "created_at": a.created_at.isoformat(),
            }
            for a in alerts
        ]

    return render_template(
        "alerts.html",
        alerts=alerts_data,
        db_unavailable=False,
        lang=lang,
        ui_text=ui_text,
    )


@app.route("/api/alerts", methods=["GET"])
def list_alerts():
    if not is_db_available():
        return jsonify({"results": []})

    with SessionLocal() as session:
        alerts = session.query(Alert).filter_by(is_active=True).order_by(Alert.created_at.desc()).all()
        return jsonify({
            "results": [
                {
                    "id": a.id,
                    "name": a.name,
                    "description": a.description,
                    "frequency": a.frequency,
                    "email": a.email,
                    "created_at": a.created_at.isoformat(),
                }
                for a in alerts
            ]
        })


@app.route("/api/alerts", methods=["POST"])
def create_alert():
    if not is_db_available():
        return jsonify({"error": "Database utilgjengelig"}), 503

    data = request.get_json() or request.form.to_dict()
    name = (data.get("name") or "").strip()
    description = (data.get("description") or "").strip()
    frequency = (data.get("frequency") or "daglig").strip()
    email = (data.get("email") or "").strip()

    if not name or not email:
        return jsonify({"error": "Navn og e-post er påkrevd"}), 400
    if "@" not in email or "." not in email:
        return jsonify({"error": "Ugyldig e-post"}), 400
    if frequency not in ("umiddelbart", "hver_time", "daglig", "ukentlig"):
        return jsonify({"error": "Ugyldig frekvens"}), 400

    with SessionLocal() as session:
        alert = Alert(name=name, description=description, frequency=frequency, email=email, is_active=True)
        session.add(alert)
        session.commit()
        return jsonify({"id": alert.id, "name": alert.name, "email": alert.email, "frequency": alert.frequency})


@app.route("/api/alerts/<int:alert_id>", methods=["DELETE"])
def delete_alert(alert_id):
    if not is_db_available():
        return jsonify({"error": "Database utilgjengelig"}), 503

    with SessionLocal() as session:
        alert = session.get(Alert, alert_id)
        if not alert:
            return jsonify({"error": "Ikke funnet"}), 404
        alert.is_active = False
        session.commit()
        return jsonify({"deleted": alert_id})


@app.route("/uken")
def weekly_digest_page():
    lang = request.args.get("lang", "no")
    if lang not in ("no", "en", "es", "ja"):
        lang = "no"
    ui_text = _UI_TEXTS.get(lang, _UI_TEXTS["no"])
    with _weekly_digest_lock:
        digest = _weekly_digest_cache.get(lang)
    return render_template("weekly.html", lang=lang, ui_text=ui_text, digest=digest)


@app.route("/api/weekly")
def api_weekly():
    lang = request.args.get("lang", "no")
    with _weekly_digest_lock:
        digest = _weekly_digest_cache.get(lang)
    if not digest:
        return jsonify({"error": "No weekly digest available"}), 404
    return jsonify(digest)


@app.route("/admin/generate-weekly", methods=["POST"])
def admin_generate_weekly():
    token = request.args.get("token") or (request.get_json(silent=True) or {}).get("token")
    if token != os.environ.get("ADMIN_TOKEN") or not token:
        return jsonify({"error": "Unauthorized"}), 401

    def _run_weekly():
        from poc.digest import generate_weekly_digest
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        with _articles_lock:
            recent = [
                a for a in _articles.values()
                if a.get("scope") in ("cermaq", "industry")
                and _parse_dt(a.get("published_at")) >= cutoff
            ]
        for lang in ("no", "en", "es", "ja"):
            try:
                digest = generate_weekly_digest(recent, lang=lang)
                if digest:
                    with _weekly_digest_lock:
                        _weekly_digest_cache[lang] = digest
                    _persist_weekly_digest(lang, digest)
                    log.info("Ukentlig digest klar lang=%s", lang)
            except Exception as exc:
                log.error("Ukentlig digest feilet lang=%s: %s", lang, exc)

    threading.Thread(target=_run_weekly, daemon=True).start()
    return jsonify({
        "status": "Ukentlig digest startet",
        "note": "Tar 4-8 minutter for 4 språk. Sjekk /uken-siden om noen minutter.",
    })


@app.route("/analytics")
def analytics_page():
    lang = request.args.get("lang", "no")
    if lang not in ("no", "en", "es", "ja"):
        lang = "no"
    return render_template("analytics.html", lang=lang)


@app.route("/reports")
def reports_index():
    lang = request.args.get("lang", "no")
    if lang not in ("no", "en", "es", "ja"):
        lang = "no"
    return render_template("reports_index.html", lang=lang)


@app.route("/reports/<int:report_id>")
def report_detail(report_id):
    lang = request.args.get("lang", "no")
    if lang not in ("no", "en", "es", "ja"):
        lang = "no"
    return render_template("report_detail.html", lang=lang, report_id=report_id)


@app.route("/api/reports", methods=["GET"])
def api_reports_list():
    lang = request.args.get("lang", "no")
    limit = min(int(request.args.get("limit", 50)), 200)
    if not is_db_available():
        return jsonify({"reports": []})
    with SessionLocal() as session:
        rows = (
            session.query(Report)
            .filter(Report.lang == lang)
            .order_by(Report.generated_at.desc())
            .limit(limit)
            .all()
        )
        return jsonify({
            "reports": [
                {
                    "id": r.id,
                    "type": r.report_type,
                    "title": r.title,
                    "period_start": r.period_start.isoformat(),
                    "period_end": r.period_end.isoformat(),
                    "generated_at": r.generated_at.isoformat(),
                    "article_count": len(r.article_ids or []),
                    "triggered_by": r.triggered_by,
                }
                for r in rows
            ]
        })


@app.route("/api/reports/<int:report_id>", methods=["GET"])
def api_report_detail(report_id):
    if not is_db_available():
        return jsonify({"error": "Database ikke tilgjengelig"}), 503
    with SessionLocal() as session:
        r = session.query(Report).filter(Report.id == report_id).first()
        if not r:
            return jsonify({"error": "not found"}), 404
        return jsonify({
            "id": r.id,
            "type": r.report_type,
            "lang": r.lang,
            "title": r.title,
            "period_start": r.period_start.isoformat(),
            "period_end": r.period_end.isoformat(),
            "generated_at": r.generated_at.isoformat(),
            "content": r.content,
            "statistics": r.statistics,
            "article_ids": r.article_ids,
            "triggered_by": r.triggered_by,
        })


@app.route("/api/reports/generate", methods=["POST"])
def api_reports_generate():
    from poc.reports import generate_report as _generate_report
    data = request.get_json() or {}
    try:
        period_start = datetime.fromisoformat(data["period_start"])
        period_end = datetime.fromisoformat(data["period_end"])
    except (KeyError, ValueError) as e:
        return jsonify({"error": f"Invalid period: {e}"}), 400

    if period_start.tzinfo is None:
        period_start = period_start.replace(tzinfo=timezone.utc)
    if period_end.tzinfo is None:
        period_end = period_end.replace(tzinfo=timezone.utc)

    lang = data.get("lang", "no")
    title = data.get("title") or None
    focus = data.get("focus", "all")
    themes = data.get("themes") or None

    if not is_db_available():
        return jsonify({"error": "Database ikke tilgjengelig"}), 503

    try:
        report = _generate_report(
            period_start=period_start,
            period_end=period_end,
            lang=lang,
            report_type="custom",
            title=title,
            focus=focus,
            theme_filter=themes,
            triggered_by="manual",
        )
    except Exception as exc:
        log.error("api_reports_generate feilet: %s", exc)
        return jsonify({"error": str(exc)}), 500

    if not report:
        return jsonify({"error": "Ingen artikler i perioden"}), 404

    return jsonify({"id": report.id, "title": report.title}), 201


@app.route("/api/reports/weekly/summary")
def api_reports_weekly_summary():
    """Legacy weekly summary endpoint — kept for backwards compatibility."""
    lang = request.args.get("lang", "no")
    if lang not in ("no", "en", "es", "ja"):
        lang = "no"

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    relevant = []
    with _articles_lock:
        for a in _articles.values():
            pub_str = a.get("published_at")
            if pub_str:
                try:
                    pub_date = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
                    if pub_date >= cutoff:
                        relevant.append(a)
                except Exception:
                    pass
            else:
                relevant.append(a)

    if not relevant:
        return jsonify({"summary": "", "article_count": 0})

    lang_names = {"no": "norsk", "en": "engelsk", "es": "spansk", "ja": "japansk"}
    article_context = "\n".join(
        f"- [{a.get('source_name', '?')}] {a.get('title', '')} "
        f"(tone: {a.get('tone', '?')}, scope: {a.get('scope', '?')})"
        for a in relevant[:30]
    )

    try:
        from anthropic import Anthropic as _Anthropic
        client = _Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": (
                f"Lag et ukentlig sammendrag (ca 300 ord på {lang_names[lang]}) av "
                f"medieaktiviteten rundt Cermaq og lakseoppdrettsbransjen siste 7 dager. "
                f"Artikler:\n{article_context}"
            )}],
        )
        summary_text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
    except Exception as exc:
        log.error("Rapport-sammendrag feilet: %s", exc)
        summary_text = ""

    return jsonify({
        "summary": summary_text,
        "article_count": len(relevant),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


@app.route("/finance")
def finance_page():
    lang = request.args.get("lang", "no")
    if lang not in ("no", "en", "es", "ja"):
        lang = "no"
    return render_template("finance.html", lang=lang)


@app.route("/api/finance/rates")
def api_finance_rates():
    from poc.finance import fetch_norges_bank_rates
    rates = fetch_norges_bank_rates()
    if rates is None:
        return jsonify({"error": "Kunne ikke hente valutakurser"}), 503
    return jsonify(rates)


@app.route("/api/finance/stocks")
def api_finance_stocks():
    from poc.finance import fetch_all_stocks
    stocks = fetch_all_stocks()
    return jsonify(stocks)


@app.route("/api/finance/digest")
def api_finance_digest():
    lang = request.args.get("lang", "no")
    if lang not in ("no", "en", "es", "ja"):
        lang = "no"
    with _finance_digest_lock:
        entry = _finance_digest_cache.get(lang)
    if entry:
        return jsonify(entry)
    if is_db_available():
        try:
            with SessionLocal() as session:
                row = (
                    session.query(FinanceDigest)
                    .filter_by(lang=lang)
                    .order_by(FinanceDigest.generated_at.desc())
                    .first()
                )
                if row:
                    result = {
                        "content": row.content,
                        "generated_at": row.generated_at.isoformat(),
                        "rates": row.rates_snapshot or {},
                        "stocks": row.stocks_snapshot or {},
                        "lang": lang,
                    }
                    with _finance_digest_lock:
                        _finance_digest_cache[lang] = result
                    return jsonify(result)
        except Exception as exc:
            log.warning("api_finance_digest DB-feil: %s", exc)
    return jsonify({"error": "Ingen finance digest tilgjengelig"}), 404


@app.route("/api/finance/salmon_prices")
def api_finance_salmon_prices():
    result: dict = {}
    if is_db_available():
        try:
            with SessionLocal() as session:
                for source_key in ("fish_pool", "nasdaq", "urner_barry"):
                    row = (
                        session.query(SalmonPrice)
                        .filter_by(source=source_key)
                        .order_by(SalmonPrice.fetched_at.desc())
                        .first()
                    )
                    if row:
                        result[source_key] = {
                            **row.price_data,
                            "fetched_at": row.fetched_at.isoformat(),
                        }
        except Exception as exc:
            log.warning("api_finance_salmon_prices DB-feil: %s", exc)

    # If DB unavailable or empty, try a live fetch (cold start)
    if not result:
        from poc.finance import fetch_all_salmon_prices
        live = fetch_all_salmon_prices()
        for k, v in live.items():
            if v:
                result[k] = v

    return jsonify(result)


@app.route("/api/finance/news")
def api_finance_news():
    """Returnerer siste finans-tag-artikler, nyeste først."""
    lang = request.args.get("lang", "no")
    limit = min(int(request.args.get("limit", 10)), 50)
    if not is_db_available():
        return jsonify({"articles": []})
    from sqlalchemy import func as _func
    with SessionLocal() as session:
        rows = (
            session.query(Article)
            .filter(Article.classified_at.isnot(None), Article.scope != "irrelevant")
            .order_by(
                _func.coalesce(Article.published_at, Article.fetched_at).desc()
            )
            .limit(200)
            .all()
        )
        finance_articles = [a for a in rows if a.themes and "Finans" in a.themes][:limit]
        return jsonify({
            "articles": [
                {
                    "id": a.id,
                    "title": (a.titles or {}).get(lang) or a.title,
                    "summary": (a.summaries or {}).get(lang, ""),
                    "source_name": a.source_name,
                    "source_domain": a.source_domain,
                    "url": a.url,
                    "published_at": a.published_at.isoformat() if a.published_at else None,
                    "fetched_at": a.fetched_at.isoformat() if a.fetched_at else None,
                    "tone": a.tone,
                    "scope": a.scope,
                    "region": a.region,
                }
                for a in finance_articles
            ]
        })


@app.route("/api/finance/commodities")
def api_finance_commodities():
    from poc.finance import fetch_commodities
    return jsonify(fetch_commodities())


@app.route("/api/finance/calendar")
def api_finance_calendar():
    days_ahead = min(int(request.args.get("days", 60)), 365)
    if not is_db_available():
        return jsonify({"events": []})
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)
    with SessionLocal() as session:
        rows = (
            session.query(CalendarEvent)
            .filter(CalendarEvent.event_date >= now, CalendarEvent.event_date <= end)
            .order_by(CalendarEvent.event_date)
            .all()
        )
        return jsonify({
            "events": [
                {
                    "id": e.id,
                    "title": e.title,
                    "description": e.description,
                    "event_date": e.event_date.isoformat(),
                    "event_type": e.event_type,
                    "company": e.company,
                    "source_url": e.source_url,
                }
                for e in rows
            ]
        })


@app.route("/admin/calendar", methods=["GET", "POST"])
def admin_calendar():
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401

    if request.method == "POST":
        if not is_db_available():
            return jsonify({"error": "Database utilgjengelig"}), 503
        data = request.get_json() or request.form.to_dict()
        title = (data.get("title") or "").strip()
        event_date_str = (data.get("event_date") or "").strip()
        if not title or not event_date_str:
            return jsonify({"error": "title og event_date er påkrevd"}), 400
        try:
            event_date = datetime.fromisoformat(event_date_str)
            if event_date.tzinfo is None:
                event_date = event_date.replace(tzinfo=timezone.utc)
        except ValueError:
            return jsonify({"error": "Ugyldig dato-format (ISO 8601)"}), 400
        with SessionLocal() as session:
            ev = CalendarEvent(
                title=title,
                description=(data.get("description") or "").strip() or None,
                event_date=event_date,
                event_type=(data.get("event_type") or "").strip() or None,
                company=(data.get("company") or "").strip() or None,
                source_url=(data.get("source_url") or "").strip() or None,
            )
            session.add(ev)
            session.commit()
            return jsonify({"id": ev.id, "title": ev.title, "event_date": ev.event_date.isoformat()})

    # GET — return all upcoming events
    if not is_db_available():
        return jsonify({"events": []})
    now = datetime.now(timezone.utc)
    with SessionLocal() as session:
        rows = (
            session.query(CalendarEvent)
            .filter(CalendarEvent.event_date >= now)
            .order_by(CalendarEvent.event_date)
            .all()
        )
        return jsonify({
            "events": [
                {
                    "id": e.id,
                    "title": e.title,
                    "description": e.description,
                    "event_date": e.event_date.isoformat(),
                    "event_type": e.event_type,
                    "company": e.company,
                }
                for e in rows
            ]
        })


@app.route("/admin/calendar/<int:event_id>", methods=["DELETE"])
def admin_calendar_delete(event_id):
    if not _check_admin_token():
        return jsonify({"error": "Unauthorized"}), 401
    if not is_db_available():
        return jsonify({"error": "Database utilgjengelig"}), 503
    with SessionLocal() as session:
        ev = session.get(CalendarEvent, event_id)
        if not ev:
            return jsonify({"error": "Ikke funnet"}), 404
        session.delete(ev)
        session.commit()
    return jsonify({"deleted": event_id})


@app.route("/api/finance/prices/timeseries")
def api_finance_prices_timeseries():
    """Return historical salmon price data from the salmon_prices table."""
    days = min(int(request.args.get("days", 90)), 365)
    if not is_db_available():
        return jsonify({"series": []})
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with SessionLocal() as session:
        rows = (
            session.query(SalmonPrice)
            .filter(SalmonPrice.fetched_at >= cutoff)
            .order_by(SalmonPrice.fetched_at)
            .all()
        )
    series: dict = {}
    for row in rows:
        src = row.source
        date_str = row.fetched_at.date().isoformat()
        data = row.price_data or {}
        price = None
        if src == "fish_pool" and data.get("forward_prices"):
            price = data["forward_prices"][0].get("price")
        elif src == "nasdaq":
            price = data.get("spot_price")
        if price is None:
            continue
        if src not in series:
            series[src] = {"dates": [], "prices": [], "label": data.get("source", src)}
        if not series[src]["dates"] or series[src]["dates"][-1] != date_str:
            series[src]["dates"].append(date_str)
            series[src]["prices"].append(round(float(price), 2))

    return jsonify({"series": list(series.values()), "days": days})


@app.route("/api/finance/stocks/timeseries")
def api_finance_stocks_timeseries():
    """Return historical stock prices for all competitor tickers."""
    days = min(int(request.args.get("days", 90)), 365)
    from poc.finance import STOCK_TICKERS, fetch_stock_quote_history
    result = {}
    for name, ticker in STOCK_TICKERS.items():
        hist = fetch_stock_quote_history(ticker, days=days)
        if hist:
            result[name] = {"dates": hist["dates"], "prices": hist["prices"], "ticker": ticker}
    return jsonify(result)


@app.route("/api/finance/scenarios")
def api_finance_scenarios():
    """Compute what-if margin scenarios for Cermaq based on current market data."""
    from poc.finance import fetch_norges_bank_rates, compute_scenarios

    rates = fetch_norges_bank_rates() or {}

    # Get latest salmon spot from DB
    salmon_spot: float | None = None
    if is_db_available():
        try:
            with SessionLocal() as session:
                row = (
                    session.query(SalmonPrice)
                    .filter_by(source="nasdaq")
                    .order_by(SalmonPrice.fetched_at.desc())
                    .first()
                )
                if row and row.price_data:
                    salmon_spot = row.price_data.get("spot_price")
        except Exception as exc:
            log.warning("api_finance_scenarios: DB feil: %s", exc)

    # Get latest commodities
    commodities: dict = {}
    try:
        from poc.finance import fetch_commodities
        commodities = fetch_commodities()
    except Exception:
        pass

    result = compute_scenarios(salmon_spot, rates, commodities)
    return jsonify(result)


@app.route("/api/finance/anomalies")
def api_finance_anomalies():
    """Run anomaly detection against latest data and return results."""
    from poc.finance import fetch_norges_bank_rates, detect_anomalies

    latest_rates = fetch_norges_bank_rates()

    history_salmon: list = []
    latest_salmon = None
    if is_db_available():
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            with SessionLocal() as session:
                rows = (
                    session.query(SalmonPrice)
                    .filter(SalmonPrice.source == "nasdaq", SalmonPrice.fetched_at >= cutoff)
                    .order_by(SalmonPrice.fetched_at)
                    .all()
                )
                history_salmon = [r.price_data for r in rows if r.price_data]
                if history_salmon:
                    latest_salmon = history_salmon[-1]
        except Exception as exc:
            log.warning("api_finance_anomalies: DB feil: %s", exc)

    anomalies = detect_anomalies(
        latest_rates=latest_rates,
        latest_salmon=latest_salmon,
        history_salmon=history_salmon,
    )
    return jsonify({
        "anomalies": anomalies,
        "count": len(anomalies),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    })


# Shares outstanding (approximate, used for market cap estimation)
_SHARES_OUTSTANDING_M = {
    "Mowi": 475,
    "SalMar": 114,
    "Grieg Seafood": 116,
    "Bakkafrost": 65,
    "Lerøy": 593,
}


@app.route("/api/finance/market-cap")
def api_finance_market_cap():
    """Return estimated market cap for competitor companies (price × shares)."""
    from poc.finance import fetch_all_stocks
    stocks = fetch_all_stocks()
    result = {}
    for name, data in stocks.items():
        shares_m = _SHARES_OUTSTANDING_M.get(name)
        price = data.get("price")
        if shares_m and price:
            result[name] = {
                "price": price,
                "currency": data.get("currency", "NOK"),
                "shares_million": shares_m,
                "market_cap_bnok": round(price * shares_m / 1000, 2),
                "change_1d": data.get("change_1d"),
                "change_30d": data.get("change_30d"),
                "ticker": data.get("ticker"),
            }
    return jsonify(result)


@app.route("/api/finance/correlation")
def api_finance_correlation():
    """Compute Pearson correlation between weekly Cermaq article count and salmon spot price."""
    import math
    if not is_db_available():
        return jsonify({"error": "Database utilgjengelig"}), 503

    weeks = min(int(request.args.get("weeks", 26)), 52)
    cutoff = datetime.now(timezone.utc) - timedelta(weeks=weeks)

    try:
        with SessionLocal() as session:
            # Weekly Cermaq article counts
            cermaq_rows = (
                session.query(Article)
                .filter(
                    Article.classified_at.isnot(None),
                    Article.scope == "cermaq",
                    Article.published_at >= cutoff,
                )
                .order_by(Article.published_at)
                .all()
            )

            # Weekly salmon prices
            salmon_rows = (
                session.query(SalmonPrice)
                .filter(SalmonPrice.source == "nasdaq", SalmonPrice.fetched_at >= cutoff)
                .order_by(SalmonPrice.fetched_at)
                .all()
            )

        # Bucket both into ISO weeks
        from collections import defaultdict
        article_by_week: dict = defaultdict(int)
        for a in cermaq_rows:
            if a.published_at:
                wk = a.published_at.isocalendar()[:2]
                article_by_week[wk] += 1

        salmon_by_week: dict = {}
        for r in salmon_rows:
            if r.fetched_at and r.price_data and r.price_data.get("spot_price"):
                wk = r.fetched_at.isocalendar()[:2]
                salmon_by_week[wk] = r.price_data["spot_price"]

        common_weeks = sorted(set(article_by_week) & set(salmon_by_week))
        if len(common_weeks) < 4:
            return jsonify({
                "r": None,
                "n": len(common_weeks),
                "message": "For lite data til å beregne korrelasjon (< 4 uker)",
                "series": [],
            })

        xs = [article_by_week[w] for w in common_weeks]
        ys = [salmon_by_week[w] for w in common_weeks]
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        cov = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n)) / n
        std_x = math.sqrt(sum((x - mean_x) ** 2 for x in xs) / n)
        std_y = math.sqrt(sum((y - mean_y) ** 2 for y in ys) / n)
        r = round(cov / (std_x * std_y), 3) if std_x > 0 and std_y > 0 else None

        return jsonify({
            "r": r,
            "n": n,
            "interpretation": (
                "Sterk positiv" if r and r > 0.6 else
                "Moderat positiv" if r and r > 0.3 else
                "Sterk negativ" if r and r < -0.6 else
                "Moderat negativ" if r and r < -0.3 else
                "Svak/ingen"
            ),
            "series": [
                {
                    "week": f"{w[0]}-W{w[1]:02d}",
                    "articles": article_by_week[w],
                    "salmon_price": salmon_by_week[w],
                }
                for w in common_weeks
            ],
        })
    except Exception as exc:
        log.error("api_finance_correlation feilet: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/api/finance/export/csv")
def api_finance_export_csv():
    """Export all current finance data as CSV download."""
    import csv
    import io
    from poc.finance import fetch_norges_bank_rates, fetch_all_stocks, fetch_commodities

    rates = fetch_norges_bank_rates() or {}
    stocks = fetch_all_stocks() or {}
    commodities = fetch_commodities() or {}

    # Latest salmon prices
    salmon: dict = {}
    if is_db_available():
        try:
            with SessionLocal() as session:
                for src in ("fish_pool", "nasdaq"):
                    row = (
                        session.query(SalmonPrice)
                        .filter_by(source=src)
                        .order_by(SalmonPrice.fetched_at.desc())
                        .first()
                    )
                    if row and row.price_data:
                        salmon[src] = row.price_data
        except Exception:
            pass

    buf = io.StringIO()
    writer = csv.writer(buf)
    now_str = datetime.now(ZoneInfo("Europe/Oslo")).strftime("%Y-%m-%d %H:%M")

    writer.writerow([f"Cermaq Watch — Finansdata eksportert {now_str} Oslo-tid"])
    writer.writerow([])

    writer.writerow(["VALUTAKURSER", "Kurs (NOK)", "Endring %", "Dato"])
    for cur, v in rates.items():
        writer.writerow([cur, v.get("rate"), v.get("change_pct"), v.get("date")])
    writer.writerow([])

    writer.writerow(["KONKURRENTAKSJER", "Kurs", "Valuta", "1D %", "7D %", "30D %", "Ticker"])
    for name, v in stocks.items():
        writer.writerow([name, v.get("price"), v.get("currency"), v.get("change_1d"), v.get("change_7d"), v.get("change_30d"), v.get("ticker")])
    writer.writerow([])

    writer.writerow(["RÅVARER", "Kurs", "Enhet", "1D %", "7D %", "Ticker"])
    for name, v in commodities.items():
        writer.writerow([name, v.get("price"), v.get("commodity_currency"), v.get("change_1d"), v.get("change_7d"), v.get("ticker")])
    writer.writerow([])

    writer.writerow(["LAKSEPRISINDEKSER", "Kilde", "Pris (NOK/kg)", "Info"])
    ns = salmon.get("nasdaq")
    if ns:
        writer.writerow(["Nasdaq Salmon Index", "nasdaq", ns.get("spot_price"), f"Uke {ns.get('week','')}"])
    fp = salmon.get("fish_pool")
    if fp and fp.get("forward_prices"):
        for fwp in fp["forward_prices"]:
            writer.writerow(["Fish Pool", f"termin {fwp.get('period')}", fwp.get("price"), "NOK/kg"])

    csv_bytes = buf.getvalue().encode("utf-8-sig")  # BOM for Excel compatibility
    from flask import Response
    filename = f"cermaq-finans-{datetime.now().strftime('%Y%m%d')}.csv"
    return Response(
        csv_bytes,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/healthz")
def healthz():
    return "OK", 200


# ---------------------------------------------------------------------------
# Analytics API
# ---------------------------------------------------------------------------

def _analytics_time_window(period_str: str):
    """Return (start_dt, end_dt) based on period string."""
    days = {"7d": 7, "30d": 30, "90d": 90}.get(period_str, 30)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    return start, end


def _analytics_base_query(session, period: str, region: str | None):
    """Shared base query for analytics: classified, non-irrelevant, within window."""
    from sqlalchemy import func as _func
    start, _ = _analytics_time_window(period)
    q = session.query(Article).filter(
        Article.classified_at.isnot(None),
        Article.scope != "irrelevant",
        _func.coalesce(Article.published_at, Article.fetched_at) >= start,
    )
    if region:
        q = q.filter(Article.region == region)
    return q


@app.route("/api/analytics/kpis")
def api_analytics_kpis():
    if not is_db_available():
        return jsonify({"error": "Database utilgjengelig"}), 503
    from sqlalchemy import func as _func
    period = request.args.get("period", "30d")
    region = request.args.get("region", "").strip() or None
    days = {"7d": 7, "30d": 30, "90d": 90}.get(period, 30)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    prev_start = start - timedelta(days=days)

    with SessionLocal() as session:
        def _count(win_start, win_end, scope=None):
            q = session.query(Article).filter(
                Article.classified_at.isnot(None),
                Article.scope != "irrelevant",
                _func.coalesce(Article.published_at, Article.fetched_at) >= win_start,
                _func.coalesce(Article.published_at, Article.fetched_at) < win_end,
            )
            if region:
                q = q.filter(Article.region == region)
            if scope:
                q = q.filter(Article.scope == scope)
            return q.count()

        total_now = _count(start, end)
        total_prev = _count(prev_start, start)
        cermaq_now = _count(start, end, scope="cermaq")
        cermaq_prev = _count(prev_start, start, scope="cermaq")

        neg_q = session.query(Article).filter(
            Article.classified_at.isnot(None),
            Article.scope == "cermaq",
            Article.tone == "negativ",
            _func.coalesce(Article.published_at, Article.fetched_at) >= start,
        )
        if region:
            neg_q = neg_q.filter(Article.region == region)
        negative_cermaq = neg_q.count()

        src_q = session.query(
            _func.count(_func.distinct(Article.source_domain))
        ).filter(
            Article.classified_at.isnot(None),
            Article.scope != "irrelevant",
            _func.coalesce(Article.published_at, Article.fetched_at) >= start,
        )
        if region:
            src_q = src_q.filter(Article.region == region)
        active_sources = src_q.scalar() or 0

        def _pct(now, prev):
            if prev < 5:
                return None
            return round((now - prev) / prev * 100, 1)

        return jsonify({
            "total_articles": total_now,
            "total_change_pct": _pct(total_now, total_prev),
            "cermaq_articles": cermaq_now,
            "cermaq_change_pct": _pct(cermaq_now, cermaq_prev),
            "negative_cermaq_pct": round(negative_cermaq / cermaq_now * 100, 1) if cermaq_now > 0 else 0.0,
            "active_sources": active_sources,
        })


@app.route("/api/analytics/timeline")
def api_analytics_timeline():
    if not is_db_available():
        return jsonify({"error": "Database utilgjengelig"}), 503
    period = request.args.get("period", "30d")
    region = request.args.get("region", "").strip() or None
    days = {"7d": 7, "30d": 30, "90d": 90}.get(period, 30)

    with SessionLocal() as session:
        articles = _analytics_base_query(session, period, region).all()

    end_date = datetime.now(timezone.utc).date()
    buckets: dict = {}
    cermaq_buckets: dict = {}
    for i in range(days):
        d = (end_date - timedelta(days=days - 1 - i)).isoformat()
        buckets[d] = 0
        cermaq_buckets[d] = 0

    for a in articles:
        dt = a.published_at or a.fetched_at
        if not dt:
            continue
        d = dt.date().isoformat()
        if d in buckets:
            buckets[d] += 1
            if a.scope == "cermaq":
                cermaq_buckets[d] += 1

    labels = sorted(buckets.keys())
    return jsonify({
        "labels": labels,
        "all_articles": [buckets[d] for d in labels],
        "cermaq_articles": [cermaq_buckets[d] for d in labels],
    })


@app.route("/api/analytics/sentiment")
def api_analytics_sentiment():
    if not is_db_available():
        return jsonify({"error": "Database utilgjengelig"}), 503
    period = request.args.get("period", "30d")
    region = request.args.get("region", "").strip() or None

    with SessionLocal() as session:
        articles = _analytics_base_query(session, period, region).all()

    result = {
        "cermaq": {"positiv": 0, "noytral": 0, "negativ": 0},
        "industry": {"positiv": 0, "noytral": 0, "negativ": 0},
    }
    for a in articles:
        tone = a.tone or "noytral"
        if tone not in result["cermaq"]:
            continue
        bucket = "cermaq" if a.scope == "cermaq" else "industry"
        result[bucket][tone] += 1

    return jsonify(result)


@app.route("/api/analytics/themes")
def api_analytics_themes():
    if not is_db_available():
        return jsonify({"error": "Database utilgjengelig"}), 503
    period = request.args.get("period", "30d")
    region = request.args.get("region", "").strip() or None

    with SessionLocal() as session:
        articles = _analytics_base_query(session, period, region).all()

    counts = {t: 0 for t in _THEMES_LIST}
    for a in articles:
        for theme in (a.themes or []):
            if theme in counts:
                counts[theme] += 1

    return jsonify({
        "themes": _THEMES_LIST,
        "counts": [counts[t] for t in _THEMES_LIST],
    })


@app.route("/api/analytics/regions")
def api_analytics_regions():
    if not is_db_available():
        return jsonify({"error": "Database utilgjengelig"}), 503
    from sqlalchemy import func as _func
    period = request.args.get("period", "30d")
    start, _ = _analytics_time_window(period)

    with SessionLocal() as session:
        articles = session.query(Article).filter(
            Article.classified_at.isnot(None),
            Article.scope != "irrelevant",
            _func.coalesce(Article.published_at, Article.fetched_at) >= start,
        ).all()

    region_keys = ["norge", "chile", "canada", "global"]
    region_labels = ["Norge", "Chile", "Canada", "Globalt"]
    result = {
        "regions": region_labels,
        "positiv": [0] * 4,
        "noytral": [0] * 4,
        "negativ": [0] * 4,
    }
    for a in articles:
        reg = a.region or "global"
        if reg not in region_keys:
            reg = "global"
        idx = region_keys.index(reg)
        tone = a.tone or "noytral"
        if tone in result:
            result[tone][idx] += 1

    return jsonify(result)


@app.route("/api/analytics/sources")
def api_analytics_sources():
    if not is_db_available():
        return jsonify({"error": "Database utilgjengelig"}), 503
    period = request.args.get("period", "30d")
    region = request.args.get("region", "").strip() or None

    with SessionLocal() as session:
        articles = _analytics_base_query(session, period, region).all()

    sources: dict = {}
    for a in articles:
        domain = a.source_domain or "ukjent"
        if domain not in sources:
            sources[domain] = {"name": a.source_name or domain, "domain": domain, "total": 0, "cermaq": 0}
        sources[domain]["total"] += 1
        if a.scope == "cermaq":
            sources[domain]["cermaq"] += 1

    top = sorted(sources.values(), key=lambda s: s["total"], reverse=True)[:10]
    return jsonify({"sources": top})





# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _start_background_threads()
    port = int(os.environ.get("PORT", 8080))
    log.info("Starting Cermaq Watch server on 0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    run()
