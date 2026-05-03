"""Flask server — serves HTML frontend and JSON API for Cermaq Watch."""

import logging
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template, request

from poc.classify import classify
from poc.fetch import fetch_miniflux, _fetch_og_image
from poc.db import (
    init_db, is_db_available, SessionLocal,
    Source, Article, Digest, Alert,
    extract_domain, get_or_create_source,
)

log = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates")

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

# Digest cache
_digest_cache: dict = {}  # lang -> digest dict
_digest_lock = threading.Lock()

_UI_TEXTS: dict = {
    "no": {
        "digest_title": "Dagens oppsummering",
        "digest_pending": "Dagens oppsummering genereres…",
        "based_on": "Basert på",
        "articles": "artikler",
        "generated_at": "Generert",
        "searches_used": "Web-søk brukt:",
        "nav_dashboard": "Dagsoversikt",
        "nav_alerts": "Krise-varsler",
        "page_title": "Nyheter",
        "page_subtitle": "Daglig oversikt over Cermaq og bransjedekning",
    },
    "en": {
        "digest_title": "Daily summary",
        "digest_pending": "Daily summary being generated…",
        "based_on": "Based on",
        "articles": "articles",
        "generated_at": "Generated",
        "searches_used": "Web searches used:",
        "nav_dashboard": "Dashboard",
        "nav_alerts": "Crisis alerts",
        "page_title": "News",
        "page_subtitle": "Daily overview of Cermaq and industry coverage",
    },
    "es": {
        "digest_title": "Resumen del día",
        "digest_pending": "Resumen del día generándose…",
        "based_on": "Basado en",
        "articles": "artículos",
        "generated_at": "Generado",
        "searches_used": "Búsquedas web usadas:",
        "nav_dashboard": "Panel",
        "nav_alerts": "Alertas",
        "page_title": "Noticias",
        "page_subtitle": "Resumen diario de Cermaq y cobertura de la industria",
    },
    "ja": {
        "digest_title": "本日のまとめ",
        "digest_pending": "本日のまとめを生成中…",
        "based_on": "対象記事",
        "articles": "件",
        "generated_at": "生成日時",
        "searches_used": "ウェブ検索使用回数:",
        "nav_dashboard": "ダッシュボード",
        "nav_alerts": "アラート",
        "page_title": "ニュース",
        "page_subtitle": "Cermaと業界報道の毎日の概要",
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
                "summaries": article.get("summaries"),
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
    for lang in ("no", "en", "es", "ja"):
        try:
            digest = generate_digest(recent, lang=lang)
            if digest:
                with _digest_lock:
                    _digest_cache[lang] = digest
                log.info("Digest klar lang=%s: %s", lang, digest["headline"][:60])
                _persist_digest(lang, digest, article_ids)
        except Exception as exc:
            log.error("Digest feilet for lang=%s: %s", lang, exc)


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

    digest = get_digest(lang)
    ui_text = _UI_TEXTS.get(lang, _UI_TEXTS["no"])

    articles, error = _get_articles()
    articles_sorted = sorted(articles, key=_sort_key)
    formatted = [
        {**a, "published_at_iso": a.get("published_at") or "", "published_at": _fmt_dt(a.get("published_at"))}
        for a in articles_sorted
    ]

    formatted = [a for a in formatted if a.get("scope") != "irrelevant"]

    irrelevant_count = sum(1 for a in articles if a.get("scope") == "irrelevant")
    if irrelevant_count:
        log.info("%d artikler filtrert som irrelevant", irrelevant_count)

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

    cermaq_count = sum(1 for a in articles if a.get("scope") == "cermaq")
    region_counts = {
        r: sum(1 for a in articles if a.get("region") == r)
        for r in ("norge", "chile", "canada", "global")
    }
    industry_core_count = sum(
        1 for a in formatted
        if a.get("scope") == "industry" and a.get("region") in ("norge", "chile", "canada")
    )
    industry_global_count = sum(
        1 for a in formatted
        if a.get("scope") == "industry" and a.get("region") not in ("norge", "chile", "canada")
    )
    kritisk_count = sum(1 for a in formatted if a.get("tone") == "kritisk")

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
        cermaq_count=cermaq_count,
        region_counts=region_counts,
        industry_core_count=industry_core_count,
        industry_global_count=industry_global_count,
        kritisk_count=kritisk_count,
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
    source_domain = request.args.get("source")
    since = request.args.get("since")
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))

    if not is_db_available():
        with _articles_lock:
            results = list(_articles.values())
        if scope:
            results = [a for a in results if a.get("scope") == scope]
        if region:
            results = [a for a in results if a.get("region") == region]
        if tone:
            results = [a for a in results if a.get("tone") == tone]
        return jsonify({
            "results": results[offset:offset + limit],
            "total": len(results),
            "limit": limit,
            "offset": offset,
        })

    with SessionLocal() as session:
        q = session.query(Article)
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
        total = q.count()
        articles = q.order_by(Article.published_at.desc()).offset(offset).limit(limit).all()
        return jsonify({
            "results": [_article_db_to_dict(a) for a in articles],
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

    with SessionLocal() as session:
        from sqlalchemy import or_
        q_filter = or_(
            Article.title.ilike(f"%{query}%"),
            Article.content.ilike(f"%{query}%"),
        )
        q = session.query(Article).filter(q_filter)
        if since:
            try:
                cutoff = datetime.fromisoformat(since)
                q = q.filter(Article.published_at >= cutoff)
            except Exception:
                pass
        articles = q.order_by(Article.published_at.desc()).limit(limit).all()
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
