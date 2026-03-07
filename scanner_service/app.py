"""
Max Scanner Service - FastAPI Application.

Advisory-based architecture: Max emits advisories into a pull buffer.
Bots pull advisories when they want. Max never touches bot state.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import urllib.parse
import webbrowser

from scanner_service.settings import get_settings
from scanner_service.schemas.events import AlertEvent, AlertType, ScannerOutput, ScannerRow
from scanner_service.schemas.profile import Profile, ProfileCondition, ProfileWeights
from scanner_service.ingest.schwab_client import SchwabClient
from scanner_service.ingest.universe import UniverseManager
from scanner_service.ingest import finviz_client
from scanner_service.features.feature_engine import FeatureEngine
from scanner_service.strategy.profile_loader import ProfileLoader
from scanner_service.strategy.scorer import Scorer
from scanner_service.strategy.ranker import Ranker
from scanner_service.alerts.router import AlertRouter
from scanner_service.storage.state import ScannerState, ScannerStatus
from scanner_service.storage.cache import QuoteCache
from scanner_service.advisory_buffer import get_advisory_buffer, AdvisoryBuffer, NegativeAdvisory
from scanner_service.watchlist.stock_classifier import StockClassifier
from scanner_service.watchlist.daily_tracker import DailyTracker
from scanner_service.watchlist.vetted_list import VettedWatchlist
from scanner_service.ingest.news_pipeline import get_news_pipeline, NewsPipeline
from scanner_service.ingest.research_client import get_research_client, ResearchClient
from scanner_service.strategy.ignition_scorer import IgnitionScorer, heat_label
from scanner_service.strategy.momentum_chain_detector import MomentumChainDetector, sector_multiplier
from scanner_service.health_monitor import get_health_monitor, HealthMonitor

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Global instances
settings = get_settings()
schwab_client: Optional[SchwabClient] = None
universe: Optional[UniverseManager] = None
feature_engine: Optional[FeatureEngine] = None
profile_loader: Optional[ProfileLoader] = None
scorer: Optional[Scorer] = None
ranker: Optional[Ranker] = None
alert_router: Optional[AlertRouter] = None
scanner_state: Optional[ScannerState] = None
quote_cache: Optional[QuoteCache] = None

# Advisory buffer
advisory_buffer: Optional[AdvisoryBuffer] = None

# Watchlist system (Warrior Trading + SMB Capital framework)
stock_classifier: Optional[StockClassifier] = None
daily_tracker: Optional[DailyTracker] = None
vetted_watchlist: Optional[VettedWatchlist] = None

# Ignition scorer + Health monitor + News pipeline + Sector intelligence
ignition_scorer: Optional[IgnitionScorer] = None
health_monitor: Optional[HealthMonitor] = None
news_pipeline: Optional[NewsPipeline] = None
research_client: Optional[ResearchClient] = None
chain_detector: Optional[MomentumChainDetector] = None

# Phase-aware advisory thresholds
# Pre-market: accumulate aggressively, low bar, long TTL
# Post-open: tighten up, require stronger signals
PHASE_CONFIG = {
    "PREMARKET": {
        "min_ai_score": 0.30,
        "ttl_seconds": 600,       # 10 minutes — sparse data, keep longer
        "label": "Pre-market accumulation (low liquidity expected)",
    },
    "OPEN": {
        "min_ai_score": 0.50,
        "ttl_seconds": 300,       # 5 minutes — standard
        "label": "Market open (standard thresholds)",
    },
    "CLOSED": {
        "min_ai_score": 0.50,
        "ttl_seconds": 300,
        "label": "Market closed",
    },
}
ADVISORY_TTL_SECONDS = 300  # default fallback


def get_market_phase() -> str:
    """
    Return current market phase based on Eastern Time.

    PREMARKET: 04:00 - 09:29 ET  (accumulate aggressively)
    OPEN:      09:30 - 16:00 ET  (standard thresholds)
    CLOSED:    16:00 - 03:59 ET  (idle)
    """
    try:
        import pytz
        et = pytz.timezone("US/Eastern")
        now_et = datetime.now(et)
        t = now_et.time()
        from datetime import time as dt_time
        if dt_time(4, 0) <= t < dt_time(9, 30):
            return "PREMARKET"
        elif dt_time(9, 30) <= t < dt_time(16, 0):
            return "OPEN"
        else:
            return "CLOSED"
    except Exception:
        return "OPEN"  # fail-open to standard thresholds

# Fundamentals cache (refreshes less frequently than quotes)
fundamentals_cache: dict[str, dict] = {}
fundamentals_last_fetch: Optional[datetime] = None

# Scanner loop task
scanner_task: Optional[asyncio.Task] = None

# Heartbeat tracking (crash hardening)
_last_scan_ts: Optional[datetime] = None
_scan_error_times: list[datetime] = []  # timestamps of errors in last hour
_scan_ok: bool = True

# TradingView pre-market gapper state
_tv_last_fetch: Optional[datetime] = None
_tv_fetch_interval_seconds: int = 300  # every 5 minutes
_tv_discovered_symbols: set[str] = set()  # track what TV already found this session

# Webull premarket gainer state
_wb_last_fetch: Optional[datetime] = None
_wb_fetch_interval_seconds: int = 300  # every 5 minutes
_wb_discovered_symbols: set[str] = set()

# Intraday small-cap mover state (TV + Finviz during OPEN phase)
_intraday_last_fetch: Optional[datetime] = None
_intraday_fetch_interval_seconds: int = 300  # every 5 minutes
_intraday_discovered_symbols: set[str] = set()

# Token refresh task
token_refresh_task: Optional[asyncio.Task] = None

# WebSocket connections for streaming
websocket_connections: dict[str, list[WebSocket]] = {}

# WebSocket connections for advisory push (real-time delivery to consumers)
advisory_ws_connections: list[WebSocket] = []


async def token_reload_loop():
    """Background loop that reloads shared token from disk (Morpheus is the writer)."""
    logger.info("[TOKEN_RELOAD] Shared token reload daemon started (read-only, Morpheus owns refresh)")
    while True:
        try:
            await asyncio.sleep(60)  # Check every 60 seconds
            if schwab_client:
                try:
                    old_access = schwab_client._access_token
                    schwab_client._load_tokens()  # Re-read from shared file
                    if schwab_client._access_token and schwab_client._access_token != old_access:
                        logger.info("[TOKEN_RELOAD] Picked up refreshed token from shared file")
                    elif not schwab_client._access_token:
                        logger.warning("[TOKEN_RELOAD] Shared token file missing or empty")
                except Exception as e:
                    logger.error(f"[TOKEN_RELOAD] Reload failed: {e}")
        except asyncio.CancelledError:
            logger.info("[TOKEN_RELOAD] Shared token reload daemon stopped")
            break
        except Exception as e:
            logger.error(f"[TOKEN_RELOAD] Loop error: {e}")
            await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global schwab_client, universe, feature_engine, profile_loader
    global scorer, ranker, alert_router, scanner_state, quote_cache, scanner_task
    global token_refresh_task, advisory_buffer
    global stock_classifier, daily_tracker, vetted_watchlist
    global ignition_scorer, health_monitor, news_pipeline, research_client, chain_detector

    logger.info("Starting Max Scanner Service...")

    # Initialize components
    schwab_client = SchwabClient()
    universe = UniverseManager()
    feature_engine = FeatureEngine()
    profile_loader = ProfileLoader()
    scorer = Scorer(feature_engine)
    ranker = Ranker()
    alert_router = AlertRouter()
    scanner_state = ScannerState()
    quote_cache = QuoteCache(ttl_seconds=1.5)  # Short TTL to allow velocity calculation
    advisory_buffer = get_advisory_buffer(ttl_seconds=ADVISORY_TTL_SECONDS)
    stock_classifier = StockClassifier()
    daily_tracker = DailyTracker()
    vetted_watchlist = VettedWatchlist(max_stocks=10)
    ignition_scorer = IgnitionScorer()
    health_monitor = get_health_monitor()
    news_pipeline = get_news_pipeline()
    research_client = get_research_client()
    chain_detector = MomentumChainDetector()
    phase = get_market_phase()
    phase_cfg = PHASE_CONFIG.get(phase, PHASE_CONFIG["OPEN"])
    logger.info(
        f"[ADVISORY] Buffer initialized | phase={phase} | "
        f"min_score={phase_cfg['min_ai_score']} | ttl={phase_cfg['ttl_seconds']}s | "
        f"{phase_cfg['label']}"
    )

    # Start scanner loop
    scanner_state.status = ScannerStatus.STARTING
    scanner_task = asyncio.create_task(scanner_loop())

    # Start shared token reload daemon (read-only — Morpheus owns the refresh)
    token_refresh_task = asyncio.create_task(token_reload_loop())
    logger.info("[TOKEN_RELOAD] Shared token reload daemon started (Morpheus is the token owner)")

    logger.info(f"Scanner service started on {settings.scanner_host}:{settings.scanner_port}")

    # Auto-start news pipeline with source redundancy
    try:
        news_pipeline.start(poll_interval=30)
        logger.info("[NEWS_PIPELINE] Auto-started with redundancy (30s interval)")
    except Exception as e:
        logger.warning(f"[NEWS_PIPELINE] Failed to auto-start: {e}")

    # Start health monitor
    try:
        health_monitor.start(check_interval=60)
        logger.info("[HEALTH] Monitor started (60s interval, logs/scanner_health.log)")
    except Exception as e:
        logger.warning(f"[HEALTH] Failed to start monitor: {e}")

    yield

    # Shutdown
    logger.info("Shutting down scanner service...")
    scanner_state.status = ScannerStatus.STOPPED

    if token_refresh_task:
        token_refresh_task.cancel()
        try:
            await token_refresh_task
        except asyncio.CancelledError:
            pass

    if scanner_task:
        scanner_task.cancel()
        try:
            await scanner_task
        except asyncio.CancelledError:
            pass

    if schwab_client:
        await schwab_client.close()

    logger.info("Scanner service stopped")


async def scanner_loop():
    """Main scanner loop with heartbeat and top-level exception guard."""
    global scanner_state, _last_scan_ts, _scan_ok, _scan_error_times

    scanner_state.status = ScannerStatus.RUNNING
    interval = settings.scan_interval_ms / 1000
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 20  # Exit after 20 consecutive failures

    try:
        while scanner_state.is_running:
            try:
                await run_scan_cycle()
                _last_scan_ts = datetime.utcnow()
                _scan_ok = True
                consecutive_failures = 0
            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_failures += 1
                _scan_ok = False
                _scan_error_times.append(datetime.utcnow())
                # Prune errors older than 1 hour
                cutoff = datetime.utcnow() - timedelta(hours=1)
                _scan_error_times[:] = [t for t in _scan_error_times if t > cutoff]

                logger.error(f"Scan cycle error ({consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}): {e}")
                scanner_state.record_error(e)

                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    logger.critical(
                        f"[CRITICAL] MAX FATAL: {MAX_CONSECUTIVE_FAILURES} consecutive scan failures. "
                        f"Last error: {e}. Exiting non-zero for restart."
                    )
                    import sys
                    sys.exit(1)

            await asyncio.sleep(interval)
    except Exception as e:
        logger.critical(f"[CRITICAL] MAX FATAL: Unhandled exception in scanner_loop: {e}")
        import sys
        sys.exit(1)


async def _fetch_tradingview_gappers():
    """Fetch TradingView pre-market gappers and inject into universe + advisory buffer.

    Runs every 5 minutes during PREMARKET phase only.
    Symbols are added to the universe so they get Schwab quotes on the next cycle.
    High-change gappers also get emitted directly as advisories.
    """
    global _tv_last_fetch, _tv_discovered_symbols

    phase = get_market_phase()
    if phase != "PREMARKET":
        return

    now = datetime.utcnow()
    if _tv_last_fetch and (now - _tv_last_fetch).total_seconds() < _tv_fetch_interval_seconds:
        return  # Too soon

    _tv_last_fetch = now

    try:
        from concurrent.futures import ThreadPoolExecutor
        from scanner_service.ingest.tradingview_client import fetch_premarket_gappers

        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as pool:
            gappers = await loop.run_in_executor(pool, fetch_premarket_gappers)

        if not gappers:
            return

        # Inject new symbols into universe
        new_symbols = []
        for g in gappers:
            sym = g["symbol"]
            if sym not in _tv_discovered_symbols:
                _tv_discovered_symbols.add(sym)
                new_symbols.append(sym)

        if new_symbols and universe:
            universe.add_symbols(new_symbols)
            logger.info(f"[TV] Added {len(new_symbols)} new symbols to universe: {new_symbols}")

        # Register TV discoveries with daily tracker
        if daily_tracker:
            for g in gappers:
                sym = g["symbol"]
                if sym in new_symbols:
                    daily_tracker.register(
                        symbol=sym,
                        price=g.get("price", 0),
                        source="tradingview_premarket",
                        change_pct=g.get("change_pct", 0),
                    )

        # Emit advisories for all gappers (already filtered >=5% by TV query)
        # Confidence: 0.55 base + change bonus. These are real pre-market movers
        # with confirmed volume — they should clear the 0.50 poller threshold.
        if advisory_buffer:
            phase_cfg = PHASE_CONFIG.get("PREMARKET", PHASE_CONFIG["OPEN"])
            ttl = phase_cfg["ttl_seconds"]
            emitted = 0
            for g in gappers:
                    adv = advisory_buffer.emit(
                        symbol=g["symbol"],
                        source="tradingview_premarket",
                        confidence=min(0.55 + (g["change_pct"] / 200.0), 0.95),
                        reason=f"TV premarket: +{g['change_pct']:.1f}% vol={g['premarket_volume']:,}",
                        price=g["price"],
                        change_pct=g["change_pct"],
                        volume=g["premarket_volume"],
                        rvol=0,
                        float_shares=0,
                        profile="tradingview_gappers",
                        ttl_override=ttl,
                    )
                    if adv:
                        emitted += 1
                        await push_advisory(adv.model_dump(mode="json"))
            if emitted:
                logger.info(f"[TV] Emitted {emitted} pre-market advisories")

    except Exception as e:
        logger.warning(f"[TV] TradingView fetch failed (non-fatal): {e}")


async def _fetch_webull_premarket_gainers():
    """Fetch Webull premarket top gainers and inject into universe + advisory buffer.

    Runs every 5 minutes during PREMARKET phase only.
    Complements TradingView with a second independent source.
    """
    global _wb_last_fetch, _wb_discovered_symbols

    phase = get_market_phase()
    if phase != "PREMARKET":
        return

    now = datetime.utcnow()
    if _wb_last_fetch and (now - _wb_last_fetch).total_seconds() < _wb_fetch_interval_seconds:
        return

    _wb_last_fetch = now

    try:
        from concurrent.futures import ThreadPoolExecutor
        from scanner_service.ingest.webull_client import fetch_premarket_gainers

        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as pool:
            gainers = await loop.run_in_executor(pool, fetch_premarket_gainers)

        if not gainers:
            return

        new_symbols = []
        for g in gainers:
            sym = g["symbol"]
            if sym not in _wb_discovered_symbols:
                _wb_discovered_symbols.add(sym)
                new_symbols.append(sym)

        if new_symbols and universe:
            universe.add_symbols(new_symbols)
            logger.info(f"[WEBULL] Added {len(new_symbols)} new symbols to universe: {new_symbols}")

        # Register Webull discoveries with daily tracker
        if daily_tracker:
            for g in gainers:
                sym = g["symbol"]
                if sym in new_symbols:
                    daily_tracker.register(
                        symbol=sym,
                        price=g.get("price", 0),
                        source="webull_premarket",
                        change_pct=g.get("change_pct", 0),
                    )

        if advisory_buffer:
            phase_cfg = PHASE_CONFIG.get("PREMARKET", PHASE_CONFIG["OPEN"])
            ttl = phase_cfg["ttl_seconds"]
            emitted = 0
            for g in gainers:
                confidence = min(0.55 + (g["change_pct"] / 200.0), 0.95)
                adv = advisory_buffer.emit(
                    symbol=g["symbol"],
                    source="webull_premarket",
                    confidence=confidence,
                    reason=f"Webull premarket: +{g['change_pct']:.1f}% vol={g['volume']:,}",
                    price=g["price"],
                    change_pct=g["change_pct"],
                    volume=g["volume"],
                    rvol=0,
                    float_shares=0,
                    profile="webull_gappers",
                    ttl_override=ttl,
                )
                if adv:
                    emitted += 1
                    await push_advisory(adv.model_dump(mode="json"))
            if emitted:
                logger.info(f"[WEBULL] Emitted {emitted} pre-market advisories")

    except Exception as e:
        logger.warning(f"[WEBULL] Premarket fetch failed (non-fatal): {e}")


async def _fetch_intraday_smallcap_movers():
    """Fetch small-cap movers during OPEN phase from TradingView + Finviz.

    Runs every 5 minutes during OPEN phase.
    TradingView: real-time change%, volume, rvol for $1-$20 stocks.
    Finviz: top gainers with float data (lags ~15 min after open).
    Both sources emit to advisory buffer for Morpheus poller to consume.
    """
    global _intraday_last_fetch, _intraday_discovered_symbols

    phase = get_market_phase()
    if phase != "OPEN":
        return

    now = datetime.utcnow()
    if _intraday_last_fetch and (now - _intraday_last_fetch).total_seconds() < _intraday_fetch_interval_seconds:
        return  # Too soon

    _intraday_last_fetch = now

    phase_cfg = PHASE_CONFIG.get("OPEN", PHASE_CONFIG["OPEN"])
    ttl = phase_cfg["ttl_seconds"]
    total_emitted = 0

    # --- Source 1: TradingView intraday small-cap movers ---
    try:
        from concurrent.futures import ThreadPoolExecutor
        from scanner_service.ingest.tradingview_client import fetch_intraday_movers

        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as pool:
            movers = await loop.run_in_executor(pool, fetch_intraday_movers)

        if movers and advisory_buffer:
            new_symbols = []
            for m in movers:
                sym = m["symbol"]
                if sym not in _intraday_discovered_symbols:
                    _intraday_discovered_symbols.add(sym)
                    new_symbols.append(sym)

                # Emit advisory — confidence based on change% and rvol
                base_conf = min(0.50 + (m["change_pct"] / 200.0), 0.90)
                rvol_bonus = min(m.get("rvol", 0) * 0.02, 0.10)  # up to +0.10 for high rvol
                confidence = min(base_conf + rvol_bonus, 0.95)

                adv = advisory_buffer.emit(
                    symbol=sym,
                    source="tradingview_intraday",
                    confidence=confidence,
                    reason=f"TV intraday: +{m['change_pct']:.1f}% vol={m['volume']:,} rvol={m.get('rvol', 0):.1f}x",
                    price=m["price"],
                    change_pct=m["change_pct"],
                    volume=m["volume"],
                    rvol=m.get("rvol", 0),
                    profile="tradingview_intraday",
                    ttl_override=ttl,
                )
                if adv:
                    total_emitted += 1
                    await push_advisory(adv.model_dump(mode="json"))

            # Add new symbols to universe for Schwab quotes
            if new_symbols and universe:
                universe.add_symbols(new_symbols)
                logger.info(f"[INTRADAY_TV] Added {len(new_symbols)} new symbols: {new_symbols}")

    except Exception as e:
        logger.warning(f"[INTRADAY_TV] TradingView intraday fetch failed (non-fatal): {e}")

    # --- Source 2: Finviz top gainers (small-cap filter) ---
    try:
        from scanner_service.ingest import finviz_client

        gainers = await finviz_client.get_top_gainers(
            max_price=20.0,
            min_change=10.0,
            limit=20,
        )

        if gainers and advisory_buffer:
            new_symbols = []
            for g in gainers:
                sym = g["symbol"]
                if sym not in _intraday_discovered_symbols:
                    _intraday_discovered_symbols.add(sym)
                    new_symbols.append(sym)

                # Emit advisory — confidence from change%
                confidence = min(0.50 + (g["change_pct"] / 200.0), 0.90)

                adv = advisory_buffer.emit(
                    symbol=sym,
                    source="finviz_intraday",
                    confidence=confidence,
                    reason=f"Finviz gainer: +{g['change_pct']:.1f}% vol={g['volume']:,}",
                    price=g["price"],
                    change_pct=g["change_pct"],
                    volume=g["volume"],
                    profile="finviz_gainers",
                    ttl_override=ttl,
                )
                if adv:
                    total_emitted += 1
                    await push_advisory(adv.model_dump(mode="json"))

            if new_symbols and universe:
                universe.add_symbols(new_symbols)
                logger.info(f"[INTRADAY_FV] Added {len(new_symbols)} Finviz symbols: {new_symbols}")

    except Exception as e:
        logger.warning(f"[INTRADAY_FV] Finviz intraday fetch failed (non-fatal): {e}")

    # --- Source 3: Re-emit TV pre-market discoveries that are still active ---
    # Symbols found in pre-market should stay visible during OPEN
    if _tv_discovered_symbols and advisory_buffer:
        re_emitted = 0
        for sym in list(_tv_discovered_symbols):
            # Check if symbol has an active advisory already
            existing = advisory_buffer.get_active(min_confidence=0.0)
            already_active = any(a.symbol == sym for a in existing)
            if already_active:
                continue

            # Re-emit with moderate confidence (it was found pre-market, still relevant)
            adv = advisory_buffer.emit(
                symbol=sym,
                source="tradingview_premarket_carryover",
                confidence=0.55,
                reason=f"Pre-market gapper (carryover from {len(_tv_discovered_symbols)} TV discoveries)",
                profile="tradingview_gappers",
                ttl_override=ttl,
            )
            if adv:
                re_emitted += 1
                total_emitted += 1
                await push_advisory(adv.model_dump(mode="json"))

        if re_emitted:
            logger.info(f"[INTRADAY] Re-emitted {re_emitted} pre-market TV discoveries as carryovers")

    if total_emitted > 0:
        logger.info(f"[INTRADAY] Total emitted: {total_emitted} small-cap advisories (TV + Finviz + carryover)")


def _check_negative_intelligence(row, phase: str) -> tuple[Optional[str], str]:
    """
    Check if a symbol should get a DO_NOT_TRADE signal.

    Returns (reason, detail) or (None, "") if no negative signal.

    Reasons:
    - extended_move: Change% too extreme (>50%), likely exhausted
    - low_follow_through: Low score despite big change (move isn't sticking)
    - volume_concern: High change but very low relative volume
    """
    symbol = row.symbol
    change = abs(row.change_pct) if hasattr(row, 'change_pct') else 0
    score = row.ai_score if hasattr(row, 'ai_score') else 0
    rvol = row.rvol if hasattr(row, 'rvol') else 0

    # Extended move: +50% or more — too late, likely to fade
    if change >= 50.0:
        return "extended_move", f"{symbol} +{change:.0f}% — parabolic, high reversal risk"

    # Low follow-through: big change but low AI score suggests move isn't sustainable
    if change >= 15.0 and score < 0.30:
        return "low_follow_through", f"{symbol} +{change:.0f}% but score={score:.2f} — weak internals"

    # Volume concern: big change but no volume confirmation
    if change >= 10.0 and rvol < 1.0 and rvol > 0:
        return "volume_concern", f"{symbol} +{change:.0f}% but rvol={rvol:.1f}x — no volume confirmation"

    return None, ""


async def run_scan_cycle():
    """Execute a single scan cycle."""
    global fundamentals_cache, fundamentals_last_fetch
    _cycle_start = datetime.utcnow()

    # TradingView pre-market gapper injection (every 5 min during pre-market)
    await _fetch_tradingview_gappers()

    # Webull pre-market top gainers (every 5 min during pre-market)
    await _fetch_webull_premarket_gainers()

    # Intraday small-cap movers (TV + Finviz, every 5 min during OPEN)
    await _fetch_intraday_smallcap_movers()

    # Feed universe symbols to news pipeline for dynamic Yahoo RSS
    try:
        if news_pipeline and universe:
            news_pipeline.set_universe_symbols(set(universe.candidates))
    except Exception:
        pass

    # Get symbols to scan
    symbols = universe.candidates

    # Check cache for recent quotes
    cached, missing = quote_cache.get_many(symbols)

    # Fetch missing quotes from Schwab
    if missing:
        snapshot = await schwab_client.get_snapshot(missing)
        quote_cache.set_many(snapshot.quotes)
        cached.update(snapshot.quotes)

    # Fetch fundamentals periodically (every 5 minutes)
    should_fetch_fundamentals = (
        fundamentals_last_fetch is None or
        (datetime.utcnow() - fundamentals_last_fetch).total_seconds() > 300
    )
    if should_fetch_fundamentals and symbols:
        try:
            new_fundamentals = await schwab_client.get_fundamentals(symbols)
            fundamentals_cache.update(new_fundamentals)
            fundamentals_last_fetch = datetime.utcnow()
            logger.info(f"Updated fundamentals for {len(new_fundamentals)} symbols")
        except Exception as e:
            logger.warning(f"Fundamentals fetch failed: {e}")

    # Enrich quotes with fundamentals (float, market cap, avg volume)
    # Convert to millions for easier display
    for symbol, quote in cached.items():
        if symbol in fundamentals_cache:
            fund = fundamentals_cache[symbol]
            quote.float_shares = fund.get("float_shares", 0) / 1_000_000  # Convert to millions
            quote.market_cap = fund.get("market_cap", 0) / 1_000_000  # Convert to millions
            # avg_volume stays as raw count (not converted to millions)
            if fund.get("avg_volume"):
                quote.avg_volume = int(fund.get("avg_volume", 0))

    # Build full snapshot
    from scanner_service.schemas.market_snapshot import MarketSnapshot
    snapshot = MarketSnapshot(quotes=cached, timestamp=datetime.utcnow())

    # Narrow universe based on activity
    if len(snapshot) > 0:
        universe.narrow_universe(snapshot.quotes)

    # Compute features
    features = feature_engine.compute_batch_features(snapshot)

    # Score and rank for each profile
    outputs = {}
    for profile in profile_loader.get_enabled():
        scores = scorer.score_batch(snapshot.quotes, features, profile)
        output = ranker.rank(scores, profile, snapshot)
        outputs[profile.name] = output

        # Check for alerts
        for row in output.rows[:10]:  # Top 10 for alerts
            symbol_features = features.get(row.symbol, {})
            alert_router.check_and_trigger(row, symbol_features, profile)

    # Record state
    scanner_state.record_scan(snapshot, outputs)

    # Broadcast to WebSocket clients
    await broadcast_updates(outputs)

    # Emit advisories for top-scoring symbols (phase-aware)
    if advisory_buffer:
        phase = get_market_phase()
        phase_cfg = PHASE_CONFIG.get(phase, PHASE_CONFIG["OPEN"])
        min_score = phase_cfg["min_ai_score"]
        ttl = phase_cfg["ttl_seconds"]
        emitted_count = 0

        for profile_name, output in outputs.items():
            if not output.rows:
                continue
            for row in output.rows[:10]:
                if row.ai_score >= min_score:
                    # Check for active negative advisory — skip if blocked
                    is_neg, neg_reason = advisory_buffer.is_negative(row.symbol)
                    if is_neg:
                        advisory_buffer.record_negative_suppression()
                        continue

                    adv = advisory_buffer.emit(
                        symbol=row.symbol,
                        source="scanner_cycle",
                        confidence=row.ai_score,
                        reason=f"{profile_name}: score={row.ai_score:.2f} chg={row.change_pct:+.1f}% rvol={row.rvol:.1f}x",
                        price=row.last_price,
                        change_pct=row.change_pct,
                        volume=row.volume,
                        rvol=row.rvol,
                        float_shares=row.float_shares,
                        profile=profile_name,
                        ttl_override=ttl,
                    )
                    if adv:
                        emitted_count += 1
                        await push_advisory(adv.model_dump(mode="json"))

        if emitted_count > 0 and phase == "PREMARKET":
            logger.info(
                f"[ADVISORY] Premarket accumulation: emitted {emitted_count} advisories "
                f"(min_score={min_score}, ttl={ttl}s)"
            )

        # Emit negative advisories (DO_NOT_TRADE signals)
        negative_count = 0
        for profile_name, output in outputs.items():
            if not output.rows:
                continue
            for row in output.rows[:20]:  # Check top 20 for negative signals
                neg_reason, neg_detail = _check_negative_intelligence(row, phase)
                if neg_reason:
                    neg = advisory_buffer.emit_negative(
                        symbol=row.symbol,
                        reason=neg_reason,
                        detail=neg_detail,
                        source="scanner_cycle",
                        change_pct=row.change_pct,
                        price=row.last_price,
                        ttl_seconds=600,  # Negative signals persist 10 min
                    )
                    negative_count += 1
                    await push_negative_advisory(neg.model_dump(mode="json"))

        if negative_count > 0:
            logger.info(f"[ADVISORY][NEGATIVE] Emitted {negative_count} DO_NOT_TRADE signals")

    # ── Watchlist system: classify + track + vet ──────────────────────
    if stock_classifier and daily_tracker and vetted_watchlist:
        phase = get_market_phase()

        # Determine market phase for classifier time-of-day adjustment
        try:
            import pytz
            from datetime import time as dt_time
            et = pytz.timezone("US/Eastern")
            now_et = datetime.now(et)
            t = now_et.time()
            if dt_time(4, 0) <= t < dt_time(9, 30):
                cls_phase = "PREMARKET"
            elif dt_time(9, 30) <= t < dt_time(10, 30):
                cls_phase = "FIRST_HOUR"
            elif dt_time(10, 30) <= t < dt_time(11, 30):
                cls_phase = "SECOND_HOUR"
            elif dt_time(11, 30) <= t < dt_time(15, 0):
                cls_phase = "MIDDAY"
            else:
                cls_phase = "POWER_HOUR"
        except Exception:
            cls_phase = "OPEN"

        # Set session start on first OPEN-phase scan
        if phase == "OPEN" and not vetted_watchlist._session_start:
            vetted_watchlist.set_session_start()

        # Feed news catalysts to classifier
        try:
            nc = _get_news_client()
            if nc.recent_alerts:
                stock_classifier.update_catalysts(nc.recent_alerts)
        except Exception:
            pass

        # Collect all unique rows across profiles for classification
        all_rows = {}
        for profile_name, output in outputs.items():
            for row in output.rows:
                sym = row.symbol
                if sym not in all_rows or row.ai_score > all_rows[sym].ai_score:
                    all_rows[sym] = row

        if all_rows:
            # Classify all symbols
            classifications = {}
            for sym, row in all_rows.items():
                sym_features = features.get(sym, {})
                cls = stock_classifier.classify(
                    symbol=sym,
                    price=row.last_price,
                    change_pct=row.change_pct,
                    gap_pct=sym_features.get("gap_pct", 0),
                    rvol=row.rvol,
                    volume=row.volume,
                    spread_pct=sym_features.get("spread", row.spread if hasattr(row, "spread") else 0),
                    float_m=row.float_shares if hasattr(row, "float_shares") else 0,
                    ai_score=row.ai_score,
                    hod_proximity=sym_features.get("hod_proximity", 0),
                    velocity=sym_features.get("velocity", 0),
                    market_phase=cls_phase,
                )
                classifications[sym] = cls

                # Register with tracker if new
                daily_tracker.register(
                    symbol=sym,
                    price=row.last_price,
                    source=cls.catalyst or "scanner_cycle",
                    tier=cls.tier,
                    catalyst=cls.catalyst,
                    change_pct=row.change_pct,
                )
                # Update tracker
                daily_tracker.update(
                    symbol=sym,
                    price=row.last_price,
                    change_pct=row.change_pct,
                    rvol=row.rvol,
                    volume=row.volume,
                    tier=cls.tier,
                )

                # Auto-add A-class to vetted list
                if cls.tier == "A":
                    vetted_watchlist.try_add(
                        symbol=sym,
                        tier="A",
                        score=cls.score,
                        price=row.last_price,
                        change_pct=row.change_pct,
                        rvol=row.rvol,
                        volume=row.volume,
                        float_m=row.float_shares if hasattr(row, "float_shares") else 0,
                        source=cls.catalyst or "scanner",
                        catalyst=cls.catalyst,
                    )

            # Update vetted list prices
            price_map = {sym: row.last_price for sym, row in all_rows.items()}
            vetted_watchlist.update_prices(price_map)

            # Check for removals
            vetted_watchlist.check_removals(classifications)

            # Log tier counts periodically (every ~60 scans)
            stats = stock_classifier.get_stats()
            if stats["current_total"] > 0 and stats["current_total"] % 10 == 0:
                logger.info(
                    f"[WATCHLIST] A={stats['current_a']} B={stats['current_b']} "
                    f"C={stats['current_c']} | vetted={len(vetted_watchlist.get_symbols())} "
                    f"| promotions={stats['promotions']} demotions={stats['demotions']}"
                )

    # ── Sector intelligence + Ignition scoring + Chain detection ────
    _sector_map: dict[str, str] = {}
    _heat_scores: dict[str, float] = {}

    if ignition_scorer and outputs:
        # Feed catalyst data from news pipeline
        try:
            if news_pipeline:
                nc = news_pipeline.news_client
                if nc.recent_alerts:
                    ignition_scorer.update_catalysts_from_news(nc.recent_alerts)
        except Exception:
            pass

        # Collect all unique rows for scoring
        all_rows_for_ignition = {}
        for profile_name, output in outputs.items():
            for row in output.rows:
                sym = row.symbol
                if sym not in all_rows_for_ignition or row.ai_score > all_rows_for_ignition[sym].ai_score:
                    all_rows_for_ignition[sym] = row

        if all_rows_for_ignition:
            # Fetch sector classifications + heatmap from research server
            try:
                if research_client:
                    symbols_to_classify = list(all_rows_for_ignition.keys())
                    sector_data = await research_client.get_symbol_sectors_batch(symbols_to_classify)
                    _sector_map = {sym: d.get("sector", "unknown") for sym, d in sector_data.items()}

                    heatmap = await research_client.get_heatmap()
                    _heat_scores = {
                        sector: data.get("heat_score", 0.30)
                        for sector, data in heatmap.items()
                    } if heatmap else {}
            except Exception as e:
                logger.debug(f"[RESEARCH] Sector fetch failed (using defaults): {e}")

            # Momentum chain detection
            if chain_detector and _sector_map:
                chain_candidates = []
                for sym, row in all_rows_for_ignition.items():
                    feat = features.get(sym, {})
                    chain_candidates.append({
                        "symbol": sym,
                        "change_pct": row.change_pct,
                        "rvol": feat.get("rvol", row.rvol if hasattr(row, "rvol") else 0),
                        "price": row.last_price,
                        "volume": row.volume,
                    })
                chain_detector.detect(chain_candidates, _sector_map)

            # Ignition scoring with sector + chain multipliers
            ignition_ranked = ignition_scorer.rank_symbols(
                rows=list(all_rows_for_ignition.values()),
                features=features,
                quotes=snapshot.quotes,
                limit=20,
                sector_map=_sector_map,
                heat_scores=_heat_scores,
                chain_detector=chain_detector,
            )
            scanner_state._ignition_ranked = ignition_ranked

            # Enrich ScannerRow objects with sector/heat/cluster_role
            for profile_name, output in outputs.items():
                for row in output.rows:
                    sym = row.symbol
                    sector = _sector_map.get(sym, "unknown")
                    s_heat = _heat_scores.get(sector, 0.30)
                    row.sector = sector
                    row.heat_score = round(s_heat, 2)
                    row.heat = heat_label(s_heat)
                    row.cluster_role = chain_detector.get_role(sym) if chain_detector else "none"

            # Log sector heat application
            hot_sectors = [s for s, h in _heat_scores.items() if h >= 0.70]
            if hot_sectors:
                logger.info(f"[SECTOR] Hot sectors applied: {', '.join(hot_sectors)}")

            # Log chain detections
            if chain_detector:
                chains = chain_detector.get_chains()
                if chains:
                    scanner_state._momentum_chains = chains

    # ── Premarket focus mode: prioritized list for Morpheus ──────────
    phase = get_market_phase()
    if phase == "PREMARKET" and advisory_buffer and outputs:
        premarket_focus = []
        for profile_name, output in outputs.items():
            for row in output.rows:
                feat = features.get(row.symbol, {})
                rvol = feat.get("rvol", row.rvol if hasattr(row, "rvol") else 0)
                float_m = row.float_shares if hasattr(row, "float_shares") else 0
                has_catalyst = row.symbol in (ignition_scorer._catalyst_symbols if ignition_scorer else {})

                if (rvol >= 3
                    and (0 < float_m < 50 or float_m == 0)
                    and 1.0 <= row.last_price <= 20.0):
                    premarket_focus.append({
                        "symbol": row.symbol,
                        "price": row.last_price,
                        "change_pct": row.change_pct,
                        "rvol": round(rvol, 2),
                        "float_m": round(float_m, 1),
                        "ai_score": row.ai_score,
                        "has_catalyst": has_catalyst,
                        "profile": profile_name,
                    })

        # Deduplicate by symbol, keep highest ai_score
        seen = {}
        for item in premarket_focus:
            sym = item["symbol"]
            if sym not in seen or item["ai_score"] > seen[sym]["ai_score"]:
                seen[sym] = item
        premarket_focus = sorted(seen.values(), key=lambda x: x["ai_score"], reverse=True)[:20]

        if premarket_focus:
            scanner_state._premarket_focus = premarket_focus
            # Emit focused advisories with boosted confidence
            emitted_focus = 0
            phase_cfg = PHASE_CONFIG.get("PREMARKET", PHASE_CONFIG["OPEN"])
            for item in premarket_focus:
                confidence = min(item["ai_score"] + 0.10, 0.95)
                adv = advisory_buffer.emit(
                    symbol=item["symbol"],
                    source="premarket_focus",
                    confidence=confidence,
                    reason=f"Premarket focus: rvol={item['rvol']}x float={item['float_m']}M{'+ catalyst' if item['has_catalyst'] else ''}",
                    price=item["price"],
                    change_pct=item["change_pct"],
                    volume=0,
                    rvol=item["rvol"],
                    float_shares=item["float_m"],
                    profile="premarket_focus",
                    ttl_override=phase_cfg["ttl_seconds"],
                )
                if adv:
                    emitted_focus += 1
                    await push_advisory(adv.model_dump(mode="json"))
            if emitted_focus:
                logger.info(
                    f"[PREMARKET_FOCUS] Emitted {emitted_focus} focused advisories "
                    f"(rvol>=3, float<50M, $1-$20)"
                )

    # ── Record scan latency for health monitor ───────────────────────
    _cycle_elapsed_ms = (datetime.utcnow() - _cycle_start).total_seconds() * 1000
    if health_monitor:
        health_monitor.record_scan_latency(_cycle_elapsed_ms)


async def push_advisory(advisory_data: dict) -> None:
    """Push advisory to all connected WebSocket consumers (Morpheus, IBKR_V2)."""
    if not advisory_ws_connections:
        return
    import json
    payload = json.dumps({"type": "advisory", "data": advisory_data})
    disconnected = []
    for ws in advisory_ws_connections:
        try:
            await ws.send_text(payload)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        advisory_ws_connections.remove(ws)
    if disconnected:
        logger.info(f"[WS_PUSH] Removed {len(disconnected)} dead advisory connections")


async def push_negative_advisory(negative_data: dict) -> None:
    """Push negative advisory to all connected WebSocket consumers."""
    if not advisory_ws_connections:
        return
    import json
    payload = json.dumps({"type": "negative", "data": negative_data})
    disconnected = []
    for ws in advisory_ws_connections:
        try:
            await ws.send_text(payload)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        advisory_ws_connections.remove(ws)


async def broadcast_updates(outputs: dict[str, ScannerOutput]):
    """Broadcast scanner updates to WebSocket clients."""
    for profile_name, connections in websocket_connections.items():
        if profile_name in outputs:
            output = outputs[profile_name]
            data = output.model_dump_json()
            disconnected = []

            for ws in connections:
                try:
                    await ws.send_text(data)
                except Exception:
                    disconnected.append(ws)

            for ws in disconnected:
                connections.remove(ws)


# Create FastAPI app
app = FastAPI(
    title="MAX_AI Scanner Service",
    description="Real-time stock scanner for trading",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files for dashboard
STATIC_DIR = Path(__file__).parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ============== Dashboard ==============

@app.get("/")
async def root():
    """Serve the dashboard."""
    index_path = STATIC_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return RedirectResponse(url="/health")


# ============== Health ==============

@app.get("/health")
async def health():
    """Health check endpoint with heartbeat, advisory stats, and error tracking."""
    # Advisory stats
    adv_active = 0
    adv_unique = 0
    neg_active = 0
    stats = {}
    if advisory_buffer:
        stats = advisory_buffer.get_stats()
        adv_active = stats.get("active_advisories", 0)
        adv_unique = stats.get("unique_symbols", 0)
        neg_active = stats.get("negative_active", 0)

    # Schwab health (can we fetch quotes?)
    schwab_ok = schwab_client is not None and schwab_client.is_authenticated()

    # RSS/news health (check pipeline if available, fallback to client)
    rss_ok = False
    news_sources_ok = 0
    try:
        if news_pipeline:
            ps = news_pipeline.get_status()
            rss_ok = ps.get("running", False) and ps.get("poll_count", 0) > 0
            news_sources_ok = ps.get("sources_ok", 0)
        else:
            from scanner_service.ingest.news_client import get_news_client
            nc = get_news_client()
            ns = nc.get_status()
            rss_ok = ns.get("running", False) and ns.get("poll_count", 0) > 0
    except Exception:
        pass

    # Errors in last hour
    cutoff = datetime.utcnow() - timedelta(hours=1)
    errors_last_hour = sum(1 for t in _scan_error_times if t > cutoff)

    ok = _scan_ok and (scanner_state.status == ScannerStatus.RUNNING if scanner_state else False)

    # Intraday scanner status
    intraday_status = "active" if _intraday_last_fetch else "idle"
    intraday_last = _intraday_last_fetch.isoformat() if _intraday_last_fetch else None

    return {
        "ok": ok,
        "service": "MAX_AI Scanner",
        "version": "0.1.0",
        "scanner_status": scanner_state.status.value if scanner_state else "unknown",
        "last_scan_ts": _last_scan_ts.isoformat() if _last_scan_ts else None,
        "advisory_active_count": adv_active,
        "unique_symbols": adv_unique,
        "negative_active": neg_active,
        "intraday_status": intraday_status,
        "intraday_last_fetch": intraday_last,
        "rss_ok": rss_ok,
        "news_sources_ok": news_sources_ok,
        "schwab_ok": schwab_ok,
        "errors_last_hour": errors_last_hour,
        "ignition_ranked_count": len(getattr(scanner_state, "_ignition_ranked", []) if scanner_state else []),
        "premarket_focus_count": len(getattr(scanner_state, "_premarket_focus", []) if scanner_state else []),
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/metrics")
async def metrics():
    """Get scanner metrics including advisory validation counters."""
    scanner_metrics = {}
    if scanner_state:
        scanner_metrics = scanner_state.get_metrics()

    advisory_metrics = {}
    if advisory_buffer:
        stats = advisory_buffer.get_stats()
        advisory_metrics = {
            "advisories_emitted_total": stats.get("total_emitted", 0),
            "advisories_rediscovered_total": stats.get("total_rediscovered", 0),
            "advisories_suppressed_dedup": stats.get("total_deduped", 0),
            "advisories_suppressed_negative": stats.get("total_suppressed_negative", 0),
            "avg_advisory_confidence": stats.get("avg_advisory_confidence", 0.0),
            "rediscovery_rate_pct": stats.get("rediscovery_rate", 0.0),
            "dedup_ratio_pct": stats.get("dedup_ratio", 0.0),
            "active_advisories": stats.get("active_advisories", 0),
            "rediscovered_active": stats.get("rediscovered_active", 0),
            "negative_active": stats.get("negative_active", 0),
        }

    return {
        **scanner_metrics,
        "advisory": advisory_metrics,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ============== Profiles ==============

@app.get("/profiles")
async def list_profiles():
    """List all profiles."""
    profiles = profile_loader.get_all()
    return {
        "profiles": [
            {
                "name": p.name,
                "description": p.description,
                "enabled": p.enabled,
                "alert_enabled": p.alert_enabled,
            }
            for p in profiles
        ]
    }


@app.get("/profiles/{name}")
async def get_profile(name: str):
    """Get a specific profile."""
    profile = profile_loader.get(name)
    if not profile:
        raise HTTPException(status_code=404, detail=f"Profile not found: {name}")
    return profile.model_dump()


class ProfileCreate(BaseModel):
    """Request body for creating a profile."""
    name: str
    description: str = ""
    enabled: bool = True
    conditions: list[dict] = []
    weights: dict = {}
    min_price: float = 1.0
    max_price: float = 500.0
    min_volume: int = 100000
    alert_enabled: bool = True
    alert_sound: Optional[str] = None
    alert_threshold: float = 0.7


@app.post("/profiles")
async def create_profile(data: ProfileCreate):
    """Create a new profile."""
    try:
        conditions = [ProfileCondition(**c) for c in data.conditions]
        weights = ProfileWeights(**data.weights) if data.weights else ProfileWeights()

        profile = Profile(
            name=data.name,
            description=data.description,
            enabled=data.enabled,
            conditions=conditions,
            weights=weights,
            min_price=data.min_price,
            max_price=data.max_price,
            min_volume=data.min_volume,
            alert_enabled=data.alert_enabled,
            alert_sound=data.alert_sound,
            alert_threshold=data.alert_threshold,
        )

        profile_loader.create(profile)
        return {"status": "created", "profile": profile.name}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/profiles/{name}/reload")
async def reload_profile(name: str):
    """Reload a profile from disk."""
    profile_loader.reload(name)
    return {"status": "reloaded", "profile": name}


# ============== Scanner Output ==============

@app.get("/scanner/rows")
async def get_scanner_rows(
    profile: str = Query(..., description="Profile name"),
    limit: int = Query(50, ge=1, le=200, description="Max rows to return"),
):
    """Get scanner rows for a profile."""
    if not scanner_state:
        raise HTTPException(status_code=503, detail="Scanner not initialized")

    output = scanner_state.get_output(profile)
    if not output:
        # Check if profile exists
        if not profile_loader.get(profile):
            raise HTTPException(status_code=404, detail=f"Profile not found: {profile}")
        return ScannerOutput(
            profile=profile,
            rows=[],
            total_candidates=0,
            scan_time_ms=0,
        )

    # Apply limit
    output.rows = output.rows[:limit]
    return output


@app.get("/scanner/symbol/{symbol}")
async def get_symbol_data(symbol: str):
    """Get aggregated data for a symbol across all profiles."""
    symbol = symbol.upper()
    data = ranker.get_symbol_data(symbol)

    # Add quote data if available
    snapshot = scanner_state.get_snapshot()
    if snapshot:
        quote = snapshot.get_quote(symbol)
        if quote:
            data["quote"] = quote.model_dump()

    return data


# ============== Alerts ==============

@app.get("/alerts/recent")
async def get_recent_alerts(limit: int = Query(50, ge=1, le=200)):
    """Get recent alerts."""
    alerts = alert_router.get_recent(limit)
    return {
        "alerts": [a.model_dump() for a in alerts],
        "stats": alert_router.get_stats(),
    }


class TestAlertRequest(BaseModel):
    """Request to trigger a test alert."""
    alert_type: AlertType = AlertType.MOMO_SURGE
    symbol: str = "TEST"


@app.post("/alerts/test")
async def test_alert(request: TestAlertRequest):
    """Trigger a test alert."""
    alert = alert_router.test_alert(request.alert_type, request.symbol)
    return {
        "status": "triggered",
        "alert": alert.model_dump(),
    }


# ============== Streaming ==============

@app.websocket("/stream/scanner")
async def websocket_scanner(
    websocket: WebSocket,
    profile: str = Query(..., description="Profile to stream"),
):
    """WebSocket endpoint for streaming scanner updates."""
    await websocket.accept()

    # Register connection
    if profile not in websocket_connections:
        websocket_connections[profile] = []
    websocket_connections[profile].append(websocket)

    logger.info(f"WebSocket connected for profile: {profile}")

    try:
        # Send initial data
        output = scanner_state.get_output(profile)
        if output:
            await websocket.send_text(output.model_dump_json())

        # Keep connection alive
        while True:
            try:
                # Wait for ping/pong or client disconnect
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # Send ping
                await websocket.send_text('{"type":"ping"}')

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for profile: {profile}")
    finally:
        if profile in websocket_connections:
            websocket_connections[profile].remove(websocket)


@app.websocket("/stream/advisories")
async def websocket_advisories(websocket: WebSocket):
    """WebSocket endpoint for real-time advisory push to consumers (Morpheus, IBKR_V2).

    Replaces 60s polling. Advisories are pushed immediately on emission.
    On connect, sends all currently active advisories as initial state.
    """
    await websocket.accept()
    advisory_ws_connections.append(websocket)
    logger.info(f"[WS_PUSH] Advisory consumer connected (total={len(advisory_ws_connections)})")

    try:
        # Send current active advisories as initial payload
        if advisory_buffer:
            import json
            active = advisory_buffer.get_active(min_confidence=0.0)
            negatives = advisory_buffer.get_negative()
            init_payload = json.dumps({
                "type": "init",
                "advisories": [a.model_dump(mode="json") for a in active],
                "negative": [n.model_dump(mode="json") for n in negatives],
            })
            await websocket.send_text(init_payload)

        # Keep alive — wait for pings or disconnect
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                if msg == "ping":
                    await websocket.send_text('{"type":"pong"}')
            except asyncio.TimeoutError:
                await websocket.send_text('{"type":"heartbeat"}')

    except WebSocketDisconnect:
        logger.info("[WS_PUSH] Advisory consumer disconnected")
    except Exception as e:
        logger.warning(f"[WS_PUSH] Advisory WebSocket error: {e}")
    finally:
        if websocket in advisory_ws_connections:
            advisory_ws_connections.remove(websocket)
        logger.info(f"[WS_PUSH] Advisory consumers remaining: {len(advisory_ws_connections)}")


# ============== Auth (Schwab OAuth) ==============

@app.get("/auth/status")
async def auth_status():
    """Check Schwab authentication status."""
    return {
        "authenticated": schwab_client.is_authenticated(),
        "has_refresh_token": schwab_client._refresh_token is not None,
        "token_expiry": schwab_client._token_expiry.isoformat() if schwab_client._token_expiry else None,
    }


@app.get("/auth/login")
async def auth_login(open_browser: bool = Query(True, description="Open browser automatically")):
    """
    Start Schwab OAuth flow.

    Returns the authorization URL. If open_browser=True, opens it automatically.
    After logging in, Schwab will redirect to /auth/callback with the code.
    """
    # Build authorization URL
    auth_url = "https://api.schwabapi.com/v1/oauth/authorize"
    params = {
        "response_type": "code",
        "client_id": settings.schwab_client_id,
        "redirect_uri": settings.schwab_redirect_uri,
        "scope": "readonly",
    }

    full_url = f"{auth_url}?{urllib.parse.urlencode(params)}"

    if open_browser:
        try:
            webbrowser.open(full_url)
            logger.info("Opened browser for Schwab authentication")
        except Exception as e:
            logger.warning(f"Could not open browser: {e}")

    return {
        "status": "authorization_required",
        "auth_url": full_url,
        "instructions": [
            "1. Open the auth_url in your browser (or it opened automatically)",
            "2. Log in to your Schwab account",
            "3. Authorize the application",
            "4. You will be redirected to the callback URL",
            "5. Copy the 'code' parameter from the URL",
            "6. POST it to /auth/callback with {\"code\": \"YOUR_CODE\"}",
        ],
    }


class AuthCallback(BaseModel):
    """OAuth callback request."""
    code: str


@app.post("/auth/callback")
async def auth_callback(data: AuthCallback):
    """
    Complete OAuth flow with authorization code.

    After Schwab redirects you, extract the 'code' parameter from the URL
    and POST it here to exchange for access tokens.
    """
    success = await schwab_client.exchange_code_for_tokens(data.code)

    if success:
        return {
            "status": "authenticated",
            "message": "Successfully authenticated with Schwab API",
            "authenticated": True,
        }
    else:
        raise HTTPException(
            status_code=400,
            detail="Failed to exchange authorization code for tokens"
        )


@app.post("/auth/refresh")
async def auth_refresh():
    """Manually refresh the access token."""
    if not schwab_client._refresh_token:
        raise HTTPException(status_code=400, detail="No refresh token available")

    success = await schwab_client.refresh_access_token()
    if success:
        return {"status": "refreshed", "authenticated": True}
    else:
        raise HTTPException(status_code=400, detail="Token refresh failed")


# ============== Watchlist System (A/B/C Classification) ==============


@app.get("/watchlist")
async def get_vetted_watchlist():
    """Get the curated vetted watchlist (top 5-10 tradeable stocks)."""
    if not vetted_watchlist:
        return {"list": [], "stats": {}}
    return {
        "list": vetted_watchlist.get_list(),
        "symbols": vetted_watchlist.get_symbols(),
        "stats": vetted_watchlist.get_stats(),
    }


@app.get("/watchlist/classifications")
async def get_classifications():
    """Get all current A/B/C stock classifications."""
    if not stock_classifier:
        return {"A": [], "B": [], "C": []}
    return stock_classifier.get_all_classified()


@app.get("/watchlist/classifications/stats")
async def get_classification_stats():
    """Get classification statistics."""
    if not stock_classifier:
        return {}
    return stock_classifier.get_stats()


@app.get("/watchlist/tracker")
async def get_daily_tracker():
    """Get daily performance comparison for all tracked symbols."""
    if not daily_tracker:
        return {"total_tracked": 0, "groups": {}}
    return daily_tracker.get_comparison()


@app.get("/watchlist/tracker/profitable")
async def get_profitable_stocks():
    """Get all currently profitable stocks from discovery price."""
    if not daily_tracker:
        return []
    return daily_tracker.get_profitable()


@app.get("/watchlist/tracker/eod")
async def get_eod_report():
    """Get end-of-day performance report."""
    if not daily_tracker:
        return {}
    return daily_tracker.end_of_day_report()


class ManualAddRequest(BaseModel):
    """Request to manually add a stock to vetted list."""
    symbol: str
    price: float = 0.0
    reason: str = "Manual conviction"


@app.post("/watchlist/add")
async def manual_add_to_watchlist(req: ManualAddRequest):
    """Manually add a stock to the vetted watchlist (B-class promotion)."""
    if not vetted_watchlist:
        raise HTTPException(status_code=503, detail="Watchlist not initialized")
    added, msg = vetted_watchlist.manual_add(
        symbol=req.symbol, price=req.price, reason=req.reason
    )
    return {"added": added, "message": msg}


@app.delete("/watchlist/{symbol}")
async def remove_from_watchlist(symbol: str, reason: str = "Manual removal"):
    """Remove a stock from the vetted watchlist."""
    if not vetted_watchlist:
        raise HTTPException(status_code=503, detail="Watchlist not initialized")
    removed = vetted_watchlist.remove(symbol, reason=reason)
    if not removed:
        raise HTTPException(status_code=404, detail=f"{symbol} not on vetted list")
    return {"removed": True, "symbol": symbol.upper()}


@app.post("/watchlist/reset")
async def reset_watchlist():
    """Reset all watchlist components for a new trading day."""
    if stock_classifier:
        stock_classifier.clear()
    if daily_tracker:
        daily_tracker.clear()
    if vetted_watchlist:
        vetted_watchlist.clear()
    return {"status": "reset", "message": "Classifier, tracker, and vetted list cleared"}


# ============== Admin ==============

@app.post("/admin/scanner/pause")
async def pause_scanner():
    """Pause the scanner."""
    scanner_state.status = ScannerStatus.PAUSED
    return {"status": "paused"}


@app.post("/admin/scanner/resume")
async def resume_scanner():
    """Resume the scanner."""
    scanner_state.status = ScannerStatus.RUNNING
    return {"status": "running"}


@app.post("/admin/cache/clear")
async def clear_cache():
    """Clear the quote cache."""
    quote_cache.clear()
    return {"status": "cleared"}


@app.get("/admin/cache/stats")
async def cache_stats():
    """Get cache statistics."""
    return quote_cache.get_stats()


class AddSymbolsRequest(BaseModel):
    """Request to add symbols to universe."""
    symbols: list[str]


@app.post("/admin/universe/add")
async def add_symbols(request: AddSymbolsRequest):
    """Add symbols to the scanning universe."""
    symbols = [s.upper().strip() for s in request.symbols]
    universe.add_symbols(symbols)
    return {"status": "added", "symbols": symbols, "total_universe": len(universe.universe)}


@app.get("/admin/universe/symbols")
async def list_universe():
    """List all symbols in the universe."""
    return {
        "universe_size": len(universe.universe),
        "candidates_size": len(universe.candidates),
        "candidates": universe.candidates[:50],  # First 50 active candidates
    }


@app.get("/admin/quote/{symbol}")
async def get_raw_quote(symbol: str):
    """Get raw quote from Schwab for a symbol (for debugging)."""
    symbol = symbol.upper()
    try:
        snapshot = await schwab_client.get_snapshot([symbol])
        if symbol in snapshot.quotes:
            return snapshot.quotes[symbol].model_dump()
        return {"error": f"No data for {symbol}"}
    except Exception as e:
        return {"error": str(e)}


# ============== Finviz Integration ==============

@app.get("/finviz/top-gainers")
async def get_finviz_top_gainers(
    max_price: float = Query(20.0, description="Maximum stock price"),
    min_change: float = Query(0.0, description="Minimum % change"),
    max_float: Optional[float] = Query(None, description="Maximum float in millions"),
    limit: int = Query(200, ge=1, le=500, description="Max results"),
    auto_add: bool = Query(True, description="Auto-add new symbols to scanner universe"),
):
    """
    Get top gainers from Finviz with float data.

    This endpoint fetches real-time top gainers from Finviz screener,
    including float, shares outstanding, and other ownership data.
    New symbols are automatically added to the scanner universe.
    """
    try:
        results = await finviz_client.get_top_gainers(
            max_price=max_price,
            min_change=min_change,
            max_float_millions=max_float,
            limit=limit,
        )

        # Auto-add new symbols to universe
        new_symbols = []
        if auto_add and universe and results:
            current = set(universe.universe)
            new_symbols = [r['symbol'] for r in results if r['symbol'] not in current]
            if new_symbols:
                universe.add_symbols(new_symbols)
                logger.info(f"Auto-added {len(new_symbols)} Finviz symbols to universe: {new_symbols}")

        return {
            "count": len(results),
            "filters": {
                "max_price": max_price,
                "min_change": min_change,
                "max_float": max_float,
            },
            "gainers": results,
            "symbols_added": new_symbols,
        }
    except Exception as e:
        logger.error(f"Finviz fetch error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/finviz/quote/{symbol}")
async def get_finviz_quote(symbol: str):
    """Get float and ownership data from Finviz for a specific symbol."""
    symbol = symbol.upper()
    result = await finviz_client.get_finviz_quote(symbol)
    if result:
        return result
    raise HTTPException(status_code=404, detail=f"No Finviz data for {symbol}")


# ============== Trading Halts ==============

from scanner_service.ingest import halt_tracker


@app.get("/halts")
async def get_trading_halts():
    """Get all current and recent trading halts."""
    halts = await halt_tracker.fetch_halts()
    active = [h for h in halts if h.get('status') == 'HALTED']
    resumed = [h for h in halts if h.get('status') == 'RESUMED']

    return {
        "count": len(halts),
        "active_count": len(active),
        "resumed_count": len(resumed),
        "halts": halts,
    }


@app.get("/halts/active")
async def get_active_halts():
    """Get only currently halted stocks."""
    await halt_tracker.fetch_halts()  # Refresh first
    halts = halt_tracker.get_active_halts()
    return {
        "count": len(halts),
        "halts": halts,
    }


@app.get("/halts/resumed")
async def get_resumed_halts(hours: int = Query(2, ge=1, le=24)):
    """Get recently resumed halts."""
    await halt_tracker.fetch_halts()  # Refresh first
    halts = halt_tracker.get_resumed_halts(hours)
    return {
        "count": len(halts),
        "hours": hours,
        "halts": halts,
    }


@app.post("/halts/add")
async def add_manual_halt(
    symbol: str,
    halt_price: float,
    reason: str = "Manual Entry"
):
    """Manually add a halt for tracking."""
    halt = await halt_tracker.add_manual_halt(symbol, halt_price, reason)
    return {"success": True, "halt": halt}


@app.post("/halts/{symbol}/resume")
async def mark_halt_resumed(symbol: str, resume_price: float):
    """Mark a halt as resumed with the resume price."""
    halt = await halt_tracker.update_halt_resume(symbol, resume_price)
    if halt:
        return {"success": True, "halt": halt}
    raise HTTPException(status_code=404, detail=f"No active halt found for {symbol}")


# ============== Universe Management ==============

@app.post("/universe/add")
async def add_symbols_to_universe(symbols: list[str]):
    """Add symbols to the scanner universe."""
    if not universe:
        raise HTTPException(status_code=500, detail="Universe not initialized")

    symbols = [s.upper().strip() for s in symbols if s]
    if not symbols:
        return {"added": 0, "message": "No valid symbols provided"}

    # Track which ones are new
    current = set(universe.universe)
    new_symbols = [s for s in symbols if s not in current]

    if new_symbols:
        universe.add_symbols(new_symbols)
        logger.info(f"Added {len(new_symbols)} new symbols to universe: {new_symbols}")

    return {
        "added": len(new_symbols),
        "new_symbols": new_symbols,
        "total_universe": len(universe.universe),
    }


@app.get("/universe/symbols")
async def get_universe_symbols():
    """Get all symbols in the scanner universe."""
    if not universe:
        raise HTTPException(status_code=500, detail="Universe not initialized")

    return {
        "count": len(universe.universe),
        "symbols": universe.universe,
    }


# ============== Advisories (Pull API) ==============


@app.get("/advisories")
async def get_advisories(
    min_confidence: float = Query(0.0, ge=0.0, le=1.0, description="Minimum confidence"),
    max_age_seconds: Optional[int] = Query(None, ge=1, description="Max age in seconds"),
    profile: Optional[str] = Query(None, description="Filter by profile name"),
):
    """Pull active (non-expired) advisories."""
    if not advisory_buffer:
        return {"advisories": [], "count": 0}

    active = advisory_buffer.get_active(
        min_confidence=min_confidence,
        max_age_seconds=max_age_seconds,
        profile=profile,
    )
    return {
        "advisories": [a.model_dump() for a in active],
        "count": len(active),
    }


@app.get("/advisories/history")
async def get_advisory_history(limit: int = Query(100, ge=1, le=500)):
    """Get recent advisories including expired."""
    if not advisory_buffer:
        return {"advisories": [], "count": 0}

    history = advisory_buffer.get_history(limit=limit)
    return {
        "advisories": [a.model_dump() for a in history],
        "count": len(history),
    }


@app.delete("/advisories")
async def clear_advisories():
    """Clear the advisory buffer."""
    if advisory_buffer:
        advisory_buffer.clear()
    return {"status": "cleared"}


@app.get("/advisories/negative")
async def get_negative_advisories(
    symbol: Optional[str] = Query(None, description="Filter by symbol"),
):
    """Get active DO_NOT_TRADE signals."""
    if not advisory_buffer:
        return {"negative": [], "count": 0}

    negs = advisory_buffer.get_negative(symbol=symbol)
    return {
        "negative": [n.model_dump() for n in negs],
        "count": len(negs),
    }


@app.get("/advisories/negative/check/{symbol}")
async def check_negative(symbol: str):
    """Check if a specific symbol has an active DO_NOT_TRADE signal."""
    if not advisory_buffer:
        return {"symbol": symbol.upper(), "blocked": False, "reason": ""}

    blocked, reason = advisory_buffer.is_negative(symbol)
    return {
        "symbol": symbol.upper(),
        "blocked": blocked,
        "reason": reason,
    }


@app.get("/advisories/stats")
async def advisory_stats():
    """Get advisory buffer statistics with current market phase."""
    if not advisory_buffer:
        return {"error": "Buffer not initialized"}

    stats = advisory_buffer.get_stats()
    phase = get_market_phase()
    phase_cfg = PHASE_CONFIG.get(phase, PHASE_CONFIG["OPEN"])
    stats["market_phase"] = phase
    stats["phase_label"] = phase_cfg["label"]
    stats["phase_min_ai_score"] = phase_cfg["min_ai_score"]
    stats["phase_ttl_seconds"] = phase_cfg["ttl_seconds"]
    return stats


# ============== Backward-Compat Stubs (IBKR bot startup checks) ==============


@app.get("/morpheus/auto-inject/status")
async def auto_inject_status_stub():
    """Deprecated. Use GET /advisories instead."""
    return {"enabled": False, "deprecated": True, "message": "Use GET /advisories"}


@app.post("/morpheus/auto-inject/start")
async def start_auto_inject_stub():
    """Deprecated. Advisories are emitted automatically."""
    return {"status": "deprecated", "message": "Use GET /advisories"}


@app.post("/morpheus/auto-inject/stop")
async def stop_auto_inject_stub():
    """Deprecated. Advisories are emitted automatically."""
    return {"status": "deprecated", "message": "Use GET /advisories"}


@app.post("/morpheus/auto-inject/reset")
async def reset_inject_stub():
    """Deprecated. Use DELETE /advisories instead."""
    return {"status": "deprecated", "message": "Use DELETE /advisories"}


@app.post("/morpheus/inject")
async def inject_stub(symbols: list[str]):
    """Deprecated. Advisories are emitted automatically from scanner cycle."""
    return {"status": "deprecated", "message": "Use GET /advisories"}


# ============== TradingView Pre-Market ==============


@app.get("/tradingview/status")
async def tradingview_status():
    """Get TradingView pre-market scanner status."""
    return {
        "last_fetch": _tv_last_fetch.isoformat() if _tv_last_fetch else None,
        "fetch_interval_seconds": _tv_fetch_interval_seconds,
        "discovered_count": len(_tv_discovered_symbols),
        "discovered_symbols": sorted(_tv_discovered_symbols),
        "phase": get_market_phase(),
        "active": get_market_phase() == "PREMARKET",
    }


@app.post("/tradingview/fetch-now")
async def tradingview_fetch_now():
    """Trigger an immediate TradingView pre-market fetch (ignores cooldown)."""
    global _tv_last_fetch
    _tv_last_fetch = None  # Reset cooldown to force fetch
    await _fetch_tradingview_gappers()
    return {
        "status": "fetched",
        "discovered_count": len(_tv_discovered_symbols),
        "discovered_symbols": sorted(_tv_discovered_symbols),
    }


@app.post("/tradingview/reset")
async def tradingview_reset():
    """Reset TradingView discovered symbols (call at session start)."""
    global _tv_discovered_symbols, _tv_last_fetch, _intraday_discovered_symbols, _intraday_last_fetch
    _tv_discovered_symbols.clear()
    _tv_last_fetch = None
    _intraday_discovered_symbols.clear()
    _intraday_last_fetch = None
    return {"status": "reset"}


@app.get("/intraday/status")
async def intraday_status():
    """Get intraday small-cap mover scanner status."""
    return {
        "last_fetch": _intraday_last_fetch.isoformat() if _intraday_last_fetch else None,
        "fetch_interval_seconds": _intraday_fetch_interval_seconds,
        "discovered_count": len(_intraday_discovered_symbols),
        "discovered_symbols": sorted(_intraday_discovered_symbols),
        "tv_premarket_carryover_count": len(_tv_discovered_symbols),
        "phase": get_market_phase(),
        "active": get_market_phase() == "OPEN",
    }


@app.post("/intraday/fetch-now")
async def intraday_fetch_now():
    """Trigger immediate intraday small-cap scan (ignores cooldown and phase)."""
    global _intraday_last_fetch
    _intraday_last_fetch = None

    # Temporarily override phase check by calling sources directly
    total = 0
    results = {"tv_movers": [], "finviz_gainers": []}

    try:
        from concurrent.futures import ThreadPoolExecutor
        from scanner_service.ingest.tradingview_client import fetch_intraday_movers

        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as pool:
            movers = await loop.run_in_executor(pool, fetch_intraday_movers)
        if movers:
            results["tv_movers"] = [f"{m['symbol']} +{m['change_pct']:.1f}%" for m in movers[:10]]
            total += len(movers)
    except Exception as e:
        results["tv_error"] = str(e)

    try:
        gainers = await finviz_client.get_top_gainers(max_price=20.0, min_change=10.0, limit=20)
        if gainers:
            results["finviz_gainers"] = [f"{g['symbol']} +{g['change_pct']:.1f}%" for g in gainers[:10]]
            total += len(gainers)
    except Exception as e:
        results["finviz_error"] = str(e)

    return {
        "status": "fetched",
        "total_found": total,
        "results": results,
    }


# ============== Webull Pre-Market ==============


@app.get("/webull/status")
async def webull_status():
    """Get Webull pre-market scanner status."""
    return {
        "last_fetch": _wb_last_fetch.isoformat() if _wb_last_fetch else None,
        "fetch_interval_seconds": _wb_fetch_interval_seconds,
        "discovered_count": len(_wb_discovered_symbols),
        "discovered_symbols": sorted(_wb_discovered_symbols),
        "phase": get_market_phase(),
        "active": get_market_phase() == "PREMARKET",
    }


@app.post("/webull/fetch-now")
async def webull_fetch_now():
    """Trigger an immediate Webull pre-market fetch (ignores cooldown)."""
    global _wb_last_fetch
    _wb_last_fetch = None  # Reset cooldown to force fetch
    await _fetch_webull_premarket_gainers()
    return {
        "status": "fetched",
        "discovered_count": len(_wb_discovered_symbols),
        "discovered_symbols": sorted(_wb_discovered_symbols),
    }


# ============== News Integration ==============

# News client instance (lazy init)
_news_client = None

def _get_news_client():
    global _news_client
    if _news_client is None:
        from scanner_service.ingest.news_client import get_news_client
        _news_client = get_news_client()
    return _news_client


@app.get("/news/status")
async def news_status():
    """Get news client status (includes pipeline health if active)."""
    status = _get_news_client().get_status()
    if news_pipeline:
        status["pipeline"] = news_pipeline.get_status()
    return status


@app.post("/news/start")
async def start_news(poll_interval: int = Query(10, ge=5, le=60)):
    """Start the news polling service."""
    client = _get_news_client()
    client.start(poll_interval=poll_interval)
    return {"status": "started", "poll_interval": poll_interval}


@app.post("/news/stop")
async def stop_news():
    """Stop the news polling service."""
    client = _get_news_client()
    client.stop()
    return {"status": "stopped"}


@app.get("/news/recent")
async def recent_news(limit: int = Query(20, ge=1, le=100)):
    """Get recent news alerts."""
    client = _get_news_client()
    return {
        "alerts": client.get_recent_alerts(limit=limit),
        "count": len(client.recent_alerts)
    }


@app.post("/news/poll")
async def poll_news_now():
    """Manually poll for news now."""
    client = _get_news_client()
    alerts = await client.poll_news()
    return {
        "new_alerts": len(alerts),
        "alerts": [a.to_dict() for a in alerts]
    }


@app.post("/news/push-to-morpheus")
async def push_news_to_morpheus_stub():
    """Deprecated. News advisories are emitted to the advisory buffer automatically."""
    return {"status": "deprecated", "message": "Use GET /advisories"}


# ============== Ignition Ranking ==============


@app.get("/ignition/ranked")
async def get_ignition_ranked(limit: int = Query(20, ge=1, le=50)):
    """Get symbols ranked by ignition probability (highest first)."""
    ranked = getattr(scanner_state, "_ignition_ranked", []) if scanner_state else []
    return {
        "ranked": ranked[:limit],
        "count": len(ranked[:limit]),
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/ignition/catalysts")
async def get_ignition_catalysts():
    """Get known catalyst symbols from news pipeline."""
    if not ignition_scorer:
        return {"catalysts": {}}
    return {"catalysts": ignition_scorer.get_catalyst_symbols()}


# ============== Premarket Focus ==============


@app.get("/premarket/focus")
async def get_premarket_focus():
    """Get premarket focus list (rvol>=3, float<50M, $1-$20, ranked by AI score)."""
    focus = getattr(scanner_state, "_premarket_focus", []) if scanner_state else []
    phase = get_market_phase()
    return {
        "focus": focus,
        "count": len(focus),
        "phase": phase,
        "active": phase == "PREMARKET",
        "criteria": {
            "rvol_min": 3,
            "float_max_m": 50,
            "price_range": "$1-$20",
            "news_catalyst": "boosted if present",
        },
    }


# ============== News Pipeline (redundant) ==============


@app.get("/news/pipeline/status")
async def news_pipeline_status():
    """Get news pipeline status with per-source health."""
    if not news_pipeline:
        return {"error": "Pipeline not initialized"}
    return news_pipeline.get_status()


# ============== Sector Intelligence ==============


@app.get("/sector/heatmap")
async def get_sector_heatmap():
    """Get sector heat scores from research server (cached 60s)."""
    if not research_client:
        return {"heatmap": {}, "available": False}
    heatmap = await research_client.get_heatmap()
    return {
        "heatmap": {
            sector: {
                **data,
                "heat": heat_label(data.get("heat_score", 0)),
                "multiplier": round(sector_multiplier(data.get("heat_score", 0)), 2),
            }
            for sector, data in heatmap.items()
        },
        "available": research_client._available,
    }


@app.get("/sector/symbol/{symbol}")
async def get_symbol_sector(symbol: str):
    """Get sector classification for a symbol."""
    if not research_client:
        return {"symbol": symbol.upper(), "sector": "unknown"}
    data = await research_client.get_symbol_sector(symbol)
    sector = data.get("sector", "unknown")
    heat = research_client.get_heat_score(sector)
    return {
        **data,
        "heat_score": round(heat, 2),
        "heat": heat_label(heat),
        "multiplier": round(sector_multiplier(heat), 2),
    }


@app.get("/sector/status")
async def sector_status():
    """Get research server connection status."""
    if not research_client:
        return {"error": "Research client not initialized"}
    return research_client.get_status()


# ============== Momentum Chains ==============


@app.get("/chains")
async def get_momentum_chains():
    """Get detected momentum chains (sector clusters with leader + sympathy)."""
    chains = getattr(scanner_state, "_momentum_chains", []) if scanner_state else []
    chain_symbols = chain_detector.get_chain_symbols() if chain_detector else {}
    return {
        "chains": chains,
        "chain_count": len(chains),
        "chain_symbols": chain_symbols,
        "timestamp": datetime.utcnow().isoformat(),
    }


# ============== Health Monitor ==============


@app.get("/health/monitor")
async def get_health_monitor_status():
    """Get health monitor status and latency stats."""
    if not health_monitor:
        return {"error": "Monitor not initialized"}
    return health_monitor.get_status()


# ============== Main ==============

def main():
    """Run the scanner service."""
    import uvicorn

    uvicorn.run(
        "scanner_service.app:app",
        host=settings.scanner_host,
        port=settings.scanner_port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
