"""
Research Server Client
========================
Connects to Morpheus Research Server for sector intelligence.

Base URL: http://RESEARCH1:9200 (configurable via env RESEARCH_SERVER)

Endpoints:
  GET /api/sector/heatmap     — sector heat scores
  GET /api/sector/symbol/{SYM} — symbol sector classification

Caches heatmap for 60s and symbol classifications indefinitely (per session).
Falls back gracefully if research server is unavailable.
"""

import asyncio
import logging
import time
from typing import Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# Defaults
import os
DEFAULT_BASE_URL = os.getenv("RESEARCH_SERVER", "http://RESEARCH1:9200")
HEATMAP_CACHE_TTL = 60  # seconds
REQUEST_TIMEOUT = 5.0  # seconds — don't slow the scan loop


class ResearchClient:
    """
    Client for Morpheus Research Server sector intelligence.

    Gracefully degrades if server is unavailable:
      sector = "unknown", heat_score = 0.30, multiplier = 1.0
    """

    FALLBACK_SECTOR = "unknown"
    FALLBACK_HEAT = 0.30

    def __init__(self, base_url: str = DEFAULT_BASE_URL):
        self._base_url = base_url.rstrip("/")
        self._available = True
        self._last_check: float = 0
        self._check_interval = 30  # re-check availability every 30s after failure

        # Heatmap cache
        self._heatmap: Dict[str, dict] = {}
        self._heatmap_ts: float = 0

        # Symbol classification cache (per session, doesn't expire)
        self._symbol_cache: Dict[str, dict] = {}

        # Stats
        self._fetch_count = 0
        self._fail_count = 0

    async def _get(self, path: str) -> Optional[dict]:
        """HTTP GET with timeout and graceful failure."""
        if not self._available:
            if time.time() - self._last_check < self._check_interval:
                return None
            # Re-check availability
            logger.info("[RESEARCH] Re-checking research server availability...")

        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    self._available = True
                    self._fetch_count += 1
                    return resp.json()
                else:
                    logger.debug(f"[RESEARCH] {path} returned {resp.status_code}")
                    return None
        except Exception as e:
            if self._available:
                logger.warning(f"[RESEARCH] Server unavailable: {e}")
            self._available = False
            self._last_check = time.time()
            self._fail_count += 1
            return None

    async def get_heatmap(self) -> Dict[str, dict]:
        """
        Fetch sector heatmap (cached 60s).

        Returns dict: {sector_name: {"heat_score": float}, ...}
        Falls back to empty dict if unavailable (all sectors get default heat).
        """
        now = time.time()
        if self._heatmap and (now - self._heatmap_ts) < HEATMAP_CACHE_TTL:
            return self._heatmap

        data = await self._get("/api/sector/heatmap")
        if data and isinstance(data, dict):
            self._heatmap = data
            self._heatmap_ts = now
            logger.info(
                f"[RESEARCH] Heatmap refreshed: {len(data)} sectors | "
                f"hot={sum(1 for v in data.values() if v.get('heat_score', 0) >= 0.7)}"
            )
        return self._heatmap

    async def get_symbol_sector(self, symbol: str) -> dict:
        """
        Get sector classification for a symbol (cached per session).

        Returns: {"symbol": str, "sector": str, "asset_type": str, "cap_bucket": str}
        Falls back to {"sector": "unknown"} if unavailable.
        """
        symbol = symbol.upper()
        if symbol in self._symbol_cache:
            return self._symbol_cache[symbol]

        data = await self._get(f"/api/sector/symbol/{symbol}")
        if data and isinstance(data, dict) and "sector" in data:
            self._symbol_cache[symbol] = data
            return data

        # Fallback
        fallback = {
            "symbol": symbol,
            "sector": self.FALLBACK_SECTOR,
            "asset_type": "unknown",
            "cap_bucket": "unknown",
        }
        self._symbol_cache[symbol] = fallback
        return fallback

    async def get_symbol_sectors_batch(self, symbols: list[str]) -> Dict[str, dict]:
        """Batch fetch sector classifications (uses cache, parallel for misses)."""
        results = {}
        to_fetch = []

        for sym in symbols:
            sym = sym.upper()
            if sym in self._symbol_cache:
                results[sym] = self._symbol_cache[sym]
            else:
                to_fetch.append(sym)

        if to_fetch and self._available:
            # Fetch in parallel (max 10 concurrent to be polite)
            sem = asyncio.Semaphore(10)
            async def _fetch(s):
                async with sem:
                    return await self.get_symbol_sector(s)
            await asyncio.gather(*[_fetch(s) for s in to_fetch])
            for sym in to_fetch:
                results[sym] = self._symbol_cache.get(sym, {
                    "symbol": sym, "sector": self.FALLBACK_SECTOR,
                    "asset_type": "unknown", "cap_bucket": "unknown",
                })
        else:
            for sym in to_fetch:
                results[sym] = {
                    "symbol": sym, "sector": self.FALLBACK_SECTOR,
                    "asset_type": "unknown", "cap_bucket": "unknown",
                }
                self._symbol_cache[sym] = results[sym]

        return results

    def get_heat_score(self, sector: str) -> float:
        """Get cached heat score for a sector. Returns FALLBACK_HEAT if unknown."""
        if not self._heatmap:
            return self.FALLBACK_HEAT
        entry = self._heatmap.get(sector.lower(), self._heatmap.get(sector, {}))
        return entry.get("heat_score", self.FALLBACK_HEAT) if entry else self.FALLBACK_HEAT

    def get_status(self) -> dict:
        return {
            "base_url": self._base_url,
            "available": self._available,
            "heatmap_sectors": len(self._heatmap),
            "heatmap_age_seconds": round(time.time() - self._heatmap_ts, 1) if self._heatmap_ts else None,
            "symbols_cached": len(self._symbol_cache),
            "fetch_count": self._fetch_count,
            "fail_count": self._fail_count,
        }


# Singleton
_client: Optional[ResearchClient] = None


def get_research_client(base_url: str = DEFAULT_BASE_URL) -> ResearchClient:
    global _client
    if _client is None:
        _client = ResearchClient(base_url=base_url)
    return _client
