"""Generic acquisition health snapshots and warning evaluation."""

import math
import time
from collections import deque
from dataclasses import dataclass, replace
from enum import StrEnum


class HealthSeverity(StrEnum):
    HEALTHY = "healthy"
    WARNING = "warning"
    FATAL = "fatal"
    WARMING_UP = "warming_up"


@dataclass(frozen=True, slots=True)
class HealthThresholds:
    window_seconds: float = 5.0
    stale_after_seconds: float | None = 2.0
    minimum_rate_ratio: float | None = 0.9
    maximum_rate_ratio: float | None = 1.1
    maximum_missing_fraction: float | None = 0.0
    maximum_lost_samples: int | None = 0

    def __post_init__(self) -> None:
        if not math.isfinite(self.window_seconds) or self.window_seconds <= 0:
            raise ValueError("window_seconds must be finite and positive")
        for name in ("stale_after_seconds",):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"{name} must be finite and positive or null")
        for name in (
            "minimum_rate_ratio",
            "maximum_rate_ratio",
            "maximum_missing_fraction",
        ):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{name} must be finite and non-negative or null")
        if self.maximum_lost_samples is not None and self.maximum_lost_samples < 0:
            raise ValueError("maximum_lost_samples must be non-negative or null")


@dataclass(frozen=True, slots=True)
class RawStreamHealth:
    stream_id: str
    nominal_rate_hz: float
    packet_count: int
    sample_count: int
    valid_value_count: int
    missing_by_channel: tuple[int, ...]
    lost_samples: int
    non_ok_packets: int
    malformed_packets: int
    misaligned_packets: int
    timestamp_errors: int
    last_packet_monotonic: float | None
    reported_rate_hz: float | None
    source_interval_sum: float
    source_interval_count: int


@dataclass(frozen=True, slots=True)
class RawHealthSnapshot:
    monotonic_time: float
    acquisition_alive: bool
    streams: tuple[RawStreamHealth, ...]


@dataclass(frozen=True, slots=True)
class StreamHealth:
    stream_id: str
    severity: HealthSeverity
    nominal_rate_hz: float
    reported_rate_hz: float | None
    observed_rate_hz: float | None
    source_rate_hz: float | None
    last_packet_age_seconds: float | None
    packet_count: int
    sample_count: int
    lost_samples: int
    missing_by_channel: tuple[int, ...]
    missing_fraction: float
    non_ok_packets: int
    malformed_packets: int
    misaligned_packets: int
    timestamp_errors: int
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    sequence: int
    monotonic_time: float
    severity: HealthSeverity
    acquisition_alive: bool
    streams: tuple[StreamHealth, ...]


@dataclass(frozen=True, slots=True)
class HealthEvent:
    sequence: int
    monotonic_time: float
    stream_id: str | None
    code: str
    active: bool
    severity: HealthSeverity
    message: str


@dataclass(frozen=True, slots=True)
class WorkerFatal:
    code: str
    message: str


@dataclass(slots=True)
class _Counters:
    stream_id: str
    nominal_rate_hz: float
    n_channels: int
    packet_count: int = 0
    sample_count: int = 0
    valid_value_count: int = 0
    missing_by_channel: tuple[int, ...] = ()
    lost_samples: int = 0
    non_ok_packets: int = 0
    malformed_packets: int = 0
    misaligned_packets: int = 0
    timestamp_errors: int = 0
    last_packet_monotonic: float | None = None
    reported_rate_hz: float | None = None
    source_interval_sum: float = 0.0
    source_interval_count: int = 0
    last_source_timestamp: float | None = None

    def __post_init__(self) -> None:
        self.missing_by_channel = (0,) * self.n_channels

    def freeze(self) -> RawStreamHealth:
        return RawStreamHealth(
            self.stream_id,
            self.nominal_rate_hz,
            self.packet_count,
            self.sample_count,
            self.valid_value_count,
            self.missing_by_channel,
            self.lost_samples,
            self.non_ok_packets,
            self.malformed_packets,
            self.misaligned_packets,
            self.timestamp_errors,
            self.last_packet_monotonic,
            self.reported_rate_hz,
            self.source_interval_sum,
            self.source_interval_count,
        )


class WorkerHealthCollector:
    """Worker-local cumulative counters; snapshots are safe to pickle."""

    def __init__(self, streams: tuple[tuple[str, float, int], ...]) -> None:
        self._streams = {
            stream_id: _Counters(stream_id, rate, channels)
            for stream_id, rate, channels in streams
        }

    def observe(
        self,
        stream_id: str,
        *,
        timestamps: list[float],
        validity: list[list[bool]],
        reported_rate_hz: float | None,
        samples_lost: int,
        status: str,
        misaligned: bool,
        now: float | None = None,
    ) -> None:
        counter = self._streams[stream_id]
        counter.packet_count += 1
        counter.sample_count += len(timestamps)
        counter.last_packet_monotonic = time.monotonic() if now is None else now
        if reported_rate_hz is not None and math.isfinite(reported_rate_hz):
            counter.reported_rate_hz = reported_rate_hz
        counter.lost_samples += max(samples_lost, 0)
        counter.non_ok_packets += status.lower() != "ok"
        counter.misaligned_packets += misaligned
        missing = list(counter.missing_by_channel)
        for row in validity:
            for index, valid in enumerate(row):
                counter.valid_value_count += bool(valid)
                missing[index] += not valid
        counter.missing_by_channel = tuple(missing)
        for timestamp in timestamps:
            if counter.last_source_timestamp is not None:
                delta = timestamp - counter.last_source_timestamp
                if delta > 0:
                    counter.source_interval_sum += delta
                    counter.source_interval_count += 1
                else:
                    counter.timestamp_errors += 1
            counter.last_source_timestamp = timestamp

    def snapshot(self, *, acquisition_alive: bool) -> RawHealthSnapshot:
        return RawHealthSnapshot(
            time.monotonic(),
            acquisition_alive,
            tuple(item.freeze() for item in self._streams.values()),
        )


class HealthEvaluator:
    """Foreground rolling-rate evaluator with warning transition history."""

    def __init__(self, thresholds: HealthThresholds | None = None) -> None:
        self.thresholds = thresholds or HealthThresholds()
        self._history: deque[RawHealthSnapshot] = deque()
        self._events: deque[HealthEvent] = deque(maxlen=1000)
        self._active: set[tuple[str | None, str]] = set()
        self._sequence = 0
        self._event_sequence = 0

    @property
    def events(self) -> tuple[HealthEvent, ...]:
        return tuple(self._events)

    def update_thresholds(self, thresholds: HealthThresholds) -> None:
        self.thresholds = thresholds

    def evaluate(self, raw: RawHealthSnapshot) -> HealthSnapshot:
        self._history.append(raw)
        cutoff = raw.monotonic_time - max(self.thresholds.window_seconds, 60.0)
        while len(self._history) > 1 and self._history[1].monotonic_time < cutoff:
            self._history.popleft()
        baseline = min(
            self._history,
            key=lambda item: abs(
                item.monotonic_time
                - (raw.monotonic_time - self.thresholds.window_seconds)
            ),
        )
        elapsed = raw.monotonic_time - baseline.monotonic_time
        previous = {item.stream_id: item for item in baseline.streams}
        first_snapshot = len(self._history) == 1
        evaluated: list[StreamHealth] = []
        current: set[tuple[str | None, str]] = set()
        for item in raw.streams:
            old = previous.get(item.stream_id, item)
            if first_snapshot:
                old = replace(
                    item,
                    packet_count=0,
                    sample_count=0,
                    valid_value_count=0,
                    missing_by_channel=(0,) * len(item.missing_by_channel),
                    lost_samples=0,
                    non_ok_packets=0,
                    malformed_packets=0,
                    misaligned_packets=0,
                    timestamp_errors=0,
                    source_interval_sum=0,
                    source_interval_count=0,
                )
            samples = item.sample_count - old.sample_count
            observed = samples / elapsed if elapsed > 0 and samples > 0 else None
            source_count = item.source_interval_count - old.source_interval_count
            source_elapsed = item.source_interval_sum - old.source_interval_sum
            source_rate = source_count / source_elapsed if source_elapsed > 0 else None
            values = max(samples * len(item.missing_by_channel), 1)
            missing = sum(item.missing_by_channel) - sum(old.missing_by_channel)
            missing_fraction = missing / values
            age = (
                raw.monotonic_time - item.last_packet_monotonic
                if item.last_packet_monotonic is not None
                else None
            )
            warnings: list[str] = []
            ratio = observed / item.nominal_rate_hz if observed is not None else None
            if self.thresholds.stale_after_seconds is not None and (
                age is None or age > self.thresholds.stale_after_seconds
            ):
                warnings.append("stale")
            if (
                ratio is not None
                and self.thresholds.minimum_rate_ratio is not None
                and ratio < self.thresholds.minimum_rate_ratio
            ):
                warnings.append("rate_low")
            if (
                ratio is not None
                and self.thresholds.maximum_rate_ratio is not None
                and ratio > self.thresholds.maximum_rate_ratio
            ):
                warnings.append("rate_high")
            if (
                self.thresholds.maximum_missing_fraction is not None
                and missing_fraction > self.thresholds.maximum_missing_fraction
            ):
                warnings.append("missing_values")
            if (
                self.thresholds.maximum_lost_samples is not None
                and item.lost_samples - old.lost_samples
                > self.thresholds.maximum_lost_samples
            ):
                warnings.append("samples_lost")
            if item.non_ok_packets > old.non_ok_packets:
                warnings.append("device_status")
            if item.misaligned_packets > old.misaligned_packets:
                warnings.append("misaligned_packet")
            if item.timestamp_errors > old.timestamp_errors:
                warnings.append("timestamp_order")
            current.update((item.stream_id, warning) for warning in warnings)
            evaluated.append(
                StreamHealth(
                    item.stream_id,
                    HealthSeverity.WARNING if warnings else HealthSeverity.HEALTHY,
                    item.nominal_rate_hz,
                    item.reported_rate_hz,
                    observed,
                    source_rate,
                    age,
                    item.packet_count,
                    item.sample_count,
                    item.lost_samples,
                    item.missing_by_channel,
                    missing_fraction,
                    item.non_ok_packets,
                    item.malformed_packets,
                    item.misaligned_packets,
                    item.timestamp_errors,
                    tuple(warnings),
                )
            )
        if not raw.acquisition_alive:
            current.add((None, "acquisition_stopped"))
        for key in sorted(current - self._active, key=str):
            self._event(raw.monotonic_time, key, True)
        for key in sorted(self._active - current, key=str):
            self._event(raw.monotonic_time, key, False)
        self._active = current
        self._sequence += 1
        severity = HealthSeverity.WARNING if current else HealthSeverity.HEALTHY
        return HealthSnapshot(
            self._sequence,
            raw.monotonic_time,
            severity,
            raw.acquisition_alive,
            tuple(evaluated),
        )

    def _event(self, when: float, key: tuple[str | None, str], active: bool) -> None:
        self._event_sequence += 1
        self._events.append(
            HealthEvent(
                self._event_sequence,
                when,
                key[0],
                key[1],
                active,
                HealthSeverity.WARNING if active else HealthSeverity.HEALTHY,
                f"{key[1].replace('_', ' ')} {'detected' if active else 'recovered'}",
            )
        )
