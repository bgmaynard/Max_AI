"""
News Pipeline with Source Redundancy
======================================
Wraps multiple news/catalyst sources with fallback ordering.
If a source fails, the pipeline continues with remaining sources.
Logs outages to logs/news_pipeline.log.

Fallback order:
  1. Benzinga (primary RSS)
  2. Finviz top gainers (catalyst proxy via big movers)
  3. RSS (remaining static feeds: Seeking Alpha, GlobeNewsWire, SEC EDGAR)
  4. Yahoo movers (dynamic universe-based feed)
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Set

from scanner_service.ingest.news_client import NewsClient, NewsAlert, get_news_client

# Pipeline-specific logger with file handler
pipeline_logger = logging.getLogger("news_pipeline")
_log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs")
os.makedirs(_log_dir, exist_ok=True)
_handler = RotatingFileHandler(
    os.path.join(_log_dir, "news_pipeline.log"),
    maxBytes=5_000_000,
    backupCount=3,
)
_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
pipeline_logger.addHandler(_handler)
pipeline_logger.setLevel(logging.INFO)

# Also log to main logger
logger = logging.getLogger(__name__)


class SourceStatus:
    """Tracks health of a single news source."""

    def __init__(self, name: str):
        self.name = name
        self.ok = True
        self.last_success: Optional[datetime] = None
        self.last_failure: Optional[datetime] = None
        self.consecutive_failures = 0
        self.total_failures = 0
        self.total_successes = 0
        self.last_error: str = ""

    def record_success(self):
        self.ok = True
        self.last_success = datetime.utcnow()
        self.consecutive_failures = 0
        self.total_successes += 1

    def record_failure(self, error: str):
        self.ok = False
        self.last_failure = datetime.utcnow()
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_error = error
        pipeline_logger.warning(
            f"[OUTAGE] {self.name} failed (consecutive={self.consecutive_failures}): {error}"
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ok": self.ok,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_failure": self.last_failure.isoformat() if self.last_failure else None,
            "consecutive_failures": self.consecutive_failures,
            "total_failures": self.total_failures,
            "total_successes": self.total_successes,
            "last_error": self.last_error,
        }


# Benzinga feed URLs (primary)
BENZINGA_FEEDS = [
    ("https://www.benzinga.com/news/feed", "benzinga_news"),
    ("https://www.benzinga.com/markets/feed", "benzinga_markets"),
]

# Other RSS feeds (tertiary)
OTHER_RSS_FEEDS = [
    ("https://seekingalpha.com/market_currents.xml", "seekingalpha"),
    ("https://www.globenewswire.com/RssFeed/subjectcode/14-Mergers%20and%20Acquisitions/feedTitle/GlobeNewswire%20-%20M%26A", "gnw_mergers"),
    ("https://www.globenewswire.com/RssFeed/subjectcode/01-Business%20Operations/feedTitle/GlobeNewswire%20-%20Business", "gnw_business"),
    ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=20&search_text=&start=0&output=atom", "sec_edgar"),
]


class NewsPipeline:
    """
    Orchestrates news fetching across multiple sources with fallback.
    Each source is tried independently; failures in one do not block others.
    """

    def __init__(self):
        self._news_client: Optional[NewsClient] = None
        self._universe_symbols: Set[str] = set()
        self._running = False
        self._poll_interval = 30
        self._poll_count = 0

        # Source health tracking
        self.sources: Dict[str, SourceStatus] = {
            "benzinga": SourceStatus("benzinga"),
            "finviz": SourceStatus("finviz"),
            "rss_other": SourceStatus("rss_other"),
            "yahoo": SourceStatus("yahoo"),
        }

    @property
    def news_client(self) -> NewsClient:
        if self._news_client is None:
            self._news_client = get_news_client()
        return self._news_client

    def set_universe_symbols(self, symbols: Set[str]):
        self._universe_symbols = symbols
        self.news_client.set_universe_symbols(symbols)

    async def _fetch_benzinga(self) -> List[NewsAlert]:
        """Source 1 (Primary): Benzinga RSS feeds."""
        alerts = []
        try:
            for url, source_name in BENZINGA_FEEDS:
                entries = await self.news_client._fetch_rss_feed(url, source_name)
                for entry in entries:
                    alert = self.news_client._process_entry(entry, source_name)
                    if alert:
                        alerts.append(alert)
            self.sources["benzinga"].record_success()
        except Exception as e:
            self.sources["benzinga"].record_failure(str(e))
        return alerts

    async def _fetch_finviz_catalysts(self) -> List[NewsAlert]:
        """Source 2 (Secondary): Finviz top gainers as catalyst proxy.

        Big movers on Finviz often have catalysts. We create synthetic
        news alerts for significant movers (>15% change) as a fallback
        catalyst signal when RSS feeds are down.
        """
        alerts = []
        try:
            from scanner_service.ingest import finviz_client

            gainers = await finviz_client.get_top_gainers(
                max_price=20.0,
                min_change=15.0,
                limit=10,
            )
            for g in gainers:
                symbol = g.get("symbol", "")
                change = g.get("change_pct", 0)
                if not symbol or change < 15:
                    continue

                # Create synthetic news alert for big mover
                alert_id = f"finviz_{symbol}_{datetime.utcnow().strftime('%H%M')}"
                if alert_id in self.news_client.seen_ids:
                    continue
                self.news_client.seen_ids.add(alert_id)

                urgency = "high" if change >= 30 else "medium"
                alerts.append(NewsAlert(
                    id=alert_id,
                    headline=f"{symbol} surging +{change:.1f}% — potential catalyst (Finviz)",
                    symbols=[symbol],
                    source="finviz_catalyst",
                    published_at=datetime.utcnow(),
                    detected_at=datetime.utcnow(),
                    sentiment="bullish",
                    urgency=urgency,
                    catalyst_type="momentum",
                    confidence=min(0.50 + (change / 200.0), 0.85),
                ))
            self.sources["finviz"].record_success()
        except Exception as e:
            self.sources["finviz"].record_failure(str(e))
        return alerts

    async def _fetch_other_rss(self) -> List[NewsAlert]:
        """Source 3 (Tertiary): Seeking Alpha, GlobeNewsWire, SEC EDGAR."""
        alerts = []
        try:
            for url, source_name in OTHER_RSS_FEEDS:
                entries = await self.news_client._fetch_rss_feed(url, source_name)
                for entry in entries:
                    alert = self.news_client._process_entry(entry, source_name)
                    if alert:
                        alerts.append(alert)
            self.sources["rss_other"].record_success()
        except Exception as e:
            self.sources["rss_other"].record_failure(str(e))
        return alerts

    async def _fetch_yahoo(self) -> List[NewsAlert]:
        """Source 4 (Fallback): Yahoo Finance dynamic feed."""
        alerts = []
        try:
            dynamic_feeds = self.news_client._build_dynamic_feeds()
            for url, source_name in dynamic_feeds:
                entries = await self.news_client._fetch_rss_feed(url, source_name)
                for entry in entries:
                    alert = self.news_client._process_entry(entry, source_name)
                    if alert:
                        alerts.append(alert)
            self.sources["yahoo"].record_success()
        except Exception as e:
            self.sources["yahoo"].record_failure(str(e))
        return alerts

    async def poll(self) -> List[NewsAlert]:
        """
        Poll all sources in parallel. Each source runs independently;
        failures in one do not affect others.
        """
        results = await asyncio.gather(
            self._fetch_benzinga(),
            self._fetch_finviz_catalysts(),
            self._fetch_other_rss(),
            self._fetch_yahoo(),
            return_exceptions=True,
        )

        all_alerts = []
        source_names = ["benzinga", "finviz", "rss_other", "yahoo"]
        for i, result in enumerate(results):
            if isinstance(result, list):
                all_alerts.extend(result)
            elif isinstance(result, Exception):
                self.sources[source_names[i]].record_failure(str(result))

        # Update news client state
        self.news_client.recent_alerts = (
            all_alerts + self.news_client.recent_alerts
        )[:self.news_client.max_recent]
        self._poll_count += 1

        # Emit advisories
        if all_alerts:
            self.news_client._emit_news_advisories(all_alerts)

        # Log summary
        active_sources = sum(1 for s in self.sources.values() if s.ok)
        if all_alerts:
            logger.info(
                f"[NEWS_PIPELINE] Poll #{self._poll_count}: {len(all_alerts)} alerts "
                f"from {active_sources}/4 sources"
            )
            pipeline_logger.info(
                f"Poll #{self._poll_count}: {len(all_alerts)} alerts, "
                f"sources_ok={active_sources}/4"
            )

        # Log outage summary if any source is down
        down_sources = [s.name for s in self.sources.values() if not s.ok]
        if down_sources:
            pipeline_logger.warning(
                f"[DEGRADED] Sources down: {', '.join(down_sources)} — "
                f"continuing with {active_sources}/4 sources"
            )

        return all_alerts

    async def _poll_loop(self):
        """Main polling loop with source redundancy."""
        pipeline_logger.info(
            f"News pipeline started | interval={self._poll_interval}s | sources=4"
        )
        logger.info(
            f"[NEWS_PIPELINE] Started with redundancy | interval={self._poll_interval}s | "
            f"order: benzinga > finviz > rss > yahoo"
        )

        while self._running:
            try:
                await self.poll()
            except Exception as e:
                pipeline_logger.error(f"Poll loop error: {e}")
                logger.error(f"[NEWS_PIPELINE] Poll loop error: {e}")
            await asyncio.sleep(self._poll_interval)

        pipeline_logger.info("News pipeline stopped")

    def start(self, poll_interval: int = 30):
        if self._running:
            return
        self._poll_interval = poll_interval
        self._running = True
        asyncio.create_task(self._poll_loop())

    def stop(self):
        self._running = False

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "poll_interval": self._poll_interval,
            "poll_count": self._poll_count,
            "sources": {name: s.to_dict() for name, s in self.sources.items()},
            "sources_ok": sum(1 for s in self.sources.values() if s.ok),
            "sources_total": len(self.sources),
            "universe_symbols_tracked": len(self._universe_symbols),
            "recent_alerts": len(self.news_client.recent_alerts),
        }


# Singleton
_pipeline: Optional[NewsPipeline] = None


def get_news_pipeline() -> NewsPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = NewsPipeline()
    return _pipeline
