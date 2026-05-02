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
    },
    "en": {
        "digest_title": "Daily summary",
        "digest_pending": "Daily summary being generated…",
        "based_on": "Based on",
        "articles": "articles",
        "generated_at": "Generated",
    },
    "es": {
        "digest_title": "Resumen del día",
        "digest_pending": "Resumen del día generándose…",
        "based_on": "Basado en",
        "articles": "artículos",
        "generated_at": "Generado",
    },
    "ja": {
        "digest_title": "本日のまとめ",
        "digest_pending": "本日のまとめを生成中…",
        "based_on": "対象記事",
        "articles": "件",
        "generated_at": "生成日時",
    },
}


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

            # Add new articles to cache immediately (unclassified) so they
            # are visible in the UI right away, then enqueue for classification
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
    """Runs forever: pop IDs from queue, classify, update cache."""
    global _classified_count

    while True:
        article_id = _classify_queue.get()  # blocks until work arrives
        try:
            with _articles_lock:
                article = _articles.get(article_id)

            if article is None or "scope" in article:
                # Already classified or removed
                _classify_queue.task_done()
                continue

            enrichment = classify(article)

            with _articles_lock:
                if article_id in _articles:
                    _articles[article_id].update(enrichment)

            with _counters_lock:
                _classified_count += 1

            # Fetch OG image for classified articles that have no image from RSS
            if enrichment.get("scope") in ("cermaq", "industry") and not article.get("image_url"):
                og_image = _fetch_og_image(article["url"])
                if og_image:
                    with _articles_lock:
                        if article_id in _articles:
                            _articles[article_id]["image_url"] = og_image
                    log.info("OG-image hentet for id=%s", article_id)

        except Exception as exc:
            log.error("classify loop error for id=%s: %s", article_id, exc)
        finally:
            _classify_queue.task_done()

        # Rate-limit: at most 1 API call per second
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
                }
        return

    from poc.digest import generate_digest
    for lang in ("no", "en", "es", "ja"):
        try:
            digest = generate_digest(recent, lang=lang)
            if digest:
                with _digest_lock:
                    _digest_cache[lang] = digest
                log.info("Digest klar lang=%s: %s", lang, digest["headline"][:60])
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
    # Give the classify loop a head-start before scanning
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
# Startup: kick off both background threads
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
    """Return a snapshot of all articles and the last fetch error."""
    global _fetched_at, _fetch_error

    # Trigger an immediate fetch if cache is empty
    if _fetched_at is None:
        # Fetch hasn't run yet — wait briefly for the thread to populate
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.before_request
def _log_request():
    log.info("%s %s", request.method, request.path)


def _today_no() -> str:
    """Return today's date in Norwegian, e.g. 'lørdag 3. mai 2026'."""
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
        digest=digest,
        lang=lang,
        ui_text=ui_text,
    )


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
