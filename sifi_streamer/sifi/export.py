"""Convert authoritative captures into canonical SiFi pandas tables.

This optional module deliberately understands SiFi signal packets and only the
generic capture vocabulary.  It does not interpret application-defined marker
or segment kinds.  Install ``sifi-streamer[parquet]`` before importing it.
"""

import math
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import numpy as np
import pandas as pd

from sifi_streamer.capture import (
    CaptureLogReader,
    CaptureStarted,
    CaptureStopped,
    Marker,
    RawPacket,
    Scalar,
    SegmentStarted,
    SegmentStopped,
)
from sifi_streamer.exceptions import DeviceError
from sifi_streamer.sifi.devices import (
    DEFAULT_MODALITIES,
    Modality,
    ModalitySpec,
    modalities_from_device_info,
)

SIFI_TABLE_SCHEMA_VERSION = 1
__all__ = [
    "SIFI_TABLE_SCHEMA_VERSION",
    "SiFiCaptureTables",
    "SiFiExportError",
    "export_sifi_capture_to_parquet",
    "read_sifi_capture_tables",
]
_CAPTURE_SUFFIX = ".capture.jsonl.zst"
_SIGNAL_COLUMNS = (
    "capture_id",
    "capture_file",
    "modality",
    "packet_sequence",
    "sample_index_in_packet",
    "host_monotonic_ns",
    "host_unix_ns",
    "device_time_s",
    "bridge_received_at_s",
    "reported_sample_rate_hz",
    "samples_lost",
    "status",
)


class SiFiExportError(ValueError):
    """A capture cannot be represented by the canonical SiFi table contract."""


@dataclass(frozen=True, slots=True)
class SiFiCaptureTables:
    """Canonical pandas tables extracted from one authoritative SiFi capture.

    ``signals`` is keyed by :class:`~sifi_streamer.sifi.Modality`.  Segment
    membership is determined without clock inference: a packet belongs to a
    segment when its sequence is greater than ``start_sequence`` and less than
    ``stop_sequence`` (or the stop is missing in a crash-truncated capture).
    """

    capture: pd.DataFrame
    streams: pd.DataFrame
    markers: pd.DataFrame
    segments: pd.DataFrame
    signals: Mapping[Modality, pd.DataFrame]


def _number(value: object, name: str, *, positive: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SiFiExportError(f"{name} must be a finite number or null")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        qualifier = "positive and " if positive else ""
        raise SiFiExportError(f"{name} must be {qualifier}finite")
    return result


def _integer(value: object, name: str, *, default: int | None = None) -> int | None:
    if value is None and default is not None:
        return default
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SiFiExportError(f"{name} must be a nonnegative integer or null")
    return value


def _text(value: object, name: str, *, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str):
        raise SiFiExportError(f"{name} must be a string or null")
    return value


def _attribute_column(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_]+", "_", name).strip("_").lower()
    return f"attribute_{cleaned or 'value'}"


def _scalar_kind(value: Scalar) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "string"


def _attributes_frame(
    rows: Sequence[Mapping[str, object]], attributes: Sequence[Mapping[str, Scalar]]
) -> pd.DataFrame:
    """Append deterministic nullable attribute columns to base rows."""
    names: dict[str, str] = {}
    kinds: dict[str, set[str]] = {}
    for values in attributes:
        for name, value in values.items():
            column = _attribute_column(name)
            previous = names.setdefault(column, name)
            if previous != name:
                raise SiFiExportError(
                    f"attribute names {previous!r} and {name!r} both normalize to "
                    f"{column!r}"
                )
            if (kind := _scalar_kind(value)) is not None:
                kinds.setdefault(column, set()).add(kind)
    frame = pd.DataFrame(rows)
    for column, original in sorted(names.items()):
        column_kinds = kinds.get(column, set())
        if column_kinds <= {"int", "float"}:
            dtype = "Float64" if "float" in column_kinds else "Int64"
        elif len(column_kinds) <= 1:
            dtype = {
                "bool": "boolean",
                "string": "string",
            }.get(next(iter(column_kinds), ""), "object")
        else:
            rendered = ", ".join(sorted(column_kinds))
            raise SiFiExportError(
                f"attribute {original!r} has incompatible scalar types: {rendered}"
            )
        values = [item.get(original) for item in attributes]
        frame[column] = pd.array(values, dtype=dtype)
    return frame


def _empty_frame(columns: tuple[str, ...]) -> pd.DataFrame:
    return pd.DataFrame({column: pd.Series(dtype="object") for column in columns})


def _typed_columns(
    frame: pd.DataFrame,
    *,
    integers: tuple[str, ...] = (),
    floats: tuple[str, ...] = (),
    strings: tuple[str, ...] = (),
) -> pd.DataFrame:
    """Apply stable nullable dtypes without losing nanosecond precision."""
    for column in integers:
        frame[column] = pd.array(frame[column], dtype="Int64")
    for column in floats:
        frame[column] = pd.array(frame[column], dtype="Float64")
    for column in strings:
        frame[column] = pd.array(frame[column], dtype="string")
    return frame


def _device_modalities(
    packet: Mapping[str, object],
) -> dict[Modality, ModalitySpec] | None:
    root = packet.get("info")
    if not isinstance(root, Mapping) or "device" not in root:
        return None
    try:
        modalities = modalities_from_device_info(packet)
    except (DeviceError, TypeError, ValueError) as exc:
        raise SiFiExportError("captured SiFi device info is invalid") from exc
    return dict(modalities.enabled())


def _modality(value: object) -> Modality | None:
    if not isinstance(value, str):
        return None
    try:
        return Modality(value)
    except ValueError:
        return None


def _packet_rows(
    record: RawPacket,
    modality: Modality,
    spec: ModalitySpec,
    capture_id: str,
    capture_file: str,
) -> tuple[list[dict[str, object]], float | None]:
    packet = record.packet
    timestamps, data = packet.get("timestamps"), packet.get("data")
    prefix = f"raw_packet sequence {record.sequence} ({modality.value})"
    if not isinstance(timestamps, list) or not isinstance(data, Mapping):
        raise SiFiExportError(f"{prefix} requires timestamp and data arrays")
    channel_values: dict[str, list[object]] = {}
    for channel in spec.channels:
        values = data.get(channel)
        if not isinstance(values, list):
            raise SiFiExportError(f"{prefix} is missing channel {channel!r}")
        if len(values) != len(timestamps):
            raise SiFiExportError(
                f"{prefix} channel {channel!r} has {len(values)} values for "
                f"{len(timestamps)} timestamps"
            )
        channel_values[channel] = values

    received_at = _number(packet.get("received_at"), f"{prefix} received_at")
    reported_rate = _number(
        packet.get("sample_rate"), f"{prefix} sample_rate", positive=True
    )
    samples_lost = _integer(
        packet.get("samples_lost"), f"{prefix} samples_lost", default=0
    )
    status = _text(packet.get("status"), f"{prefix} status", default="ok")
    rows: list[dict[str, object]] = []
    for sample_index, timestamp in enumerate(timestamps):
        device_time = _number(timestamp, f"{prefix} timestamp {sample_index}")
        if device_time is None:
            raise SiFiExportError(f"{prefix} timestamp {sample_index} cannot be null")
        row: dict[str, object] = {
            "capture_id": capture_id,
            "capture_file": capture_file,
            "modality": modality.value,
            "packet_sequence": record.sequence,
            "sample_index_in_packet": sample_index,
            "host_monotonic_ns": record.host_monotonic_ns,
            "host_unix_ns": record.host_unix_ns,
            "device_time_s": device_time,
            "bridge_received_at_s": received_at,
            "reported_sample_rate_hz": reported_rate,
            "samples_lost": samples_lost,
            "status": status,
        }
        for channel, values in channel_values.items():
            value = values[sample_index]
            row[channel] = _number(value, f"{prefix} {channel}[{sample_index}]")
        rows.append(row)
    return rows, reported_rate


def _signal_frame(rows: list[dict[str, object]], spec: ModalitySpec) -> pd.DataFrame:
    columns = (*_SIGNAL_COLUMNS, *spec.channels)
    frame = pd.DataFrame(rows, columns=columns) if rows else _empty_frame(columns)
    _typed_columns(
        frame,
        integers=(
            "packet_sequence",
            "sample_index_in_packet",
            "host_monotonic_ns",
            "host_unix_ns",
            "samples_lost",
        ),
        floats=(
            "device_time_s",
            "bridge_received_at_s",
            "reported_sample_rate_hz",
        ),
        strings=("capture_id", "capture_file", "modality", "status"),
    )
    for channel in spec.channels:
        frame[channel] = pd.array(frame[channel], dtype="Float32")
    return frame


def _rate(
    modality: Modality,
    declared: Mapping[Modality, ModalitySpec],
    reported: Mapping[Modality, float],
) -> tuple[float, str]:
    if modality in declared:
        expected = float(declared[modality].sample_rate)
        if modality in reported and not math.isclose(
            expected, reported[modality], rel_tol=1e-6
        ):
            raise SiFiExportError(
                f"{modality.value} reports {reported[modality]:g} Hz but captured "
                f"device info declares {expected:g} Hz"
            )
        return expected, "device_info"
    if modality in reported:
        return reported[modality], "packet"
    return float(DEFAULT_MODALITIES.require(modality).sample_rate), "default"


def read_sifi_capture_tables(source: Path) -> SiFiCaptureTables:
    """Read one capture into canonical, synchronized SiFi pandas tables."""
    source = Path(source)
    capture_start: CaptureStarted | None = None
    capture_stop: CaptureStopped | None = None
    declared: dict[Modality, ModalitySpec] = {}
    observed: set[Modality] = set()
    reported_rates: dict[Modality, float] = {}
    raw_packets: list[tuple[RawPacket, Modality]] = []
    marker_rows: list[dict[str, object]] = []
    marker_attributes: list[Mapping[str, Scalar]] = []
    segment_rows: list[dict[str, object]] = []
    segment_attributes: list[Mapping[str, Scalar]] = []
    open_segments: dict[str, int] = {}

    for record in CaptureLogReader(source):
        match record:
            case CaptureStarted():
                capture_start = record
            case CaptureStopped():
                capture_stop = record
            case Marker():
                marker_rows.append(
                    {
                        "marker_id": record.marker_id,
                        "marker_kind": record.marker_kind,
                        "sequence": record.sequence,
                        "host_monotonic_ns": record.host_monotonic_ns,
                        "host_unix_ns": record.host_unix_ns,
                        "source_time_ns": record.source_time_ns,
                        "source_clock": record.source_clock,
                    }
                )
                marker_attributes.append(record.attributes)
            case SegmentStarted():
                open_segments[record.segment_id] = len(segment_rows)
                segment_rows.append(
                    {
                        "segment_id": record.segment_id,
                        "segment_kind": record.segment_kind,
                        "start_sequence": record.sequence,
                        "start_host_monotonic_ns": record.host_monotonic_ns,
                        "start_host_unix_ns": record.host_unix_ns,
                        "stop_sequence": None,
                        "stop_host_monotonic_ns": None,
                        "stop_host_unix_ns": None,
                        "stop_reason": None,
                    }
                )
                segment_attributes.append(record.attributes)
            case SegmentStopped():
                row = segment_rows[open_segments.pop(record.segment_id)]
                row.update(
                    stop_sequence=record.sequence,
                    stop_host_monotonic_ns=record.host_monotonic_ns,
                    stop_host_unix_ns=record.host_unix_ns,
                    stop_reason=record.reason,
                )
            case RawPacket():
                if (device_specs := _device_modalities(record.packet)) is not None:
                    if declared and declared != device_specs:
                        raise SiFiExportError(
                            "capture contains conflicting SiFi device-info documents"
                        )
                    declared = device_specs
                modality = _modality(record.packet.get("packet_type"))
                if modality is not None:
                    observed.add(modality)
                    raw_packets.append((record, modality))

    if capture_start is None:
        raise SiFiExportError("capture has no capture_started record")
    available = set(declared) | observed
    if not available:
        raise SiFiExportError(
            "capture contains neither SiFi stream metadata nor SiFi signal packets"
        )

    signal_rows: dict[Modality, list[dict[str, object]]] = {
        modality: [] for modality in available
    }
    for record, modality in raw_packets:
        spec = declared.get(modality, DEFAULT_MODALITIES.require(modality))
        rows, packet_rate = _packet_rows(
            record, modality, spec, capture_start.capture_id, source.name
        )
        signal_rows[modality].extend(rows)
        if packet_rate is not None:
            previous = reported_rates.setdefault(modality, packet_rate)
            if not math.isclose(previous, packet_rate, rel_tol=1e-6):
                raise SiFiExportError(
                    f"{modality.value} packets report inconsistent sample rates: "
                    f"{previous:g} and {packet_rate:g} Hz"
                )

    stream_rows: list[dict[str, object]] = []
    signals: dict[Modality, pd.DataFrame] = {}
    for modality in Modality:
        if modality not in available:
            continue
        spec = declared.get(modality, DEFAULT_MODALITIES.require(modality))
        nominal_rate, rate_source = _rate(modality, declared, reported_rates)
        for index, channel in enumerate(spec.channels):
            stream_rows.append(
                {
                    "modality": modality.value,
                    "channel_id": channel,
                    "channel_index": index,
                    "dtype": np.dtype(spec.dtype).name,
                    "nominal_rate_hz": nominal_rate,
                    "rate_source": rate_source,
                }
            )
        signals[modality] = _signal_frame(signal_rows[modality], spec)

    capture_row: dict[str, object] = {
        "table_schema_version": SIFI_TABLE_SCHEMA_VERSION,
        "capture_id": capture_start.capture_id,
        "capture_file": source.name,
        "capture_schema_version": capture_start.schema_version,
        "start_sequence": capture_start.sequence,
        "start_host_monotonic_ns": capture_start.host_monotonic_ns,
        "start_host_unix_ns": capture_start.host_unix_ns,
        "stop_sequence": capture_stop.sequence if capture_stop else None,
        "stop_host_monotonic_ns": capture_stop.host_monotonic_ns
        if capture_stop
        else None,
        "stop_host_unix_ns": capture_stop.host_unix_ns if capture_stop else None,
        "stop_reason": capture_stop.reason if capture_stop else None,
    }
    capture = _attributes_frame([capture_row], [capture_start.attributes])
    _typed_columns(
        capture,
        integers=(
            "table_schema_version",
            "capture_schema_version",
            "start_sequence",
            "start_host_monotonic_ns",
            "start_host_unix_ns",
            "stop_sequence",
            "stop_host_monotonic_ns",
            "stop_host_unix_ns",
        ),
        strings=("capture_id", "capture_file", "stop_reason"),
    )
    markers = _attributes_frame(marker_rows, marker_attributes)
    if markers.empty:
        markers = _empty_frame(
            (
                "marker_id",
                "marker_kind",
                "sequence",
                "host_monotonic_ns",
                "host_unix_ns",
                "source_time_ns",
                "source_clock",
            )
        )
    _typed_columns(
        markers,
        integers=(
            "sequence",
            "host_monotonic_ns",
            "host_unix_ns",
            "source_time_ns",
        ),
        strings=("marker_id", "marker_kind", "source_clock"),
    )
    segments = _attributes_frame(segment_rows, segment_attributes)
    if segments.empty:
        segments = _empty_frame(
            (
                "segment_id",
                "segment_kind",
                "start_sequence",
                "start_host_monotonic_ns",
                "start_host_unix_ns",
                "stop_sequence",
                "stop_host_monotonic_ns",
                "stop_host_unix_ns",
                "stop_reason",
            )
        )
    _typed_columns(
        segments,
        integers=(
            "start_sequence",
            "start_host_monotonic_ns",
            "start_host_unix_ns",
            "stop_sequence",
            "stop_host_monotonic_ns",
            "stop_host_unix_ns",
        ),
        strings=("segment_id", "segment_kind", "stop_reason"),
    )
    streams = _typed_columns(
        pd.DataFrame(stream_rows),
        integers=("channel_index",),
        floats=("nominal_rate_hz",),
        strings=("modality", "channel_id", "dtype", "rate_source"),
    )
    return SiFiCaptureTables(
        capture,
        streams,
        markers,
        segments,
        MappingProxyType(signals),
    )


def _default_output(source: Path) -> Path:
    name = source.name
    if name.endswith(_CAPTURE_SUFFIX):
        base = name[: -len(_CAPTURE_SUFFIX)]
    else:
        base = source.stem
    return source.with_name(f"{base}.parquet")


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _write_tables(tables: SiFiCaptureTables, destination: Path) -> None:
    destination.mkdir()
    tables.capture.to_parquet(destination / "capture.parquet", index=False)
    tables.streams.to_parquet(destination / "streams.parquet", index=False)
    tables.markers.to_parquet(destination / "markers.parquet", index=False)
    tables.segments.to_parquet(destination / "segments.parquet", index=False)
    signal_directory = destination / "signals"
    signal_directory.mkdir()
    for modality, frame in tables.signals.items():
        frame.to_parquet(signal_directory / f"{modality.value}.parquet", index=False)


def export_sifi_capture_to_parquet(
    source: Path, output: Path | None = None, *, force: bool = False
) -> Path:
    """Write a canonical SiFi Parquet dataset and return its directory.

    The dataset is prepared in a temporary sibling directory.  Existing output
    is refused unless ``force`` is true, and failed writes leave it untouched.
    """
    source = Path(source)
    destination = Path(output) if output is not None else _default_output(source)
    if source.resolve() == destination.resolve():
        raise ValueError("output dataset must not replace the source capture")
    if destination.exists() and not force:
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tables = read_sifi_capture_tables(source)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    backup: Path | None = None
    try:
        _remove_path(temporary)
        _write_tables(tables, temporary)
        if destination.exists():
            backup = Path(
                tempfile.mkdtemp(
                    prefix=f".{destination.name}.backup.", dir=destination.parent
                )
            )
            _remove_path(backup)
            destination.replace(backup)
        try:
            temporary.replace(destination)
        except BaseException:
            if backup is not None and not destination.exists():
                backup.replace(destination)
                backup = None
            raise
        if backup is not None:
            _remove_path(backup)
        return destination
    finally:
        _remove_path(temporary)
