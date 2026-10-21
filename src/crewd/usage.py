"""Measured session usage and resume-safe per-attempt deltas."""
from __future__ import annotations

from dataclasses import dataclass, field
import math


@dataclass(frozen=True)
class ModelUsage:
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_nano_aiu: int | None


@dataclass(frozen=True)
class UsageSnapshot:
    total_nano_aiu: int | None
    premium_request_cost: float
    models: dict[str, ModelUsage]


@dataclass
class AttemptUsage:
    before: UsageSnapshot | None = None
    after: UsageSnapshot | None = None
    delta_nano_aiu: int | None = None
    complete: bool = False
    settled: bool = True
    calls: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def reconcile(self) -> None:
        self.delta_nano_aiu = None
        self.complete = False
        if self.before is None or self.after is None:
            return
        start, end = self.before.total_nano_aiu, self.after.total_nano_aiu
        if start is None or end is None:
            return
        if end < start:
            self.errors.append("Session usage decreased; delta cannot be reconciled")
            return
        self.delta_nano_aiu = end - start
        self.complete = self.settled and not self.errors


def _nano_units(value: int | float | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not math.isfinite(value) or value < 0 or int(value) != value:
        raise ValueError("Invalid measured nano-AI usage")
    return int(value)


def snapshot_from_metrics(metrics) -> UsageSnapshot:
    """Copy only accounting fields, never session content or credentials."""
    return UsageSnapshot(
        total_nano_aiu=_nano_units(metrics.total_nano_aiu),
        premium_request_cost=metrics.total_premium_request_cost,
        models={
            name: ModelUsage(
                calls=model.requests.count,
                input_tokens=model.usage.input_tokens,
                output_tokens=model.usage.output_tokens,
                cache_read_tokens=model.usage.cache_read_tokens,
                cache_write_tokens=model.usage.cache_write_tokens,
                total_nano_aiu=_nano_units(model.total_nano_aiu),
            )
            for name, model in metrics.model_metrics.items()
        },
    )
