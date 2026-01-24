# MAX_AI Scanner Service

Real-time stock scanner service for trading, powered by Schwab/thinkorswim market data.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                      MAX_AI Scanner Service                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐          │
│  │   Schwab     │───▶│   Universe   │───▶│   Feature    │          │
│  │   Client     │    │   Manager    │    │   Engine     │          │
│  └──────────────┘    └──────────────┘    └──────────────┘          │
│         │                   │                   │                   │
│         ▼                   ▼                   ▼                   │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐          │
│  │    Quote     │    │   Profile    │    │   Scorer /   │          │
│  │    Cache     │    │   Loader     │    │   Ranker     │          │
│  └──────────────┘    └──────────────┘    └──────────────┘          │
│                             │                   │                   │
│                             ▼                   ▼                   │
│                      ┌──────────────┐    ┌──────────────┐          │
│                      │    Alert     │    │   Scanner    │          │
│                      │    Router    │    │    State     │          │
│                      └──────────────┘    └──────────────┘          │
│                             │                   │                   │
│                             ▼                   ▼                   │
│                      ┌─────────────────────────────────┐           │
│                      │         FastAPI Server          │           │
│                      │    (REST + WebSocket APIs)      │           │
│                      └─────────────────────────────────┘           │
│                                    │                                │
└────────────────────────────────────┼────────────────────────────────┘
                                     │
                    ┌────────────────┼────────────────┐
                    │                │                │
                    ▼                ▼                ▼
             ┌──────────┐    ┌──────────┐    ┌──────────────┐
             │Morpheus  │    │ai_project│    │  Dashboard   │
             │   AI     │    │   hub    │    │   (future)   │
             └──────────┘    └──────────┘    └──────────────┘
```

## Features

- **Real-time Scanning**: Poll Schwab API for live quotes at configurable intervals
- **Universe Narrowing**: Intelligent filtering to focus on active stocks
- **Strategy Profiles**: YAML-based, hot-reloadable scanning strategies
- **AI Scoring**: Rule-based scoring (ML coming in v0.2)
- **Audio Alerts**: Non-blocking audio notifications for significant events
- **REST API**: Full API for integration with other services
- **WebSocket Streaming**: Real-time updates for dashboards

## Project Structure

```
C:\Max_AI
│
├── scanner_service/
│   ├── __init__.py
│   ├── app.py              # FastAPI application
│   ├── settings.py         # Configuration
│   │
│   ├── schemas/            # Pydantic models
│   │   ├── market_snapshot.py
│   │   ├── profile.py
│   │   └── events.py
│   │
│   ├── ingest/             # Data ingestion
│   │   ├── schwab_client.py
│   │   └── universe.py
│   │
│   ├── features/           # Feature computation
│   │   ├── rolling.py
│   │   └── feature_engine.py
│   │
│   ├── strategy/           # Scoring & ranking
│   │   ├── profile_loader.py
│   │   ├── scorer.py
│   │   └── ranker.py
│   │
│   ├── alerts/             # Alert system
│   │   ├── router.py
│   │   ├── audio.py
│   │   └── sounds/
│   │
│   ├── storage/            # State & caching
│   │   ├── state.py
│   │   └── cache.py
│   │
│   └── config/
│       └── profiles/       # Strategy YAML files
│           ├── FAST_MOVERS.yaml
│           ├── GAPPERS.yaml
│           ├── HOD_BREAK.yaml
│           └── TOP_GAINERS.yaml
│
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

## Getting Started

### Prerequisites

- Python 3.11+
- Schwab Developer Account (for API access)

### Installation

1. Clone the repository:
```bash
git clone https://github.com/bgmaynard/Max_AI.git
cd Max_AI
```

2. Create virtual environment:
```bash
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/Mac
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Configure environment:
```bash
copy .env.example .env
# Edit .env with your Schwab credentials
```

5. Add sound files (optional):
Place `.wav` files in `scanner_service/alerts/sounds/`:
- `hod_break.wav`
- `momo_surge.wav`
- `gap_alert.wav`
- `news.wav`
- `risk.wav`

### Running the Service

```bash
python -m scanner_service.app
```

The service starts at `http://127.0.0.1:8787`

## API Reference

### Health & Metrics

```bash
# Health check
curl http://127.0.0.1:8787/health

# Get metrics
curl http://127.0.0.1:8787/metrics
```

### Profiles

```bash
# List all profiles
curl http://127.0.0.1:8787/profiles

# Get specific profile
curl http://127.0.0.1:8787/profiles/FAST_MOVERS

# Create new profile
curl -X POST http://127.0.0.1:8787/profiles \
  -H "Content-Type: application/json" \
  -d '{
    "name": "MY_PROFILE",
    "description": "Custom scanner profile",
    "conditions": [
      {"field": "change_pct", "operator": "gte", "value": 3.0}
    ]
  }'

# Reload profile from disk
curl -X POST http://127.0.0.1:8787/profiles/FAST_MOVERS/reload
```

### Scanner Output

```bash
# Get scanner rows
curl "http://127.0.0.1:8787/scanner/rows?profile=FAST_MOVERS&limit=20"

# Get data for specific symbol
curl http://127.0.0.1:8787/scanner/symbol/AAPL
```

### Alerts

```bash
# Get recent alerts
curl http://127.0.0.1:8787/alerts/recent?limit=50

# Test alert (triggers sound)
curl -X POST http://127.0.0.1:8787/alerts/test \
  -H "Content-Type: application/json" \
  -d '{"alert_type": "HOD_BREAK", "symbol": "TEST"}'
```

### WebSocket Streaming

```javascript
// JavaScript example
const ws = new WebSocket('ws://127.0.0.1:8787/stream/scanner?profile=FAST_MOVERS');

ws.onmessage = (event) => {
  const data = JSON.parse(event.data);
  console.log('Scanner update:', data);
};
```

## Morpheus_AI Integration

Morpheus_AI can pull scanner data via HTTP:

```python
import httpx

async def get_scanner_rows(profile: str = "FAST_MOVERS", limit: int = 20):
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "http://127.0.0.1:8787/scanner/rows",
            params={"profile": profile, "limit": limit}
        )
        return response.json()

# Get top movers
rows = await get_scanner_rows("FAST_MOVERS")
for row in rows["rows"]:
    print(f"{row['rank']}. {row['symbol']}: {row['change_pct']:.1f}% (AI: {row['ai_score']:.2f})")
```

## Adding a New Strategy Profile

1. Create a YAML file in `scanner_service/config/profiles/`:

```yaml
name: MY_STRATEGY
description: Description of what this strategy finds

enabled: true

# Filter conditions (all must pass)
conditions:
  - field: change_pct
    operator: gte
    value: 2.0
  - field: rvol
    operator: gte
    value: 1.5

# Scoring weights (higher = more important)
weights:
  change_pct: 1.0
  velocity: 1.5
  rvol: 2.0
  hod_proximity: 1.0
  spread: 0.5
  volume: 0.5

# Filters
min_price: 1.0
max_price: 500.0
min_volume: 100000

# Alert configuration
alert_enabled: true
alert_sound: momo_surge.wav
alert_threshold: 0.70
```

2. Reload profiles (or restart service):
```bash
curl -X POST http://127.0.0.1:8787/profiles/MY_STRATEGY/reload
```

## Alert System

### Alert Types

| Type | Sound | Trigger |
|------|-------|---------|
| `HOD_BREAK` | hod_break.wav | Near/at high of day with momentum |
| `GAP_ALERT` | gap_alert.wav | Significant gap from previous close |
| `MOMO_SURGE` | momo_surge.wav | High velocity + volume surge |
| `NEWS` | news.wav | News catalyst (future) |
| `RISK` | risk.wav | Risk warning (future) |

### Alert Cooldowns

- Default: 60 seconds per symbol/profile combination
- Configurable via `ALERT_COOLDOWN_SEC` in `.env`

### Testing Alerts

```bash
# Test HOD break sound
curl -X POST http://127.0.0.1:8787/alerts/test \
  -d '{"alert_type": "HOD_BREAK"}'

# Test all sounds
curl -X POST http://127.0.0.1:8787/alerts/test -d '{"alert_type": "GAP_ALERT"}'
curl -X POST http://127.0.0.1:8787/alerts/test -d '{"alert_type": "MOMO_SURGE"}'
```

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SCHWAB_CLIENT_ID` | - | Schwab API client ID |
| `SCHWAB_CLIENT_SECRET` | - | Schwab API client secret |
| `SCHWAB_REDIRECT_URI` | - | OAuth redirect URI |
| `SCHWAB_TOKEN_PATH` | `C:\Max_AI\tokens\schwab_token.json` | Token storage path |
| `SCANNER_HOST` | `127.0.0.1` | Service host |
| `SCANNER_PORT` | `8787` | Service port |
| `SCAN_INTERVAL_MS` | `1500` | Scan interval in milliseconds |
| `MAX_WATCH_SYMBOLS` | `300` | Maximum symbols to track |
| `ALERT_COOLDOWN_SEC` | `60` | Alert cooldown period |

## Development

### Running Tests

```bash
pytest tests/ -v
```

### Type Checking

```bash
mypy scanner_service/
```

### Formatting

```bash
black scanner_service/
isort scanner_service/
```

## Version History

### v0.1.0 (Current)

- Initial release
- Core scanner functionality
- REST API + WebSocket streaming
- 4 default strategy profiles
- Rule-based AI scoring
- Audio alerts

### v0.2.0 (Planned)

- ML model training
- Schwab WebSocket streaming
- Full-market discovery
- News NLP integration

## License

Internal use only.

---

Built for the MAX_AI trading stack.
