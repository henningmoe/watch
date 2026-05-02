"""Fetching articles from Miniflux and Google Programmable Search Engine."""

import os
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup


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


def fetch_google_pse(query: str, days: int = 1) -> list[dict]:
    """Search Google PSE for recent articles matching *query* within *days* days."""
    raise NotImplementedError


if __name__ == "__main__":
    results = fetch_miniflux()
    print(f"Artikler funnet: {len(results)}")
    if results:
        print(f"Første tittel: {results[0]['title']}")
