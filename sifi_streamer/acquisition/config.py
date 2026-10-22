"""Immutable acquisition and bounded recorder policies."""

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StreamerConfig:
    """Settings for device workers, live history, and recorder admission."""

    ring_buffer_seconds: float = 10.0
    ack_timeout_s: float = 2.0
    startup_timeout_s: float = 30.0
    shutdown_timeout_s: float = 10.0
    commit_interval_s: float = 1.0
    transaction_target_bytes: int = 8 << 20
    producer_buffer_bytes: int = 32 << 20
    max_submission_bytes: int = 8 << 20
    enqueue_timeout_s: float = 2.0

    def __post_init__(self) -> None:
        for name in (
            "ring_buffer_seconds",
            "ack_timeout_s",
            "startup_timeout_s",
            "shutdown_timeout_s",
            "commit_interval_s",
            "enqueue_timeout_s",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in (
            "transaction_target_bytes",
            "producer_buffer_bytes",
            "max_submission_bytes",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_submission_bytes > self.producer_buffer_bytes:
            raise ValueError("max_submission_bytes exceeds producer_buffer_bytes")
