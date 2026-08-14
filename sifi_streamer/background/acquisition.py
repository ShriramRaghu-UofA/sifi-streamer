"""Device polling thread."""

import contextlib
import threading
from collections.abc import Callable

from sifi_streamer.devices import SiFiDevice, SiFiPacket
from sifi_streamer.exceptions import DeviceError


class AcquisitionThread(threading.Thread):
    """Poll one worker-owned device and deliver packets to a callback.

    Expected device and operating-system I/O errors end the thread quietly; the
    worker command loop remains responsible for coordinated teardown.

    Args:
        device: Connected here and disconnected when the thread exits.
        on_packet: Callback invoked serially for every successfully read packet.
        stop_event: Cooperative shutdown event owned by the worker process.
    """

    def __init__(
        self,
        device: SiFiDevice,
        on_packet: Callable[[SiFiPacket], None],
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name="sifi-acquisition")
        self._device, self._on_packet, self._stop = device, on_packet, stop_event

    def run(self) -> None:
        """Connect, poll until stopped or failed, and always disconnect."""
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
