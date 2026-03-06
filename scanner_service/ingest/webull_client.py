"""
Webull public rankings API — premarket top gainers.

No authentication required. Free public endpoint.
Provides: symbol, price, change%, volume, market cap, exchange.
Runs alongside TradingView as a second premarket discovery source.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

WEBULL_RANKINGS_URL = "https://quotes-gw.webullfintech.com/api/wlas/ranking/topGainers"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}


def fetch_premarket_gainers(
    min_change_pct: float = 5.0,
    min_price: float = 2.0,
    max_price: float = 20.0,
    min_volume: int = 50_000,
    limit: int = 30,
) -> list[dict]:
    """
    Fetch premarket top gainers from Webull public API.

    Returns list of dicts with: symbol, price, change_pct, volume, market_cap, exchange, name.
    Returns empty list on any error (fail-open).
    """
    try:
        params = {
            "regionId": 6,
            "rankType": "preMarket",
            "pageIndex": 1,
            "pageSize": min(limit, 50),
        }

        resp = requests.get(
            WEBULL_RANKINGS_URL,
            headers=HEADERS,
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("data", []):
            ticker = item.get("ticker", {})
            values = item.get("values", {})

            symbol = ticker.get("symbol", "").upper().strip()
            if not symbol or len(symbol) > 5:
                continue

            price = float(values.get("price", 0) or 0)
            change_ratio = float(values.get("changeRatio", 0) or 0)
            change_pct = change_ratio * 100
            volume = int(ticker.get("volume", 0) or 0)
            market_cap = float(ticker.get("marketValue", 0) or 0)
            exchange = ticker.get("disExchangeCode", "")

            # Filters
            if price < min_price or price > max_price:
                continue
            if change_pct < min_change_pct:
                continue
            if volume < min_volume:
                continue
            # Skip OTC / non-standard exchanges
            if exchange not in ("NASDAQ", "NYSE", "AMEX"):
                continue

            results.append({
                "symbol": symbol,
                "price": price,
                "change_pct": round(change_pct, 1),
                "volume": volume,
                "market_cap": market_cap,
                "exchange": exchange,
                "name": ticker.get("name", "")[:50],
                "source": "webull_premarket",
            })

        logger.info(
            "[WEBULL] Premarket gainers: %d results (%d after filters)",
            len(data.get("data", [])),
            len(results),
        )
        return results

    except requests.RequestException as e:
        logger.warning("[WEBULL] Premarket fetch failed: %s", e)
        return []
    except Exception as e:
        logger.warning("[WEBULL] Unexpected error: %s", e)
        return []
