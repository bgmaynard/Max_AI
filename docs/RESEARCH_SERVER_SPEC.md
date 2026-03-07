# Research Server Build Spec — For Claude Code on RESEARCH1

## Goal

Build a lightweight FastAPI research server that provides **sector intelligence** to the Max_AI scanner. Max_AI's `research_client.py` will poll this server for sector classifications and heatmap data.

**This server runs on RESEARCH1 (192.168.1.154) on port 9200.**

---

## API Contract (MUST match exactly)

Max_AI expects these two endpoints:

### 1. `GET /api/sector/heatmap`

Returns sector heat scores. Max_AI polls this every 60s.

**Response format:**
```json
{
  "Technology": {"heat_score": 0.75},
  "Healthcare": {"heat_score": 0.55},
  "Energy": {"heat_score": 0.40},
  "Cannabis": {"heat_score": 0.20},
  "Financial": {"heat_score": 0.30}
}
```

- Keys = sector names (string)
- `heat_score` = float 0.0 to 1.0
- Heat thresholds used by Max_AI: HOT >= 0.70, WARM >= 0.50, COOL >= 0.30, COLD < 0.30
- Include ALL active sectors, not just hot ones

### 2. `GET /api/sector/symbol/{SYMBOL}`

Returns sector classification for a single stock symbol (uppercase).

**Response format:**
```json
{
  "symbol": "AAPL",
  "sector": "Technology",
  "asset_type": "stock",
  "cap_bucket": "large"
}
```

- `sector` — must match sector names used in heatmap (case-sensitive)
- `asset_type` — "stock", "etf", "adr", or "unknown"
- `cap_bucket` — "micro" (<300M), "small" (300M-2B), "mid" (2B-10B), "large" (>10B), or "unknown"
- If symbol not found, still return 200 with `"sector": "unknown"`

### 3. `GET /health` (recommended)

```json
{
  "status": "ok",
  "service": "research-server",
  "symbols_cached": 1500,
  "sectors_tracked": 12,
  "last_update": "2026-03-07T15:30:00"
}
```

---

## Data Sources for Sector Classification

Use **any** of these approaches (in order of preference):

### Option A: Finviz scraping (simplest)
- `https://finviz.com/quote.ashx?t={SYMBOL}` — parse sector from page
- Cache results indefinitely (sectors don't change)
- Rate limit: 1 req/sec to avoid blocking

### Option B: Yahoo Finance API
- `https://query1.finance.yahoo.com/v10/finance/quoteSummary/{SYMBOL}?modules=assetProfile`
- Returns `assetProfile.sector` and `assetProfile.industry`
- Free, no auth needed

### Option C: Static sector mapping + API fallback
- Maintain a CSV/JSON of known symbol->sector mappings
- Fall back to Yahoo/Finviz for unknown symbols
- Best for speed — most scanner symbols are repeat visitors

---

## Heatmap Calculation

The heatmap score represents "how hot is this sector right now." Calculate from:

1. **Count active movers per sector** — how many stocks in this sector are moving >3% today
2. **Average gain in sector** — mean change_pct of movers
3. **Volume surge** — average relative volume of sector movers

Simple formula:
```python
raw = (mover_count / max_movers) * 0.4 + (avg_gain / max_gain) * 0.35 + (avg_rvol / max_rvol) * 0.25
heat_score = min(1.0, raw)
```

Data source options:
- Poll Max_AI `GET http://{TRADING_PC}:8787/scanner/rows?profile=ALL_MOVERS&limit=200` to get current movers, group by sector
- Or independently scrape Finviz screener for today's gainers
- Refresh every 30-60 seconds during market hours (9:30-16:00 ET)

---

## Implementation Requirements

### Stack
- **Python 3.11+** with **FastAPI** + **uvicorn**
- `httpx` for async HTTP calls
- No database required — in-memory caching is fine (sectors are stable)

### Startup
```bash
pip install fastapi uvicorn httpx beautifulsoup4 lxml
uvicorn research_server:app --host 0.0.0.0 --port 9200
```

### File structure (keep it simple)
```
research_server/
  research_server.py    # Single-file FastAPI app
  requirements.txt      # fastapi, uvicorn, httpx, beautifulsoup4, lxml
```

### Key behaviors
- **Bind to 0.0.0.0:9200** so it's accessible from the network (not just localhost)
- **Cache symbol sectors in memory** — only fetch from source on first lookup
- **Pre-warm cache on startup** — optionally load common symbols (top 500 by volume)
- **Heatmap refresh loop** — background task every 60s during market hours
- **Timeout gracefully** — if a data source is slow, return cached/default data
- **CORS enabled** — allow all origins (internal network only)

### Market hours awareness
- Market hours: 9:30 AM - 4:00 PM Eastern (America/New_York)
- During off-hours: serve cached heatmap, still respond to symbol lookups
- Premarket (4:00-9:30 ET): can optionally run heatmap at reduced frequency

---

## Max_AI Client Behavior (for reference)

This is how Max_AI consumes the data — helps you understand what matters:

- **Heatmap**: Fetched every 60s, cached. Sectors not in heatmap get default heat_score=0.30
- **Symbol lookup**: Fetched in parallel batches (up to 10 concurrent). Cached for entire session. ~50-200 symbols per cycle.
- **Timeout**: Max_AI gives 5 seconds per request. If you're slower, it falls back to defaults.
- **Failure handling**: If server is down, Max_AI marks it unavailable and retries every 30s. No crash, no errors — just defaults to "unknown" sector and 0.30 heat.

---

## Test it works

From the trading PC (where Max_AI runs):
```bash
# Health
curl http://RESEARCH1:9200/health

# Heatmap
curl http://RESEARCH1:9200/api/sector/heatmap

# Symbol lookup
curl http://RESEARCH1:9200/api/sector/symbol/AAPL
curl http://RESEARCH1:9200/api/sector/symbol/TSLA
curl http://RESEARCH1:9200/api/sector/symbol/FAKESYM
```

Expected: AAPL returns Technology, TSLA returns Consumer Cyclical or Automotive, FAKESYM returns "unknown".

---

## Environment

- **Server hostname**: RESEARCH1 (192.168.1.154)
- **Port**: 9200
- **Consumer**: Max_AI scanner at `http://{TRADING_PC}:8787`
- **Network**: Local LAN, no auth required
- **OS**: Check with `uname -a` or `ver`
