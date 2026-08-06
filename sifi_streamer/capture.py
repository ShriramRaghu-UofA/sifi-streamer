"""Authoritative append-only JSON Lines captures in Zstandard frames."""

import json
import math
import os
import time
from collections.abc import Callable, Iterator, Mapping
from compression import zstd
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

SCHEMA_VERSION = 2
type Scalar = str | int | float | bool | None
type Attributes = Mapping[str, Scalar]
type Packet = Mapping[str, object]
type Clock = Callable[[], int]


class CaptureError(ValueError):
    """Base class for capture schema and lifecycle errors."""


class CaptureDecodeError(CaptureError):
    """A capture record cannot be decoded safely."""


class CaptureLifecycleError(CaptureError):
    """A capture lifecycle invariant was violated."""


@dataclass(frozen=True, slots=True)
class _Record:
    schema_version: int
    sequence: int
    host_monotonic_ns: int
    host_unix_ns: int


@dataclass(frozen=True, slots=True)
class RawPacket(_Record):
    packet: Packet
    record_type: Literal["raw_packet"] = "raw_packet"


@dataclass(frozen=True, slots=True)
class CaptureStarted(_Record):
    capture_id: str
    attributes: Attributes
    record_type: Literal["capture_started"] = "capture_started"


@dataclass(frozen=True, slots=True)
class CaptureStopped(_Record):
    reason: str
    record_type: Literal["capture_stopped"] = "capture_stopped"


@dataclass(frozen=True, slots=True)
class SegmentStarted(_Record):
    segment_id: str
    segment_kind: str
    attributes: Attributes
    record_type: Literal["segment_started"] = "segment_started"


@dataclass(frozen=True, slots=True)
class SegmentStopped(_Record):
    segment_id: str
    reason: str | None
    record_type: Literal["segment_stopped"] = "segment_stopped"


@dataclass(frozen=True, slots=True)
class Marker(_Record):
    marker_id: str
    marker_kind: str
    attributes: Attributes
    source_time_ns: int | None
    source_clock: str | None
    record_type: Literal["marker"] = "marker"


type CaptureRecord = (
    RawPacket
    | CaptureStarted
    | CaptureStopped
    | SegmentStarted
    | SegmentStopped
    | Marker
)


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CaptureDecodeError(f"{name} must be a non-empty string")
    return value


def _require_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CaptureDecodeError(f"{name} must be a nonnegative integer")
    return value


def validate_attributes(value: object) -> dict[str, Scalar]:
    """Validate and defensively copy scalar annotations."""
    if not isinstance(value, Mapping):
        raise CaptureDecodeError("attributes must be a mapping")
    result: dict[str, Scalar] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise CaptureDecodeError("attribute keys must be non-empty strings")
        if isinstance(item, float) and not math.isfinite(item):
            raise CaptureDecodeError(f"attribute {key!r} must be finite")
        if not isinstance(item, (str, int, float, bool)) and item is not None:
            raise CaptureDecodeError(f"attribute {key!r} is not a scalar")
        result[key] = item
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _packet(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise CaptureDecodeError("packet must be a JSON object")
    try:
        parsed = json.loads(
            json.dumps(dict(value), allow_nan=False), parse_constant=_reject_constant
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CaptureDecodeError("packet must contain only finite JSON values") from exc
    if not isinstance(parsed, dict):
        raise CaptureDecodeError("packet must be a JSON object")
    return parsed


def record_to_wire_map(record: CaptureRecord) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": record.schema_version,
        "sequence": record.sequence,
        "host_monotonic_ns": record.host_monotonic_ns,
        "host_unix_ns": record.host_unix_ns,
        "record_type": record.record_type,
    }
    match record:
        case RawPacket(packet=packet):
            value["packet"] = dict(packet)
        case CaptureStarted(capture_id=identifier, attributes=attributes):
            value.update(capture_id=identifier, attributes=dict(attributes))
        case CaptureStopped(reason=reason):
            value["reason"] = reason
        case SegmentStarted(
            segment_id=identifier, segment_kind=kind, attributes=attributes
        ):
            value.update(
                segment_id=identifier, segment_kind=kind, attributes=dict(attributes)
            )
        case SegmentStopped(segment_id=identifier, reason=reason):
            value.update(segment_id=identifier, reason=reason)
        case Marker(
            marker_id=identifier,
            marker_kind=kind,
            attributes=attributes,
            source_time_ns=source_time,
            source_clock=clock,
        ):
            value.update(
                marker_id=identifier,
                marker_kind=kind,
                attributes=dict(attributes),
                source_time_ns=source_time,
                source_clock=clock,
            )
    return value


def encode_record(record: CaptureRecord) -> bytes:
    value = record_to_wire_map(record)
    decode_record(value)
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode()


def decode_record(value: object) -> CaptureRecord:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise CaptureDecodeError("record must be an object with string keys")
    required = (
        "schema_version",
        "sequence",
        "host_monotonic_ns",
        "host_unix_ns",
        "record_type",
    )
    missing = [field for field in required if field not in value]
    if missing:
        raise CaptureDecodeError(
            f"record missing required fields: {', '.join(missing)}"
        )
    schema = _require_int(value["schema_version"], "schema_version")
    if schema != SCHEMA_VERSION:
        raise CaptureDecodeError(f"unsupported schema_version {schema}")
    base = {
        "schema_version": schema,
        "sequence": _require_int(value["sequence"], "sequence"),
        "host_monotonic_ns": _require_int(
            value["host_monotonic_ns"], "host_monotonic_ns"
        ),
        "host_unix_ns": _require_int(value["host_unix_ns"], "host_unix_ns"),
    }
    record_type = _require_string(value["record_type"], "record_type")
    match record_type:
        case "raw_packet":
            return RawPacket(**base, packet=_packet(value.get("packet")))
        case "capture_started":
            return CaptureStarted(
                **base,
                capture_id=_require_string(value.get("capture_id"), "capture_id"),
                attributes=validate_attributes(value.get("attributes")),
            )
        case "capture_stopped":
            return CaptureStopped(
                **base, reason=_require_string(value.get("reason"), "reason")
            )
        case "segment_started":
            return SegmentStarted(
                **base,
                segment_id=_require_string(value.get("segment_id"), "segment_id"),
                segment_kind=_require_string(value.get("segment_kind"), "segment_kind"),
                attributes=validate_attributes(value.get("attributes")),
            )
        case "segment_stopped":
            reason = value.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise CaptureDecodeError("reason must be a string or null")
            return SegmentStopped(
                **base,
                segment_id=_require_string(value.get("segment_id"), "segment_id"),
                reason=reason,
            )
        case "marker":
            source_time = value.get("source_time_ns")
            if source_time is not None:
                source_time = _require_int(source_time, "source_time_ns")
            source_clock = value.get("source_clock")
            if source_clock is not None and not isinstance(source_clock, str):
                raise CaptureDecodeError("source_clock must be a string or null")
            return Marker(
                **base,
                marker_id=_require_string(value.get("marker_id"), "marker_id"),
                marker_kind=_require_string(value.get("marker_kind"), "marker_kind"),
                attributes=validate_attributes(value.get("attributes")),
                source_time_ns=source_time,
                source_clock=source_clock,
            )
        case _:
            raise CaptureDecodeError(f"unknown capture record type {record_type!r}")


class CaptureLogWriter:
    """Append schema-v2 records without rewriting an existing capture."""

    def __init__(
        self,
        path: Path,
        capture_id: str,
        attributes: Attributes | None = None,
        *,
        frame_target_bytes: int = 1 << 20,
        flush_interval_s: float = 1.0,
        compression_level: int | None = None,
        fsync_on_boundary: bool = False,
        monotonic_ns: Clock = time.monotonic_ns,
        unix_ns: Clock = time.time_ns,
    ) -> None:
        if frame_target_bytes <= 0 or flush_interval_s <= 0:
            raise ValueError("frame target and flush interval must be positive")
        values = validate_attributes(attributes or {})
        self.path, self._file = path, path.open("xb")
        self._target, self._interval_ns, self._level, self._fsync = (
            frame_target_bytes,
            int(flush_interval_s * 1e9),
            compression_level,
            fsync_on_boundary,
        )
        self._monotonic_ns, self._unix_ns = monotonic_ns, unix_ns
        self._next_sequence, self._buffer, self._open_segments = 0, bytearray(), set()
        self._last_flush_ns, self._stopped, self._closed = monotonic_ns(), False, False
        self._append(
            CaptureStarted(
                SCHEMA_VERSION,
                0,
                self._last_flush_ns,
                unix_ns(),
                _require_string(capture_id, "capture_id"),
                values,
            ),
            boundary=True,
        )

    def _record(self, cls: type[_Record], *args: object) -> _Record:
        return cls(
            SCHEMA_VERSION,
            self._next_sequence,
            self._monotonic_ns(),
            self._unix_ns(),
            *args,
        )  # type: ignore[call-arg]

    def _append(self, record: CaptureRecord, *, boundary: bool = False) -> int:
        if self._closed or self._stopped:
            raise CaptureLifecycleError("capture writer is closed")
        self._buffer.extend(encode_record(record))
        self._next_sequence += 1
        now = self._monotonic_ns()
        if (
            boundary
            or len(self._buffer) >= self._target
            or now - self._last_flush_ns >= self._interval_ns
        ):
            self.flush(boundary=boundary)
        return record.sequence

    def append_packet(self, packet: Packet) -> int:
        return self._append(self._record(RawPacket, _packet(packet)))

    def start_segment(
        self, segment_id: str, segment_kind: str, attributes: Attributes | None = None
    ) -> int:
        segment_id, segment_kind = (
            _require_string(segment_id, "segment_id"),
            _require_string(segment_kind, "segment_kind"),
        )
        if segment_id in self._open_segments:
            raise CaptureLifecycleError(f"segment {segment_id!r} is already open")
        values = validate_attributes(attributes or {})
        self._open_segments.add(segment_id)
        return self._append(
            self._record(SegmentStarted, segment_id, segment_kind, values),
            boundary=True,
        )

    def stop_segment(self, segment_id: str, reason: str | None = None) -> int:
        if segment_id not in self._open_segments:
            raise CaptureLifecycleError(f"segment {segment_id!r} is not open")
        self._open_segments.remove(segment_id)
        return self._append(
            self._record(SegmentStopped, segment_id, reason), boundary=True
        )

    def append_marker(
        self,
        marker_id: str,
        marker_kind: str,
        attributes: Attributes | None = None,
        *,
        source_time_ns: int | None = None,
        source_clock: str | None = None,
    ) -> int:
        marker_id, marker_kind = (
            _require_string(marker_id, "marker_id"),
            _require_string(marker_kind, "marker_kind"),
        )
        if source_time_ns is not None:
            _require_int(source_time_ns, "source_time_ns")
        return self._append(
            self._record(
                Marker,
                marker_id,
                marker_kind,
                validate_attributes(attributes or {}),
                source_time_ns,
                source_clock,
            )
        )

    def flush(self, *, boundary: bool = False) -> None:
        if not self._buffer:
            return
        self._file.write(zstd.compress(bytes(self._buffer), level=self._level))
        self._file.flush()
        if self._fsync and boundary:
            os.fsync(self._file.fileno())
        self._buffer.clear()
        self._last_flush_ns = self._monotonic_ns()

    def close(self, reason: str = "normal_completion") -> None:
        if self._closed:
            return
        if self._open_segments:
            raise CaptureLifecycleError(
                "cannot close capture while segments remain open"
            )
        if not self._stopped:
            self._append(
                self._record(CaptureStopped, _require_string(reason, "reason")),
                boundary=True,
            )
            self._stopped = True
        self.flush(boundary=True)
        self._file.close()
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class CaptureLogReader:
    """Stream and lifecycle-validate records from concatenated Zstandard frames."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __iter__(self) -> Iterator[CaptureRecord]:
        expected, started, stopped, open_segments = 0, False, False, set()
        try:
            with zstd.open(self.path, "rb") as file:
                for line_number, line in enumerate(file, 1):
                    if not line.endswith(b"\n"):
                        raise CaptureDecodeError(
                            f"JSONL record {line_number} is not newline-terminated"
                        )
                    try:
                        value = json.loads(
                            line.decode(), parse_constant=_reject_constant
                        )
                    except (
                        UnicodeDecodeError,
                        ValueError,
                        json.JSONDecodeError,
                    ) as exc:
                        raise CaptureDecodeError(
                            f"JSON decoding failed near record {expected}: {exc}"
                        ) from exc
                    record = decode_record(value)
                    if record.sequence != expected:
                        raise CaptureDecodeError(
                            "sequence discontinuity: "
                            f"expected {expected}, got {record.sequence}"
                        )
                    expected += 1
                    if isinstance(record, CaptureStarted):
                        if started:
                            raise CaptureLifecycleError(
                                "multiple capture_started records"
                            )
                        started = True
                    elif not started:
                        raise CaptureLifecycleError(
                            "first record must be capture_started"
                        )
                    if stopped:
                        raise CaptureLifecycleError("record after capture_stopped")
                    if isinstance(record, SegmentStarted):
                        if record.segment_id in open_segments:
                            raise CaptureLifecycleError(
                                f"duplicate open segment {record.segment_id!r}"
                            )
                        open_segments.add(record.segment_id)
                    elif isinstance(record, SegmentStopped):
                        if record.segment_id not in open_segments:
                            raise CaptureLifecycleError(
                                f"unknown segment {record.segment_id!r}"
                            )
                        open_segments.remove(record.segment_id)
                    elif isinstance(record, CaptureStopped):
                        if open_segments:
                            raise CaptureLifecycleError(
                                "capture stopped with open segments"
                            )
                        stopped = True
                    yield record
        except zstd.ZstdError as exc:
            raise CaptureDecodeError(f"Zstandard decompression failed: {exc}") from exc
        if expected == 0:
            raise CaptureLifecycleError("capture is empty")
