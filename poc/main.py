"""Entry point for the Cermaq Media Watch pipeline.

Run with:
    python -m poc.main --dry-run
    python -m poc.main --serve
"""

import argparse
import logging
from pathlib import Path

from poc.fetch import fetch_miniflux
from poc.render import render_html

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main(dry_run: bool = False, serve: bool = False) -> None:
    if serve:
        from poc.server import run
        run()
        return

    if dry_run:
        log.info("Cermaq Media Watch POC starter")
        log.info("Dry-run mode aktiv — ingen mail sendes")
        log.info("Henter artikler fra Miniflux...")
        articles = fetch_miniflux()
        log.info("Hentet %d artikler", len(articles))
        if articles:
            log.info("Første tittel: %s", articles[0].get("title", "(ingen tittel)"))
        html = render_html(articles)
        out = Path("output/index.html")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        log.info("HTML skrevet til output/index.html")
        return

    parser.print_help()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cermaq Media Watch POC")
    parser.add_argument("--dry-run", action="store_true", help="Kjør uten å sende mail")
    parser.add_argument("--serve", action="store_true", help="Start Flask-webserveren")
    args = parser.parse_args()
    main(dry_run=args.dry_run, serve=args.serve)
