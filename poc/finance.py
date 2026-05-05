"""Finance data fetchers: Norges Bank exchange rates, Yahoo Finance stock quotes,
and salmon price indices (Fish Pool, Nasdaq Salmon)."""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

log = logging.getLogger(__name__)

CURRENCIES = ["USD", "EUR", "CLP", "CAD", "JPY", "CNY"]

STOCK_TICKERS = {
    "Mowi": "MOWI.OL",
    "SalMar": "SALM.OL",
    "Grieg Seafood": "GSF.OL",
    "Bakkafrost": "BAKKA.OL",
    "Lerøy": "LSG.OL",
}

# CLP and JPY are reported per 100 units by Norges Bank
_UNIT_DIVISOR = {"CLP": 100.0, "JPY": 100.0}


def fetch_norges_bank_rates() -> Optional[Dict]:
    """Fetch latest FX rates from Norges Bank (free API).

    Returns dict keyed by currency code:
      {'USD': {'rate': 10.85, 'date': '2026-05-05', 'change_pct': 0.4}, ...}
    """
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=7)
    currencies_str = "+".join(CURRENCIES)
    url = (
        f"https://data.norges-bank.no/api/data/EXR/B.{currencies_str}.NOK.SP"
        f"?startPeriod={start.isoformat()}&endPeriod={end.isoformat()}"
        f"&format=sdmx-json&locale=no"
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        datasets = data.get("data", {}).get("dataSets", [{}])
        series_map = datasets[0].get("series", {}) if datasets else {}
        structure = data.get("data", {}).get("structure", {})
        series_dims = structure.get("dimensions", {}).get("series", [])
        obs_dims = structure.get("dimensions", {}).get("observation", [{}])
        time_values = obs_dims[0].get("values", []) if obs_dims else []

        # Find position of BASE_CUR dimension
        currency_pos = next(
            (i for i, d in enumerate(series_dims) if d.get("id") == "BASE_CUR"), None
        )
        if currency_pos is None:
            log.warning("Norges Bank: BASE_CUR dimension not found")
            return None

        currency_values = series_dims[currency_pos].get("values", [])
        result: Dict = {}

        for series_key, series_data in series_map.items():
            parts = series_key.split(":")
            if len(parts) <= currency_pos:
                continue
            cur_idx = int(parts[currency_pos])
            if cur_idx >= len(currency_values):
                continue
            currency = currency_values[cur_idx].get("id")
            if not currency or currency not in CURRENCIES:
                continue

            observations = series_data.get("observations", {})
            sorted_obs = sorted(
                [(int(k), v[0]) for k, v in observations.items() if v and v[0] is not None],
                key=lambda x: x[0],
            )
            if not sorted_obs:
                continue

            latest_idx, latest_raw = sorted_obs[-1]
            date_str = time_values[latest_idx].get("id") if latest_idx < len(time_values) else None
            divisor = _UNIT_DIVISOR.get(currency, 1.0)
            rate = round(latest_raw / divisor, 4)

            change_pct = None
            if len(sorted_obs) >= 2:
                prev_raw = sorted_obs[-2][1]
                if prev_raw and prev_raw != 0:
                    change_pct = round((latest_raw - prev_raw) / prev_raw * 100, 2)

            result[currency] = {"rate": rate, "date": date_str, "change_pct": change_pct}

        log.info("Norges Bank: hentet kurser for %s", list(result.keys()))
        return result if result else None

    except Exception as exc:
        log.exception("Norges Bank fetch feilet: %s", exc)
        return None


def fetch_stock_quote(ticker: str) -> Optional[Dict]:
    """Fetch latest stock price and changes from Yahoo Finance."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"range": "1mo", "interval": "1d", "includePrePost": "false"}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CermaqWatch/1.0)"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("chart", {}).get("result", [])
        if not results:
            return None
        result = results[0]
        meta = result.get("meta", {})
        timestamps = result.get("timestamp", [])
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])

        valid = [(t, c) for t, c in zip(timestamps, closes) if c is not None]
        if not valid:
            return None

        current = valid[-1][1]

        def _chg(days_back: int) -> Optional[float]:
            if len(valid) <= days_back:
                return None
            prev = valid[-1 - days_back][1]
            if not prev:
                return None
            return round((current - prev) / prev * 100, 2)

        return {
            "price": round(current, 2),
            "currency": meta.get("currency", "NOK"),
            "change_1d": _chg(1),
            "change_7d": _chg(5),   # ~5 trading days ≈ 7 calendar days
            "change_30d": _chg(22),
        }
    except Exception as exc:
        log.warning("Stock fetch feilet for %s: %s", ticker, exc)
        return None


def fetch_all_stocks() -> Dict:
    """Fetch stock quotes for all competitor tickers."""
    result: Dict = {}
    for name, ticker in STOCK_TICKERS.items():
        quote = fetch_stock_quote(ticker)
        if quote:
            result[name] = {**quote, "ticker": ticker}
    log.info("Aksjer hentet: %d av %d", len(result), len(STOCK_TICKERS))
    return result


def fetch_stock_quote_history(ticker: str, days: int = 90) -> Optional[Dict]:
    """Fetch historical daily closing prices for a ticker via Yahoo Finance."""
    if days <= 30:
        range_str, interval = "1mo", "1d"
    elif days <= 90:
        range_str, interval = "3mo", "1d"
    elif days <= 180:
        range_str, interval = "6mo", "1d"
    else:
        range_str, interval = "1y", "1wk"

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"range": range_str, "interval": interval, "includePrePost": "false"}
    headers = {"User-Agent": "Mozilla/5.0 (compatible; CermaqWatch/1.0)"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        results = data.get("chart", {}).get("result", [])
        if not results:
            return None
        result = results[0]
        timestamps = result.get("timestamp", [])
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])

        valid = [(t, c) for t, c in zip(timestamps, closes) if c is not None]
        if not valid:
            return None

        return {
            "ticker": ticker,
            "dates": [
                datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat()
                for t, _ in valid
            ],
            "prices": [round(c, 2) for _, c in valid],
        }
    except Exception as exc:
        log.warning("History fetch feilet for %s: %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# Commodity futures via Yahoo Finance
# ---------------------------------------------------------------------------

COMMODITY_TICKERS = {
    "Soya": "ZS=F",
    "Soyamel": "ZM=F",
    "Hvete": "ZW=F",
}

COMMODITY_CURRENCY = {
    "ZS=F": "USd/bu",   # US cents per bushel
    "ZM=F": "USD/ton",
    "ZW=F": "USd/bu",
}


def fetch_commodities() -> Dict:
    """Fetch futures prices for feed-input commodities via Yahoo Finance."""
    result: Dict = {}
    for name, ticker in COMMODITY_TICKERS.items():
        quote = fetch_stock_quote(ticker)
        if quote:
            result[name] = {
                **quote,
                "ticker": ticker,
                "commodity_currency": COMMODITY_CURRENCY.get(ticker, "USD"),
            }
    log.info("Råvarer hentet: %d av %d", len(result), len(COMMODITY_TICKERS))
    return result


# ---------------------------------------------------------------------------
# Salmon price indices
# ---------------------------------------------------------------------------

_FISH_POOL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://fishpool.eu/",
}


def _parse_fish_pool_html(html: str) -> List[Dict]:
    """Extract forward prices from Fish Pool HTML. Returns list of {period, price} dicts."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    prices: List[Dict] = []

    # Strategy 1: look for JSON-LD or inline JSON data blobs
    for script in soup.find_all("script"):
        src = script.string or ""
        # Fish Pool sometimes embeds chart data as JS arrays
        m = re.search(r'"forward[_-]?prices?"?\s*:\s*(\[.*?\])', src, re.DOTALL | re.IGNORECASE)
        if m:
            try:
                import json as _json
                raw = _json.loads(m.group(1))
                for item in raw:
                    if isinstance(item, dict) and item.get("price"):
                        prices.append({"period": str(item.get("period", "")), "price": float(item["price"])})
                if prices:
                    log.info("Fish Pool: JSON-blob strategy OK (%d prices)", len(prices))
                    return prices
            except Exception:
                pass

    # Strategy 2: find a <table> containing NOK/kg values
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        for row in rows:
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            period_cell = cells[0]
            price_cell = cells[1] if len(cells) > 1 else ""
            # Look for quarter/month pattern and a numeric price
            if re.search(r"(Q[1-4]|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", period_cell, re.I):
                price_str = re.sub(r"[^\d.,]", "", price_cell).replace(",", ".")
                try:
                    price = float(price_str)
                    if 20 < price < 500:  # sanity check: salmon price in NOK/kg
                        prices.append({"period": period_cell, "price": price})
                except ValueError:
                    pass
        if prices:
            log.info("Fish Pool: table strategy OK (%d prices)", len(prices))
            return prices

    # Strategy 3: scan all text for price patterns like "Q2 2026: 86.20"
    text = soup.get_text(" ")
    for m in re.finditer(
        r"(Q[1-4]\s+20\d{2}|[A-Z][a-z]{2}\s+20\d{2})\s*[:\s]\s*(\d{2,3}[.,]\d{1,2})",
        text,
    ):
        period = m.group(1).strip()
        price_str = m.group(2).replace(",", ".")
        try:
            price = float(price_str)
            if 20 < price < 500:
                prices.append({"period": period, "price": price})
        except ValueError:
            pass

    if prices:
        log.info("Fish Pool: regex strategy OK (%d prices)", len(prices))
    return prices


def fetch_fish_pool_index() -> Optional[Dict]:
    """Fetch Fish Pool forward prices via web scraping with multiple fallback strategies."""
    # Primary URL
    primary = "https://fishpool.eu/forward-prices/"
    # Fallback: Fish Pool sometimes serves data via a JSON endpoint used by their charts
    json_endpoints = [
        "https://fishpool.eu/wp-json/fishpool/v1/forward-prices",
        "https://fishpool.eu/api/forward-prices",
        "https://fishpool.eu/forward-prices/feed/json",
    ]

    # Try JSON endpoints first (cheaper and more reliable than HTML parsing)
    for url in json_endpoints:
        try:
            resp = requests.get(url, headers=_FISH_POOL_HEADERS, timeout=10)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    # Normalise whatever structure the JSON has
                    prices = []
                    if isinstance(data, list):
                        for item in data:
                            if isinstance(item, dict):
                                period = item.get("period") or item.get("date") or item.get("month") or ""
                                price = item.get("price") or item.get("value") or item.get("forwardPrice")
                                if period and price:
                                    try:
                                        prices.append({"period": str(period), "price": float(price)})
                                    except (TypeError, ValueError):
                                        pass
                    elif isinstance(data, dict):
                        for k, v in data.items():
                            try:
                                prices.append({"period": str(k), "price": float(v)})
                            except (TypeError, ValueError):
                                pass
                    if prices:
                        log.info("Fish Pool JSON endpoint %s OK: %d prices", url, len(prices))
                        return {
                            "source": "Fish Pool",
                            "currency": "NOK/kg",
                            "forward_prices": prices[:8],
                            "last_updated": datetime.now(timezone.utc).isoformat(),
                        }
                except ValueError:
                    pass
        except Exception as exc:
            log.debug("Fish Pool JSON endpoint %s: %s", url, exc)

    # Try HTML scraping
    try:
        session = requests.Session()
        # First hit the home page to get cookies
        session.get("https://fishpool.eu/", headers=_FISH_POOL_HEADERS, timeout=10)
        resp = session.get(primary, headers=_FISH_POOL_HEADERS, timeout=15)
        if resp.status_code == 200:
            prices = _parse_fish_pool_html(resp.text)
            if prices:
                return {
                    "source": "Fish Pool",
                    "currency": "NOK/kg",
                    "forward_prices": prices[:8],
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }
            log.warning("Fish Pool: page fetched but no prices parsed (HTML may have changed)")
        else:
            log.warning("Fish Pool: HTTP %d from %s", resp.status_code, primary)
    except Exception as exc:
        log.warning("Fish Pool scrape feilet: %s", exc)

    return None


def _parse_nasdaq_salmon_html(html: str) -> Optional[Dict]:
    """Extract spot price from Nasdaq Salmon Index HTML."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: JSON-LD or embedded JSON
    for script in soup.find_all("script"):
        src = script.string or ""
        for pattern in [
            r'"price"\s*:\s*"?([\d.,]+)"?',
            r'"spotPrice"\s*:\s*"?([\d.,]+)"?',
            r'"lastPrice"\s*:\s*"?([\d.,]+)"?',
            r'"indexValue"\s*:\s*"?([\d.,]+)"?',
        ]:
            m = re.search(pattern, src, re.IGNORECASE)
            if m:
                price_str = m.group(1).replace(",", ".")
                try:
                    price = float(price_str)
                    if 20 < price < 500:
                        log.info("Nasdaq Salmon: found price via JSON pattern: %.2f", price)
                        return {"spot_price": price}
                except ValueError:
                    pass

    # Strategy 2: look for prominent numeric text near "NOK/kg" or "salmon"
    text = soup.get_text(" ")
    # Typical format: "84.50 NOK/kg" or "Week 18: 84.50"
    for m in re.finditer(r"(\d{2,3}[.,]\d{1,2})\s*(?:NOK|nok|kr)?(?:\s*/\s*kg)?", text):
        price_str = m.group(1).replace(",", ".")
        try:
            price = float(price_str)
            if 30 < price < 300:
                log.info("Nasdaq Salmon: found price via text regex: %.2f", price)
                return {"spot_price": price}
        except ValueError:
            pass

    return None


_NASDAQ_SALMON_URLS = [
    "https://salmonprice.nasdaqomxtrader.com/SalmonPrice",
    "https://salmonprice.nasdaqomxtrader.com/",
    "https://www.nasdaqomxnordic.com/commodities/salmon",
    "https://indexes.nasdaqomx.com/Index/Overview/NQSALMON",
]

_NASDAQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def fetch_nasdaq_salmon_index() -> Optional[Dict]:
    """Fetch Nasdaq Salmon Index spot price, trying several known URLs."""
    for url in _NASDAQ_SALMON_URLS:
        try:
            resp = requests.get(url, headers=_NASDAQ_HEADERS, timeout=12)
            if resp.status_code != 200:
                log.debug("Nasdaq Salmon %s: HTTP %d", url, resp.status_code)
                continue

            # Try JSON first
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                try:
                    data = resp.json()
                    price = None
                    week = None
                    if isinstance(data, dict):
                        for key in ("price", "spotPrice", "lastPrice", "indexValue", "value"):
                            if key in data:
                                price = float(data[key])
                                break
                        week = data.get("week") or data.get("weekNumber")
                    if price and 20 < price < 500:
                        result: Dict = {
                            "source": "Nasdaq Salmon Index",
                            "currency": "NOK/kg",
                            "spot_price": round(price, 2),
                            "last_updated": datetime.now(timezone.utc).isoformat(),
                        }
                        if week:
                            result["week"] = week
                        log.info("Nasdaq Salmon JSON from %s: %.2f NOK/kg", url, price)
                        return result
                except ValueError:
                    pass

            # Fall back to HTML parsing
            parsed = _parse_nasdaq_salmon_html(resp.text)
            if parsed and parsed.get("spot_price"):
                return {
                    "source": "Nasdaq Salmon Index",
                    "currency": "NOK/kg",
                    "spot_price": round(parsed["spot_price"], 2),
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }

        except Exception as exc:
            log.debug("Nasdaq Salmon %s: %s", url, exc)

    log.warning("Nasdaq Salmon: alle URL-er feilet")
    return None


def fetch_urner_barry() -> Optional[Dict]:
    """Urner Barry US import price — requires paid subscription; not yet implemented."""
    return None


def fetch_all_salmon_prices() -> Dict:
    """Fetch all available salmon price indices."""
    result: Dict = {}

    fp = fetch_fish_pool_index()
    if fp:
        result["fish_pool"] = fp

    ns = fetch_nasdaq_salmon_index()
    if ns:
        result["nasdaq"] = ns

    # Urner Barry placeholder — always None for now
    result["urner_barry"] = None

    log.info(
        "Laksepriser hentet: Fish Pool=%s, Nasdaq=%s",
        "OK" if fp else "FEILET",
        "OK" if ns else "FEILET",
    )
    return result
