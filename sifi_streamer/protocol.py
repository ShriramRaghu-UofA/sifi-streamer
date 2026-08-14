"""Typed commands and acknowledgements exchanged with the acquisition worker.

These frozen messages cross a multiprocessing queue boundary.  Attribute
mappings are defensively copied by the sending API before a message is queued.
"""

from dataclasses import dataclass
from pathlib import Path

from sifi_streamer.capture import Attributes


@dataclass(frozen=True, slots=True)
class StartCapture:
    """Request creation of a new authoritative capture.

    Attributes:
        capture_file: New output path, which must not already exist.
        capture_id: Identifier for the capture occurrence.
        attributes: Scalar capture metadata.
    """

    capture_file: Path
    capture_id: str
    attributes: Attributes


@dataclass(frozen=True, slots=True)
class StopCapture:
    """Request a flushed capture close with a machine-readable reason."""

    reason: str = "normal_completion"


@dataclass(frozen=True, slots=True)
class StartSegment:
    """Request an authoritative segment start boundary.

    Attributes:
        segment_id: Identifier for this segment occurrence.
        segment_kind: Stable category assigned by the consumer.
        attributes: Scalar segment metadata.
    """

    segment_id: str
    segment_kind: str
    attributes: Attributes


@dataclass(frozen=True, slots=True)
class StopSegment:
    """Request a stop boundary for an open segment.

    Attributes:
        segment_id: Identifier of the open segment.
        reason: Optional machine-readable completion reason.
    """

    segment_id: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AddMarker:
    """Request one point marker in ``marker_id, marker_kind`` order.

    Attributes:
        marker_id: Identifier for this marker occurrence.
        marker_kind: Stable category assigned by the consumer.
        attributes: Scalar marker metadata.
        source_time_ns: Optional timestamp from an external source clock.
        source_clock: Optional external clock name.
    """

    marker_id: str
    marker_kind: str
    attributes: Attributes
    source_time_ns: int | None = None
    source_clock: str | None = None


@dataclass(frozen=True, slots=True)
class Shutdown:
    """Request orderly worker shutdown and resource cleanup."""


type CommandMessage = (
    StartCapture | StopCapture | StartSegment | StopSegment | AddMarker | Shutdown
)


@dataclass(frozen=True, slots=True)
class ModalityInfo:
    """Shared-memory layout published for one enabled modality.

    Attributes:
        shm_name: Operating-system shared-memory block name.
        n_samples: Ring-buffer capacity in samples.
        n_channels: Number of columns in each sample.
        channels: Ordered channel names corresponding to matrix columns.
        sample_rate: Nominal samples per second.
        payload_dtype: NumPy dtype string for stored values.
    """

    shm_name: str
    n_samples: int
    n_channels: int
    channels: tuple[str, ...]
    sample_rate: int
    payload_dtype: str = "<f4"

    def samples_for_seconds(self, seconds: float) -> int:
        """Convert a duration to a rounded sample count, with a minimum of one."""
        return max(round(seconds * self.sample_rate), 1)


@dataclass(frozen=True, slots=True)
class StreamInfo:
    """Shared-memory and display metadata for one generic live stream."""

    stream_id: str
    shm_name: str
    n_samples: int
    channels: tuple[str, ...]
    nominal_rate_hz: float
    payload_dtype: str = "<f4"
    label: str | None = None
    channel_labels: tuple[str | None, ...] = ()
    channel_units: tuple[str | None, ...] = ()

    @property
    def n_channels(self) -> int:
        return len(self.channels)

    def samples_for_seconds(self, seconds: float) -> int:
        return max(round(seconds * self.nominal_rate_hz), 1)


@dataclass(frozen=True, slots=True)
class Ready:
    """Report successful worker startup and available live streams.

    Attributes:
        modalities: Shared-memory layout for each enabled modality.
        device_info: Optional vendor metadata reported during device connection.
    """

    streams: tuple[StreamInfo, ...]
    device_info: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class CaptureStarted:
    """Acknowledge that ``capture_file`` was created and recording began."""

    capture_file: Path


@dataclass(frozen=True, slots=True)
class CaptureStopped:
    """Acknowledge that the capture was flushed and stopped for ``reason``."""

    reason: str


@dataclass(frozen=True, slots=True)
class SegmentStarted:
    """Acknowledge the authoritative start of ``segment_id``."""

    segment_id: str


@dataclass(frozen=True, slots=True)
class SegmentStopped:
    """Acknowledge the authoritative stop of ``segment_id``."""

    segment_id: str


@dataclass(frozen=True, slots=True)
class MarkerAdded:
    """Acknowledge persistence of ``marker_id``."""

    marker_id: str


@dataclass(frozen=True, slots=True)
class ErrorAck:
    """Report that a worker command failed.

    Attributes:
        message: Human-readable detail from the specific worker-side exception.
    """

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
