"""Finance data fetchers: Norges Bank exchange rates and Yahoo Finance stock quotes."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

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
