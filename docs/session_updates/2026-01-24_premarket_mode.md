# Session Update - January 24, 2026 (Premarket Mode)

## Summary
Implemented Premarket Mode + Strategy Gating for Monday testing.

## Trading Windows (Eastern Time)

| Mode | Time Window | active_trading | observe_only |
|------|-------------|----------------|--------------|
| PREMARKET | 07:00-09:30 ET | `True` | `False` |
| RTH | 09:30-16:00 ET | `False` | `True` |
| OFFHOURS | All other times | `False` | `True` |

## Files Changed

### A) Market Mode Module (NEW)
**`morpheus/core/market_mode.py`**
- `MarketMode` dataclass with `name`, `active_trading`, `observe_only`
- `get_market_mode()` - returns current mode based on ET time
- `get_time_et()` - returns current ET timestamp for logging
- Helper functions: `is_premarket()`, `is_rth()`, `is_trading_allowed()`

### B) Strategy Base Class
**`morpheus/strategies/base.py`**
- Added `SignalMode` enum: `ACTIVE` | `OBSERVED`
- Added `allowed_market_modes` property to Strategy (default: `{"RTH"}`)
- Added `market_mode` field to `StrategyContext`
- Added to `SignalCandidate`:
  - `market_mode: str` - "PREMARKET", "RTH", "OFFHOURS"
  - `signal_mode: SignalMode` - ACTIVE or OBSERVED
  - `time_et: str` - ET timestamp for audit
- Updated `can_evaluate()` to filter by market mode
- Updated `to_event()` to emit `SIGNAL_OBSERVED` for observed signals

### C) Signal Pipeline
**`morpheus/orchestrator/pipeline.py`**
- Stage 0 added: Get market mode at pipeline start
- Market mode injected into `StrategyContext`
- Strategies filtered by market mode before running
- `_process_signal()` handles OBSERVED signals differently:
  - Logs the signal
  - Skips scoring/gate/risk for observed signals
- `get_status()` now includes:
  - `market_mode`
  - `active_trading`
  - `observe_only`
  - `time_et`
  - `strategies_for_current_mode`

### D) Events
**`morpheus/core/events.py`**
- Added `SIGNAL_OBSERVED` event type

## Strategy Gating Policy for Monday

### RTH-Only Strategies (default)
- `FirstPullbackStrategy` - RTH only
- `HighOfDayContinuationStrategy` - RTH only
- All mean-reversion strategies - RTH only

### PREMARKET Observer Strategy (NEW)
**`morpheus/strategies/premarket_observer.py`**
- `PremarketStructureObserver` - PREMARKET only, OBSERVE-only
- Classifies symbol behavior without generating actionable signals
- Structure classifications:
  - `GAP_UP_HOLDING` - Gapped up and holding above gap
  - `GAP_UP_FADING` - Gapped up but fading back
  - `GAP_DOWN_HOLDING` - Gapped down and holding below
  - `GAP_DOWN_RECOVERING` - Gapped down but recovering
  - `FLAT_CONSOLIDATING` - No significant gap, consolidating
  - `HIGH_VOLATILITY` - Erratic premarket action
- Emits `SIGNAL_OBSERVED` events with classification tags
- Tags include: `structure:*`, `gap:*`, `volume:*`, `scanner:*`

**Result during PREMARKET (07:00-09:30 ET):**
- Pipeline runs and computes features
- Regime detection works
- `PremarketStructureObserver` eligible and runs
- Emits OBSERVED signals with structure classifications
- NO actionable signals - all are observe-only

## Signal Mode Behavior

| Condition | signal_mode | Event Type | Processed |
|-----------|-------------|------------|-----------|
| PREMARKET + active_trading=True | ACTIVE | SIGNAL_CANDIDATE | Full pipeline |
| RTH + observe_only=True | OBSERVED | SIGNAL_OBSERVED | Logged only |
| OFFHOURS + observe_only=True | OBSERVED | SIGNAL_OBSERVED | Logged only |

## API Status Endpoint

`GET /api/pipeline/status` now returns:
```json
{
  "market_mode": "PREMARKET",
  "active_trading": true,
  "observe_only": false,
  "time_et": "2026-01-24T07:30:00-05:00",
  "strategies_for_current_mode": [],
  "registered_strategies": ["FirstPullback", "HODContinuation", ...]
}
```

## Event Payload Fields (Mandatory for Audit)

Every signal event includes:
```json
{
  "market_mode": "PREMARKET",
  "signal_mode": "ACTIVE",
  "time_et": "2026-01-24T07:30:00-05:00"
}
```

## Monday Testing Verification

To verify correct operation:

1. **07:00-09:30 ET (PREMARKET):**
   - `GET /api/pipeline/status` → `market_mode: "PREMARKET"`, `active_trading: true`
   - `strategies_for_current_mode: []` (empty - no strategies allowed)
   - No signals generated (all strategies filtered)

2. **09:30-16:00 ET (RTH):**
   - `GET /api/pipeline/status` → `market_mode: "RTH"`, `observe_only: true`
   - `strategies_for_current_mode` → list of all strategies
   - Signals emitted as `SIGNAL_OBSERVED`
   - Signals logged but not processed through gate/risk

3. **After 16:00 ET (OFFHOURS):**
   - `GET /api/pipeline/status` → `market_mode: "OFFHOURS"`, `observe_only: true`
   - Minimal activity

## Future: Adding Premarket Strategies

To add a premarket-safe strategy later:
```python
class PremarketGapStrategy(Strategy):
    @property
    def allowed_market_modes(self) -> frozenset[str]:
        return frozenset({"PREMARKET"})  # Only runs in premarket
```

Or for strategies that work in both:
```python
@property
def allowed_market_modes(self) -> frozenset[str]:
    return frozenset({"PREMARKET", "RTH"})
```
