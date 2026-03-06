"""
Scanner Health Monitor
========================
Periodic health check that logs scanner status to logs/scanner_health.log.

Tracks:
  - scan_latency (time per scan cycle)
  - symbols_scanned (count per cycle)
  - news_feed_status (per-source health)
  - advisory buffer stats
  - error rate
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Optional

# Health-specific logger with file handler
health_logger = logging.getLogger("scanner_health")
_log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
os.makedirs(_log_dir, exist_ok=True)
_handler = RotatingFileHandler(
    os.path.join(_log_dir, "scanner_health.log"),
    maxBytes=5_000_000,
    backupCount=3,
)
_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
health_logger.addHandler(_handler)
health_logger.setLevel(logging.INFO)

logger = logging.getLogger(__name__)


class HealthMonitor:
    """
    Periodically samples scanner health metrics and writes to log file.
    """

    def __init__(self, check_interval: int = 60):
        self._check_interval = check_interval
        self._running = False
        self._check_count = 0

        # Latency tracking (rolling window)
        self._scan_latencies: list[float] = []  # ms
        self._max_latency_window = 100

    def record_scan_latency(self, latency_ms: float):
        """Called after each scan cycle to record latency."""
        self._scan_latencies.append(latency_ms)
        if len(self._scan_latencies) > self._max_latency_window:
            self._scan_latencies = self._scan_latencies[-self._max_latency_window:]

    def _get_latency_stats(self) -> dict:
        if not self._scan_latencies:
            return {"avg_ms": 0, "max_ms": 0, "min_ms": 0, "p95_ms": 0, "samples": 0}

        sorted_lat = sorted(self._scan_latencies)
        n = len(sorted_lat)
        p95_idx = int(n * 0.95)

        return {
            "avg_ms": round(sum(sorted_lat) / n, 1),
            "max_ms": round(sorted_lat[-1], 1),
            "min_ms": round(sorted_lat[0], 1),
            "p95_ms": round(sorted_lat[min(p95_idx, n - 1)], 1),
            "samples": n,
        }

    async def _check(self):
        """Run a single health check and log results."""
        self._check_count += 1
        now = datetime.utcnow()

        # Scanner state
        scanner_ok = False
        scanner_status = "unknown"
        symbols_scanned = 0
        try:
            from scanner_service.storage.state import ScannerStatus
            from scanner_service.app import scanner_state, _last_scan_ts, _scan_error_times
            if scanner_state:
                scanner_status = scanner_state.status.value
                scanner_ok = scanner_state.status == ScannerStatus.RUNNING
                metrics = scanner_state.get_metrics()
                symbols_scanned = metrics.get("symbols_scanned", 0)
        except Exception:
            pass

        # Scan latency
        latency_stats = self._get_latency_stats()

        # Advisory buffer stats
        adv_active = 0
        adv_negative = 0
        try:
            from scanner_service.advisory_buffer import get_advisory_buffer
            buf = get_advisory_buffer()
            stats = buf.get_stats()
            adv_active = stats.get("active_advisories", 0)
            adv_negative = stats.get("negative_active", 0)
        except Exception:
            pass

        # News pipeline status
        news_sources_ok = 0
        news_sources_total = 0
        news_down = []
        try:
            from scanner_service.ingest.news_pipeline import get_news_pipeline
            pipeline = get_news_pipeline()
            ps = pipeline.get_status()
            news_sources_ok = ps.get("sources_ok", 0)
            news_sources_total = ps.get("sources_total", 0)
            for name, src in ps.get("sources", {}).items():
                if not src.get("ok", True):
                    news_down.append(name)
        except Exception:
            pass

        # Errors in last hour
        errors_last_hour = 0
        try:
            from scanner_service.app import _scan_error_times
            cutoff = now - timedelta(hours=1)
            errors_last_hour = sum(1 for t in _scan_error_times if t > cutoff)
        except Exception:
            pass

        # Schwab token health
        schwab_ok = False
        try:
            from scanner_service.app import schwab_client
            if schwab_client:
                schwab_ok = schwab_client.is_authenticated()
        except Exception:
            pass

        # Build log line
        status_emoji = "OK" if scanner_ok else "WARN"
        health_logger.info(
            f"[{status_emoji}] "
            f"scanner={scanner_status} | "
            f"symbols={symbols_scanned} | "
            f"latency_avg={latency_stats['avg_ms']}ms p95={latency_stats['p95_ms']}ms | "
            f"advisories={adv_active} negative={adv_negative} | "
            f"news={news_sources_ok}/{news_sources_total} "
            f"{'(down: ' + ','.join(news_down) + ')' if news_down else ''} | "
            f"schwab={'ok' if schwab_ok else 'FAIL'} | "
            f"errors_1h={errors_last_hour}"
        )

        # Alert on degraded state
        if not scanner_ok:
            health_logger.warning(f"[ALERT] Scanner not running: status={scanner_status}")
        if errors_last_hour > 5:
            health_logger.warning(f"[ALERT] High error rate: {errors_last_hour} errors in last hour")
        if not schwab_ok:
            health_logger.warning("[ALERT] Schwab token not authenticated")
        if news_down:
            health_logger.warning(f"[ALERT] News sources down: {', '.join(news_down)}")

    async def _monitor_loop(self):
        """Main monitoring loop."""
        health_logger.info(f"Health monitor started | interval={self._check_interval}s")
        logger.info(f"[HEALTH] Monitor started (logging to logs/scanner_health.log every {self._check_interval}s)")

        while self._running:
            try:
                await self._check()
            except Exception as e:
                health_logger.error(f"Health check error: {e}")
            await asyncio.sleep(self._check_interval)

        health_logger.info("Health monitor stopped")

    def start(self, check_interval: int = 60):
        if self._running:
            return
        self._check_interval = check_interval
        self._running = True
        asyncio.create_task(self._monitor_loop())

    def stop(self):
        self._running = False

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "check_interval": self._check_interval,
            "check_count": self._check_count,
            "latency": self._get_latency_stats(),
        }


# Singleton
_monitor: Optional[HealthMonitor] = None


def get_health_monitor() -> HealthMonitor:
    global _monitor
    if _monitor is None:
        _monitor = HealthMonitor()
    return _monitor
