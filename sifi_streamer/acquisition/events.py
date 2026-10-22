"""Optional structural acquisition extension for explicit capture events."""

from typing import Protocol, runtime_checkable

from sifi_streamer.capture.events import CaptureEventSink


@runtime_checkable
class CaptureEventSource(Protocol):
    """Optional device extension for startup and during-capture reports.

    The worker installs the sink before connect. Integrations call it only for
    explicit occurrences, including repeated or empty reports. Calls must finish
    before disconnect returns; do not retain the sink beyond device ownership.
    Devices without this extension publish their device_info once after connect.
    """

    def set_capture_event_sink(self, sink: CaptureEventSink) -> None: ...
