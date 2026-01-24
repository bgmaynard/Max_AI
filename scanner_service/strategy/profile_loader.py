"""Profile loading and management."""

import logging
from pathlib import Path
from typing import Optional

import yaml

from scanner_service.settings import get_settings
from scanner_service.schemas.profile import Profile, ProfileCondition, ProfileWeights

logger = logging.getLogger(__name__)


class ProfileLoader:
    """
    Loads and manages strategy profiles from YAML files.

    Profiles are hot-reloadable at runtime without service restart.
    """

    def __init__(self):
        self.settings = get_settings()
        self._profiles: dict[str, Profile] = {}
        self._load_all()

    def _load_all(self) -> None:
        """Load all profiles from the profiles directory."""
        profiles_dir = self.settings.profiles_dir
        if not profiles_dir.exists():
            logger.warning(f"Profiles directory not found: {profiles_dir}")
            profiles_dir.mkdir(parents=True, exist_ok=True)
            return

        for yaml_file in profiles_dir.glob("*.yaml"):
            try:
                profile = self._load_profile(yaml_file)
                if profile:
                    self._profiles[profile.name] = profile
                    logger.info(f"Loaded profile: {profile.name}")
            except Exception as e:
                logger.error(f"Failed to load profile {yaml_file}: {e}")

        logger.info(f"Loaded {len(self._profiles)} profiles")

    def _load_profile(self, path: Path) -> Optional[Profile]:
        """Load a single profile from YAML."""
        with open(path, "r") as f:
            data = yaml.safe_load(f)

        if not data:
            return None

        # Parse conditions
        conditions = []
        for cond_data in data.get("conditions", []):
            conditions.append(ProfileCondition(**cond_data))

        # Parse weights
        weights_data = data.get("weights", {})
        weights = ProfileWeights(**weights_data)

        return Profile(
            name=data.get("name", path.stem),
            description=data.get("description", ""),
            enabled=data.get("enabled", True),
            conditions=conditions,
            weights=weights,
            min_price=data.get("min_price", 1.0),
            max_price=data.get("max_price", 500.0),
            min_volume=data.get("min_volume", 100000),
            alert_enabled=data.get("alert_enabled", True),
            alert_sound=data.get("alert_sound"),
            alert_threshold=data.get("alert_threshold", 0.7),
        )

    def get(self, name: str) -> Optional[Profile]:
        """Get a profile by name."""
        return self._profiles.get(name)

    def get_all(self) -> list[Profile]:
        """Get all loaded profiles."""
        return list(self._profiles.values())

    def get_enabled(self) -> list[Profile]:
        """Get all enabled profiles."""
        return [p for p in self._profiles.values() if p.enabled]

    def names(self) -> list[str]:
        """Get all profile names."""
        return list(self._profiles.keys())

    def reload(self, name: Optional[str] = None) -> None:
        """Reload profiles from disk."""
        if name:
            # Reload specific profile
            profile_path = self.settings.profiles_dir / f"{name}.yaml"
            if profile_path.exists():
                profile = self._load_profile(profile_path)
                if profile:
                    self._profiles[name] = profile
                    logger.info(f"Reloaded profile: {name}")
        else:
            # Reload all
            self._profiles.clear()
            self._load_all()

    def save(self, profile: Profile) -> None:
        """Save a profile to disk."""
        profile_path = self.settings.profiles_dir / f"{profile.name}.yaml"

        # Convert to dict for YAML
        data = {
            "name": profile.name,
            "description": profile.description,
            "enabled": profile.enabled,
            "conditions": [
                {"field": c.field, "operator": c.operator, "value": c.value}
                for c in profile.conditions
            ],
            "weights": profile.weights.model_dump(),
            "min_price": profile.min_price,
            "max_price": profile.max_price,
            "min_volume": profile.min_volume,
            "alert_enabled": profile.alert_enabled,
            "alert_sound": profile.alert_sound,
            "alert_threshold": profile.alert_threshold,
        }

        with open(profile_path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        # Update in-memory
        self._profiles[profile.name] = profile
        logger.info(f"Saved profile: {profile.name}")

    def create(self, profile: Profile) -> None:
        """Create a new profile."""
        if profile.name in self._profiles:
            raise ValueError(f"Profile already exists: {profile.name}")
        self.save(profile)

    def delete(self, name: str) -> bool:
        """Delete a profile."""
        if name not in self._profiles:
            return False

        profile_path = self.settings.profiles_dir / f"{name}.yaml"
        if profile_path.exists():
            profile_path.unlink()

        del self._profiles[name]
        logger.info(f"Deleted profile: {name}")
        return True
