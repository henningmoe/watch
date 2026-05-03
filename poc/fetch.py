"""Fetching articles from Miniflux and Google Programmable Search Engine."""

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)


def fetch_miniflux() -> list[dict]:
    """Fetch articles published in the last 24 hours from Miniflux.

    Reads MINIFLUX_URL and MINIFLUX_API_KEY from environment variables.
    Paginates through all results using limit=50 per request.
    """
    base_url = os.environ["MINIFLUX_URL"].rstrip("/")
    api_key = os.environ["MINIFLUX_API_KEY"]
    headers = {"X-Auth-Token": api_key}

    published_after = datetime.now(timezone.utc) - timedelta(hours=24)
    after_ts = int(published_after.timestamp())

    articles: list[dict] = []
    offset = 0
    limit = 50

    while True:
        response = requests.get(
            f"{base_url}/v1/entries",
            headers=headers,
            params={
                "published_after": after_ts,
                "limit": limit,
                "offset": offset,
                "order": "published_at",
                "direction": "desc",
            },
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        entries = data.get("entries") or []
        for entry in entries:
            articles.append({
                "id": entry["id"],
                "title": entry["title"],
                "url": entry["url"],
                "content": entry.get("content", ""),
                "published_at": entry.get("published_at"),
                "source_name": entry.get("feed", {}).get("title"),
                "image_url": _extract_image(entry.get("content", "")),
            })

        if offset + limit >= data.get("total", 0):
            break
        offset += limit

    return articles


def _extract_image(html: str) -> str | None:
    """Return the first <img> src found in *html*, or None."""
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    img = soup.find("img")
    if img and img.get("src"):
        return img["src"]
    return None


def _fetch_og_image(url: str) -> Optional[str]:
    """Fetch the OG/twitter/image_src image for a URL. Returns None on failure."""
    _DESKTOP_UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    _MOBILE_UA = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.0 Mobile/15E148 Safari/604.1"
    )
    headers = {
        "User-Agent": _DESKTOP_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "no,en-US;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    try:
        response = requests.get(url, timeout=5, headers=headers)
        if response.status_code == 403:
            headers["User-Agent"] = _MOBILE_UA
            response = requests.get(url, timeout=5, headers=headers)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        og = soup.find("meta", property="og:image")
        if og and og.get("content"):
            return urljoin(url, og["content"])

        tw = soup.find("meta", attrs={"name": "twitter:image"})
        if tw and tw.get("content"):
            return urljoin(url, tw["content"])

        link = soup.find("link", attrs={"rel": "image_src"})
        if link and link.get("href"):
            return urljoin(url, link["href"])

        return None
    except Exception as exc:
        log.warning("OG-fetch feilet for %s: %s", url, exc)
        return None


def enrich_industry_with_og_images(articles: list[dict]) -> None:
    """Fetch OG images for industry articles that have no image_url. Mutates in place."""
    targets = [a for a in articles if a.get("scope") == "industry" and not a.get("image_url")]
    if not targets:
        return

    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(_fetch_og_image, [a["url"] for a in targets]))

    success_count = 0
    for article, og_image in zip(targets, results):
        if og_image:
            article["image_url"] = og_image
            success_count += 1
    log.info("Hentet OG-image for %d av %d bransjeartikler", success_count, len(targets))


def fetch_google_pse(query: str, days: int = 1) -> list[dict]:
    """Search Google PSE for recent articles matching *query* within *days* days."""
    raise NotImplementedError


if __name__ == "__main__":
    results = fetch_miniflux()
    print(f"Artikler funnet: {len(results)}")
    if results:
        print(f"Første tittel: {results[0]['title']}")
