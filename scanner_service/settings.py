"""Configuration settings for MAX_AI Scanner Service."""

import os
from pathlib import Path
from functools import lru_cache
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Schwab API
    schwab_client_id: str = ""
    schwab_client_secret: str = ""
    schwab_redirect_uri: str = ""
    schwab_token_path: Path = Path("C:/Max_AI/tokens/schwab_token.json")

    # Scanner Service
    scanner_host: str = "0.0.0.0"
    scanner_port: int = 8787
    scan_interval_ms: int = 1500
    max_watch_symbols: int = 300

    # Alerts
    alert_cooldown_sec: int = 60

    # Paths
    project_root: Path = Path("C:/Max_AI")
    profiles_dir: Path = Path("C:/Max_AI/scanner_service/config/profiles")
    sounds_dir: Path = Path("C:/Max_AI/scanner_service/alerts/sounds")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
