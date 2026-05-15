"""Bulletin pipeline orchestrator — fetch data, generate, factcheck, persist."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .factcheck import run_factcheck
from .generator import run_generator
from .schema import BulletinVerified

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent / "data"
_INDEX_FILE = _DATA_DIR / "index.json"

# ── Data fetchers (pull from Watch DB / finance module) ──────────────────────

def fetch_weekly_feed(from_date: date, to_date: date) -> list[dict]:
    """Fetch classified articles from the past week from the Watch DB."""
    try:
        from poc.db import SessionLocal, Article, is_db_available
        from sqlalchemy import func as _func

        if not is_db_available():
            log.warning("DB not available — returning empty weekly feed")
            return []

        cutoff = datetime.combine(from_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        end_dt = datetime.combine(to_date, datetime.max.time()).replace(tzinfo=timezone.utc)

        with SessionLocal() as session:
            rows = (
                session.query(Article)
                .filter(
                    Article.classified_at.isnot(None),
                    Article.scope != "irrelevant",
                    _func.coalesce(Article.published_at, Article.fetched_at) >= cutoff,
                    _func.coalesce(Article.published_at, Article.fetched_at) <= end_dt,
                )
                .order_by(_func.coalesce(Article.published_at, Article.fetched_at).desc())
                .limit(200)
                .all()
            )
            return [
                {
                    "source": r.source_name or r.source_domain or "",
                    "tier": _infer_tier(r.source_domain or ""),
                    "title": r.title or "",
                    "url": r.url or "",
                    "published_at": (r.published_at or r.fetched_at or datetime.now(timezone.utc)).isoformat(),
                    "body": (r.summaries or {}).get("no", "") if r.summaries else "",
                    "region": r.region or "global",
                    "scope": r.scope or "",
                    "tone": r.tone or "",
                }
                for r in rows
            ]
    except Exception as exc:
        log.error("fetch_weekly_feed failed: %s", exc)
        return []


def _infer_tier(domain: str) -> int:
    """Infer source tier based on domain."""
    tier1_domains = {
        "e24.no", "dn.no", "aftenposten.no", "nrk.no",
        "intrafish.no", "ilaks.no", "kyst.no", "salmonbusiness.com",
        "fishfarmingexpert.com", "undercurrentnews.com", "reuters.com",
        "bloomberg.com", "ft.com", "wsj.com", "ap.org",
        "salmonexpert.cl", "aqua.cl", "mercurio.cl",
        "globeandmail.com", "cbc.ca", "seawestnews.com",
        "mattilsynet.no", "sjomatradet.no", "ssb.no",
    }
    tier2_domains = {
        "avisanordland.no", "ifinnmark.no", "fiskeribladet.no",
        "wwf.no", "bellona.no",
    }
    for t1 in tier1_domains:
        if t1 in domain:
            return 1
    for t2 in tier2_domains:
        if t2 in domain:
            return 2
    return 3


def fetch_market_data() -> dict:
    """Fetch current market data from finance module."""
    try:
        from poc.finance import fetch_norges_bank_rates, fetch_all_stocks, fetch_all_salmon_prices
        rates = fetch_norges_bank_rates() or {}
        stocks = fetch_all_stocks() or {}
        salmon = fetch_all_salmon_prices() or {}
        return {"rates": rates, "stocks": stocks, "salmon_prices": salmon}
    except Exception as exc:
        log.warning("fetch_market_data failed: %s", exc)
        return {}


def fetch_calendar_next_week() -> list[dict]:
    """Fetch calendar events for the coming week."""
    try:
        from poc.db import SessionLocal, CalendarEvent, is_db_available

        if not is_db_available():
            return []

        now = datetime.now(timezone.utc)
        next_week_end = now + timedelta(days=14)

        with SessionLocal() as session:
            events = (
                session.query(CalendarEvent)
                .filter(CalendarEvent.event_date >= now, CalendarEvent.event_date <= next_week_end)
                .order_by(CalendarEvent.event_date)
                .all()
            )
            return [
                {
                    "title": e.title,
                    "event_date": e.event_date.isoformat(),
                    "event_type": e.event_type or "",
                    "company": e.company or "",
                    "description": e.description or "",
                }
                for e in events
            ]
    except Exception as exc:
        log.warning("fetch_calendar_next_week failed: %s", exc)
        return []


# ── Index management ─────────────────────────────────────────────────────────

def _load_index() -> list[dict]:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    if _INDEX_FILE.exists():
        return json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
    return []


def _save_index(index: list[dict]) -> None:
    _INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")


def _next_bulletin_number() -> int:
    index = _load_index()
    if not index:
        return 1
    return max(b["nr"] for b in index) + 1


def _persist_bulletin(verified: BulletinVerified) -> Path:
    """Write verified bulletin JSON to data/ and update index."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    filename = f"bulletin-{verified.utgave_nummer:04d}-{verified.publisert_dato}.json"
    path = _DATA_DIR / filename
    path.write_text(
        json.dumps(dataclasses.asdict(verified), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("Bulletin saved to %s", path)

    index = _load_index()
    # Remove existing entry for this number (re-run case)
    index = [b for b in index if b["nr"] != verified.utgave_nummer]
    index.insert(0, {
        "nr": verified.utgave_nummer,
        "dato": verified.publisert_dato,
        "dato_display": _format_dato(verified.publisert_dato),
        "oneliner": verified.hovedsak.headline,
        "html_url": f"/bulletin/{verified.utgave_nummer}",
        "pdf_url":  f"/bulletin/{verified.utgave_nummer}/pdf",
    })
    # Keep newest first
    index.sort(key=lambda b: b["nr"], reverse=True)
    _save_index(index)
    log.info("Index updated — %d bulletins", len(index))
    return path


def _format_dato(iso_date: str) -> str:
    """Format '2026-05-16' → 'Fredag 16. mai 2026'."""
    dag_names = ["Mandag", "Tirsdag", "Onsdag", "Torsdag", "Fredag", "Lørdag", "Søndag"]
    mnd_names = ["", "januar", "februar", "mars", "april", "mai", "juni",
                 "juli", "august", "september", "oktober", "november", "desember"]
    try:
        d = date.fromisoformat(iso_date)
        return f"{dag_names[d.weekday()]} {d.day}. {mnd_names[d.month]} {d.year}"
    except Exception:
        return iso_date


# ── Main entry point ─────────────────────────────────────────────────────────

def generate_weekly_bulletin(
    week_start: date | None = None,
    *,
    api_key: str | None = None,
) -> BulletinVerified:
    """Full pipeline: fetch → generate → factcheck → persist.

    Args:
        week_start: Monday of the bulletin week (defaults to current week's Monday).
        api_key: Anthropic API key (defaults to ANTHROPIC_API_KEY env var).

    Returns:
        Validated and factchecked BulletinVerified object.
    """
    if week_start is None:
        today = date.today()
        week_start = today - timedelta(days=today.weekday())  # this Monday

    week_end = week_start + timedelta(days=6)
    utgave_nummer = _next_bulletin_number()
    publisert_dato = week_start + timedelta(days=4)  # Friday

    log.info(
        "Starting bulletin pipeline — utgave %d, uke %s–%s",
        utgave_nummer, week_start.isoformat(), week_end.isoformat(),
    )

    # 1. Fetch data
    log.info("Fetching weekly feed...")
    weekly_feed = fetch_weekly_feed(week_start, week_end)
    log.info("Weekly feed: %d articles", len(weekly_feed))

    log.info("Fetching market data...")
    market_data = fetch_market_data()

    log.info("Fetching calendar...")
    calendar_data = fetch_calendar_next_week()
    log.info("Calendar: %d events", len(calendar_data))

    # 2. Generate draft
    log.info("Running generator (claude-opus-4-7)...")
    draft = run_generator(
        weekly_feed=weekly_feed,
        market_data=market_data,
        calendar_data=calendar_data,
        utgave_nummer=utgave_nummer,
        api_key=api_key,
    )
    log.info("Draft generated — headline: '%s'", draft.hovedsak.headline)

    # Override dates from pipeline (source of truth)
    draft.utgave_nummer = utgave_nummer
    draft.publisert_dato = publisert_dato.isoformat()

    # 3. Factcheck
    log.info("Running factcheck (claude-opus-4-7)...")
    verified = run_factcheck(draft, source_pool=weekly_feed, api_key=api_key)
    log.info("Factcheck complete")

    # 4. Persist
    path = _persist_bulletin(verified)
    log.info("Pipeline complete — bulletin at %s", path)

    return verified
