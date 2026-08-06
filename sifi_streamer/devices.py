"""SiFi modalities, packet model, real TCP reader, and synthetic device."""

import contextlib
import json
import socket
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol, runtime_checkable

import numpy as np
import numpy.typing as npt

from sifi_streamer.exceptions import DeviceError


@dataclass(frozen=True, slots=True)
class ModalitySpec:
    channels: tuple[str, ...]
    sample_rate: int
    dtype: npt.DTypeLike = np.float32

    @property
    def n_channels(self) -> int:
        return len(self.channels)

    @property
    def numpy_dtype(self) -> np.dtype:
        return np.dtype(self.dtype)


class Modality(StrEnum):
    EMG = "emg_armband"
    IMU = "imu"
    ECG = "ecg"
    EDA = "eda"
    PPG = "ppg"
    TEMPERATURE = "temperature"


@dataclass(frozen=True, slots=True)
class Modalities[T]:
    emg: T | None = None
    imu: T | None = None
    ecg: T | None = None
    eda: T | None = None
    ppg: T | None = None
    temperature: T | None = None

    def get(self, modality: Modality) -> T | None:
        return getattr(self, "emg" if modality is Modality.EMG else modality.value)

    def require(self, modality: Modality) -> T:
        if (value := self.get(modality)) is None:
            raise LookupError(f"{modality.value} is not enabled")
        return value

    def with_value(self, modality: Modality, value: T) -> Modalities[T]:
        return replace(
            self, **{"emg" if modality is Modality.EMG else modality.value: value}
        )

    def enabled(self) -> Iterator[tuple[Modality, T]]:
        for modality in Modality:
            if (value := self.get(modality)) is not None:
                yield modality, value

    @classmethod
    def from_enabled(cls, values: Iterator[tuple[Modality, T]]) -> Modalities[T]:
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


def modalities_from_device_info(info: Mapping[str, object]) -> Modalities[ModalitySpec]:
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
        try:
            return Modality(self.packet_type)
        except ValueError:
            return None

    def capture_document(self) -> dict[str, object]:
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
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def read_packet(self) -> SiFiPacket: ...
    @property
    def modalities(self) -> Modalities[ModalitySpec]: ...
    @property
    def device_info(self) -> dict[str, object] | None: ...


@runtime_checkable
class PacketReader(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def read_packet(self) -> SiFiPacket: ...


type DeviceFactory = Callable[[], SiFiDevice]


def packet_from_json_line(line: str | bytes) -> SiFiPacket | None:
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
    def __init__(self, host: str = "127.0.0.1", port: int = 5000) -> None:
        self._host, self._port, self._sock, self._file = host, port, None, None

    @property
    def modalities(self) -> Modalities[ModalitySpec]:
        return DEFAULT_MODALITIES

    @property
    def device_info(self) -> None:
        return None

    def connect(self) -> None:
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
        for resource in (self._file, self._sock):
            if resource is not None:
                with contextlib.suppress(OSError):
                    resource.close()
        self._file = self._sock = None

    def read_packet(self) -> SiFiPacket:
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
    """Deterministic multi-modal development device."""

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
        return DEFAULT_MODALITIES.with_value(
            Modality.EMG,
            ModalitySpec(DEFAULT_MODALITIES.require(Modality.EMG).channels, self._rate),
        )

    @property
    def device_info(self) -> None:
        return None

    def connect(self) -> None:
        self._t, self._connected = 0.0, True

    def disconnect(self) -> None:
        self._connected = False

    def read_packet(self) -> SiFiPacket:
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
