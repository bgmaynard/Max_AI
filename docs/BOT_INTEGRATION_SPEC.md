# MAX_AI_SCANNER → BOT INTEGRATION SPECIFICATION

**Version:** 1.0
**Date:** 2026-01-24
**Status:** ACTIVE

---

## 1. EXECUTIVE SUMMARY

MAX_AI_SCANNER is the **single source of truth** for all market discovery. All bots (Morpheus_AI, future bots) MUST consume scanner data via local APIs. No bot may independently scrape, scan, or poll for discovery.

### Current State Assessment

| Component | Status | Notes |
|-----------|--------|-------|
| MAX_AI_SCANNER | ✅ OPERATIONAL | Running at `http://127.0.0.1:8787` |
| Morpheus_AI | ⏳ NOT IN REPO | External project, needs integration |
| Legacy Bots | ✅ NONE FOUND | No duplicate scanners exist |
| Finviz Scraping | ✅ CENTRALIZED | `scanner_service/ingest/finviz_client.py` |
| Halt Tracking | ✅ CENTRALIZED | `scanner_service/ingest/halt_tracker.py` |
| Schwab Polling | ✅ CENTRALIZED | `scanner_service/ingest/schwab_client.py` |

---

## 2. SCANNER TOUCHPOINTS PER BOT

### 2.1 Morpheus_AI (External - Needs Implementation)

When Morpheus_AI is created/integrated, it MUST use these endpoints:

| Purpose | Endpoint | Method | Frequency |
|---------|----------|--------|-----------|
| Fast Movers | `GET /scanner/rows?profile=FAST_MOVERS&limit=25` | Pull | 2-5 sec |
| Gappers | `GET /scanner/rows?profile=GAPPERS&limit=25` | Pull | 2-5 sec |
| HOD Breaks | `GET /scanner/rows?profile=HOD_BREAK&limit=25` | Pull | 2-5 sec |
| Symbol Context | `GET /scanner/symbol/{symbol}` | Pull | Before trade |
| Trading Halts | `GET /halts/active` | Pull | 5-10 sec |
| Resumed Halts | `GET /halts/resumed?hours=2` | Pull | 5-10 sec |
| Real-time Stream | `WS /stream/scanner?profile=FAST_MOVERS` | Push | Continuous |

### 2.2 Future Bots (Template)

Any new bot MUST implement:

```
┌─────────────────────────────────────────────────────────────────┐
│  REQUIRED INTEGRATIONS                                          │
├─────────────────────────────────────────────────────────────────┤
│  1. Scanner Row Consumer      → GET /scanner/rows               │
│  2. Symbol Context Fetcher    → GET /scanner/symbol/{sym}       │
│  3. Halt Monitor              → GET /halts/active               │
│  4. Resume Watcher            → GET /halts/resumed              │
│  5. Health Checker            → GET /health                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. CODE PATHS TO REMOVE OR DISABLE

### Current Codebase (MAX_AI Repo)

**NO CODE PATHS TO REMOVE** - All scanning is already centralized in MAX_AI_SCANNER.

| File | Status | Action |
|------|--------|--------|
| `scanner_service/ingest/finviz_client.py` | ✅ Correct Location | KEEP - Central Finviz source |
| `scanner_service/ingest/halt_tracker.py` | ✅ Correct Location | KEEP - Central halt source |
| `scanner_service/ingest/schwab_client.py` | ✅ Correct Location | KEEP - Central quote source |

### External Bots (When Integrated)

Any external bot (Morpheus_AI, etc.) MUST have these removed:

```
❌ REMOVE FROM EXTERNAL BOTS:
────────────────────────────────────────────────────────
[ ] Any import of finvizfinance
[ ] Any import of feedparser (RSS)
[ ] Any import of yahoo finance clients
[ ] Any "screener" or "scanner" module
[ ] Any code fetching from:
    - finviz.com
    - nasdaqtrader.com
    - nyse.com/trade-halt-current
    - finance.yahoo.com
[ ] Any scheduled task polling for "top gainers"
[ ] Any scheduled task polling for "halts"
[ ] Any direct Schwab quote polling for discovery
────────────────────────────────────────────────────────
```

---

## 4. INTEGRATION PATCH PLAN

### Phase 1: Bot Discovery Audit

```bash
# For each external bot, search for forbidden patterns:
grep -r "finviz" /path/to/bot/
grep -r "yahoo" /path/to/bot/
grep -r "nasdaqtrader" /path/to/bot/
grep -r "screener" /path/to/bot/
grep -r "scanner" /path/to/bot/
grep -r "top.*gainer" /path/to/bot/
grep -r "halt.*rss" /path/to/bot/
```

### Phase 2: Create Scanner Client Module

Each bot should have a dedicated scanner client:

```python
# bot/scanner_client.py
"""MAX_AI Scanner Client - Single source of market discovery."""

import httpx
from typing import Optional
from dataclasses import dataclass

SCANNER_BASE_URL = "http://127.0.0.1:8787"

@dataclass
class ScannerRow:
    """Scanner row from MAX_AI_SCANNER."""
    rank: int
    symbol: str
    price: float
    change_pct: float
    volume: int
    ai_score: float
    tags: list[str]
    velocity_1m: Optional[float] = None
    rvol_proxy: Optional[float] = None
    hod_distance_pct: Optional[float] = None
    spread: Optional[float] = None
    halt_status: Optional[str] = None

class ScannerClient:
    """Client for MAX_AI_SCANNER API."""

    def __init__(self, base_url: str = SCANNER_BASE_URL, timeout: float = 5.0):
        self.base_url = base_url
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=self.timeout)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def get_rows(
        self,
        profile: str = "FAST_MOVERS",
        limit: int = 25
    ) -> list[ScannerRow]:
        """Fetch ranked scanner rows."""
        resp = await self._client.get(
            f"{self.base_url}/scanner/rows",
            params={"profile": profile, "limit": limit}
        )
        resp.raise_for_status()
        data = resp.json()
        return [ScannerRow(**row) for row in data.get("rows", [])]

    async def get_symbol(self, symbol: str) -> dict:
        """Fetch context for a specific symbol."""
        resp = await self._client.get(
            f"{self.base_url}/scanner/symbol/{symbol.upper()}"
        )
        resp.raise_for_status()
        return resp.json()

    async def get_active_halts(self) -> list[dict]:
        """Fetch currently halted stocks."""
        resp = await self._client.get(f"{self.base_url}/halts/active")
        resp.raise_for_status()
        return resp.json().get("halts", [])

    async def get_resumed_halts(self, hours: int = 2) -> list[dict]:
        """Fetch recently resumed halts."""
        resp = await self._client.get(
            f"{self.base_url}/halts/resumed",
            params={"hours": hours}
        )
        resp.raise_for_status()
        return resp.json().get("halts", [])

    async def health_check(self) -> bool:
        """Check if scanner is healthy."""
        try:
            resp = await self._client.get(f"{self.base_url}/health")
            return resp.status_code == 200
        except Exception:
            return False
```

### Phase 3: Replace Discovery Logic

```python
# BEFORE (FORBIDDEN):
async def find_opportunities():
    from finvizfinance.screener.overview import Overview
    screener = Overview()
    screener.set_filter(filters_dict={"Change": "Up 5%"})
    return screener.screener_view()

# AFTER (REQUIRED):
async def find_opportunities():
    async with ScannerClient() as scanner:
        return await scanner.get_rows(profile="FAST_MOVERS", limit=25)
```

### Phase 4: Implement Safe Mode

```python
class TradingBot:
    """Bot with proper scanner integration."""

    def __init__(self):
        self.scanner = ScannerClient()
        self.safe_mode = False

    async def run(self):
        async with self.scanner:
            while True:
                # Check scanner health
                if not await self.scanner.health_check():
                    self.safe_mode = True
                    logger.warning("Scanner down - entering safe mode")
                    await asyncio.sleep(10)
                    continue

                self.safe_mode = False

                # Get opportunities (ONLY from scanner)
                rows = await self.scanner.get_rows("FAST_MOVERS")

                for row in rows:
                    await self.evaluate_and_trade(row)

                await asyncio.sleep(2)

    async def evaluate_and_trade(self, row: ScannerRow):
        """Bot-local strategy evaluation."""
        # Get full context from scanner
        context = await self.scanner.get_symbol(row.symbol)

        # Apply strategy-specific gating (BOT RESPONSIBILITY)
        if not self.passes_strategy_filter(row, context):
            return

        # Check risk limits (BOT RESPONSIBILITY)
        if not self.check_risk_limits(row):
            return

        # Execute via Schwab (BOT RESPONSIBILITY)
        await self.execute_trade(row.symbol, context)
```

---

## 5. API REFERENCE (AUTHORITATIVE)

### Base URL
```
http://127.0.0.1:8787
```

### Scanner Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/scanner/rows` | GET | Get ranked rows for a profile |
| `/scanner/symbol/{symbol}` | GET | Get symbol context |
| `/stream/scanner` | WS | Real-time scanner stream |

### Halt Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/halts` | GET | All halts (active + resumed) |
| `/halts/active` | GET | Currently halted stocks |
| `/halts/resumed` | GET | Recently resumed (hours param) |

### Health Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Service health check |
| `/metrics` | GET | Scanner metrics |

### Response Schema (Scanner Rows)

```json
{
  "profile": "FAST_MOVERS",
  "rows": [
    {
      "rank": 1,
      "symbol": "XYZ",
      "price": 12.50,
      "change_pct": 15.2,
      "volume": 5000000,
      "ai_score": 87.3,
      "tags": ["GAPPER", "HALT_RESUMED"],
      "velocity_1m": 4.2,
      "rvol_proxy": 9.1,
      "hod_distance_pct": 2.1,
      "spread": 0.03,
      "float_shares": 5.2,
      "market_cap": 125.0
    }
  ],
  "total_candidates": 150,
  "scan_time_ms": 45
}
```

---

## 6. SHARED SCHEMA CONTRACT

### Authoritative Fields (DO NOT CHANGE)

| Field | Type | Description |
|-------|------|-------------|
| `pct_change` | float | % change from previous close |
| `gap_pct` | float | Gap % from previous close |
| `velocity_1m` | float | 1-minute price velocity |
| `rvol_proxy` | float | Relative volume estimate |
| `hod_distance_pct` | float | Distance from high of day |
| `spread` | float | Bid-ask spread |
| `ai_score` | float | Rule-based AI score (0-100) |
| `halt_status` | string | HALTED, RESUMED, or null |

### Adding New Fields

If a bot needs a field not in the schema:
1. Open issue on MAX_AI_SCANNER repo
2. Add field to scanner first
3. Update this spec
4. Bot consumes new field

**NEVER add derived fields in bots.**

---

## 7. FAILURE HANDLING

### Scanner Down Protocol

```python
if not scanner.health_check():
    # REQUIRED ACTIONS:
    1. Enter safe idle mode
    2. Cancel pending orders (optional)
    3. Log "SCANNER_DOWN" event
    4. Retry health check every 10 seconds

    # FORBIDDEN ACTIONS:
    ❌ Revert to scraping
    ❌ Self-scan markets
    ❌ Use cached stale data (>30 sec old)
    ❌ Continue trading blind
```

---

## 8. TESTING REQUIREMENTS

Each bot MUST include:

```python
# tests/test_scanner_integration.py

import pytest
from unittest.mock import AsyncMock, patch

MOCK_SCANNER_RESPONSE = {
    "profile": "FAST_MOVERS",
    "rows": [
        {"rank": 1, "symbol": "TEST", "price": 10.0, "change_pct": 5.0,
         "volume": 1000000, "ai_score": 75.0, "tags": ["GAPPER"]}
    ],
    "total_candidates": 1
}

@pytest.mark.asyncio
async def test_scanner_client_parses_rows():
    """Verify bot correctly parses scanner response."""
    with patch("httpx.AsyncClient.get") as mock_get:
        mock_get.return_value.json.return_value = MOCK_SCANNER_RESPONSE
        mock_get.return_value.status_code = 200

        async with ScannerClient() as client:
            rows = await client.get_rows("FAST_MOVERS")

        assert len(rows) == 1
        assert rows[0].symbol == "TEST"
        assert rows[0].ai_score == 75.0

@pytest.mark.asyncio
async def test_no_fallback_scanners():
    """Verify bot has no fallback discovery logic."""
    # This test should FAIL if any fallback exists
    import bot  # Your bot module

    # Check for forbidden imports
    forbidden = ["finvizfinance", "feedparser", "yfinance"]
    for module in forbidden:
        assert module not in dir(bot), f"Found forbidden import: {module}"

@pytest.mark.asyncio
async def test_safe_mode_on_scanner_down():
    """Verify bot enters safe mode when scanner is down."""
    with patch("httpx.AsyncClient.get", side_effect=Exception("Connection refused")):
        async with ScannerClient() as client:
            healthy = await client.health_check()

        assert healthy is False
        # Bot should now be in safe mode
```

---

## 9. CONFIRMATION CHECKLIST

### MAX_AI_SCANNER is the ONLY Discovery Source

- [x] Finviz scraping centralized in `scanner_service/ingest/finviz_client.py`
- [x] Halt tracking centralized in `scanner_service/ingest/halt_tracker.py`
- [x] Schwab polling centralized in `scanner_service/ingest/schwab_client.py`
- [x] No duplicate scanners in MAX_AI repo
- [x] API endpoints exposed for bot consumption
- [x] WebSocket streaming available for real-time updates

### Bot Responsibilities (Confirmed Separation)

| Responsibility | Owner |
|---------------|-------|
| Discovery | MAX_AI_SCANNER |
| Ranking | MAX_AI_SCANNER |
| Momentum features | MAX_AI_SCANNER |
| Halt tracking | MAX_AI_SCANNER |
| Audio alerts | MAX_AI_SCANNER |
| Strategy gating | BOT |
| Risk limits | BOT |
| Trade execution | BOT (via Schwab) |
| Position tracking | BOT |
| P&L tracking | BOT |

---

## 10. CANONICAL DATA FLOW (LOCKED)

```
┌───────────────────────────────────────────────────────────────────────┐
│                        EXTERNAL DISCOVERY                              │
│                  Finviz / NASDAQ Halts / News RSS                      │
└───────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌───────────────────────────────────────────────────────────────────────┐
│                        MAX_AI_SCANNER                                  │
│              (context + ranking + alerts + streaming)                  │
│                                                                        │
│   • Schwab Client (quotes)                                             │
│   • Finviz Client (float data)                                         │
│   • Halt Tracker (RSS + manual)                                        │
│   • Feature Engine (velocity, rvol, HOD distance)                      │
│   • Scorer/Ranker (AI scoring)                                         │
│   • Alert Router (audio + events)                                      │
└───────────────────────────────────────────────────────────────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
              ┌──────────┐  ┌──────────┐  ┌──────────┐
              │ REST API │  │WebSocket │  │Dashboard │
              │  /scan*  │  │ /stream  │  │   UI     │
              └──────────┘  └──────────┘  └──────────┘
                    │              │
                    ▼              ▼
┌───────────────────────────────────────────────────────────────────────┐
│                         MORPHEUS_AI                                    │
│                  (strategy + gating + execution)                       │
│                                                                        │
│   • Scanner Client (pulls from MAX_AI_SCANNER)                         │
│   • Strategy Engine (bot-local logic)                                  │
│   • Risk Manager (bot-local limits)                                    │
│   • Order Router (Schwab execution)                                    │
└───────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌───────────────────────────────────────────────────────────────────────┐
│                       SCHWAB EXECUTION                                 │
│                    (orders + positions)                                │
└───────────────────────────────────────────────────────────────────────┘
```

---

## SUCCESS CRITERIA

Integration is **COMPLETE** when:

- [x] All scanning logic centralized in MAX_AI_SCANNER
- [ ] Morpheus_AI consumes scanner data (external project - pending)
- [x] No duplicate scanners exist in MAX_AI repo
- [x] Halts sourced from MAX_AI_SCANNER only
- [x] Strategy logic remains bot-local (spec defined)
- [x] Execution remains Schwab-local (spec defined)
- [x] Integration spec documented (this file)
- [ ] Bot scanner client module created (pending bot repo)
- [ ] Integration tests added (pending bot repo)

---

*Generated: 2026-01-24*
*Integration Phase: Documentation Complete*
