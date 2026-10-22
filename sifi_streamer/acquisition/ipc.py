"""Immutable device-worker declarations and lifecycle acknowledgements."""

from dataclasses import dataclass, field
from multiprocessing.synchronize import Semaphore
from pathlib import Path

from sifi_streamer.acquisition.devices import SignalStreamSpec
from sifi_streamer.capture.records import Attributes, Packet


@dataclass(slots=True)
class ProcessSignal:
    """One-shot OS semaphore notification, cached independently by each reader.

    Startup releases one token per worker. Shutdown uses one token for its one
    worker. No worker-owned mutex is needed to notify a killed process.
    """

    semaphore: Semaphore
    readers: int = 1
    observed: bool = False

    def set(self) -> None:
        if not self.observed:
            for _ in range(self.readers):
                self.semaphore.release()
            self.observed = True

    def is_set(self) -> bool:
        if not self.observed and self.semaphore.acquire(block=False):
            self.observed = True
        return self.observed

    def wait(self, timeout: float) -> bool:
        if not self.observed and self.semaphore.acquire(timeout=timeout):
            self.observed = True
        return self.observed


@dataclass(frozen=True, slots=True)
class CaptureConfiguration:
    """Capture identity and settings prepared before any worker starts."""

    capture_file: Path
    capture_id: str
    attributes: Attributes
    configuration: Packet = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StreamInfo:
    """Shared-memory and display metadata for one device-scoped live stream."""

    stream_id: str
    shm_name: str
    n_samples: int
    channels: tuple[str, ...]
    nominal_rate_hz: float
    payload_dtype: str = "<f4"
    label: str | None = None
    channel_labels: tuple[str | None, ...] = ()
    channel_units: tuple[str | None, ...] = ()
    device_id: str = "device"
    local_stream_id: str = ""
    source_clock: str = "producer-defined"
    timestamp_unit: str = "seconds"

    @property
    def n_channels(self) -> int:
        return len(self.channels)

    def samples_for_seconds(self, seconds: float) -> int:
        return max(round(seconds * self.nominal_rate_hz), 1)


@dataclass(frozen=True, slots=True)
class DeviceInfoUpdate:
    """Latest report snapshot, independently ordered for each device."""

    revision: int
    info: dict[str, object]
    device_id: str = "device"


@dataclass(frozen=True, slots=True)
class Ready:
    """Connected device declaration; polling waits for the global start gate."""

    streams: tuple[StreamInfo, ...]
    device_info: dict[str, object] = field(default_factory=dict)
    device_info_revision: int = 0
    registry: tuple[SignalStreamSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkerStopped:
    """All final submissions and device cleanup precede this acknowledgement."""

    failure: str | None = None
    device_info: dict[str, object] = field(default_factory=dict)
    device_info_revision: int = 0
    producer_completed: bool = False


@dataclass(frozen=True, slots=True)
class ErrorAck:
    """Connected device startup failed; the coordinator tears down the capture."""

    message: str
