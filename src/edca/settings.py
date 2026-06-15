"""Runtime configuration. Constructed at the edge (entrypoints), never in core."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    control_center_url: str
    model: str
    request_timeout_s: float

    @staticmethod
    def from_env() -> "Settings":
        url = os.environ.get("CONTROL_CENTER_URL")
        if not url:
            raise RuntimeError("CONTROL_CENTER_URL is required")  # fail loud
        return Settings(
            control_center_url=url,
            model=os.environ.get("EDCA_MODEL", "anthropic/claude-opus-4-8"),
            request_timeout_s=float(os.environ.get("EDCA_TIMEOUT_S", "30")),
        )
