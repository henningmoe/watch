"""Entry point for the Cermaq Media Watch pipeline.

Run with:
    python -m poc.main
    python -m poc.main --dry-run
"""

import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main(dry_run: bool = False) -> None:
    log.info("Cermaq Media Watch POC starter")
    if dry_run:
        log.info("Dry-run mode aktiv — ingen mail sendes")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cermaq Media Watch POC")
    parser.add_argument("--dry-run", action="store_true", help="Kjør uten å sende mail")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
