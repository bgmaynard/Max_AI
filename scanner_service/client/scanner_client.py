"""
MAX_AI Scanner Client - Single source of market discovery for bots.

This client provides the ONLY approved method for bots to consume
market discovery data. Bots MUST NOT implement their own scanners.

Copy this module to your bot project and use it for all discovery needs.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Any
from enum import Enum

import httpx

logger = logging.getLogger(__name__)

# Default scanner URL (local service)
SCANNER_BASE_URL = "http://127.0.0.1:8787"


class ScannerHealthError(Exception):
    """Raised when scanner is unhealthy or unavailable."""
    pass


class HaltStatus(str, Enum):
    """Trading halt status."""
    HALTED = "HALTED"
    RESUMED = "RESUMED"
    NONE = ""


@dataclass
class ScannerRow:
    """
    A ranked row from MAX_AI_SCANNER.

    This is the authoritative data structure for scanner output.
    Bots should consume these fields and NOT recompute them.
    """
    rank: int
    symbol: str
    price: float
    change_pct: float
    volume: int
    ai_score: float
    tags: list[str] = field(default_factory=list)

    # Momentum features (computed by scanner)
    velocity_1m: Optional[float] = None
    rvol_proxy: Optional[float] = None
    hod_distance_pct: Optional[float] = None
    spread: Optional[float] = None

    # Float data (from Finviz)
    float_shares: Optional[float] = None  # In millions
    market_cap: Optional[float] = None    # In millions

    # Gap data
    gap_pct: Optional[float] = None
    prev_close: Optional[float] = None

    # Halt data
    halt_status: Optional[str] = None

    @property
    def is_halted(self) -> bool:
        return self.halt_status == HaltStatus.HALTED.value

    @property
    def is_halt_resumed(self) -> bool:
        return self.halt_status == HaltStatus.RESUMED.value

    @property
    def has_tag(self) -> callable:
        """Check if row has a specific tag."""
        def check(tag: str) -> bool:
            return tag.upper() in [t.upper() for t in self.tags]
        return check


@dataclass
class SymbolContext:
    """
    Full context for a symbol from MAX_AI_SCANNER.

    Use this before making trade decisions to get all available data.
    """
    symbol: str
    profiles: dict[str, Any] = field(default_factory=dict)
    quote: Optional[dict] = None

    def get_score(self, profile: str) -> Optional[float]:
        """Get AI score for a specific profile."""
        if profile in self.profiles:
            return self.profiles[profile].get("ai_score")
        return None

    def get_rank(self, profile: str) -> Optional[int]:
        """Get rank for a specific profile."""
        if profile in self.profiles:
            return self.profiles[profile].get("rank")
        return None


@dataclass
class HaltInfo:
    """Trading halt information."""
    symbol: str
    halt_time: Optional[datetime] = None
    halt_price: Optional[float] = None
    halt_reason: Optional[str] = None
    resume_time: Optional[datetime] = None
    resume_price: Optional[float] = None
    status: str = "HALTED"
    exchange: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return self.status == "HALTED"

    @property
    def is_resumed(self) -> bool:
        return self.status == "RESUMED"


class ScannerClient:
    """
    Client for consuming MAX_AI_SCANNER data.

    This is the ONLY approved method for bots to access market discovery.

    Usage:
        async with ScannerClient() as client:
            # Get ranked opportunities
            rows = await client.get_rows("FAST_MOVERS", limit=25)

            # Get symbol context before trading
            context = await client.get_symbol("AAPL")

            # Monitor halts
            halts = await client.get_active_halts()

    IMPORTANT:
        - Poll /scanner/rows every 2-5 seconds (NOT faster)
        - Cache results locally
        - Enter safe mode if scanner is down
        - NEVER implement fallback scraping
    """

    def __init__(
        self,
        base_url: str = SCANNER_BASE_URL,
        timeout: float = 5.0,
        retry_attempts: int = 3,
        retry_delay: float = 1.0,
    ):
        """
        Initialize scanner client.

        Args:
            base_url: Scanner service URL (default: http://127.0.0.1:8787)
            timeout: Request timeout in seconds
            retry_attempts: Number of retry attempts on failure
            retry_delay: Delay between retries in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retry_attempts = retry_attempts
        self.retry_delay = retry_delay
        self._client: Optional[httpx.AsyncClient] = None
        self._last_health_check: Optional[datetime] = None
        self._is_healthy: bool = False

    async def __aenter__(self):
        """Async context manager entry."""
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers={"User-Agent": "MAX_AI_Bot/1.0"}
        )
        return self

    async def __aexit__(self, *args):
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
    ) -> dict:
        """Make HTTP request with retry logic."""
        if not self._client:
            raise RuntimeError("Client not initialized. Use 'async with ScannerClient():'")

        url = f"{self.base_url}{path}"
        last_error = None

        for attempt in range(self.retry_attempts):
            try:
                if method == "GET":
                    resp = await self._client.get(url, params=params)
                elif method == "POST":
                    resp = await self._client.post(url, json=json, params=params)
                else:
                    raise ValueError(f"Unsupported method: {method}")

                resp.raise_for_status()
                return resp.json()

            except httpx.HTTPStatusError as e:
                last_error = e
                logger.warning(f"HTTP error on {path}: {e.response.status_code}")
                if e.response.status_code < 500:
                    raise  # Don't retry client errors

            except httpx.RequestError as e:
                last_error = e
                logger.warning(f"Request error on {path}: {e}")

            if attempt < self.retry_attempts - 1:
                await asyncio.sleep(self.retry_delay)

        raise ScannerHealthError(f"Scanner unavailable after {self.retry_attempts} attempts: {last_error}")

    # ==================== Health ====================

    async def health_check(self) -> bool:
        """
        Check if scanner is healthy.

        Returns:
            True if scanner is healthy, False otherwise.
        """
        try:
            data = await self._request("GET", "/health")
            self._is_healthy = data.get("status") == "healthy"
            self._last_health_check = datetime.utcnow()
            return self._is_healthy
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            self._is_healthy = False
            return False

    async def require_healthy(self):
        """Raise exception if scanner is not healthy."""
        if not await self.health_check():
            raise ScannerHealthError("Scanner is not healthy")

    @property
    def is_healthy(self) -> bool:
        """Last known health status (may be stale)."""
        return self._is_healthy

    # ==================== Scanner Rows ====================

    async def get_rows(
        self,
        profile: str = "FAST_MOVERS",
        limit: int = 25,
    ) -> list[ScannerRow]:
        """
        Get ranked scanner rows for a profile.

        Args:
            profile: Strategy profile (FAST_MOVERS, GAPPERS, HOD_BREAK, etc.)
            limit: Maximum rows to return (1-200)

        Returns:
            List of ScannerRow objects, ranked by AI score.

        Example:
            rows = await client.get_rows("FAST_MOVERS", limit=25)
            for row in rows:
                if row.ai_score > 70 and row.change_pct > 5:
                    # Evaluate for trade
                    pass
        """
        data = await self._request(
            "GET",
            "/scanner/rows",
            params={"profile": profile, "limit": limit}
        )

        rows = []
        for row_data in data.get("rows", []):
            rows.append(ScannerRow(
                rank=row_data.get("rank", 0),
                symbol=row_data.get("symbol", ""),
                price=row_data.get("price", 0.0),
                change_pct=row_data.get("change_pct", 0.0),
                volume=row_data.get("volume", 0),
                ai_score=row_data.get("ai_score", 0.0),
                tags=row_data.get("tags", []),
                velocity_1m=row_data.get("velocity_1m"),
                rvol_proxy=row_data.get("rvol_proxy"),
                hod_distance_pct=row_data.get("hod_distance_pct"),
                spread=row_data.get("spread"),
                float_shares=row_data.get("float_shares"),
                market_cap=row_data.get("market_cap"),
                gap_pct=row_data.get("gap_pct"),
                prev_close=row_data.get("prev_close"),
                halt_status=row_data.get("halt_status"),
            ))

        return rows

    async def get_all_profiles(self) -> dict[str, list[ScannerRow]]:
        """
        Get rows from all enabled profiles.

        Returns:
            Dict mapping profile name to list of rows.
        """
        # First get list of profiles
        data = await self._request("GET", "/profiles")
        profiles = [p["name"] for p in data.get("profiles", []) if p.get("enabled")]

        # Fetch rows for each profile concurrently
        results = {}
        tasks = [self.get_rows(profile) for profile in profiles]
        rows_list = await asyncio.gather(*tasks, return_exceptions=True)

        for profile, rows in zip(profiles, rows_list):
            if isinstance(rows, Exception):
                logger.warning(f"Failed to fetch {profile}: {rows}")
                results[profile] = []
            else:
                results[profile] = rows

        return results

    # ==================== Symbol Context ====================

    async def get_symbol(self, symbol: str) -> SymbolContext:
        """
        Get full context for a symbol.

        Use this before making trade decisions.

        Args:
            symbol: Stock symbol (e.g., "AAPL")

        Returns:
            SymbolContext with data across all profiles.

        Example:
            context = await client.get_symbol("XYZ")
            if context.get_score("FAST_MOVERS") > 80:
                # High score in FAST_MOVERS profile
                pass
        """
        data = await self._request("GET", f"/scanner/symbol/{symbol.upper()}")

        return SymbolContext(
            symbol=symbol.upper(),
            profiles=data.get("profiles", {}),
            quote=data.get("quote"),
        )

    # ==================== Trading Halts ====================

    async def get_active_halts(self) -> list[HaltInfo]:
        """
        Get currently halted stocks.

        Returns:
            List of HaltInfo for stocks currently halted.
        """
        data = await self._request("GET", "/halts/active")
        return self._parse_halts(data.get("halts", []))

    async def get_resumed_halts(self, hours: int = 2) -> list[HaltInfo]:
        """
        Get recently resumed halts.

        Args:
            hours: Look back period (1-24 hours)

        Returns:
            List of HaltInfo for recently resumed stocks.
        """
        data = await self._request("GET", "/halts/resumed", params={"hours": hours})
        return self._parse_halts(data.get("halts", []))

    async def get_all_halts(self) -> tuple[list[HaltInfo], list[HaltInfo]]:
        """
        Get both active and resumed halts.

        Returns:
            Tuple of (active_halts, resumed_halts)
        """
        data = await self._request("GET", "/halts")
        all_halts = self._parse_halts(data.get("halts", []))

        active = [h for h in all_halts if h.is_active]
        resumed = [h for h in all_halts if h.is_resumed]

        return active, resumed

    def _parse_halts(self, halts_data: list[dict]) -> list[HaltInfo]:
        """Parse halt data into HaltInfo objects."""
        halts = []
        for h in halts_data:
            halts.append(HaltInfo(
                symbol=h.get("symbol", ""),
                halt_time=self._parse_datetime(h.get("halt_time")),
                halt_price=h.get("halt_price"),
                halt_reason=h.get("halt_reason"),
                resume_time=self._parse_datetime(h.get("resume_time")),
                resume_price=h.get("resume_price"),
                status=h.get("status", "HALTED"),
                exchange=h.get("exchange"),
            ))
        return halts

    @staticmethod
    def _parse_datetime(dt_str: Optional[str]) -> Optional[datetime]:
        """Parse datetime string."""
        if not dt_str:
            return None
        try:
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return None

    # ==================== Finviz Data ====================

    async def get_finviz_quote(self, symbol: str) -> Optional[dict]:
        """
        Get float and ownership data from Finviz.

        Args:
            symbol: Stock symbol

        Returns:
            Dict with float_shares, market_cap, short_float, etc.
            or None if not found.
        """
        try:
            data = await self._request("GET", f"/finviz/quote/{symbol.upper()}")
            return data
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None
            raise


# ==================== WebSocket Client (Optional) ====================

class ScannerStreamClient:
    """
    WebSocket client for real-time scanner updates.

    Usage:
        async with ScannerStreamClient("FAST_MOVERS") as stream:
            async for rows in stream:
                for row in rows:
                    print(f"{row.symbol}: {row.change_pct}%")
    """

    def __init__(
        self,
        profile: str = "FAST_MOVERS",
        base_url: str = SCANNER_BASE_URL,
    ):
        self.profile = profile
        self.base_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
        self._ws = None

    async def __aenter__(self):
        import websockets
        url = f"{self.base_url}/stream/scanner?profile={self.profile}"
        self._ws = await websockets.connect(url)
        return self

    async def __aexit__(self, *args):
        if self._ws:
            await self._ws.close()

    def __aiter__(self):
        return self

    async def __anext__(self) -> list[ScannerRow]:
        import json

        while True:
            try:
                msg = await self._ws.recv()
                data = json.loads(msg)

                # Skip ping messages
                if data.get("type") == "ping":
                    await self._ws.send('{"type":"pong"}')
                    continue

                # Parse rows
                rows = []
                for row_data in data.get("rows", []):
                    rows.append(ScannerRow(
                        rank=row_data.get("rank", 0),
                        symbol=row_data.get("symbol", ""),
                        price=row_data.get("price", 0.0),
                        change_pct=row_data.get("change_pct", 0.0),
                        volume=row_data.get("volume", 0),
                        ai_score=row_data.get("ai_score", 0.0),
                        tags=row_data.get("tags", []),
                        velocity_1m=row_data.get("velocity_1m"),
                        rvol_proxy=row_data.get("rvol_proxy"),
                        hod_distance_pct=row_data.get("hod_distance_pct"),
                        spread=row_data.get("spread"),
                        halt_status=row_data.get("halt_status"),
                    ))

                return rows

            except Exception as e:
                logger.error(f"WebSocket error: {e}")
                raise StopAsyncIteration
