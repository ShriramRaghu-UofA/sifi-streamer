"""Myo-style integration boundary without a dependency on a particular SDK.

Implement MyoTransport for the SDK in use. Its request_stop must unblock receive;
the device worker then calls disconnect after its polling thread finishes.
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from sifi_streamer.acquisition import SignalChannelSpec, SignalStreamSpec


class MyoTransport(Protocol):
    def connect(self) -> None: ...

    def receive(self) -> Sequence[int | None]: ...

    def request_stop(self) -> None: ...

    def disconnect(self) -> None: ...


@dataclass(frozen=True, slots=True)
class MyoPacket:
    """One SDK observation, timestamped by this adapter after receive returns."""

    timestamps: tuple[int, ...]
    data: dict[str, tuple[int | None, ...]]
    validity: dict[str, tuple[bool, ...]]
    stream_id: str = "emg"
    reported_rate_hz: float = 200.0
    samples_lost: int = 0
    status: str = "ok"

    def capture_document(self) -> None:
        # Typed samples remain authoritative without an additional raw document.
        return None


class MyoDevice:
    """Compose an SDK transport with the generic acquisition protocol."""

    def __init__(self, transport_factory: Callable[[], MyoTransport]) -> None:
        self._transport = transport_factory()

    @property
    def streams(self) -> tuple[SignalStreamSpec, ...]:
        return (
            SignalStreamSpec(
                "emg",
                tuple(SignalChannelSpec(f"emg{i}", unit="raw") for i in range(8)),
                200.0,
                dtype=np.int16,
                source_clock="adapter time.perf_counter_ns()",
                timestamp_unit="nanoseconds",
                timestamp_dtype=np.int64,
            ),
        )

    @property
    def device_info(self) -> dict[str, object]:
        return {"integration": "Myo SDK adapter example"}

    def connect(self) -> None:
        self._transport.connect()

    def request_stop(self) -> None:
        self._transport.request_stop()

    def disconnect(self) -> None:
        self._transport.disconnect()

    def read_packet(self) -> MyoPacket:
        values = tuple(self._transport.receive())
        received = time.perf_counter_ns()
        if len(values) != 8:
            raise ValueError("Myo observation must contain eight channel values")
        return MyoPacket(
            (received,),
            {f"emg{i}": (value,) for i, value in enumerate(values)},
            {f"emg{i}": (value is not None,) for i, value in enumerate(values)},
        )
