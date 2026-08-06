"""Device polling thread."""

import contextlib
import threading
from collections.abc import Callable

from sifi_streamer.devices import SiFiDevice, SiFiPacket
from sifi_streamer.exceptions import DeviceError


class AcquisitionThread(threading.Thread):
    def __init__(
        self,
        device: SiFiDevice,
        on_packet: Callable[[SiFiPacket], None],
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name="sifi-acquisition")
        self._device, self._on_packet, self._stop = device, on_packet, stop_event

    def run(self) -> None:
        try:
            self._device.connect()
            while not self._stop.is_set():
                self._on_packet(self._device.read_packet())
        except DeviceError, OSError:
            if not self._stop.is_set():
                return
        finally:
            with contextlib.suppress(DeviceError, OSError):
                self._device.disconnect()
