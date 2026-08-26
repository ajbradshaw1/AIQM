"""Value objects shared by the heater UI and PSU worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


SAFETY_COMMANDS = frozenset({"emergency_stop", "safe_shutdown"})


@dataclass(frozen=True)
class PowerSupplyCommand:
    request_id: str
    source: str
    command: str
    args: tuple[Any, ...] = field(default_factory=tuple)
    requested_at_utc: str = ""
    requested_monotonic_ns: int = 0
    priority: int = 10
    safety_epoch: int = 0

    @property
    def safety_critical(self) -> bool:
        return self.command in SAFETY_COMMANDS


@dataclass(frozen=True)
class PowerSupplyCommandResult:
    request_id: str
    source: str
    command: str
    status: str
    requested: dict[str, Any]
    effective: dict[str, Any]
    readback: dict[str, Any]
    error: str
    completed_at_utc: str
    completed_monotonic_ns: int
    confirmation_sample_sequence: int = 0
    connection_generation: int = 0
    safety_epoch: int = 0

    @property
    def confirmed(self) -> bool:
        return self.status == "CONFIRMED"
