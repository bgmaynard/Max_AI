"""MAX_AI Scanner Service - FastAPI Application."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
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
from scanner_service.features.feature_engine import FeatureEngine
from scanner_service.strategy.profile_loader import ProfileLoader
from scanner_service.strategy.scorer import Scorer
from scanner_service.strategy.ranker import Ranker
from scanner_service.alerts.router import AlertRouter
from scanner_service.storage.state import ScannerState, ScannerStatus
from scanner_service.storage.cache import QuoteCache

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

# Scanner loop task
scanner_task: Optional[asyncio.Task] = None

# WebSocket connections for streaming
websocket_connections: dict[str, list[WebSocket]] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    global schwab_client, universe, feature_engine, profile_loader
    global scorer, ranker, alert_router, scanner_state, quote_cache, scanner_task

    logger.info("Starting MAX_AI Scanner Service...")

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

    # Start scanner loop
    scanner_state.status = ScannerStatus.STARTING
    scanner_task = asyncio.create_task(scanner_loop())

    logger.info(f"Scanner service started on {settings.scanner_host}:{settings.scanner_port}")

    yield

    # Shutdown
    logger.info("Shutting down scanner service...")
    scanner_state.status = ScannerStatus.STOPPED

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
    """Main scanner loop."""
    global scanner_state

    scanner_state.status = ScannerStatus.RUNNING
    interval = settings.scan_interval_ms / 1000

    while scanner_state.is_running:
        try:
            await run_scan_cycle()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Scan cycle error: {e}")
            scanner_state.record_error(e)

        await asyncio.sleep(interval)


async def run_scan_cycle():
    """Execute a single scan cycle."""
    # Get symbols to scan
    symbols = universe.candidates

    # Check cache for recent quotes
    cached, missing = quote_cache.get_many(symbols)

    # Fetch missing quotes from Schwab
    if missing:
        snapshot = await schwab_client.get_snapshot(missing)
        quote_cache.set_many(snapshot.quotes)
        cached.update(snapshot.quotes)

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
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "MAX_AI Scanner",
        "version": "0.1.0",
        "scanner_status": scanner_state.status.value if scanner_state else "unknown",
        "timestamp": datetime.utcnow().isoformat(),
    }


@app.get("/metrics")
async def metrics():
    """Get scanner metrics."""
    if not scanner_state:
        raise HTTPException(status_code=503, detail="Scanner not initialized")
    return scanner_state.get_metrics()


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
