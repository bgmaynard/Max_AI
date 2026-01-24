"""Strategy profile schemas."""

from typing import Optional, Literal
from pydantic import BaseModel, Field


class ProfileCondition(BaseModel):
    """Single filter condition for a profile."""

    field: str = Field(description="Field to evaluate (e.g., 'change_pct', 'rvol')")
    operator: Literal["gt", "gte", "lt", "lte", "eq", "neq", "between"] = Field(
        description="Comparison operator"
    )
    value: float | list[float] = Field(
        description="Threshold value (or [min, max] for 'between')"
    )

    def evaluate(self, actual_value: float) -> bool:
        """Evaluate condition against an actual value."""
        if self.operator == "gt":
            return actual_value > self.value
        elif self.operator == "gte":
            return actual_value >= self.value
        elif self.operator == "lt":
            return actual_value < self.value
        elif self.operator == "lte":
            return actual_value <= self.value
        elif self.operator == "eq":
            return actual_value == self.value
        elif self.operator == "neq":
            return actual_value != self.value
        elif self.operator == "between":
            if isinstance(self.value, list) and len(self.value) == 2:
                return self.value[0] <= actual_value <= self.value[1]
        return False


class ProfileWeights(BaseModel):
    """Scoring weights for profile ranking."""

    change_pct: float = Field(default=1.0, ge=0)
    velocity: float = Field(default=1.0, ge=0)
    rvol: float = Field(default=1.0, ge=0)
    hod_proximity: float = Field(default=1.0, ge=0)
    spread: float = Field(default=0.5, ge=0)
    volume: float = Field(default=0.5, ge=0)


class Profile(BaseModel):
    """Strategy profile defining scanner behavior."""

    name: str = Field(description="Profile identifier")
    description: str = Field(default="", description="Human-readable description")
    enabled: bool = Field(default=True)

    # Filtering
    conditions: list[ProfileCondition] = Field(
        default_factory=list,
        description="Filter conditions (all must pass)"
    )

    # Scoring weights
    weights: ProfileWeights = Field(default_factory=ProfileWeights)

    # Limits
    min_price: float = Field(default=1.0, ge=0, description="Minimum stock price")
    max_price: float = Field(default=500.0, ge=0, description="Maximum stock price")
    min_volume: int = Field(default=100000, ge=0, description="Minimum daily volume")

    # Alert settings
    alert_enabled: bool = Field(default=True)
    alert_sound: Optional[str] = Field(default=None, description="Sound file for alerts")
    alert_threshold: float = Field(
        default=0.7, ge=0, le=1,
        description="AI score threshold to trigger alert"
    )

    def matches_filters(self, features: dict) -> bool:
        """Check if features pass all conditions."""
        for condition in self.conditions:
            value = features.get(condition.field, 0)
            if not condition.evaluate(value):
                return False
        return True
