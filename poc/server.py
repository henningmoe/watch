"""Flask server — serves HTML frontend and JSON API for Cermaq Watch."""

import logging
import os
import threading
import time
from datetime import datetime

from flask import Flask, jsonify, render_template

from poc.fetch import fetch_miniflux

log = logging.getLogger(__name__)

app = Flask(__name__, template_folder="../poc/templates")

_cache: dict = {"articles": [], "fetched_at": None, "error": None}
_cache_lock = threading.Lock()
_CACHE_TTL = 30 * 60  # 30 minutes


def _refresh_cache() -> None:
    log.info("Refreshing article cache from Miniflux...")
    try:
        articles = fetch_miniflux()
        with _cache_lock:
            _cache["articles"] = articles
            _cache["fetched_at"] = time.monotonic()
            _cache["error"] = None
        log.info("Cache refreshed: %d articles", len(articles))
    except Exception as exc:
        log.error("Failed to fetch articles: %s", exc)
        with _cache_lock:
            _cache["error"] = str(exc)
            _cache["fetched_at"] = time.monotonic()
    _schedule_refresh()


def _schedule_refresh() -> None:
    t = threading.Timer(_CACHE_TTL, _refresh_cache)
    t.daemon = True
    t.start()


def _get_articles() -> tuple[list[dict], str | None]:
    with _cache_lock:
        fetched_at = _cache["fetched_at"]
        articles = _cache["articles"]
        error = _cache["error"]

    if fetched_at is None or (time.monotonic() - fetched_at) > _CACHE_TTL:
        _refresh_cache()
        with _cache_lock:
            articles = _cache["articles"]
            error = _cache["error"]

    return articles, error


def _fmt_dt(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        MONTHS = [
            "januar", "februar", "mars", "april", "mai", "juni",
            "juli", "august", "september", "oktober", "november", "desember",
        ]
        return f"{dt.day}. {MONTHS[dt.month - 1]} {dt.year}, {dt.strftime('%H:%M')}"
    except ValueError:
        return iso


@app.before_request
def _log_request():
    from flask import request
    log.info("%s %s", request.method, request.path)


@app.route("/")
def index():
    articles, error = _get_articles()
    formatted = [
        {**a, "published_at": _fmt_dt(a.get("published_at"))}
        for a in articles
    ]
    generated_at = _fmt_dt(datetime.utcnow().isoformat() + "Z")
    return render_template(
        "index.html",
        articles=formatted,
        error=error,
        generated_at=generated_at,
    )


@app.route("/api/feed")
def api_feed():
    articles, error = _get_articles()
    return jsonify({"articles": articles, "count": len(articles), "error": error})


@app.route("/healthz")
def healthz():
    return "OK", 200


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.environ.get("PORT", 8080))
    log.info("Starting Cermaq Watch server on 0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    run()
