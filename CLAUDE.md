# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Max_AI is a pull-based advisory scanner service for a multi-bot trading system. It discovers symbols via news, momentum scanning, and market analysis, then serves advisories to downstream bots via REST API and WebSocket.

**Entry Point:** `scanner_service/app.py` (FastAPI, port 8787)
**Language:** Python 3.11+, FastAPI

## Commands

```bash
# Start scanner
python -m scanner_service.app

# Health check
curl http://localhost:8787/health

# Query advisories
curl "http://localhost:8787/advisories?min_confidence=0.5"

# Run tests
pytest tests/ -v

# Type checking / formatting
mypy scanner_service/
black scanner_service/
isort scanner_service/
```

## Architecture — Data Pipeline

```
INGESTION → UNIVERSE → QUOTE CACHE → FEATURE ENGINE → SCORER → RANKER → ADVISORY BUFFER → API
```

### Ingestion Sources (scanner_service/ingest/)

| Source | File | Phase | Interval | What It Does |
|--------|------|-------|----------|-------------|
| **Schwab API** | `schwab_client.py` | All | Every scan (1.5s) | Real-time quotes (price, volume, bid/ask, high/low) + fundamentals (float, market cap) |
| **TradingView** | `tradingview_client.py` | PREMARKET + OPEN | 5 min | Premarket gappers (>=5%, $1-$20) and intraday small-cap movers (>=10%) |
| **Webull** | `webull_client.py` | PREMARKET | 5 min | Public API (no auth), top premarket gainers (>=5%, $2-$20) |
| **News RSS** | `news_client.py` | All | 30s | 6 static feeds (Benzinga, Seeking Alpha, GlobeNewsWire, SEC EDGAR) + dynamic Yahoo RSS built from universe symbols |
| **Finviz** | `finviz_client.py` | OPEN | On-demand | Float data, top gainers screener ($0-$20, >=10% change) |

### Scan Loop (app.py `run_scan_cycle()`)

Every 1.5s:
1. Fetch from external sources (TV/Webull/Finviz with 5min cooldowns, phase-gated)
2. Get universe candidates, fetch quotes from Schwab (cached 1.5s TTL)
3. Enrich with fundamentals (float, market cap) every 5min
4. Build `MarketSnapshot`, narrow universe by activity
5. `FeatureEngine.compute_batch_features()` — 24 features per symbol including rolling window (20 obs)
6. `Scorer.score_batch()` per enabled profile — filter conditions + weighted scoring
7. `Ranker.rank()` — sort by AI score, top 50 per profile
8. Emit advisories for qualifying symbols → push to WebSocket
9. Check negative intelligence (extended moves, low follow-through, volume concerns)
10. Update watchlist classifier (A/B/C tiers)

### AI Score v0.2 — Acceleration-Based Detection

Primary signals (detect momentum *building*, not existing):
- `velocity_accel` (0-0.25) — rate of velocity change between scans
- `momentum_slope` (0-0.20) — linear trend of last 5 momentum scores
- `rvol_cross_up` (0-0.15) — volume crossing 2.0x threshold
- `change_accel` (0-0.10) — rate of price change acceleration

Confirmation signals (reduced weight): velocity, hod_proximity, spread, change_pct

### Advisory Buffer (advisory_buffer.py)

- **In-memory only** — no persistence across restarts
- **Dedup cooldown:** 120s per symbol (not per source)
- **Rediscovery gate:** Re-emits if price extends >=10pp or volume 2x original
- **Confidence decay:** -40% linear over TTL, floor at 0.20
- **Phase-aware thresholds:**
  - PREMARKET: min_score=0.30, TTL=600s (aggressive accumulation)
  - OPEN: min_score=0.50, TTL=300s (standard)
- **Negative advisories (DO_NOT_TRADE):** Extended move (>50% change), low follow-through (>15% change but <0.30 score), volume concern (>10% change but <1.0x rvol)

### Strategy Profiles (scanner_service/config/profiles/*.yaml)

6 profiles: FAST_MOVERS, TOP_GAINERS, PENNY_STOCKS, GAPPERS, HOD_BREAK, ALL_MOVERS. Each defines:
- `conditions` — AND-logic filters (field/operator/value)
- `weights` — scoring component weights (change_pct, velocity, rvol, hod_proximity, spread, volume)
- `min_price`/`max_price`/`min_volume` — universe gates
- `alert_enabled`/`alert_sound`/`alert_threshold` — audio alert config

Profiles are hot-reloadable: `POST /profiles/{name}/reload`

## Key Endpoints

```
GET  /health                           # Health check
GET  /advisories                       # Active advisories (params: min_confidence, limit)
GET  /advisories/negative              # DO_NOT_TRADE symbols
GET  /advisories/{symbol}              # Single symbol advisory
POST /advisories/refresh               # Force advisory refresh
GET  /scanner/rows?profile=X&limit=N   # Raw scanner rows
GET  /scanner/symbol/{symbol}          # Single symbol data
GET  /profiles                         # List all profiles
POST /profiles/{name}/reload           # Hot-reload profile YAML
GET  /metrics                          # Scanner metrics
GET  /webull/status                    # Webull source status
POST /webull/fetch-now                 # Force Webull fetch
WS   /stream/scanner?profile=X        # Real-time scanner updates
WS   /stream/advisories               # Real-time advisory push (init payload + live)
```

Deprecated stubs (return `{"status": "deprecated"}`): `/morpheus/inject`, `/morpheus/auto-inject/*`, `/news/push-to-morpheus`

## Key Modules

| Module | Purpose |
|--------|---------|
| `app.py` | FastAPI monolith (~54K): all routes, scan loop, token reload, advisory emission, watchlist |
| `advisory_buffer.py` | Advisory accumulation, dedup, decay, negative signals |
| `settings.py` | Pydantic settings from .env (host, port, scan interval, paths) |
| `ingest/universe.py` | 300+ seed symbols, dynamic narrowing by activity |
| `features/feature_engine.py` | Batch feature computation, acceleration signals |
| `features/rolling.py` | Rolling window (20 obs) for velocity, volatility, momentum, HOD breaks |
| `strategy/scorer.py` | Profile condition filtering + weighted AI scoring |
| `strategy/ranker.py` | Sort by AI score, produce top-50 ScannerRow objects per profile |
| `storage/state.py` | Scanner status tracking (STOPPED/STARTING/RUNNING/PAUSED/ERROR), metrics |
| `storage/cache.py` | TTL cache (1.5s) with LRU eviction (500 symbols), hit/miss counting |
| `schemas/` | Pydantic models: Quote, MarketSnapshot, Profile, AlertEvent, ScannerRow/Output |

## Important Constraints

- **Schwab token is READ-ONLY.** IBKR_Algo_BOT_V2 is the sole writer. Max_AI reloads from `SCHWAB_TOKEN_PATH` every 60s via `token_reload_loop()`. Never call the refresh endpoint.
- **All times are Eastern (ET)** via `ZoneInfo("America/New_York")`.
- **Market phases:** PREMARKET (4:00-9:30 ET), OPEN (9:30-16:00 ET), CLOSED.
- **app.py is a monolith** — routes, scan loop, emission logic, watchlist are all in one file.
- **20 consecutive scan failures** triggers process exit (expects PM2/supervisor restart).

---

## Bot Ecosystem — Cross-Bot Context

This bot is part of a multi-bot trading system. All bots share a Schwab account and coordinate via APIs and shared files.

### All Bots

| Bot | Path | Port | Role |
|-----|------|------|------|
| **Morpheus_AI** | `C:\Morpheus\Morpheus_AI` | 8020 | Signal generation, risk management, paper execution |
| **Morpheus_UI** | `C:\Morpheus\Morpheus_UI` | — | Trading desktop frontend (Electron/React/TypeScript) |
| **IBKR_Algo_BOT_V2** | `C:\ai_project_hub\store\code\IBKR_Algo_BOT_V2` | 9100 | Primary execution bot, owns Schwab token refresh |
| **Max_AI** | `C:\Max_AI` | 8787 | Advisory scanner (this bot) |
| **AI_SUPERVISOR** | `C:\AI_SUPERVISOR` | 9001 | Orchestration, oversight, cross-bot variance |

### Inter-Bot Communication (this bot's links)

- **Morpheus_AI -> Max_AI**: `GET /advisories?min_confidence=0.5` + WebSocket `/stream/advisories`. Primary: WS with auto-reconnect. Fallback: HTTP poll (300s when WS up, 60s when WS down).
- **IBKR_V2 -> Max_AI**: `GET /advisories?min_confidence=0.5` + `GET /advisories/negative` every 60s. Optional — continues if Max down.
- **This bot does NOT call** Morpheus_AI, IBKR_V2, or AI_SUPERVISOR. Purely pull-based.

### Shared Resources

- **Schwab Token:** `C:\ai_project_hub\store\code\IBKR_Algo_BOT_V2\tokens\schwab_token.json` (IBKR_V2 = sole writer, Max_AI = read-only)
- **External Data:** `D:\AI_BOT_DATA\` (Databento cache, replays, validation logs)
- **Master CLAUDE.md:** `C:\Morpheus\CLAUDE.md` (~60KB, full system context)

### Boot Order (via `C:\AI_SUPERVISOR\scripts\safe_start.ps1`)

1. Morpheus (AI + UI) -> health check on port 8020
2. OAuth token verify
3. Max_AI scanner -> port 8787, 10s init wait
4. Supervisor + Control API + ngrok -> port 9001
