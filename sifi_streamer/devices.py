"""Model SiFi modalities, packets, device protocols, and device implementations.

Live acquisition is intentionally SiFi-shaped: each known modality has a fixed
slot and channel layout.  Consumers can inject any structural :class:`SiFiDevice`
implementation, including :class:`SyntheticSiFiDevice` for hardware-free runs.
"""

import contextlib
import json
import socket
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt

from sifi_streamer.exceptions import DeviceError

type StreamId = str


def _identifier(value: str, name: str) -> str:
    if not value or value != value.strip() or len(value) > 128:
        raise ValueError(
            f"{name} must be a non-empty trimmed string of at most 128 characters"
        )
    return value


@dataclass(frozen=True, slots=True)
class SignalChannelSpec:
    """Display and identity metadata for one signal column."""

    channel_id: str
    label: str | None = None
    unit: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.channel_id, "channel_id")


@dataclass(frozen=True, slots=True)
class SignalStreamSpec:
    """Fixed live layout declared by an injected acquisition device."""

    stream_id: StreamId
    channels: tuple[SignalChannelSpec, ...]
    nominal_rate_hz: float
    dtype: npt.DTypeLike = np.float32
    label: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.stream_id, "stream_id")
        if not self.channels or len({item.channel_id for item in self.channels}) != len(
            self.channels
        ):
            raise ValueError("stream channels must be non-empty and unique")
        if not np.isfinite(self.nominal_rate_hz) or self.nominal_rate_hz <= 0:
            raise ValueError("nominal_rate_hz must be finite and positive")
        dtype = np.dtype(self.dtype)
        if dtype.hasobject or dtype.fields is not None:
            raise ValueError("stream dtype must be a non-object scalar dtype")

    @property
    def n_channels(self) -> int:
        return len(self.channels)

    @property
    def numpy_dtype(self) -> np.dtype:
        return np.dtype(self.dtype)


@runtime_checkable
class AcquisitionPacket(Protocol):
    """One raw document with an optional contribution to one live stream."""

    @property
    def stream_id(self) -> StreamId | None: ...

    @property
    def timestamps(self) -> Sequence[float]: ...

    @property
    def data(self) -> Mapping[str, Sequence[float | int | None]]: ...

    @property
    def reported_rate_hz(self) -> float | None: ...

    @property
    def samples_lost(self) -> int: ...

    @property
    def status(self) -> str: ...

    def capture_document(self) -> dict[str, object] | None:
        """Return the raw document, or ``None`` for an adapter-only contribution."""
        ...


@runtime_checkable
class AcquisitionDevice(Protocol):
    """Generic injected device with streams fixed after connection."""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def read_packet(self) -> AcquisitionPacket: ...

    @property
    def streams(self) -> tuple[SignalStreamSpec, ...]: ...

    @property
    def device_info(self) -> dict[str, object] | None: ...


@dataclass(frozen=True, slots=True)
class ModalitySpec:
    """Static layout of one enabled signal modality.

    Attributes:
        channels: Ordered channel names used to build sample matrices.
        sample_rate: Nominal samples per second.
        dtype: NumPy-compatible scalar payload type.
    """

    channels: tuple[str, ...]
    sample_rate: int
    dtype: npt.DTypeLike = np.float32

    @property
    def n_channels(self) -> int:
        """Return the number of channels in the modality."""
        return len(self.channels)

    @property
    def numpy_dtype(self) -> np.dtype:
        """Return ``dtype`` normalized as a NumPy dtype."""
        return np.dtype(self.dtype)

    def as_stream(
        self, stream_id: StreamId, *, label: str | None = None
    ) -> SignalStreamSpec:
        """Return the generic stream representation of this SiFi modality."""
        return SignalStreamSpec(
            stream_id,
            tuple(SignalChannelSpec(channel) for channel in self.channels),
            float(self.sample_rate),
            self.dtype,
            label,
        )


class Modality(StrEnum):
    """Signal modality identifiers emitted by SiFi packet documents."""

    EMG = "emg_armband"
    IMU = "imu"
    ECG = "ecg"
    EDA = "eda"
    PPG = "ppg"
    TEMPERATURE = "temperature"


@dataclass(frozen=True, slots=True)
class Modalities[T]:
    """Fixed-shape optional values keyed by :class:`Modality`.

    A ``None`` slot means that the modality is disabled or unavailable. Instances
    are immutable; :meth:`with_value` returns a modified copy.

    Attributes:
        emg: Value associated with :attr:`Modality.EMG`.
        imu: Value associated with :attr:`Modality.IMU`.
        ecg: Value associated with :attr:`Modality.ECG`.
        eda: Value associated with :attr:`Modality.EDA`.
        ppg: Value associated with :attr:`Modality.PPG`.
        temperature: Value associated with :attr:`Modality.TEMPERATURE`.
    """

    emg: T | None = None
    imu: T | None = None
    ecg: T | None = None
    eda: T | None = None
    ppg: T | None = None
    temperature: T | None = None

    def get(self, modality: Modality) -> T | None:
        """Return the value for ``modality``, or ``None`` when disabled."""
        return getattr(self, "emg" if modality is Modality.EMG else modality.value)

    def require(self, modality: Modality) -> T:
        """Return an enabled value.

        Raises:
            LookupError: If ``modality`` is disabled.
        """
        if (value := self.get(modality)) is None:
            raise LookupError(f"{modality.value} is not enabled")
        return value

    def with_value(self, modality: Modality, value: T) -> Modalities[T]:
        """Return a copy with ``value`` assigned to ``modality``."""
        return replace(
            self, **{"emg" if modality is Modality.EMG else modality.value: value}
        )

    def enabled(self) -> Iterator[tuple[Modality, T]]:
        """Yield enabled ``(modality, value)`` pairs in enum order."""
        for modality in Modality:
            if (value := self.get(modality)) is not None:
                yield modality, value

    @classmethod
    def from_enabled(cls, values: Iterator[tuple[Modality, T]]) -> Modalities[T]:
        """Build an instance from enabled ``(modality, value)`` pairs.

        Later duplicate modalities replace earlier values.
        """
        result = cls()
        for modality, value in values:
            result = result.with_value(modality, value)
        return result


DEFAULT_MODALITIES = Modalities(
    emg=ModalitySpec(tuple(f"emg{i}" for i in range(8)), 1600),
    imu=ModalitySpec(("ax", "ay", "az", "qw", "qx", "qy", "qz"), 100),
    ecg=ModalitySpec(("ecg",), 500),
    eda=ModalitySpec(("eda",), 50),
    ppg=ModalitySpec(("ir", "r", "g", "b"), 50),
    temperature=ModalitySpec(("temperature",), 1),
)
SIGNAL_MODALITIES = tuple(Modality)


def streams_from_modalities(
    modalities: Modalities[ModalitySpec],
) -> tuple[SignalStreamSpec, ...]:
    """Convert the legacy fixed SiFi layout into the generic ordered registry."""
    return tuple(
        spec.as_stream(modality.value) for modality, spec in modalities.enabled()
    )


def modalities_from_device_info(info: Mapping[str, object]) -> Modalities[ModalitySpec]:
    """Derive enabled modality layouts from a bridge ``info`` document.

    EMG, ECG, EDA, IMU, and temperature use the bridge ``fs`` value. PPG uses
    ``sps / avg``. Channel names come from :data:`DEFAULT_MODALITIES`.

    Raises:
        DeviceError: If the document shape is invalid or contains no enabled
            signal modalities.
    """
    root = info.get("info", info)
    if not isinstance(root, Mapping):
        raise DeviceError("Bridge info is invalid")
    device = root.get("device", root)
    if not isinstance(device, Mapping):
        raise DeviceError("Bridge device info is invalid")
    result: Modalities[ModalitySpec] = Modalities()
    for modality, name in (
        (Modality.EMG, "emg"),
        (Modality.ECG, "ecg"),
        (Modality.EDA, "eda"),
        (Modality.IMU, "imu"),
        (Modality.TEMPERATURE, "temperature"),
    ):
        values = device.get(name, {})
        if (
            isinstance(values, Mapping)
            and values.get("enabled", True)
            and values.get("fs")
        ):
            result = result.with_value(
                modality,
                ModalitySpec(
                    DEFAULT_MODALITIES.require(modality).channels,
                    round(float(values["fs"])),
                ),
            )
    ppg = device.get("ppg", {})
    if (
        isinstance(ppg, Mapping)
        and ppg.get("enabled", True)
        and ppg.get("sps")
        and ppg.get("avg")
    ):
        result = result.with_value(
            Modality.PPG,
            ModalitySpec(
                DEFAULT_MODALITIES.require(Modality.PPG).channels,
                round(float(ppg["sps"]) / float(ppg["avg"])),
            ),
        )
    if not tuple(result.enabled()):
        raise DeviceError("Bridge info did not contain enabled signal modalities")
    return result


@dataclass(slots=True)
class SiFiPacket:
    """One packet read from a SiFi-compatible device.

    Attributes:
        packet_type: Vendor packet type; known signal types map to a modality.
        timestamps: Per-sample source timestamps.
        data: Channel names mapped to sample values.
        received_at: Host wall-clock time at receipt, in seconds.
        sample_rate: Optional sample rate reported by the packet.
        samples_lost: Device-reported lost-sample count.
        status: Device-reported packet status.
        document: Original decoded JSON object, retained for authoritative capture.
    """

    packet_type: str
    timestamps: list[float]
    data: dict[str, list[float]]
    received_at: float
    sample_rate: float | None = None
    samples_lost: int = 0
    status: str = "ok"
    document: dict[str, object] | None = None

    @property
    def modality(self) -> Modality | None:
        """Return the known signal modality, or ``None`` for other packets."""
        try:
            return Modality(self.packet_type)
        except ValueError:
            return None

    @property
    def stream_id(self) -> StreamId | None:
        """Return the generic stream identifier for known and custom packet types."""
        return self.packet_type or None

    @property
    def reported_rate_hz(self) -> float | None:
        """Return the optional packet-reported sample rate."""
        return self.sample_rate

    def capture_document(self) -> dict[str, object]:
        """Return the complete document that should be written to a capture.

        The original document is returned unchanged when available. Programmatic
        packets receive a document assembled from their typed fields.
        """
        return (
            self.document
            if self.document is not None
            else {
                "packet_type": self.packet_type,
                "timestamps": self.timestamps,
                "data": self.data,
                "received_at": self.received_at,
                "sample_rate": self.sample_rate,
                "samples_lost": self.samples_lost,
                "status": self.status,
            }
        )


@runtime_checkable
class SiFiDevice(Protocol):
    """Structural interface owned by the background acquisition worker."""

    def connect(self) -> None:
        """Acquire transport resources and prepare packet streaming."""
        ...

    def disconnect(self) -> None:
        """Stop streaming and release all transport resources."""
        ...

    def read_packet(self) -> SiFiPacket:
        """Block until and return the next valid packet."""
        ...

    @property
    def modalities(self) -> Modalities[ModalitySpec]:
        """Return layouts for all enabled signal modalities."""
        ...

    @property
    def device_info(self) -> dict[str, object] | None:
        """Return optional vendor device metadata."""
        ...


@runtime_checkable
class PacketReader(Protocol):
    """Minimal packet transport used by :class:`SiFiBridgeDevice`."""

    def connect(self) -> None:
        """Open the packet transport."""
        ...

    def disconnect(self) -> None:
        """Close the packet transport."""
        ...

    def read_packet(self) -> SiFiPacket:
        """Return the next valid packet."""
        ...


type DeviceFactory = Callable[[], AcquisitionDevice | SiFiDevice]


class _BinaryLineReader(Protocol):
    """Private structural interface for a binary newline reader."""

    def readline(self) -> bytes:
        """Read through the next newline or end of stream."""
        ...

    def close(self) -> None:
        """Release the underlying stream."""
        ...


def packet_from_json_line(line: str | bytes) -> SiFiPacket | None:
    """Parse one bridge JSON line into a packet.

    Returns ``None`` for malformed JSON or non-object JSON. Missing packet fields
    receive conservative defaults, and the original object is retained on the
    resulting packet.
    """
    try:
        raw = json.loads(line)
    except json.JSONDecodeError, UnicodeDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return SiFiPacket(
        str(raw.get("packet_type", "invalid")),
        list(raw.get("timestamps") or []),
        dict(raw.get("data") or {}),
        float(raw.get("received_at", time.time())),
        raw.get("sample_rate"),
        int(raw.get("samples_lost", 0)),
        str(raw.get("status", "ok")),
        raw,
    )


class SiFiBandDevice:
    """Read newline-delimited SiFi packet JSON from a TCP endpoint.

    This low-level device assumes the default modality layout and does not manage
    or configure a bridge process. Use :class:`~sifi_streamer.SiFiBridgeDevice`
    when the package should own the vendor bridge.

    Args:
        host: Interface or host serving packet data.
        port: TCP port serving packet data.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 5000) -> None:
        self._host, self._port = host, port
        self._sock: socket.socket | None = None
        self._file: _BinaryLineReader | None = None

    @property
    def modalities(self) -> Modalities[ModalitySpec]:
        """Return the default SiFi modality layouts."""
        return DEFAULT_MODALITIES

    @property
    def streams(self) -> tuple[SignalStreamSpec, ...]:
        """Return the generic stream registry for the default SiFi layout."""
        return streams_from_modalities(self.modalities)

    @property
    def device_info(self) -> None:
        """Return ``None`` because the raw TCP stream has no info handshake."""
        return None

    def connect(self) -> None:
        """Connect to the configured TCP endpoint; repeated calls are no-ops."""
        if self._file is not None:
            return
        try:
            self._sock = socket.create_connection((self._host, self._port))
            self._file = self._sock.makefile("rb")
        except OSError as exc:
            raise DeviceError(
                f"SiFiBandDevice: cannot connect to {self._host}:{self._port}: {exc}"
            ) from exc

    def disconnect(self) -> None:
        """Close the socket and file wrapper; repeated calls are safe."""
        for resource in (self._file, self._sock):
            if resource is not None:
                with contextlib.suppress(OSError):
                    resource.close()
        self._file = None
        self._sock = None

    def read_packet(self) -> SiFiPacket:
        """Return the next decodable packet, skipping malformed lines.

        Raises:
            DeviceError: If called before connection or the socket fails/closes.
        """
        if self._file is None:
            raise DeviceError("SiFiBandDevice.read_packet() called before connect()")
        while True:
            try:
                line = self._file.readline()
            except OSError as exc:
                raise DeviceError(f"SiFiBandDevice: TCP receive failed: {exc}") from exc
            if not line:
                raise DeviceError("SiFiBandDevice: TCP connection closed by remote")
            if packet := packet_from_json_line(line.rstrip(b"\r\n")):
                return packet


class SyntheticSiFiDevice:
    """Deterministic hardware-free SiFi device that produces EMG packets.

    The device advertises the default modality layout but currently emits one
    eight-channel sinusoidal EMG sample per :meth:`read_packet` call.

    Args:
        emg_sample_rate: Positive generated EMG rate in samples per second.
        amplitude: Peak amplitude of generated channel sinusoids.
    """

    def __init__(self, emg_sample_rate: int = 1600, amplitude: float = 100.0) -> None:
        if emg_sample_rate <= 0:
            raise ValueError("emg_sample_rate must be positive")
        self._rate, self._amplitude, self._t, self._connected = (
            emg_sample_rate,
            amplitude,
            0.0,
            False,
        )

    @property
    def modalities(self) -> Modalities[ModalitySpec]:
        """Return default layouts with the configured EMG sample rate."""
        return DEFAULT_MODALITIES.with_value(
            Modality.EMG,
            ModalitySpec(DEFAULT_MODALITIES.require(Modality.EMG).channels, self._rate),
        )

    @property
    def streams(self) -> tuple[SignalStreamSpec, ...]:
        """Return the generated stream registry."""
        return streams_from_modalities(self.modalities)

    @property
    def device_info(self) -> None:
        """Return ``None`` because no physical-device metadata exists."""
        return None

    def connect(self) -> None:
        """Reset generated time to zero and enable packet reads."""
        self._t, self._connected = 0.0, True

    def disconnect(self) -> None:
        """Disable packet reads."""
        self._connected = False

    def read_packet(self) -> SiFiPacket:
        """Wait one sample interval and return the next EMG packet.

        Raises:
            DeviceError: If called before :meth:`connect`.
        """
        if not self._connected:
            raise DeviceError(
                "SyntheticSiFiDevice.read_packet() called before connect()"
            )
        time.sleep(1 / self._rate)
        self._t += 1 / self._rate
        values = (
            self._amplitude * np.sin(2 * np.pi * np.linspace(5, 40, 8) * self._t)
        ).tolist()
        return SiFiPacket(
            Modality.EMG,
            [self._t],
            {
                name: [value]
                for name, value in zip(
                    DEFAULT_MODALITIES.require(Modality.EMG).channels,
                    values,
                    strict=True,
                )
            },
            time.time(),
            float(self._rate),
        )
