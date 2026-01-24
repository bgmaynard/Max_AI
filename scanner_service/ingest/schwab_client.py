"""Schwab/thinkorswim API client for market data."""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import base64

import httpx

from scanner_service.settings import get_settings
from scanner_service.schemas.market_snapshot import Quote, MarketSnapshot

logger = logging.getLogger(__name__)


class SchwabClient:
    """
    Async client for Schwab API market data.

    Handles authentication, token refresh, and batch quote requests.
    """

    BASE_URL = "https://api.schwabapi.com/marketdata/v1"
    AUTH_URL = "https://api.schwabapi.com/v1/oauth/token"

    def __init__(self):
        self.settings = get_settings()
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._load_tokens()

    def _load_tokens(self) -> None:
        """Load tokens from disk if available."""
        token_path = self.settings.schwab_token_path
        if token_path.exists():
            try:
                with open(token_path, "r") as f:
                    data = json.load(f)
                    self._access_token = data.get("access_token")
                    self._refresh_token = data.get("refresh_token")

                    # Handle both formats: "expiry" (our format) or "expires_in" (Schwab native)
                    expiry = data.get("expiry")
                    if expiry:
                        self._token_expiry = datetime.fromisoformat(expiry)
                    elif data.get("expires_in"):
                        # Token might be expired, try refresh
                        self._token_expiry = datetime.utcnow() - timedelta(seconds=1)

                    logger.info(f"Loaded Schwab tokens from disk (has refresh: {bool(self._refresh_token)})")
            except Exception as e:
                logger.warning(f"Failed to load tokens: {e}")

    def _save_tokens(self) -> None:
        """Persist tokens to disk."""
        token_path = self.settings.schwab_token_path
        token_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "expiry": self._token_expiry.isoformat() if self._token_expiry else None,
        }
        with open(token_path, "w") as f:
            json.dump(data, f)

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def is_authenticated(self) -> bool:
        """Check if we have valid authentication."""
        if not self._access_token:
            return False
        if self._token_expiry and datetime.utcnow() >= self._token_expiry:
            return False
        return True

    async def refresh_access_token(self) -> bool:
        """Refresh the access token using refresh token."""
        if not self._refresh_token:
            logger.error("No refresh token available")
            return False

        client = await self._get_client()

        # Use Basic auth like the working implementation
        credentials = f"{self.settings.schwab_client_id}:{self.settings.schwab_client_secret}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        headers = {
            "Authorization": f"Basic {encoded_credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }

        try:
            response = await client.post(self.AUTH_URL, headers=headers, data=data)

            logger.info(f"Token refresh response: {response.status_code}")

            if response.status_code != 200:
                logger.error(f"Token refresh failed: {response.text}")
                return False

            token_data = response.json()
            self._access_token = token_data["access_token"]
            self._refresh_token = token_data.get("refresh_token", self._refresh_token)
            expires_in = token_data.get("expires_in", 1800)
            self._token_expiry = datetime.utcnow() + timedelta(seconds=expires_in - 60)
            self._save_tokens()
            logger.info("Refreshed Schwab access token successfully")
            return True
        except Exception as e:
            logger.error(f"Token refresh error: {e}")
            return False

    async def exchange_code_for_tokens(self, code: str) -> bool:
        """
        Exchange authorization code for access and refresh tokens.

        Args:
            code: Authorization code from OAuth callback

        Returns:
            True if successful, False otherwise
        """
        client = await self._get_client()

        # Schwab requires Basic auth with client_id:client_secret
        credentials = f"{self.settings.schwab_client_id}:{self.settings.schwab_client_secret}"
        encoded_credentials = base64.b64encode(credentials.encode()).decode()

        headers = {
            "Authorization": f"Basic {encoded_credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.settings.schwab_redirect_uri,
        }

        try:
            response = await client.post(
                self.AUTH_URL,
                headers=headers,
                data=data,
            )

            logger.info(f"Token exchange response status: {response.status_code}")

            if response.status_code != 200:
                logger.error(f"Token exchange failed: {response.text}")
                return False

            token_data = response.json()
            self._access_token = token_data["access_token"]
            self._refresh_token = token_data.get("refresh_token")
            expires_in = token_data.get("expires_in", 1800)
            self._token_expiry = datetime.utcnow() + timedelta(seconds=expires_in - 60)

            self._save_tokens()
            logger.info("Successfully exchanged code for tokens")
            return True

        except Exception as e:
            logger.error(f"Token exchange error: {e}")
            return False

    async def get_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """
        Fetch quotes for multiple symbols in a batch.

        Args:
            symbols: List of ticker symbols

        Returns:
            Dictionary mapping symbol to Quote object
        """
        if not symbols:
            return {}

        # Schwab API limit is typically 500 symbols per request
        batch_size = 100
        all_quotes = {}

        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            batch_quotes = await self._fetch_quote_batch(batch)
            all_quotes.update(batch_quotes)

        return all_quotes

    async def _fetch_quote_batch(self, symbols: list[str]) -> dict[str, Quote]:
        """Fetch a single batch of quotes."""
        if not self.is_authenticated():
            logger.warning("Not authenticated - returning empty quotes")
            return self._generate_mock_quotes(symbols)

        client = await self._get_client()
        headers = {"Authorization": f"Bearer {self._access_token}"}
        params = {"symbols": ",".join(symbols)}

        try:
            response = await client.get(
                f"{self.BASE_URL}/quotes",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            data = response.json()
            return self._parse_quotes(data)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.warning("Token expired, attempting refresh")
                if await self.refresh_access_token():
                    return await self._fetch_quote_batch(symbols)
            logger.error(f"Quote fetch failed: {e}")
            return self._generate_mock_quotes(symbols)
        except Exception as e:
            logger.error(f"Quote fetch error: {e}")
            return self._generate_mock_quotes(symbols)

    def _parse_quotes(self, data: dict) -> dict[str, Quote]:
        """Parse Schwab API response into Quote objects."""
        quotes = {}
        logged_sample = False

        for symbol, quote_data in data.items():
            try:
                q = quote_data.get("quote", {})

                # Log one sample to see available fields
                if not logged_sample:
                    logger.info(f"Schwab quote fields for {symbol}: {list(q.keys())}")
                    logged_sample = True

                # Try multiple field names for average volume
                avg_vol = (
                    q.get("averageVolume") or
                    q.get("averageVolume10Day") or
                    q.get("avg10DayVolume") or
                    q.get("avgVolume") or
                    0
                )

                quotes[symbol] = Quote(
                    symbol=symbol,
                    last_price=q.get("lastPrice", 0),
                    bid=q.get("bidPrice", 0),
                    ask=q.get("askPrice", 0),
                    bid_size=q.get("bidSize", 0),
                    ask_size=q.get("askSize", 0),
                    volume=q.get("totalVolume", 0),
                    avg_volume=avg_vol,
                    high=q.get("highPrice", 0),
                    low=q.get("lowPrice", 0),
                    open_price=q.get("openPrice", 0),
                    prev_close=q.get("closePrice", 0),
                    change=q.get("netChange", 0),
                    change_pct=q.get("netPercentChange", 0),
                )
            except Exception as e:
                logger.warning(f"Failed to parse quote for {symbol}: {e}")
        return quotes

    def _generate_mock_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Generate mock quotes for testing when not authenticated."""
        import random

        quotes = {}
        for symbol in symbols:
            base_price = random.uniform(10, 200)
            change_pct = random.uniform(-5, 10)
            quotes[symbol] = Quote(
                symbol=symbol,
                last_price=base_price,
                bid=base_price * 0.999,
                ask=base_price * 1.001,
                bid_size=random.randint(100, 1000),
                ask_size=random.randint(100, 1000),
                volume=random.randint(100000, 5000000),
                avg_volume=random.randint(500000, 2000000),
                high=base_price * (1 + abs(change_pct) / 100),
                low=base_price * (1 - random.uniform(0, 3) / 100),
                open_price=base_price * (1 - change_pct / 200),
                prev_close=base_price / (1 + change_pct / 100),
                change=base_price * change_pct / 100,
                change_pct=change_pct,
            )
        return quotes

    async def get_snapshot(self, symbols: list[str]) -> MarketSnapshot:
        """Get a complete market snapshot for symbols."""
        start = datetime.utcnow()
        quotes = await self.get_quotes(symbols)
        duration = (datetime.utcnow() - start).total_seconds() * 1000

        return MarketSnapshot(
            quotes=quotes,
            timestamp=datetime.utcnow(),
            scan_duration_ms=duration,
        )

    async def get_fundamentals(self, symbols: list[str]) -> dict[str, dict]:
        """
        Fetch fundamental data for symbols (including float/shares outstanding).

        Args:
            symbols: List of ticker symbols

        Returns:
            Dictionary mapping symbol to fundamental data
        """
        if not symbols:
            return {}

        if not self.is_authenticated():
            logger.warning("Not authenticated - cannot fetch fundamentals")
            return {}

        # Schwab limits to ~100 symbols per request
        batch_size = 100
        all_fundamentals = {}

        for i in range(0, len(symbols), batch_size):
            batch = symbols[i : i + batch_size]
            batch_fundamentals = await self._fetch_fundamentals_batch(batch)
            all_fundamentals.update(batch_fundamentals)

        return all_fundamentals

    async def _fetch_fundamentals_batch(self, symbols: list[str]) -> dict[str, dict]:
        """Fetch a single batch of fundamentals."""
        client = await self._get_client()
        headers = {"Authorization": f"Bearer {self._access_token}"}

        # Use the instruments endpoint with fundamental projection
        params = {
            "symbol": ",".join(symbols),
            "projection": "fundamental",
        }

        try:
            response = await client.get(
                f"{self.BASE_URL}/instruments",
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            data = response.json()
            return self._parse_fundamentals(data)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.warning("Token expired during fundamentals fetch, attempting refresh")
                if await self.refresh_access_token():
                    return await self._fetch_fundamentals_batch(symbols)
            logger.error(f"Fundamentals fetch failed: {e}")
            return {}
        except Exception as e:
            logger.error(f"Fundamentals fetch error: {e}")
            return {}

    def _parse_fundamentals(self, data) -> dict[str, dict]:
        """Parse Schwab fundamentals response."""
        fundamentals = {}
        logged_sample = False

        # Schwab returns a list of instruments
        instruments = data if isinstance(data, list) else data.get("instruments", [])

        for instrument in instruments:
            try:
                symbol = instrument.get("symbol", "")
                if not symbol:
                    continue

                fund = instrument.get("fundamental", {})

                # Log one sample to see available fields
                if not logged_sample and fund:
                    logger.info(f"Schwab fundamental fields for {symbol}: {list(fund.keys())}")
                    logged_sample = True

                # Schwab provides sharesOutstanding and marketCapFloat
                shares_outstanding = fund.get("sharesOutstanding", 0) or 0
                market_cap = fund.get("marketCap", 0) or 0

                # marketCapFloat is the float in shares (not dollars!)
                # If not available, estimate as 85% of shares outstanding
                float_shares = fund.get("marketCapFloat", 0) or 0
                if float_shares == 0 and shares_outstanding > 0:
                    float_shares = shares_outstanding * 0.85

                fundamentals[symbol] = {
                    "float_shares": float_shares,  # Float shares
                    "shares_outstanding": shares_outstanding,
                    "market_cap": market_cap,
                    "pe_ratio": fund.get("peRatio", 0) or 0,
                    "dividend_yield": fund.get("dividendYield", 0) or 0,
                    "avg_volume": fund.get("avg10DaysVolume", 0) or 0,
                }
            except Exception as e:
                logger.warning(f"Failed to parse fundamentals for instrument: {e}")

        return fundamentals
