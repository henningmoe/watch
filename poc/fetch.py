"""Fetching articles from FreshRSS and Google Programmable Search Engine."""


def fetch_freshrss() -> list[dict]:
    """Fetch unread articles from FreshRSS via the GReader API."""
    raise NotImplementedError


def fetch_google_pse(query: str, days: int = 1) -> list[dict]:
    """Search Google PSE for recent articles matching *query* within *days* days."""
    raise NotImplementedError


if __name__ == "__main__":
    print(fetch_freshrss())
    print(fetch_google_pse("Cermaq"))
