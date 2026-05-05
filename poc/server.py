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
    is_db_available, SessionLocal,
    Source, Article, Digest, Alert, WeeklyDigest,
    extract_domain, get_or_create_source,
)

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

    return dict(
        theme_counts=theme_counts,
        region_counts=region_counts,
        total_classified=total_classified,
        active_region=active_region,
        active_theme=active_theme,
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
                    .filter_by(lang=lang)
                    .order_by(Digest.generated_at.desc())
                    .first()
                )
                if latest:
                    with _digest_lock:
                        _digest_cache[lang] = {
                            "headline": latest.headline,
                            "body": latest.body,
                            "sources": latest.web_search_urls or [],
                            "article_count": latest.article_count or 0,
                            "cermaq_count": latest.cermaq_count or 0,
                            "search_count": latest.search_count or 0,
                            "generated_at": latest.generated_at.isoformat(),
                            "lang": lang,
                        }
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


def _persist_digest(lang: str, digest: dict, used_article_ids: list = None) -> None:
    if not is_db_available():
        return

    try:
        with SessionLocal() as session:
            d = Digest(
                lang=lang,
                headline=digest.get("headline", ""),
                body=digest.get("body", ""),
                article_ids=used_article_ids or [],
                web_search_urls=digest.get("sources", []),
                article_count=digest.get("article_count", 0),
                cermaq_count=digest.get("cermaq_count", 0),
                search_count=digest.get("search_count", 0),
            )
            session.add(d)
            session.commit()
            log.info("Digest persistert for %s", lang)
    except Exception as exc:
        log.error("Digest-persist-feil for %s: %s", lang, exc)


# ---------------------------------------------------------------------------
# Startup: initialise DB and load existing data
# ---------------------------------------------------------------------------

init_db()
ensure_columns()
migrate_id_to_bigint()
migrate_add_themes()
_load_from_db()


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

def _generate_all_digests() -> None:
    """Generate digest for all four languages from the last 24 hours of articles."""
    with _articles_lock:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        recent = [
            a for a in _articles.values()
            if a.get("scope") in ("cermaq", "industry")
            and _parse_dt(a.get("published_at")) >= cutoff
        ]

    cermaq_count = sum(1 for a in recent if a.get("scope") == "cermaq")
    log.info("Genererer digest for %d artikler (%d Cermaq)", len(recent), cermaq_count)

    if len(recent) < 5:
        log.info("For få artikler (%d) — setter stille-melding", len(recent))
        _minimal = {
            "no": ("Stille mediedøgn", "<p>Få relevante artikler siste 24 timer.</p>"),
            "en": ("Quiet news cycle", "<p>Few relevant articles in the last 24 hours.</p>"),
            "es": ("Ciclo de noticias tranquilo", "<p>Pocos artículos relevantes en las últimas 24 horas.</p>"),
            "ja": ("静かなニュースサイクル", "<p>過去24時間の関連記事は少数です。</p>"),
        }
        now_iso = datetime.now(timezone.utc).isoformat()
        with _digest_lock:
            for lang, (headline, body) in _minimal.items():
                _digest_cache[lang] = {
                    "headline": headline,
                    "body": body,
                    "generated_at": now_iso,
                    "lang": lang,
                    "article_count": len(recent),
                    "cermaq_count": cermaq_count,
                    "search_count": 0,
                    "sources": [],
                }
        return

    from poc.digest import generate_digest
    article_ids = [a["id"] for a in recent]
    all_sources: set = set()
    for lang in ("no", "en", "es", "ja"):
        try:
            digest = generate_digest(recent, lang=lang)
            if digest:
                with _digest_lock:
                    _digest_cache[lang] = digest
                log.info("Digest klar lang=%s: %s", lang, digest["headline"][:60])
                _persist_digest(lang, digest, article_ids)
                for url in digest.get("sources", []):
                    if url:
                        all_sources.add(url)
        except Exception as exc:
            log.error("Digest feilet for lang=%s: %s", lang, exc)

    if all_sources:
        added = _ingest_urls_as_articles(list(all_sources))
        log.info("Digest-generering la til %d nye artikler fra web-søk", added)


def _digest_scheduler() -> None:
    """Regenerate digest every day at 06:00 Oslo time."""
    while True:
        try:
            now = datetime.now(ZoneInfo("Europe/Oslo"))
            target = now.replace(hour=6, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
            log.info("Neste digest-generering: %s (om %.1f timer)", target.isoformat(), wait / 3600)
            time.sleep(wait)
            _generate_all_digests()
        except Exception as exc:
            log.error("Digest-scheduler-feil: %s", exc)
            time.sleep(3600)


def _initial_digest() -> None:
    """Generate digest once at startup, after fetch+classify have had time to run."""
    time.sleep(60)
    with _digest_lock:
        has_digest = len(_digest_cache) > 0
    if not has_digest:
        _generate_all_digests()


def get_digest(lang: str = "no") -> dict | None:
    with _digest_lock:
        return _digest_cache.get(lang)


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

    digest = get_digest(lang)
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
        digest=digest,
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

    threading.Thread(target=_generate_all_digests, daemon=True).start()
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
    with _digest_lock:
        digest = _digest_cache.get(lang)
    if not digest:
        return jsonify({"error": "No digest available"}), 404
    return jsonify(digest)


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


@app.route("/reports")
def reports_page():
    lang = request.args.get("lang", "no")
    if lang not in ("no", "en", "es", "ja"):
        lang = "no"
    ui_text = _UI_TEXTS.get(lang, _UI_TEXTS["no"])
    return render_template("reports.html", lang=lang, ui_text=ui_text)


@app.route("/api/reports/weekly")
def api_reports_weekly():
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

    by_scope: dict = {}
    by_tone: dict = {}
    by_country: dict = {}
    sources: set = set()
    for a in relevant:
        scope = a.get("scope") or "unknown"
        tone = a.get("tone") or "unknown"
        if tone == "kritisk":
            tone = "negativ"
        country = a.get("region") or "global"
        by_scope[scope] = by_scope.get(scope, 0) + 1
        by_tone[tone] = by_tone.get(tone, 0) + 1
        by_country[country] = by_country.get(country, 0) + 1
        if a.get("source_name"):
            sources.add(a["source_name"])

    top_articles = sorted(relevant, key=lambda x: x.get("relevance") or 0, reverse=True)[:10]

    return jsonify({
        "period": {
            "from": cutoff.isoformat(),
            "to": datetime.now(timezone.utc).isoformat(),
            "days": 7,
        },
        "stats": {
            "total": len(relevant),
            "source_count": len(sources),
            "by_scope": by_scope,
            "by_tone": by_tone,
            "by_country": by_country,
        },
        "top_articles": [
            {
                "id": a.get("id"),
                "title": a.get("title"),
                "url": a.get("url"),
                "source_name": a.get("source_name"),
                "scope": a.get("scope"),
                "tone": "negativ" if a.get("tone") == "kritisk" else a.get("tone"),
                "region": a.get("region"),
                "relevance": a.get("relevance"),
                "summary": (a.get("summaries") or {}).get("no"),
            }
            for a in top_articles
        ],
    })


@app.route("/api/reports/weekly/summary")
def api_reports_weekly_summary():
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
        f"- [{a.get('source_name', '?')}] {a.get('title', '')} (tone: {a.get('tone', '?')}, scope: {a.get('scope', '?')})"
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
                f"Strukturer med:\n"
                f"1. Hovedtemaer\n2. Cermaq-spesifikke saker\n"
                f"3. Bransjeutvikling\n4. Hva vi bør følge fremover\n\n"
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


@app.route("/healthz")
def healthz():
    return "OK", 200


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
