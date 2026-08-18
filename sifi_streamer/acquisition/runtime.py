"""Foreground monitoring over one entered acquisition handle."""

from dataclasses import dataclass

from sifi_streamer.acquisition.handle import BackgroundHandle
from sifi_streamer.acquisition.health import (
    HealthEvaluator,
    HealthEvent,
    HealthSnapshot,
    HealthThresholds,
    WorkerFatal,
)
from sifi_streamer.acquisition.ipc import StreamInfo
from sifi_streamer.acquisition.reader import SignalWindow
from sifi_streamer.capture.controller import CaptureController


class AcquisitionMonitor:
    """Read-only live data and evaluated health for one capture backend."""

    def __init__(
        self,
        handle: BackgroundHandle,
        thresholds: HealthThresholds | None = None,
    ) -> None:
        self._handle = handle
        self._evaluator = HealthEvaluator(thresholds)
        self._latest: HealthSnapshot | None = None
        self._streams_cache: tuple[StreamInfo, ...] = ()

    @property
    def thresholds(self) -> HealthThresholds:
        return self._evaluator.thresholds

    @property
    def streams(self) -> tuple[StreamInfo, ...]:
        if not self._streams_cache:
            self._streams_cache = self._handle.streams
        return self._streams_cache

    @property
    def events(self) -> tuple[HealthEvent, ...]:
        return self._evaluator.events

    def update_thresholds(self, thresholds: HealthThresholds) -> None:
        self._evaluator.update_thresholds(thresholds)

    def latest(self) -> HealthSnapshot | None:
        """Drain and evaluate the newest worker snapshot."""
        if (raw := self._handle.poll_health()) is not None:
            self._latest = self._evaluator.evaluate(raw)
        return self._latest

    def fatal(self) -> WorkerFatal | None:
        return self._handle.poll_fatal()

    def read_since(
        self, stream_id: str, cursor: int, *, max_samples: int | None = None
    ) -> SignalWindow | None:
        try:
            reader = self._handle.stream_readers[stream_id]
        except KeyError as exc:
            raise LookupError(f"unknown stream {stream_id!r}") from exc
        return reader.read_since(cursor, max_samples=max_samples)


@dataclass(frozen=True, slots=True)
class CaptureRuntime:
    """Composition bundle used by monitored launchers."""

    controller: CaptureController
    monitor: AcquisitionMonitor
