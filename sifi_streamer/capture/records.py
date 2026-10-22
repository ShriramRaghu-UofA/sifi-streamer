"""Read and write authoritative device-neutral DuckDB captures.

Writers create new schema-version-1 databases and append typed events and samples.
Readers never repair or mutate a capture. This module also defines the
device-neutral record values used by lifecycle and inspection APIs.

Annotation values are deliberately limited to JSON-compatible scalars.  Raw
packet documents, by contrast, retain complete finite JSON objects so unknown
device fields survive a decode-and-encode round trip.
"""

import json
import math
import shutil
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, Self

import duckdb
import numpy as np
import numpy.typing as npt

from sifi_streamer.capture.schema import SCHEMA_SQL, quote_identifier

SCHEMA_VERSION = 1
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
    """Fields common to every wire record.

    Attributes:
        schema_version: Capture schema version.  Version 3 is currently supported.
        sequence: Zero-based position of the record in its capture.
        host_monotonic_ns: Host monotonic-clock time when the record was created.
        host_unix_ns: Host Unix-epoch time in nanoseconds.
    """

    schema_version: int
    sequence: int
    host_monotonic_ns: int
    host_unix_ns: int


@dataclass(frozen=True, slots=True)
class RawPacket(_Record):
    """A complete JSON packet received from the acquisition device.

    Attributes:
        packet: Finite JSON object, including fields unknown to this package.
        record_type: Stable wire discriminator, always ``"raw_packet"``.
    """

    packet: Packet | None
    record_type: Literal["raw_packet"] = "raw_packet"


@dataclass(frozen=True, slots=True)
class LaunchConfiguration(_Record):
    """Resolved launcher settings, recorded before device connection."""

    configuration: Packet
    record_type: Literal["launch_configuration"] = "launch_configuration"


@dataclass(frozen=True, slots=True)
class DeviceInfo(_Record):
    """One explicit device report, including an explicitly empty report."""

    stage: str
    info: Packet
    record_type: Literal["device_info"] = "device_info"


type DiagnosticSeverity = Literal["info", "warning", "error"]


@dataclass(frozen=True, slots=True)
class Diagnostic(_Record):
    """An explicit diagnostic; severity does not control capture lifecycle."""

    severity: DiagnosticSeverity
    source: str
    stage: str
    code: str
    message: str
    details: Packet
    record_type: Literal["diagnostic"] = "diagnostic"


@dataclass(frozen=True, slots=True)
class CaptureStarted(_Record):
    """The first record in a capture.

    Attributes:
        capture_id: Non-empty identifier for this capture occurrence.
        attributes: Device-neutral scalar metadata copied at validation time.
        record_type: Stable wire discriminator, always ``"capture_started"``.
    """

    capture_id: str
    attributes: Attributes
    record_type: Literal["capture_started"] = "capture_started"


@dataclass(frozen=True, slots=True)
class CaptureStopped(_Record):
    """The terminal record of a normally closed capture.

    Attributes:
        reason: Non-empty machine-readable reason for stopping.
        record_type: Stable wire discriminator, always ``"capture_stopped"``.
    """

    reason: str
    record_type: Literal["capture_stopped"] = "capture_stopped"


@dataclass(frozen=True, slots=True)
class SegmentStarted(_Record):
    """The authoritative start boundary of a generic duration segment.

    Attributes:
        segment_id: Identifier for this segment occurrence.
        segment_kind: Stable category assigned by the consumer.
        attributes: Device-neutral scalar metadata.
        record_type: Stable wire discriminator, always ``"segment_started"``.
    """

    segment_id: str
    segment_kind: str
    attributes: Attributes
    record_type: Literal["segment_started"] = "segment_started"


@dataclass(frozen=True, slots=True)
class SegmentStopped(_Record):
    """The authoritative stop boundary of a generic duration segment.

    Attributes:
        segment_id: Identifier of the corresponding open segment.
        reason: Optional machine-readable completion reason.
        record_type: Stable wire discriminator, always ``"segment_stopped"``.
    """

    segment_id: str
    reason: str | None
    record_type: Literal["segment_stopped"] = "segment_stopped"


@dataclass(frozen=True, slots=True)
class Marker(_Record):
    """A point-in-time fact recorded during a capture.

    Attributes:
        marker_id: Identifier for this marker occurrence.
        marker_kind: Stable category assigned by the consumer.
        attributes: Device-neutral scalar metadata.
        source_time_ns: Optional timestamp from an external source clock.
        source_clock: Optional name of the clock used by ``source_time_ns``.
        record_type: Stable wire discriminator, always ``"marker"``.
    """

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
    | LaunchConfiguration
    | DeviceInfo
    | Diagnostic
)


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CaptureDecodeError(f"{name} must be a non-empty string")
    return value


def _require_int(value: object, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 2**63 - 1
    ):
        raise CaptureDecodeError(f"{name} must be a nonnegative signed 64-bit integer")
    return value


def validate_attributes(value: object) -> dict[str, Scalar]:
    """Validate and defensively copy a scalar annotation mapping.

    Keys must be non-empty strings. Values may be strings, integers, finite
    floats, booleans, or ``None``; nested containers and non-finite floats are
    rejected.

    Args:
        value: Candidate annotation mapping.

    Returns:
        A new dictionary containing the validated values.

    Raises:
        CaptureDecodeError: If ``value`` is not a mapping or contains an invalid
            key or value.
    """
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


def validate_document(value: object) -> dict[str, object]:
    """Validate and deeply copy a finite JSON object without vendor interpretation."""

    def check(item: object) -> None:
        if isinstance(item, Mapping):
            if not all(isinstance(key, str) for key in item):
                raise CaptureDecodeError("JSON object keys must be strings")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and not isinstance(item, str | int | float | bool):
            raise CaptureDecodeError("document must contain only JSON values")

    check(value)
    return _packet(value)


def record_to_wire_map(record: CaptureRecord) -> dict[str, object]:
    """Convert a typed capture record to its schema-version-3 wire mapping.

    The returned mapping is newly allocated, including copies of attribute and
    packet mappings.  Call :func:`encode_record` when validation and JSONL bytes
    are required.
    """
    value: dict[str, object] = {
        "schema_version": record.schema_version,
        "sequence": record.sequence,
        "host_monotonic_ns": record.host_monotonic_ns,
        "host_unix_ns": record.host_unix_ns,
        "record_type": record.record_type,
    }
    match record:
        case LaunchConfiguration(configuration=configuration):
            value["configuration"] = validate_document(configuration)
        case DeviceInfo(stage=stage, info=info):
            value.update(stage=stage, info=validate_document(info))
        case Diagnostic(
            severity=severity,
            source=source,
            stage=stage,
            code=code,
            message=message,
            details=details,
        ):
            value.update(
                severity=severity,
                source=source,
                stage=stage,
                code=code,
                message=message,
                details=validate_document(details),
            )
        case RawPacket(packet=packet):
            value["packet"] = validate_document(packet) if packet is not None else None
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
    """Validate and encode one capture record as newline-terminated UTF-8 JSON.

    Raises:
        CaptureDecodeError: If any record field violates the wire schema.
        TypeError: If ``record`` is not one of the supported record dataclasses.
    """
    value = record_to_wire_map(record)
    decode_record(value)
    return (
        json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode()


def decode_record(value: object) -> CaptureRecord:
    """Decode and validate one schema-version-3 wire mapping.

    Unknown fields on known record types are ignored for forward compatibility.
    Unknown record types and unsupported schema versions are rejected.

    Args:
        value: Parsed JSON value expected to contain a capture record object.

    Returns:
        The corresponding typed record.

    Raises:
        CaptureDecodeError: If required fields are missing or invalid, the schema
            version is unsupported, or the record type is unknown.
    """
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
    sequence = _require_int(value["sequence"], "sequence")
    host_monotonic_ns = _require_int(value["host_monotonic_ns"], "host_monotonic_ns")
    host_unix_ns = _require_int(value["host_unix_ns"], "host_unix_ns")
    record_type = _require_string(value["record_type"], "record_type")
    match record_type:
        case "launch_configuration":
            return LaunchConfiguration(
                schema,
                sequence,
                host_monotonic_ns,
                host_unix_ns,
                validate_document(value.get("configuration")),
            )
        case "device_info":
            return DeviceInfo(
                schema,
                sequence,
                host_monotonic_ns,
                host_unix_ns,
                _require_string(value.get("stage"), "stage"),
                validate_document(value.get("info")),
            )
        case "diagnostic":
            message = value.get("message")
            if not isinstance(message, str):
                raise CaptureDecodeError("message must be a string")
            severity = value.get("severity")
            if severity not in ("info", "warning", "error"):
                raise CaptureDecodeError("severity must be info, warning, or error")
            return Diagnostic(
                schema,
                sequence,
                host_monotonic_ns,
                host_unix_ns,
                severity,
                _require_string(value.get("source"), "source"),
                _require_string(value.get("stage"), "stage"),
                _require_string(value.get("code"), "code"),
                message,
                validate_document(value.get("details")),
            )
        case "raw_packet":
            return RawPacket(
                schema_version=schema,
                sequence=sequence,
                host_monotonic_ns=host_monotonic_ns,
                host_unix_ns=host_unix_ns,
                packet=(
                    validate_document(value["packet"])
                    if value.get("packet") is not None
                    else None
                ),
            )
        case "capture_started":
            return CaptureStarted(
                schema_version=schema,
                sequence=sequence,
                host_monotonic_ns=host_monotonic_ns,
                host_unix_ns=host_unix_ns,
                capture_id=_require_string(value.get("capture_id"), "capture_id"),
                attributes=validate_attributes(value.get("attributes")),
            )
        case "capture_stopped":
            return CaptureStopped(
                schema_version=schema,
                sequence=sequence,
                host_monotonic_ns=host_monotonic_ns,
                host_unix_ns=host_unix_ns,
                reason=_require_string(value.get("reason"), "reason"),
            )
        case "segment_started":
            return SegmentStarted(
                schema_version=schema,
                sequence=sequence,
                host_monotonic_ns=host_monotonic_ns,
                host_unix_ns=host_unix_ns,
                segment_id=_require_string(value.get("segment_id"), "segment_id"),
                segment_kind=_require_string(value.get("segment_kind"), "segment_kind"),
                attributes=validate_attributes(value.get("attributes")),
            )
        case "segment_stopped":
            reason = value.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise CaptureDecodeError("reason must be a string or null")
            return SegmentStopped(
                schema_version=schema,
                sequence=sequence,
                host_monotonic_ns=host_monotonic_ns,
                host_unix_ns=host_unix_ns,
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
                schema_version=schema,
                sequence=sequence,
                host_monotonic_ns=host_monotonic_ns,
                host_unix_ns=host_unix_ns,
                marker_id=_require_string(value.get("marker_id"), "marker_id"),
                marker_kind=_require_string(value.get("marker_kind"), "marker_kind"),
                attributes=validate_attributes(value.get("attributes")),
                source_time_ns=source_time,
                source_clock=source_clock,
            )
        case _:
            raise CaptureDecodeError(f"unknown capture record type {record_type!r}")


class ChannelDeclaration(Protocol):
    """Structural channel declaration consumed by storage."""

    @property
    def channel_id(self) -> str: ...
    @property
    def label(self) -> str | None: ...
    @property
    def unit(self) -> str | None: ...


class StreamDeclaration(Protocol):
    """Storage consumes declarations without importing an acquisition integration."""

    @property
    def stream_id(self) -> str: ...
    @property
    def channels(self) -> Sequence[ChannelDeclaration]: ...
    @property
    def nominal_rate_hz(self) -> float: ...
    @property
    def label(self) -> str | None: ...
    @property
    def numpy_dtype(self) -> np.dtype: ...
    @property
    def source_clock(self) -> str: ...
    @property
    def timestamp_unit(self) -> str: ...
    @property
    def timestamp_dtype(self) -> npt.DTypeLike: ...


@dataclass(frozen=True, slots=True)
class _StreamLayout:
    table: str
    channels: tuple[str, ...]
    dtype: np.dtype
    timestamp_dtype: np.dtype


def _sql_dtype(dtype: np.dtype) -> str:
    values = {
        ("b", 1): "BOOLEAN",
        ("i", 1): "TINYINT",
        ("i", 2): "SMALLINT",
        ("i", 4): "INTEGER",
        ("i", 8): "BIGINT",
        ("u", 1): "UTINYINT",
        ("u", 2): "USMALLINT",
        ("u", 4): "UINTEGER",
        ("u", 8): "UBIGINT",
        ("f", 4): "FLOAT",
        ("f", 8): "DOUBLE",
    }
    try:
        return values[dtype.kind, dtype.itemsize]
    except KeyError as exc:
        raise CaptureDecodeError(f"unsupported capture dtype {dtype}") from exc


def _json(value: object) -> str:
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))


def validate_sample_value(
    value: object,
    dtype: np.dtype,
    name: str,
) -> int | float | bool:
    """Validate a scalar before dtype conversion can truncate or overflow it."""
    if isinstance(value, np.generic):
        value = value.item()
    if dtype.kind == "b":
        if not isinstance(value, bool):
            raise CaptureDecodeError(f"{name} must be boolean")
    elif dtype.kind in "iu":
        if isinstance(value, bool) or not isinstance(value, int):
            raise CaptureDecodeError(f"{name} must be an integer")
        limits = np.iinfo(dtype)
        if not limits.min <= value <= limits.max:
            raise CaptureDecodeError(f"{name} exceeds declared dtype {dtype}")
    elif (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or abs(value) > np.finfo(dtype).max
    ):
        raise CaptureDecodeError(
            f"{name} must be finite and fit declared dtype {dtype}"
        )
    if not isinstance(value, int | float | bool):
        raise CaptureDecodeError(f"{name} must be numeric")
    return value


class CaptureLogWriter:
    """Own one new DuckDB capture and its uncommitted transaction.

    The owner calls :meth:`flush` on an independent deadline. Accepted events
    remain uncommitted until then. This object is single-threaded and must never
    be shared across processes. A writer failure requires :meth:`abort`.
    """

    def __init__(
        self,
        path: Path,
        capture_id: str,
        attributes: Attributes | None = None,
        *,
        monotonic_ns: Clock = time.monotonic_ns,
        unix_ns: Clock = time.time_ns,
    ) -> None:
        capture_id = _require_string(capture_id, "capture_id")
        attributes = validate_attributes(attributes or {})
        self.path = Path(path)
        # Reserve creation atomically without opening an existing DuckDB file.
        # All package writers use this same sibling-directory reservation.
        reservation = self.path.with_name(self.path.name + ".creating")
        reservation.mkdir()
        try:
            if (
                self.path.exists()
                or self.path.with_name(self.path.name + ".wal").exists()
            ):
                raise FileExistsError(self.path)
            self._connection = duckdb.connect(str(self.path))
        finally:
            reservation.rmdir()
        self._monotonic_ns, self._unix_ns = monotonic_ns, unix_ns
        self._next_sequence = 0
        self._producer_next: dict[str, int] = {}
        self._producer_devices: dict[str, str | None] = {"recorder": None}
        self._context: tuple[str, int, int, int] | None = None
        self._context_used = False
        self._open_segments: dict[str, int] = {}
        self._streams: dict[tuple[str, str], _StreamLayout] = {}
        self._devices: set[str] = set()
        self._registration_closed = False
        self._closed = False
        self._failed = False
        self._sample_appenders: dict[str, duckdb.Appender] = {}
        try:
            self._connection.execute("SET threads = 1")
            self._connection.execute(SCHEMA_SQL)
            self._connection.execute(
                "INSERT INTO capture VALUES (?, ?, ?, ?::JSON::VARIANT, ?)",
                [
                    capture_id,
                    SCHEMA_VERSION,
                    "0.9.0",
                    _json(attributes),
                    "Python time.monotonic_ns; same host boot only",
                ],
            )

            self._connection.execute("BEGIN")
            self._append(
                CaptureStarted(
                    SCHEMA_VERSION, 0, monotonic_ns(), unix_ns(), capture_id, attributes
                )
            )
            self.flush()
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    @contextmanager
    def submission(
        self,
        producer_id: str,
        producer_sequence: int,
        received_monotonic_ns: int,
        received_unix_ns: int,
    ) -> Iterator[None]:
        """Apply one producer-assigned identity and receipt time to one event."""
        if self._context is not None:
            raise CaptureLifecycleError("nested producer submissions are invalid")
        producer_id = _require_string(producer_id, "producer_id")
        if producer_id not in self._producer_devices:
            raise CaptureLifecycleError(f"undeclared producer {producer_id!r}")
        _require_int(producer_sequence, "producer_sequence")
        _require_int(received_monotonic_ns, "received_monotonic_ns")
        _require_int(received_unix_ns, "received_unix_ns")
        if producer_sequence != self._producer_next.get(producer_id, 0):
            raise CaptureLifecycleError(
                f"producer sequence discontinuity: {producer_id}"
            )
        self._context = (
            producer_id,
            producer_sequence,
            received_monotonic_ns,
            received_unix_ns,
        )
        self._context_used = False
        try:
            yield
            if not self._context_used:
                raise CaptureLifecycleError(
                    "a submission must contain exactly one event"
                )
        finally:
            self._context = None

    def _record(self, record_type: str, **values: object) -> CaptureRecord:
        times = (
            self._context[2:]
            if self._context
            else (self._monotonic_ns(), self._unix_ns())
        )
        return decode_record(
            {
                "schema_version": SCHEMA_VERSION,
                "sequence": self._next_sequence,
                "host_monotonic_ns": times[0],
                "host_unix_ns": times[1],
                "record_type": record_type,
                **values,
            }
        )

    def _append(
        self,
        record: CaptureRecord,
        *,
        device_id: str | None = None,
        stream_id: str | None = None,
        reported_rate_hz: float | None = None,
        samples_lost: int = 0,
        status: str = "ok",
    ) -> int:
        if self._closed or self._failed:
            raise CaptureLifecycleError("capture writer is unavailable")
        # Validate before changing sequence or SQL state.
        record = decode_record(record_to_wire_map(record))
        if self._context:
            if self._context_used:
                raise CaptureLifecycleError(
                    "a submission must contain exactly one event"
                )
            producer, local, monotonic, unix = self._context
            if device_id is not None and self._producer_devices[producer] != device_id:
                raise CaptureLifecycleError(
                    "event device differs from its registered producer"
                )
        else:
            producer = "recorder"
            local = self._producer_next.get(producer, 0)
            monotonic, unix = record.host_monotonic_ns, record.host_unix_ns
        sequence = self._next_sequence
        try:
            self._connection.execute(
                "INSERT INTO producers VALUES (?,NULL,'recorder') "
                "ON CONFLICT DO NOTHING",
                [producer],
            )
            self._connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                [sequence, producer, local, record.record_type, monotonic, unix],
            )
            match record:
                case CaptureStarted():
                    self._connection.execute(
                        "INSERT INTO capture_started VALUES (?)", [sequence]
                    )
                case CaptureStopped(reason=reason):
                    outcome = (
                        "completed"
                        if reason == "normal_completion"
                        else "aborted"
                        if reason in ("operator_request", "operator_interrupt")
                        else "failed"
                    )
                    self._connection.execute(
                        "INSERT INTO capture_stopped VALUES (?,?,?)",
                        [sequence, reason, outcome],
                    )
                case RawPacket(packet=packet):
                    self._connection.execute(
                        "INSERT INTO packets VALUES "
                        "(?, ?, ?, ?::JSON::VARIANT, ?, ?, ?)",
                        [
                            sequence,
                            device_id,
                            stream_id,
                            _json(packet) if packet is not None else None,
                            reported_rate_hz,
                            samples_lost,
                            status,
                        ],
                    )
                case LaunchConfiguration(configuration=configuration):
                    self._connection.execute(
                        "INSERT INTO launch_configuration VALUES (?, ?::JSON::VARIANT)",
                        [sequence, _json(configuration)],
                    )
                case DeviceInfo(stage=stage, info=info):
                    self._connection.execute(
                        "INSERT INTO device_reports VALUES (?, ?, ?, ?::JSON::VARIANT)",
                        [sequence, device_id, stage, _json(info)],
                    )
                case Diagnostic():
                    self._connection.execute(
                        "INSERT INTO diagnostics VALUES "
                        "(?,?,?,?,?,?,?,?::JSON::VARIANT)",
                        [
                            sequence,
                            device_id,
                            record.severity,
                            record.source,
                            record.stage,
                            record.code,
                            record.message,
                            _json(record.details),
                        ],
                    )
                case Marker():
                    self._connection.execute(
                        "INSERT INTO markers VALUES (?,?,?,?::JSON::VARIANT,?,?)",
                        [
                            sequence,
                            record.marker_id,
                            record.marker_kind,
                            _json(dict(record.attributes)),
                            record.source_time_ns,
                            record.source_clock,
                        ],
                    )
                case SegmentStarted():
                    self._connection.execute(
                        "INSERT INTO segment_starts VALUES (?,?,?,?::JSON::VARIANT)",
                        [
                            sequence,
                            record.segment_id,
                            record.segment_kind,
                            _json(dict(record.attributes)),
                        ],
                    )
                case SegmentStopped():
                    self._connection.execute(
                        "INSERT INTO segment_stops VALUES (?,?,?)",
                        [
                            sequence,
                            self._open_segments[record.segment_id],
                            record.reason,
                        ],
                    )
        except duckdb.Error:
            self._failed = True
            raise
        self._next_sequence += 1
        self._producer_next[producer] = local + 1
        self._context_used = self._context is not None
        return sequence

    def register_device(
        self,
        device_id: str,
        integration: str = "",
        configuration: Packet | None = None,
    ) -> None:
        """Declare one fixed device identity before stream registration."""
        device_id = _require_string(device_id, "device_id")
        if self._registration_closed:
            raise CaptureLifecycleError("device registration is closed")
        if device_id in self._devices:
            raise CaptureLifecycleError(f"duplicate device {device_id!r}")
        if not isinstance(integration, str):
            raise CaptureDecodeError("integration must be text")
        document = validate_document(configuration or {})
        self._connection.execute(
            "INSERT INTO devices VALUES (?,?,?::JSON::VARIANT)",
            [device_id, integration, _json(document)],
        )
        self._devices.add(device_id)

    def register_producer(
        self,
        producer_id: str,
        device_id: str | None = None,
        role: str = "device",
    ) -> None:
        """Persist a producer's device association and descriptive role."""
        producer_id = _require_string(producer_id, "producer_id")
        if producer_id in self._producer_devices:
            raise CaptureLifecycleError(f"duplicate producer {producer_id!r}")
        if device_id is not None:
            device_id = _require_string(device_id, "device_id")
        self._connection.execute(
            "INSERT INTO producers VALUES (?,?,?)",
            [
                producer_id,
                device_id,
                _require_string(role, "role"),
            ],
        )
        self._producer_devices[producer_id] = device_id

    def register_stream(self, device_id: str, spec: StreamDeclaration) -> None:
        """Create a typed sample table and persist its declaration."""
        if self._registration_closed:
            raise CaptureLifecycleError("stream registration is closed")
        if device_id not in self._devices:
            raise CaptureLifecycleError(f"undeclared device {device_id!r}")
        stream_id = _require_string(spec.stream_id, "stream_id")
        source_clock = _require_string(spec.source_clock, "source_clock")
        timestamp_unit = _require_string(spec.timestamp_unit, "timestamp_unit")
        if (
            isinstance(spec.nominal_rate_hz, bool)
            or not math.isfinite(spec.nominal_rate_hz)
            or spec.nominal_rate_hz <= 0
        ):
            raise CaptureDecodeError("nominal rate must be finite and positive")
        if spec.label is not None and not isinstance(spec.label, str):
            raise CaptureDecodeError("stream label must be text or null")
        if any(
            value is not None and not isinstance(value, str)
            for channel in spec.channels
            for value in (channel.label, channel.unit)
        ):
            raise CaptureDecodeError("channel labels and units must be text or null")
        key = (device_id, stream_id)
        if key in self._streams:
            raise CaptureLifecycleError(f"duplicate stream {key!r}")
        names = tuple(
            _require_string(channel.channel_id, "channel_id")
            for channel in spec.channels
        )
        if not names or len(set(names)) != len(names):
            raise CaptureDecodeError("stream channels must be non-empty and unique")
        dtype, timestamp_dtype = (
            np.dtype(spec.numpy_dtype),
            np.dtype(spec.timestamp_dtype),
        )
        value_type, time_type = _sql_dtype(dtype), _sql_dtype(timestamp_dtype)
        if timestamp_dtype.kind not in "iuf":
            raise CaptureDecodeError("source timestamp dtype must be numeric")
        table = f"samples_{len(self._streams):04d}"
        definitions = ", ".join(
            f"value_{i} {value_type}, valid_{i} BOOLEAN NOT NULL"
            for i in range(len(names))
        )
        self._connection.execute(
            f"CREATE TABLE {table} (event_sequence BIGINT NOT NULL, "
            f"sample_index BIGINT NOT NULL, source_time {time_type}, {definitions}, "
            "PRIMARY KEY(event_sequence,sample_index))"
        )
        self._connection.execute(
            "INSERT INTO streams VALUES (?,?,?,?,?,?,?,?,?)",
            [
                device_id,
                stream_id,
                table,
                dtype.str,
                spec.nominal_rate_hz,
                spec.label,
                source_clock,
                timestamp_unit,
                timestamp_dtype.str,
            ],
        )
        for index, channel in enumerate(spec.channels):
            self._connection.execute(
                "INSERT INTO channels VALUES (?,?,?,?,?,?,?,?)",
                [
                    device_id,
                    spec.stream_id,
                    channel.channel_id,
                    index,
                    f"value_{index}",
                    f"valid_{index}",
                    channel.label,
                    channel.unit,
                ],
            )
        self._streams[key] = _StreamLayout(table, names, dtype, timestamp_dtype)
        self._sample_appenders[table] = self._connection.appender(table)

    def append_packet(self, packet: Packet) -> int:
        """Record a raw document with no declared signal contribution."""
        sequence = self._append(
            self._record("raw_packet", packet=validate_document(packet))
        )
        self._registration_closed = True
        return sequence

    def append_packet_data(
        self,
        device_id: str,
        stream_id: str | None,
        timestamps: Sequence[int | float],
        data: Mapping[str, Sequence[int | float | None]],
        validity: Mapping[str, Sequence[bool]],
        document: Packet | None,
        *,
        reported_rate_hz: float | None = None,
        samples_lost: int = 0,
        status: str = "ok",
    ) -> int:
        """Record a complete packet and its typed samples in the same transaction."""
        if device_id not in self._devices:
            raise CaptureLifecycleError(f"undeclared device {device_id!r}")
        if reported_rate_hz is not None and (
            isinstance(reported_rate_hz, bool)
            or not isinstance(reported_rate_hz, int | float)
            or not math.isfinite(reported_rate_hz)
            or reported_rate_hz <= 0
        ):
            raise CaptureDecodeError("reported sample rate must be finite and positive")
        samples_lost = _require_int(samples_lost, "samples_lost")
        if not isinstance(status, str):
            raise CaptureDecodeError("packet status must be text")
        document = validate_document(document) if document is not None else None
        layout = None
        rows: list[tuple[object, ...]] = []
        if stream_id is None:
            if timestamps or data or validity:
                raise CaptureDecodeError("samples require a declared stream")
        else:
            try:
                layout = self._streams[device_id, stream_id]
            except KeyError as exc:
                raise CaptureLifecycleError(
                    f"undeclared stream {(device_id, stream_id)!r}"
                ) from exc
            if set(data) != set(layout.channels) or set(validity) != set(
                layout.channels
            ):
                raise CaptureDecodeError(
                    "packet channels do not match the stream declaration"
                )
            count = len(timestamps)
            if any(
                len(data[name]) != count or len(validity[name]) != count
                for name in layout.channels
            ):
                raise CaptureDecodeError(
                    "timestamps, values and validity lengths must agree"
                )
            for index, timestamp in enumerate(timestamps):
                timestamp = validate_sample_value(
                    timestamp, layout.timestamp_dtype, "timestamp"
                )
                values: list[object] = [self._next_sequence, index, timestamp]
                for name in layout.channels:
                    value, valid = data[name][index], validity[name][index]
                    if isinstance(valid, np.bool_):
                        valid = bool(valid)
                    if not isinstance(valid, bool):
                        raise CaptureDecodeError("validity values must be boolean")
                    if value is not None:
                        value = validate_sample_value(value, layout.dtype, name)
                    values.extend((value, valid))
                rows.append(tuple(values))
        sequence = self._append(
            self._record("raw_packet", packet=document),
            device_id=device_id,
            stream_id=stream_id,
            reported_rate_hz=reported_rate_hz,
            samples_lost=samples_lost,
            status=status,
        )
        self._registration_closed = True
        try:
            if layout is not None:
                self._sample_appenders[layout.table].append_rows(rows)
        except duckdb.Error:
            self._failed = True
            raise
        return sequence

    def append_launch_configuration(self, configuration: Packet) -> int:
        if self._next_sequence != 1:
            raise CaptureLifecycleError(
                "launch configuration must be the second record"
            )
        return self._append(
            self._record("launch_configuration", configuration=configuration)
        )

    def append_device_info(
        self,
        stage: str,
        info: Packet,
        *,
        device_id: str | None = None,
    ) -> int:
        return self._append(
            self._record("device_info", stage=stage, info=info), device_id=device_id
        )

    def append_diagnostic(
        self,
        severity: DiagnosticSeverity,
        source: str,
        stage: str,
        code: str,
        message: str,
        details: Packet,
        *,
        device_id: str | None = None,
    ) -> int:
        return self._append(
            self._record(
                "diagnostic",
                severity=severity,
                source=source,
                stage=stage,
                code=code,
                message=message,
                details=details,
            ),
            device_id=device_id,
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
        return self._append(
            self._record(
                "marker",
                marker_id=marker_id,
                marker_kind=marker_kind,
                attributes=attributes or {},
                source_time_ns=source_time_ns,
                source_clock=source_clock,
            )
        )

    def start_segment(
        self,
        segment_id: str,
        segment_kind: str,
        attributes: Attributes | None = None,
    ) -> int:
        if segment_id in self._open_segments:
            raise CaptureLifecycleError(f"segment {segment_id!r} is already open")
        sequence = self._append(
            self._record(
                "segment_started",
                segment_id=segment_id,
                segment_kind=segment_kind,
                attributes=attributes or {},
            )
        )
        self._open_segments[segment_id] = sequence
        return sequence

    def stop_segment(self, segment_id: str, reason: str | None = None) -> int:
        if not self._open_segments or next(reversed(self._open_segments)) != segment_id:
            raise CaptureLifecycleError("segments must stop in reverse start order")
        sequence = self._append(
            self._record("segment_stopped", segment_id=segment_id, reason=reason)
        )
        del self._open_segments[segment_id]
        return sequence

    def flush(self) -> None:
        """Commit all accepted events and samples, including during idle acquisition."""
        if self._closed or self._failed:
            raise CaptureLifecycleError("capture writer is unavailable")
        try:
            for appender in self._sample_appenders.values():
                appender.flush()
            self._connection.execute("COMMIT")
            self._connection.execute("BEGIN")
        except duckdb.Error:
            self._failed = True
            raise

    def close(self, reason: str = "normal_completion") -> None:
        """Close remaining segments, commit a terminal event and finalize the file."""
        if self._closed:
            return
        reason = _require_string(reason, "reason")
        segment_reason = "completed" if reason == "normal_completion" else "aborted"
        for segment_id in list(reversed(self._open_segments)):
            self.stop_segment(segment_id, segment_reason)
        self._append(self._record("capture_stopped", reason=reason))
        self.flush()
        self._connection.execute("ROLLBACK")
        self._connection.execute("CHECKPOINT")
        self._connection.close()
        self._closed = True

    def abort(self) -> None:
        """Discard the open transaction; retain the last committed prefix."""
        if self._closed:
            return
        try:
            for appender in self._sample_appenders.values():
                appender.clear()
            self._connection.execute("ROLLBACK")
        finally:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, *_: object) -> None:
        if exc_type is None:
            try:
                self.close()
            except BaseException:
                self.abort()
                raise
        else:
            self.abort()


class CaptureLogReader:
    """Read-only discovery and validated event iteration for a DuckDB capture.

    A crashed capture requiring WAL replay must first be copied and explicitly
    recovered with :func:`recover_capture`. Readers never mutate source files.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _open(self) -> duckdb.DuckDBPyConnection:
        connection = None
        try:
            connection = duckdb.connect(str(self.path), read_only=True)
            rows = connection.execute("SELECT schema_version FROM capture").fetchall()
            if len(rows) != 1 or rows[0][0] != SCHEMA_VERSION:
                connection.close()
                raise CaptureDecodeError("unsupported capture database schema")
            return connection
        except duckdb.Error as exc:
            if connection is not None:
                connection.close()
            raise CaptureDecodeError(
                f"cannot open capture database read-only: {exc}"
            ) from exc

    def query(
        self,
        sql: str,
        parameters: Sequence[object] = (),
    ) -> list[tuple[object, ...]]:
        """Execute one SELECT against a read-only database connection."""
        with self._open() as connection:
            statements = connection.extract_statements(sql)
            if len(statements) != 1 or statements[0].type.name != "SELECT":
                raise ValueError("capture queries must contain exactly one SELECT")
            return connection.execute(sql, list(parameters)).fetchall()

    def tables(self) -> tuple[str, ...]:
        """Discover user-facing physical tables and convenience views."""
        return tuple(
            str(row[0])
            for row in self.query(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='main' AND table_name <> 'record_bodies' "
                "ORDER BY table_name"
            )
        )

    def streams(self) -> list[dict[str, object]]:
        """Return stream metadata including the associated typed sample table."""
        columns = (
            "device_id",
            "stream_id",
            "table_name",
            "dtype",
            "nominal_rate_hz",
            "label",
            "source_clock",
            "timestamp_unit",
            "timestamp_dtype",
        )
        return [
            dict(zip(columns, row, strict=True))
            for row in self.query("SELECT * FROM streams ORDER BY device_id, stream_id")
        ]

    def export(
        self,
        destination: Path,
        *,
        format: Literal["parquet", "csv"] = "parquet",
        native_variant: bool = False,
        tables: Sequence[str] | None = None,
    ) -> dict[str, Path]:
        """Export selected tables into a new directory without optional libraries.

        By default dynamic columns are ordinary JSON text for portability.
        Native VARIANT export is available only for Parquet. No file is replaced.
        """
        if format not in ("parquet", "csv"):
            raise ValueError("export format must be parquet or csv")
        if native_variant and format != "parquet":
            raise ValueError("native VARIANT export requires Parquet")
        available = self.tables()
        selected = tuple(tables) if tables is not None else available
        if not selected or len(set(selected)) != len(selected):
            raise ValueError("select at least one distinct table")
        if unknown := set(selected) - set(available):
            raise ValueError(f"unknown capture tables: {sorted(unknown)}")
        if any(
            table in (".", "..") or any(c in table for c in "/\\:\0")
            for table in selected
        ):
            raise ValueError("table name cannot be used as an export filename")
        destination = Path(destination)
        destination.mkdir(parents=False, exist_ok=False)
        exported: dict[str, Path] = {}
        with self._open() as connection:
            for table in selected:
                information = connection.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema='main' AND table_name=? "
                    "ORDER BY ordinal_position",
                    [table],
                ).fetchall()
                projection = ", ".join(
                    f"{quote_identifier(name)}::JSON::VARCHAR "
                    f"AS {quote_identifier(name)}"
                    if dtype == "VARIANT" and not native_variant
                    else quote_identifier(name)
                    for name, dtype in information
                )
                path = destination / f"{table}.{format}"
                options = (
                    "FORMAT PARQUET, COMPRESSION ZSTD"
                    if format == "parquet"
                    else "FORMAT CSV, HEADER TRUE"
                )
                connection.execute(
                    f"COPY (SELECT {projection} FROM {quote_identifier(table)}) "
                    f"TO ? ({options})",
                    [str(path)],
                )
                exported[table] = path
        return exported

    def __iter__(self) -> Iterator[CaptureRecord]:
        expected, started, stopped = 0, False, False
        open_segments: list[str] = []
        producer_sequences: dict[str, int] = {}
        try:
            with self._open() as connection:
                connection.execute("""
                    SELECT e.event_sequence, e.record_type, e.received_monotonic_ns,
                           e.received_unix_ns, b.body::VARCHAR,
                           e.producer_id, e.producer_sequence, p.producer_id
                    FROM events e LEFT JOIN record_bodies b USING(event_sequence)
                    LEFT JOIN producers p ON p.producer_id=e.producer_id
                    ORDER BY e.event_sequence
                """)
                while rows := connection.fetchmany(4096):
                    for (
                        sequence,
                        kind,
                        monotonic,
                        unix,
                        body,
                        producer,
                        local,
                        known,
                    ) in rows:
                        if sequence != expected:
                            raise CaptureDecodeError("capture sequence discontinuity")
                        if body is None:
                            raise CaptureDecodeError(
                                f"missing event body at sequence {sequence}"
                            )
                        if known is None or local != producer_sequences.get(
                            producer, 0
                        ):
                            raise CaptureDecodeError(
                                "capture producer sequence discontinuity"
                            )
                        producer_sequences[producer] = local + 1
                        record = decode_record(
                            {
                                "schema_version": SCHEMA_VERSION,
                                "sequence": sequence,
                                "record_type": kind,
                                "host_monotonic_ns": monotonic,
                                "host_unix_ns": unix,
                                **json.loads(body),
                            }
                        )
                        if isinstance(record, CaptureStarted):
                            if started or expected != 0:
                                raise CaptureLifecycleError(
                                    "duplicate or misplaced capture start"
                                )
                            started = True
                        elif not started:
                            raise CaptureLifecycleError(
                                "first event must start capture"
                            )
                        if stopped:
                            raise CaptureLifecycleError(
                                "event follows terminal capture event"
                            )
                        if isinstance(record, LaunchConfiguration) and expected != 1:
                            raise CaptureLifecycleError(
                                "launch configuration must be second"
                            )
                        if isinstance(record, SegmentStarted):
                            if record.segment_id in open_segments:
                                raise CaptureLifecycleError("duplicate active segment")
                            open_segments.append(record.segment_id)
                        elif isinstance(record, SegmentStopped):
                            if (
                                not open_segments
                                or open_segments[-1] != record.segment_id
                            ):
                                raise CaptureLifecycleError(
                                    "segment closure is out of order"
                                )
                            open_segments.pop()
                        elif isinstance(record, CaptureStopped):
                            if open_segments:
                                raise CaptureLifecycleError(
                                    "terminal event has open segments"
                                )
                            stopped = True
                        expected += 1
                        yield record
        except duckdb.Error as exc:
            raise CaptureDecodeError(f"invalid capture database: {exc}") from exc
        if not expected:
            raise CaptureLifecycleError("capture has no start event")


def recover_capture(source: Path, destination: Path) -> Path:
    """Recover a crashed database into a new copy, leaving its source untouched.

    Copy the database and its WAL together while no writer owns them. Opening the
    copy read/write replays committed WAL transactions; a checkpoint then produces
    an independently readable file. This operation never appends a terminal event.
    """
    source, destination = Path(source), Path(destination)
    wal = source.with_name(source.name + ".wal")
    target_wal = destination.with_name(destination.name + ".wal")
    if target_wal.exists():
        raise FileExistsError(target_wal)
    with source.open("rb") as input_file, destination.open("xb") as output:
        shutil.copyfileobj(input_file, output)
    if wal.exists():
        with target_wal.open("xb") as output, wal.open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
    try:
        with duckdb.connect(str(destination)) as connection:
            rows = connection.execute("SELECT schema_version FROM capture").fetchall()
            if len(rows) != 1 or rows[0][0] != SCHEMA_VERSION:
                raise CaptureDecodeError("unsupported capture database schema")
            connection.execute("CHECKPOINT")
    except duckdb.Error as exc:
        raise CaptureDecodeError(f"capture recovery failed: {exc}") from exc
    return destination
