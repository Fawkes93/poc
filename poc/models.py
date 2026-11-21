"""The decision the model returns."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AlertLevel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    price: float
    type: Literal["support", "resistance", "custom"] = "custom"


class ManageItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    position_id: str
    action: Literal["leave", "close", "move_sl_to_breakeven", "cancel"]
    new_sl: float | None = None
    new_tp: float | None = None


class Decision(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["none", "market", "pending"]
    side: Literal["buy", "sell"] | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    volume: float | None = None
    confidence: float = Field(ge=0, le=100)
    reasoning: str = ""
    alert_levels: list[AlertLevel] = Field(default_factory=list)
    manage_existing: list[ManageItem] = Field(default_factory=list)

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence_scale(cls, value: object) -> object:
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return value
        if 0 < number <= 1:
            return round(number * 100, 2)
        return number
