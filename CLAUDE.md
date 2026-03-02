# Max_AI Scanner Service - Claude Context

## Overview
Max_AI is a pull-based advisory scanner service for the multi-bot trading system. It discovers symbols via news, momentum scanning, and market analysis, then serves advisories to downstream bots via REST API.

**Entry Point:** `scanner_service/app.py` (FastAPI, port 8787)
**Language:** Python 3.11+, FastAPI

## Directory Structure
```
C:\Max_AI\
├── .env                               # Schwab credentials (READ-ONLY token)
├── README.md
├── requirements.txt
├── scanner_service\                   # Main package
│   ├── app.py                         # FastAPI entry (~54K, all routes)
│   ├── advisory_buffer.py             # Advisory accumulation
│   ├── settings.py                    # Configuration
│   ├── alerts\                        # Alert handlers
│   ├── client\
│   │   └── scanner_client.py          # External scanner client
│   ├── config\
│   │   └── profiles\                  # Scanner profiles
│   ├── features\                      # Feature extraction
│   ├── ingest\                        # Data ingestion
│   ├── schemas\                       # API schemas
│   ├── static\                        # Static assets
│   ├── storage\                       # Data persistence
│   └── strategy\
│       ├── profile_loader.py
│       ├── ranker.py
│       └── scorer.py
├── docs\
│   ├── BOT_INTEGRATION_SPEC.md
│   └── session_updates\
└── tokens\
    └── schwab_token.json              # READ-ONLY shared token
```

## Key Endpoints
```
GET  /health                    # Health check
GET  /advisories                # Active advisories (params: min_confidence, limit)
GET  /advisories/negative       # DO_NOT_TRADE symbols
GET  /advisories/{symbol}       # Single symbol advisory
POST /advisories/refresh        # Force advisory refresh
```

## Commands
```bash
cd C:\Max_AI
python -m scanner_service.app          # Start scanner (port 8787)

# Test
curl http://localhost:8787/health
curl "http://localhost:8787/advisories?min_confidence=0.5"
```

---

## Bot Ecosystem — Cross-Bot Context

This bot is part of a multi-bot trading system. All bots share a Schwab account and coordinate via APIs and shared files.

### All Bots

| Bot | Path | Entry Point | Port | Role |
|-----|------|-------------|------|------|
| **Morpheus_AI** | `C:\Morpheus\Morpheus_AI` | `python -m morpheus.server.main` | 8020 | Signal generation, risk management, paper execution (Python/FastAPI) |
| **Morpheus_UI** | `C:\Morpheus\Morpheus_UI` | `npm run dev` | — | Trading desktop frontend (Electron/React/TypeScript) |
| **IBKR_Algo_BOT_V2** | `C:\ai_project_hub\store\code\IBKR_Algo_BOT_V2` | `feed_server.py` | 9100 | Primary execution bot, owns Schwab token refresh (Python/FastAPI, 200+ modules) |
| **IBKR_Algo_BOT** | `C:\ai_project_hub\store\code\IBKR_Algo_BOT` | `bot_entry.py` | — | Legacy V1, dormant |
| **Max_AI** | `C:\Max_AI` | `scanner_service/app.py` | 8787 | Advisory scanner — symbol discovery, news, momentum (Python/FastAPI) |
| **AI_SUPERVISOR** | `C:\AI_SUPERVISOR` | `api/control.py` | 9001 | Orchestration, oversight, cross-bot variance, voice dashboard (Python/FastAPI+PowerShell) |
| **STG_AI_Trader** | `C:\STG_AI_Trader` | monolith .py | — | Prototype, dormant |
| **ai_project_hub** | `C:\ai_project_hub` | `orchestrator.py` | — | Shared infrastructure, backups, AI mesh |

### Shared Resources

- **Schwab Token:** `C:\ai_project_hub\store\code\IBKR_Algo_BOT_V2\tokens\schwab_token.json`
  - **IBKR_Algo_BOT_V2** = SOLE WRITER (refreshes every ~25 min)
  - **Morpheus_AI** + **Max_AI** = READ-ONLY (reload from disk, never call refresh endpoint)
- **External Data Root:** `D:\AI_BOT_DATA\` — Databento cache, replays, validation logs, momentum logs
- **Master CLAUDE.md:** `C:\Morpheus\CLAUDE.md` (~60KB, full system context with directory trees for every bot)

### Key Directory Structures

**IBKR_Algo_BOT_V2** (`C:\ai_project_hub\store\code\IBKR_Algo_BOT_V2\`):
- `ai/` — 200+ modules: momentum_engine, trading_engine, trading_pipeline, chronos_predictor, signal_gating_engine, ignition_funnel, central_gating, circuit_breaker, regime_classifier, position_controller, strategies/, orchestrator/, ats/, fsm/, indicators/
- `config/` — broker_config.py, warrior_config.json
- `core/` — time_authority.py (broker-corrected ET time)
- `scanners/` — gainer, gap, hod scanners + coordinator
- `startup/` — start_all_bots.ps1, start_morpheus.ps1, start_max_ai.ps1
- `tokens/schwab_token.json` — shared token (this bot is sole writer)
- `store/` — state files, trade_journal.db, watchlists, models
- `tools/` — ledger_loader, replay_simulator, token tools

**Morpheus_AI** (`C:\Morpheus\Morpheus_AI\`):
- `morpheus/server/main.py` — FastAPI monolith (port 8020)
- `morpheus/` subdirs: ai/, broker/, charting/, classify/, core/, data/, evolve/, execution/, features/, integrations/, observer/, orchestrator/, persistence/, regime/, reporting/, risk/, scanner/, scoring/, server/, services/, spine/, strategies/, structure/, supervise/, worklist/
- `data/runtime_config.json` — hot-reloadable config
- `scripts/` — replay, analysis, validation tools
- `tests/` — 20+ test files
- `reports/YYYY-MM-DD/` — daily trade data

**AI_SUPERVISOR** (`C:\AI_SUPERVISOR\`):
- `api/control.py` — FastAPI (port 9001), `api/dashboard.html` — voice UI
- `scripts/safe_start.ps1` — daily boot script (starts all bots in order)
- `variance/` — cross-bot EOD comparison (compare_eod.py, runner.py)
- `bus/` — message bus (inbox/outbox/archive)
- `bots.json` — bot registry

**ai_project_hub** (`C:\ai_project_hub\`):
- `store/code/IBKR_Algo_BOT_V2/` — active execution bot
- `store/code/IBKR_Algo_BOT/` — legacy V1
- `store/ai_shared/` — trainer audit, validator reports
- `backups/` — dated IBKR_V2 snapshots (2024-12 through 2026-01)

**D:\AI_BOT_DATA\**:
- `databento_cache/XNAS.ITCH/` — raw .dbn files
- `logs/` — live validation, startup logs
- `replays/` — enriched replay JSONL
- `reports/` — validation summaries, trading rules

### Boot Order (via `C:\AI_SUPERVISOR\scripts\safe_start.ps1`)
1. **Morpheus** (AI + UI) → health check on port 8020
2. **OAuth token verify** → pipeline responding, reload daemon active
3. **Max_AI scanner** → port 8787, 10s init wait
4. **Supervisor + Control API + ngrok** → port 9001 + tunnel URL

### Inter-Bot Communication (this bot's links)
- **Morpheus_AI → Max_AI**: `GET http://localhost:8787/advisories?min_confidence=0.5` every 60s (`morpheus/services/max_advisory_poller.py`). Also `GET /health` at boot. Optional — Morpheus continues if Max down.
- **IBKR_V2 → Max_AI**: `GET http://localhost:8787/advisories?min_confidence=0.5` + `GET /advisories/negative` every 60s (`ai/max_advisory_poller.py`). Optional.
- **Max_AI → Schwab token**: Reads `SCHWAB_TOKEN_PATH` from `.env` every 60s (`token_reload_loop()` in `app.py`) + on 401. IBKR_V2 is sole writer.
- **This bot does NOT**: Call Morpheus_AI, IBKR_V2, or AI_SUPERVISOR. Purely pull-based — bots poll this service.
- **Deprecated stubs**: `/morpheus/inject`, `/morpheus/auto-inject/*`, `/news/push-to-morpheus` return `{"status": "deprecated"}`.
- **AI_SUPERVISOR**: Does not call Max_AI (not in bots.json).
- **Full inter-comm map**: See `C:\Morpheus\CLAUDE.md` Bot Ecosystem section.
- All bots use Eastern Time (ET) via TimeAuthority or ZoneInfo fallback
