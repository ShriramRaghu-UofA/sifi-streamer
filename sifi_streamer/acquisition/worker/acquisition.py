"""One device polling thread, with timestamps captured before queueing."""

import threading
import time
from collections.abc import Callable

from sifi_streamer.acquisition.devices import AcquisitionDevice, AcquisitionPacket
from sifi_streamer.exceptions import DeviceError


class AcquisitionThread(threading.Thread):
    """Poll until requested to stop; every unhandled failure is retained."""

    def __init__(
        self,
        device: AcquisitionDevice,
        on_packet: Callable[[AcquisitionPacket, int, int], None],
        stop_event: threading.Event,
    ) -> None:
        super().__init__(name="device-poll", daemon=True)
        self._device = device
        self._on_packet = on_packet
        self._stop_event = stop_event
        self.failure: BaseException | None = None

    def run(self) -> None:
        try:
            while not self._stop_event.is_set():
                packet = self._device.read_packet()
                monotonic_ns, unix_ns = time.perf_counter_ns(), time.time_ns()
                self._on_packet(packet, monotonic_ns, unix_ns)
        except DeviceError as exc:
            if not self._stop_event.is_set():
                self.failure = exc
        except BaseException as exc:
            self.failure = exc
