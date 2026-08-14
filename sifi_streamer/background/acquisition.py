"""Device polling thread."""

import contextlib
import logging
import threading
from collections.abc import Callable

from sifi_streamer.devices import (
    AcquisitionDevice,
    AcquisitionPacket,
    SiFiDevice,
)
from sifi_streamer.exceptions import DeviceError

logger = logging.getLogger(__name__)


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
        device: AcquisitionDevice | SiFiDevice,
        on_packet: Callable[[AcquisitionPacket], None],
        stop_event: threading.Event,
        *,
        already_connected: bool = False,
    ) -> None:
        super().__init__(daemon=True, name="sifi-acquisition")
        self._device, self._on_packet, self._stop = device, on_packet, stop_event
        self._already_connected = already_connected
        self.failure: BaseException | None = None

    def run(self) -> None:
        """Connect, poll until stopped or failed, and always disconnect."""
        try:
            if not self._already_connected:
                self._device.connect()
            logger.info("Acquisition thread started")
            while not self._stop.is_set():
                self._on_packet(self._device.read_packet())
        except (DeviceError, OSError, RuntimeError, TypeError, ValueError) as exc:
            if not self._stop.is_set():
                self.failure = exc
                logger.exception("Acquisition thread stopped unexpectedly")
        finally:
            with contextlib.suppress(DeviceError, OSError):
                self._device.disconnect()
            logger.info("Acquisition thread stopped")
