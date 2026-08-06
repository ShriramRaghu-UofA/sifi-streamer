"""Typed messages exchanged with the acquisition worker."""

from dataclasses import dataclass
from pathlib import Path

from sifi_streamer.capture import Attributes
from sifi_streamer.devices import Modalities


@dataclass(frozen=True, slots=True)
class StartCapture:
    capture_file: Path
    capture_id: str
    attributes: Attributes


@dataclass(frozen=True, slots=True)
class StopCapture:
    reason: str = "normal_completion"


@dataclass(frozen=True, slots=True)
class StartSegment:
    segment_id: str
    segment_kind: str
    attributes: Attributes


@dataclass(frozen=True, slots=True)
class StopSegment:
    segment_id: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AddMarker:
    marker_id: str
    marker_kind: str
    attributes: Attributes
    source_time_ns: int | None = None
    source_clock: str | None = None


@dataclass(frozen=True, slots=True)
class Shutdown:
    pass


type CommandMessage = (
    StartCapture | StopCapture | StartSegment | StopSegment | AddMarker | Shutdown
)


@dataclass(frozen=True, slots=True)
class ModalityInfo:
    shm_name: str
    n_samples: int
    n_channels: int
    channels: tuple[str, ...]
    sample_rate: int
    payload_dtype: str = "<f4"

    def samples_for_seconds(self, seconds: float) -> int:
        return max(round(seconds * self.sample_rate), 1)


@dataclass(frozen=True, slots=True)
class Ready:
    modalities: Modalities[ModalityInfo]
    device_info: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class CaptureStarted:
    capture_file: Path


@dataclass(frozen=True, slots=True)
class CaptureStopped:
    reason: str


@dataclass(frozen=True, slots=True)
class SegmentStarted:
    segment_id: str


@dataclass(frozen=True, slots=True)
class SegmentStopped:
    segment_id: str


@dataclass(frozen=True, slots=True)
class MarkerAdded:
    marker_id: str


@dataclass(frozen=True, slots=True)
class ErrorAck:
    message: str


type AckMessage = (
    Ready
    | CaptureStarted
    | CaptureStopped
    | SegmentStarted
    | SegmentStopped
    | MarkerAdded
    | ErrorAck
)
