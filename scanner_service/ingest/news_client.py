"""
News Client for Max Scanner
============================
Fetches news from multiple RSS feeds and detects trading catalysts.
Emits advisories to buffer for pull-based consumption.

Sources:
  - Benzinga (news + markets) — best for small-cap catalyst detection
  - Yahoo Finance — dynamic symbol list from scanner universe
  - Seeking Alpha — market currents
  - GlobeNewsWire — M&A, FDA, press releases
  - SEC EDGAR — 8-K material event filings
"""

import asyncio
import aiohttp
import feedparser
import logging
import hashlib
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
from dataclasses import dataclass
import pytz

logger = logging.getLogger(__name__)

ET_TZ = pytz.timezone('US/Eastern')

# Well-known ticker symbols to match in headlines (no $ prefix needed)
# Only match these common ones to avoid false positives on short words
KNOWN_TICKERS = {
    'AAPL', 'TSLA', 'NVDA', 'AMD', 'AMZN', 'GOOG', 'GOOGL', 'META', 'MSFT',
    'NFLX', 'GME', 'AMC', 'BBAI', 'SOFI', 'NIO', 'RIVN', 'LCID', 'PLUG',
    'PLTR', 'MARA', 'RIOT', 'COIN', 'ROKU', 'SNAP', 'UBER', 'LYFT', 'HOOD',
    'DKNG', 'PENN', 'RBLX', 'ABNB', 'CPNG', 'GRAB', 'JOBY', 'ACHR', 'EVTL',
    'HIMS', 'SOUN', 'QUBT', 'RGTI', 'IONQ', 'LUNR', 'CLSK', 'BITF', 'WULF',
    'CIFR', 'CORZ', 'FUBO', 'PATH', 'XPEV',
}

# Catalyst keywords for instant detection
CRITICAL_CATALYSTS = {
    'fda': ['fda approval', 'fda approves', 'fda grants', 'breakthrough therapy',
            'fda clears', 'fda accepts', 'pdufa', 'nda approved', 'fda fast track',
            'emergency use authorization', 'fda advisory committee'],
    'merger': ['acquisition', 'merger', 'buyout', 'takeover bid', 'acquire',
               'all-cash deal', 'tender offer', 'to be acquired', 'definitive agreement'],
    'earnings_beat': ['beats estimates', 'eps beat', 'revenue beat', 'earnings surprise',
                      'exceeds expectations', 'blows past', 'tops estimates',
                      'better than expected', 'raises guidance', 'raises outlook'],
    'earnings_miss': ['misses estimates', 'eps miss', 'revenue miss', 'falls short',
                      'below expectations', 'lowers guidance', 'cuts outlook',
                      'warns on revenue', 'profit warning'],
    'contract': ['contract award', 'wins contract', 'awarded contract', 'secures deal',
                 'government contract', 'defense contract', 'billion dollar contract',
                 'multi-year deal'],
    'clinical': ['positive results', 'met primary endpoint', 'phase 3 success',
                 'clinical trial success', 'positive phase', 'trial met',
                 'positive data', 'statistically significant'],
    'partnership': ['partnership', 'collaboration', 'strategic alliance', 'joint venture',
                    'licensing agreement', 'distribution agreement'],
    'upgrade': ['upgrade', 'price target raised', 'initiates buy', 'raises rating',
                'overweight', 'outperform', 'strong buy'],
    'downgrade': ['downgrade', 'price target cut', 'initiates sell', 'lowers rating',
                  'underweight', 'underperform'],
    'offering': ['stock offering', 'secondary offering', 'shelf registration', 'dilution',
                 'public offering', 'at-the-market offering'],
    'bankruptcy': ['bankruptcy', 'chapter 11', 'restructuring', 'default',
                   'going concern', 'delisting'],
    'split': ['stock split', 'reverse split'],
    'halt': ['trading halt', 'halted', 'luld halt', 'circuit breaker'],
    'short_squeeze': ['short squeeze', 'short interest', 'heavily shorted', 'days to cover'],
}

# Sentiment keywords
BULLISH_KEYWORDS = ['surge', 'soar', 'jump', 'rally', 'gain', 'positive', 'beats',
                    'approval', 'success', 'breakthrough', 'upgrade', 'buy', 'surging',
                    'skyrocket', 'moon', 'rocket', 'explode', 'breakout', 'record high']
BEARISH_KEYWORDS = ['plunge', 'crash', 'drop', 'fall', 'decline', 'negative', 'miss',
                    'reject', 'fail', 'downgrade', 'sell', 'warning', 'plummet', 'tank',
                    'sink', 'tumble', 'collapse', 'halt', 'delisting']


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

    # Static RSS feeds (always fetched)
    STATIC_FEEDS = [
        ("https://www.benzinga.com/news/feed", "benzinga_news"),
        ("https://www.benzinga.com/markets/feed", "benzinga_markets"),
        ("https://seekingalpha.com/market_currents.xml", "seekingalpha"),
        ("https://www.globenewswire.com/RssFeed/subjectcode/14-Mergers%20and%20Acquisitions/feedTitle/GlobeNewswire%20-%20M%26A", "gnw_mergers"),
        ("https://www.globenewswire.com/RssFeed/subjectcode/01-Business%20Operations/feedTitle/GlobeNewswire%20-%20Business", "gnw_business"),
        ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=20&search_text=&start=0&output=atom", "sec_edgar"),
    ]

    def __init__(self):
        self.seen_ids: Set[str] = set()
        self.recent_alerts: List[NewsAlert] = []
        self.max_recent = 200
        self._running = False
        self._poll_interval = 30  # seconds between polls
        self._poll_count = 0
        self._universe_symbols: Set[str] = set()
        self._feed_stats: Dict[str, int] = {}  # source -> alert count

    def set_universe_symbols(self, symbols: Set[str]):
        """Update the set of symbols the scanner is tracking (for dynamic Yahoo feed)."""
        self._universe_symbols = symbols

    def _build_dynamic_feeds(self) -> List[tuple]:
        """Build Yahoo Finance RSS URL from current universe symbols."""
        feeds = []
        # Yahoo Finance with discovered symbols (max 20 in URL)
        syms = sorted(self._universe_symbols)[:20] if self._universe_symbols else []
        # Always include some high-interest tickers
        base_syms = ['TSLA', 'NVDA', 'AMD', 'GME', 'AMC', 'AAPL']
        all_syms = list(dict.fromkeys(syms + base_syms))[:25]  # dedupe, max 25
        if all_syms:
            sym_str = ','.join(all_syms)
            url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym_str}&region=US&lang=en-US"
            feeds.append((url, "yahoo"))
        return feeds

    def _generate_id(self, headline: str, source: str) -> str:
        """Generate unique ID for news item"""
        content = f"{headline}:{source}"
        return hashlib.md5(content.encode()).hexdigest()[:12]

    def _extract_symbols(self, text: str) -> List[str]:
        """Extract stock symbols from text using multiple strategies."""
        symbols = set()

        # Strategy 1: $SYMBOL pattern (most reliable)
        dollar_symbols = re.findall(r'\$([A-Z]{1,5})\b', text)
        symbols.update(dollar_symbols)

        # Strategy 2: (NASDAQ: SYMBOL) or (NYSE: SYMBOL) patterns
        exchange_symbols = re.findall(
            r'\((?:NASDAQ|NYSE|AMEX|OTC):\s*([A-Z]{1,5})\)', text, re.IGNORECASE
        )
        symbols.update(s.upper() for s in exchange_symbols)

        # Strategy 3: "SYMBOL stock" or "shares of SYMBOL" patterns
        stock_refs = re.findall(r'\b([A-Z]{2,5})\s+(?:stock|shares|Inc\.|Corp\.)', text)
        for s in stock_refs:
            if s in KNOWN_TICKERS or s in self._universe_symbols:
                symbols.update([s])

        # Strategy 4: Match known tickers as standalone words
        words = set(re.findall(r'\b([A-Z]{2,5})\b', text))
        for w in words:
            if w in KNOWN_TICKERS or w in self._universe_symbols:
                symbols.add(w)

        # Filter out common false positives
        false_positives = {
            'CEO', 'CFO', 'COO', 'CTO', 'IPO', 'SEC', 'FDA', 'ETF', 'NYSE',
            'NASDAQ', 'AMEX', 'OTC', 'EPS', 'GDP', 'CPI', 'PMI', 'USA', 'USD',
            'THE', 'FOR', 'AND', 'NOT', 'ALL', 'NEW', 'TOP', 'BIG', 'LOW',
            'HIGH', 'INC', 'LLC', 'LTD', 'CORP', 'EST', 'PST', 'EDG',
        }
        symbols -= false_positives

        return list(symbols)[:5]  # Max 5 symbols per news

    def _detect_catalyst(self, headline: str) -> tuple:
        """Detect catalyst type and urgency from headline."""
        headline_lower = headline.lower()

        for catalyst, keywords in CRITICAL_CATALYSTS.items():
            for keyword in keywords:
                if keyword in headline_lower:
                    if catalyst in ['fda', 'merger', 'clinical']:
                        return catalyst, 'critical'
                    elif catalyst in ['earnings_beat', 'earnings_miss', 'contract', 'halt']:
                        return catalyst, 'high'
                    else:
                        return catalyst, 'medium'

        return 'general', 'low'

    def _analyze_sentiment(self, headline: str) -> tuple:
        """Analyze sentiment from headline. Returns (sentiment, confidence)."""
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
                'User-Agent': 'Max_AI/1.0 Scanner (compatible; +https://github.com)'
            }
            # SEC EDGAR requires a proper User-Agent with contact
            if 'sec.gov' in url:
                headers['User-Agent'] = 'Max_AI/1.0 scanner@example.com'

            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        content = await resp.text()
                        feed = feedparser.parse(content)
                        entries = feed.entries[:20]
                        for entry in entries:
                            entry['_source'] = source
                        return entries
                    else:
                        logger.debug(f"[NEWS] {source} returned {resp.status}")
        except Exception as e:
            logger.debug(f"[NEWS] {source} fetch failed: {e}")
        return []

    async def _fetch_all_feeds(self) -> List[Dict]:
        """Fetch from all RSS feeds in parallel"""
        all_feeds = self.STATIC_FEEDS + self._build_dynamic_feeds()
        tasks = [self._fetch_rss_feed(url, source) for url, source in all_feeds]
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

            # Extract symbols from headline + summary
            summary = entry.get('summary', '')
            symbols = self._extract_symbols(headline + ' ' + summary)
            if not symbols:
                return None  # Skip news without symbols

            # Detect catalyst
            catalyst_type, urgency = self._detect_catalyst(headline)

            # Analyze sentiment
            sentiment, confidence = self._analyze_sentiment(headline)

            # Boost confidence for critical/high urgency catalysts
            if urgency == 'critical':
                confidence = max(confidence, 0.80)
            elif urgency == 'high':
                confidence = max(confidence, 0.65)

            # Parse published time
            published_at = None
            if 'published_parsed' in entry and entry['published_parsed']:
                published_at = datetime(*entry['published_parsed'][:6], tzinfo=pytz.UTC)

            # Mark as seen
            self.seen_ids.add(news_id)

            # Keep seen_ids from growing too large
            if len(self.seen_ids) > 2000:
                self.seen_ids = set(list(self.seen_ids)[-1000:])

            # Track feed stats
            self._feed_stats[source] = self._feed_stats.get(source, 0) + 1

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
            logger.error(f"[NEWS] Error processing entry: {e}")
            return None

    async def poll_news(self) -> List[NewsAlert]:
        """Poll all RSS feeds and return new alerts"""
        new_alerts = []

        all_entries = await self._fetch_all_feeds()

        for entry in all_entries:
            source = entry.get('_source', 'unknown')
            alert = self._process_entry(entry, source)
            if alert:
                new_alerts.append(alert)

        # Store recent alerts
        self.recent_alerts = (new_alerts + self.recent_alerts)[:self.max_recent]

        self._poll_count += 1

        if new_alerts:
            logger.info(
                f"[NEWS] Poll #{self._poll_count}: {len(all_entries)} entries, "
                f"{len(new_alerts)} new alerts"
            )

        return new_alerts

    def _emit_news_advisories(self, alerts: List[NewsAlert]):
        """Emit high-priority news alerts to advisory buffer."""
        try:
            from scanner_service.advisory_buffer import get_advisory_buffer
            buf = get_advisory_buffer()

            actionable = [a for a in alerts if a.urgency in ['critical', 'high', 'medium']]
            for alert in actionable:
                for symbol in alert.symbols:
                    buf.emit(
                        symbol=symbol,
                        source="news_rss",
                        confidence=alert.confidence,
                        reason=f"{alert.catalyst_type}: {alert.headline[:80]}",
                        profile="news",
                    )
                    logger.info(
                        f"[NEWS] Advisory: {symbol} | {alert.urgency} | "
                        f"{alert.catalyst_type} | {alert.source}"
                    )
        except Exception as e:
            logger.error(f"[NEWS] Error emitting advisories: {e}")

    async def _poll_loop(self):
        """Main polling loop"""
        logger.info(
            f"[NEWS] Client started | interval={self._poll_interval}s | "
            f"feeds={len(self.STATIC_FEEDS)}+dynamic"
        )

        while self._running:
            try:
                alerts = await self.poll_news()
                if alerts:
                    self._emit_news_advisories(alerts)
            except Exception as e:
                logger.error(f"[NEWS] Poll loop error: {e}")

            await asyncio.sleep(self._poll_interval)

        logger.info("[NEWS] Client stopped")

    def start(self, poll_interval: int = 30):
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
            "poll_count": self._poll_count,
            "seen_count": len(self.seen_ids),
            "recent_alerts": len(self.recent_alerts),
            "feed_stats": self._feed_stats,
            "universe_symbols_tracked": len(self._universe_symbols),
        }


# Singleton instance
_news_client: Optional[NewsClient] = None


def get_news_client() -> NewsClient:
    """Get or create the news client singleton"""
    global _news_client
    if _news_client is None:
        _news_client = NewsClient()
    return _news_client
