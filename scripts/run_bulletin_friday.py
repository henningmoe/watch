#!/usr/bin/env python3
"""Cron script — run every Friday at 06:00 to generate the weekly News Bulletin.

Crontab entry:
    0 6 * * 5 /path/to/venv/bin/python /path/to/watch/scripts/run_bulletin_friday.py

Environment variables required:
    ANTHROPIC_API_KEY   — Anthropic API key
    SLACK_WEBHOOK_URL   — Optional: Slack webhook for completion notification
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import date
from pathlib import Path

# Ensure poc package is importable when run from scripts/
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("run_bulletin_friday")


def send_slack(message: str) -> None:
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        log.info("No SLACK_WEBHOOK_URL set, skipping Slack notification")
        return
    try:
        import requests
        resp = requests.post(webhook_url, json={"text": message}, timeout=10)
        resp.raise_for_status()
        log.info("Slack notification sent")
    except Exception as exc:
        log.warning("Slack notification failed: %s", exc)


def main() -> None:
    log.info("News Bulletin Friday pipeline starting — %s", date.today().isoformat())

    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY is not set — aborting")
        sys.exit(1)

    from poc.bulletin.pipeline import generate_weekly_bulletin

    try:
        bulletin = generate_weekly_bulletin()
    except Exception as exc:
        log.exception("Pipeline failed: %s", exc)
        send_slack(f":x: News Bulletin generering feilet: {exc}")
        sys.exit(1)

    fc = bulletin._factcheck_report
    fc_summary = ""
    if fc:
        fc_summary = (
            f" Faktasjekk: {fc.claims_verified} verifisert, "
            f"{fc.claims_corrected} korrigert, {fc.claims_removed} fjernet."
        )

    message = (
        f":newspaper: *News Bulletin utgave {bulletin.utgave_nummer}* er publisert. "
        f"_{bulletin.hovedsak.headline}_ "
        f"Åpne arkivet i Cermaq Watch: /bulletin{fc_summary}"
    )
    log.info("Pipeline success — %s", message)
    send_slack(message)


if __name__ == "__main__":
    main()
