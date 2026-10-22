"""Integration-owned reports and diagnostics accepted by capture APIs."""

from collections.abc import Callable
from dataclasses import dataclass

from sifi_streamer.capture.records import (
    SCHEMA_VERSION,
    DeviceInfo,
    Diagnostic,
    DiagnosticSeverity,
    Packet,
    encode_record,
)


@dataclass(frozen=True, slots=True)
class DeviceInfoEvent:
    """One report occurrence; an empty info object is meaningful."""

    stage: str
    info: Packet


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    """One diagnostic occurrence, independent of capture termination."""

    severity: DiagnosticSeverity
    source: str
    stage: str
    code: str
    message: str
    details: Packet


type CaptureEvent = DeviceInfoEvent | DiagnosticEvent
type CaptureEventSink = Callable[[CaptureEvent], None]


def validate_event(event: CaptureEvent) -> None:
    """Validate the envelope and complete finite JSON payload."""
    match event:
        case DeviceInfoEvent():
            encode_record(DeviceInfo(SCHEMA_VERSION, 0, 0, 0, event.stage, event.info))
        case DiagnosticEvent():
            encode_record(
                Diagnostic(
                    SCHEMA_VERSION,
                    0,
                    0,
                    0,
                    event.severity,
                    event.source,
                    event.stage,
                    event.code,
                    event.message,
                    event.details,
                )
            )
        case _:
            raise TypeError("unsupported capture event")


def copy_event(event: CaptureEvent) -> CaptureEvent:
    """Validate and deeply copy an event at a mutable or process boundary."""
    from sifi_streamer.capture.records import validate_document

    validate_event(event)
    match event:
        case DeviceInfoEvent():
            return DeviceInfoEvent(event.stage, validate_document(event.info))
        case DiagnosticEvent():
            return DiagnosticEvent(
                event.severity,
                event.source,
                event.stage,
                event.code,
                event.message,
                validate_document(event.details),
            )
        case _:
            raise TypeError("unsupported capture event")
