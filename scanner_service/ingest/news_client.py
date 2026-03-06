"""
News Client for Max Scanner
============================
Fetches news from Benzinga RSS and detects catalysts.
Emits advisories to buffer for pull-based consumption.
"""

import asyncio
import aiohttp
import feedparser
import logging
import hashlib
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, asdict
import pytz

logger = logging.getLogger(__name__)

ET_TZ = pytz.timezone('US/Eastern')

# Catalyst keywords for instant detection
CRITICAL_CATALYSTS = {
    'fda': ['fda approval', 'fda approves', 'fda grants', 'breakthrough therapy',
            'fda clears', 'fda accepts', 'pdufa', 'nda approved'],
    'merger': ['acquisition', 'merger', 'buyout', 'takeover bid', 'acquire',
               'all-cash deal', 'tender offer'],
    'earnings_beat': ['beats estimates', 'eps beat', 'revenue beat', 'earnings surprise',
                      'exceeds expectations', 'blows past'],
    'earnings_miss': ['misses estimates', 'eps miss', 'revenue miss', 'falls short',
                      'below expectations'],
    'contract': ['contract award', 'wins contract', 'awarded contract', 'secures deal',
                 'government contract', 'defense contract'],
    'clinical': ['positive results', 'met primary endpoint', 'phase 3 success',
                 'clinical trial success', 'positive phase', 'trial met'],
    'partnership': ['partnership', 'collaboration', 'strategic alliance', 'joint venture'],
    'upgrade': ['upgrade', 'price target raised', 'initiates buy', 'raises rating'],
    'downgrade': ['downgrade', 'price target cut', 'initiates sell', 'lowers rating'],
    'offering': ['stock offering', 'secondary offering', 'shelf registration', 'dilution'],
    'bankruptcy': ['bankruptcy', 'chapter 11', 'restructuring', 'default'],
    'split': ['stock split', 'reverse split'],
}

# Sentiment keywords
BULLISH_KEYWORDS = ['surge', 'soar', 'jump', 'rally', 'gain', 'positive', 'beats',
                    'approval', 'success', 'breakthrough', 'upgrade', 'buy']
BEARISH_KEYWORDS = ['plunge', 'crash', 'drop', 'fall', 'decline', 'negative', 'miss',
                    'reject', 'fail', 'downgrade', 'sell', 'warning']


@dataclass
class NewsAlert:
    """News alert from MAX_AI"""
    id: str
    headline: str
    symbols: List[str]
    source: str
    published_at: datetime
    detected_at: datetime
    sentiment: str  # bullish, bearish, neutral
    urgency: str    # critical, high, medium, low
    catalyst_type: str
    confidence: float

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "headline": self.headline,
            "symbols": self.symbols,
            "source": self.source,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "detected_at": self.detected_at.isoformat(),
            "sentiment": self.sentiment,
            "urgency": self.urgency,
            "catalyst_type": self.catalyst_type,
            "confidence": self.confidence
        }


class NewsClient:
    """
    News client for MAX_AI Scanner.
    Fetches news from RSS feeds and detects trading catalysts.
    """

    # RSS Feed URLs - Multiple sources for better coverage
    RSS_FEEDS = [
        ("https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL,TSLA,NVDA,AMD,GME,AMC&region=US&lang=en-US", "yahoo"),
        ("https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best", "reuters"),
        ("https://seekingalpha.com/market_currents.xml", "seekingalpha"),
        ("https://www.prnewswire.com/rss/news-releases-list.rss", "prnewswire"),
    ]

    def __init__(self):
        self.seen_ids: Set[str] = set()
        self.recent_alerts: List[NewsAlert] = []
        self.max_recent = 100
        self._running = False
        self._poll_interval = 10  # seconds between polls

    def _generate_id(self, headline: str, source: str) -> str:
        """Generate unique ID for news item"""
        content = f"{headline}:{source}"
        return hashlib.md5(content.encode()).hexdigest()[:12]

    def _extract_symbols(self, text: str) -> List[str]:
        """Extract stock symbols from text"""
        # Look for $SYMBOL pattern
        dollar_symbols = re.findall(r'\$([A-Z]{1,5})\b', text)

        # Look for (NASDAQ: SYMBOL) or (NYSE: SYMBOL) patterns
        exchange_symbols = re.findall(r'\((?:NASDAQ|NYSE|AMEX):\s*([A-Z]{1,5})\)', text, re.IGNORECASE)

        # Combine and dedupe
        symbols = list(set(dollar_symbols + exchange_symbols))
        return symbols[:5]  # Max 5 symbols per news

    def _detect_catalyst(self, headline: str) -> tuple[str, str]:
        """
        Detect catalyst type and urgency from headline.
        Returns (catalyst_type, urgency)
        """
        headline_lower = headline.lower()

        for catalyst, keywords in CRITICAL_CATALYSTS.items():
            for keyword in keywords:
                if keyword in headline_lower:
                    # FDA and merger are critical
                    if catalyst in ['fda', 'merger', 'clinical']:
                        return catalyst, 'critical'
                    elif catalyst in ['earnings_beat', 'earnings_miss', 'contract']:
                        return catalyst, 'high'
                    else:
                        return catalyst, 'medium'

        return 'general', 'low'

    def _analyze_sentiment(self, headline: str) -> tuple[str, float]:
        """
        Analyze sentiment from headline.
        Returns (sentiment, confidence)
        """
        headline_lower = headline.lower()

        bullish_count = sum(1 for kw in BULLISH_KEYWORDS if kw in headline_lower)
        bearish_count = sum(1 for kw in BEARISH_KEYWORDS if kw in headline_lower)

        if bullish_count > bearish_count:
            confidence = min(0.5 + (bullish_count * 0.1), 0.95)
            return 'bullish', confidence
        elif bearish_count > bullish_count:
            confidence = min(0.5 + (bearish_count * 0.1), 0.95)
            return 'bearish', confidence
        else:
            return 'neutral', 0.5

    async def _fetch_rss_feed(self, url: str, source: str) -> List[Dict]:
        """Fetch news from a single RSS feed"""
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=15, headers=headers) as resp:
                    if resp.status == 200:
                        content = await resp.text()
                        feed = feedparser.parse(content)
                        entries = feed.entries[:15]  # Latest 15 per feed
                        # Tag entries with source
                        for entry in entries:
                            entry['_source'] = source
                        return entries
        except Exception as e:
            logger.debug(f"{source} RSS fetch failed: {e}")
        return []

    async def _fetch_all_feeds(self) -> List[Dict]:
        """Fetch from all RSS feeds in parallel"""
        tasks = [self._fetch_rss_feed(url, source) for url, source in self.RSS_FEEDS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_entries = []
        for result in results:
            if isinstance(result, list):
                all_entries.extend(result)

        return all_entries

    def _process_entry(self, entry: Dict, source: str) -> Optional[NewsAlert]:
        """Process a single RSS entry into a NewsAlert"""
        try:
            headline = entry.get('title', '')
            if not headline:
                return None

            # Generate ID and check if seen
            news_id = self._generate_id(headline, source)
            if news_id in self.seen_ids:
                return None

            # Extract symbols
            symbols = self._extract_symbols(headline + ' ' + entry.get('summary', ''))
            if not symbols:
                return None  # Skip news without symbols

            # Detect catalyst
            catalyst_type, urgency = self._detect_catalyst(headline)

            # Analyze sentiment
            sentiment, confidence = self._analyze_sentiment(headline)

            # Parse published time
            published_at = None
            if 'published_parsed' in entry and entry['published_parsed']:
                published_at = datetime(*entry['published_parsed'][:6], tzinfo=pytz.UTC)

            # Mark as seen
            self.seen_ids.add(news_id)

            # Keep seen_ids from growing too large
            if len(self.seen_ids) > 1000:
                self.seen_ids = set(list(self.seen_ids)[-500:])

            return NewsAlert(
                id=news_id,
                headline=headline,
                symbols=symbols,
                source=source,
                published_at=published_at,
                detected_at=datetime.now(ET_TZ),
                sentiment=sentiment,
                urgency=urgency,
                catalyst_type=catalyst_type,
                confidence=confidence
            )
        except Exception as e:
            logger.error(f"Error processing entry: {e}")
            return None

    async def poll_news(self) -> List[NewsAlert]:
        """Poll all RSS feeds and return new alerts"""
        new_alerts = []

        # Fetch from all sources in parallel
        all_entries = await self._fetch_all_feeds()

        logger.info(f"Fetched {len(all_entries)} RSS entries from all feeds")

        # Process all entries
        for entry in all_entries:
            source = entry.get('_source', 'unknown')
            alert = self._process_entry(entry, source)
            if alert:
                new_alerts.append(alert)

        # Store recent alerts
        self.recent_alerts = (new_alerts + self.recent_alerts)[:self.max_recent]

        return new_alerts

    def _emit_news_advisories(self, alerts: List[NewsAlert]):
        """Emit high-priority news alerts to advisory buffer."""
        try:
            from scanner_service.advisory_buffer import get_advisory_buffer
            buf = get_advisory_buffer()

            actionable = [a for a in alerts if a.urgency in ['critical', 'high']]
            for alert in actionable:
                for symbol in alert.symbols:
                    buf.emit(
                        symbol=symbol,
                        source="news_rss",
                        confidence=alert.confidence,
                        reason=f"{alert.catalyst_type}: {alert.headline[:80]}",
                        profile="news",
                    )
                    logger.info(f"[ADVISORY] News advisory: {symbol} ({alert.catalyst_type})")
        except Exception as e:
            logger.error(f"Error emitting news advisories: {e}")

    async def _poll_loop(self):
        """Main polling loop"""
        logger.info(f"News client started (poll interval: {self._poll_interval}s)")

        while self._running:
            try:
                alerts = await self.poll_news()
                if alerts:
                    logger.info(f"Found {len(alerts)} new news alerts")
                    self._emit_news_advisories(alerts)
            except Exception as e:
                logger.error(f"Poll loop error: {e}")

            await asyncio.sleep(self._poll_interval)

        logger.info("News client stopped")

    def start(self, poll_interval: int = 10):
        """Start the news polling service"""
        if self._running:
            return

        self._poll_interval = poll_interval
        self._running = True
        asyncio.create_task(self._poll_loop())

    def stop(self):
        """Stop the news polling service"""
        self._running = False

    def get_recent_alerts(self, limit: int = 20) -> List[Dict]:
        """Get recent alerts"""
        return [a.to_dict() for a in self.recent_alerts[:limit]]

    def get_status(self) -> Dict:
        """Get client status"""
        return {
            "running": self._running,
            "poll_interval": self._poll_interval,
            "seen_count": len(self.seen_ids),
            "recent_alerts": len(self.recent_alerts),
        }


# Singleton instance
_news_client: Optional[NewsClient] = None


def get_news_client() -> NewsClient:
    """Get or create the news client singleton"""
    global _news_client
    if _news_client is None:
        _news_client = NewsClient()
    return _news_client
