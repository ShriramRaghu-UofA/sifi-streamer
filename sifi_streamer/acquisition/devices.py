"""Device-neutral acquisition protocols and fixed stream definitions."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt

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
        """Return the complete raw document to persist, if any."""
        ...


@runtime_checkable
class AcquisitionDevice(Protocol):
    """Injected device with a stream registry fixed after connection."""

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def read_packet(self) -> AcquisitionPacket: ...

    @property
    def streams(self) -> tuple[SignalStreamSpec, ...]: ...

    @property
    def device_info(self) -> dict[str, object] | None: ...


type DeviceFactory = Callable[[], AcquisitionDevice]
