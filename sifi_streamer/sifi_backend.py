"""Composition adapter from generic capture to SiFi acquisition."""

from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path

from sifi_streamer.bridge import EMG_SAMPLE_RATES, BridgeTransport, SiFiBridgeDevice
from sifi_streamer.capture import Attributes, Scalar, validate_attributes
from sifi_streamer.client.handle import BackgroundHandle
from sifi_streamer.config import StreamerConfig
from sifi_streamer.controller import CaptureController
from sifi_streamer.devices import DeviceFactory, SyntheticSiFiDevice


class SiFiCaptureBackend:
    """Adapt one background SiFi handle to the generic capture backend protocol.

    The backend enters exactly one :class:`BackgroundHandle`, starts exactly one
    authoritative capture on it, and releases partially acquired resources if
    startup fails.

    Args:
        config: Worker, shared-memory, and recording settings.
        device_factory: Picklable factory called inside the worker process.
        capture_file: New authoritative capture path.
        capture_id: Identifier for the capture occurrence.
        attributes: Optional scalar capture metadata.
        handle_factory: Injectable handle constructor for tests.
    """

    def __init__(
        self,
        config: StreamerConfig,
        device_factory: DeviceFactory,
        capture_file: Path,
        capture_id: str,
        attributes: Attributes | None = None,
        *,
        handle_factory: Callable[
            [StreamerConfig, DeviceFactory], BackgroundHandle
        ] = BackgroundHandle,
    ) -> None:
        self._handle = handle_factory(config, device_factory)
        self._capture_file, self._capture_id, self._attributes = (
            capture_file,
            capture_id,
            validate_attributes(attributes or {}),
        )
        self._entered = self._capture_started = False

    def start(self) -> None:
        """Enter the background handle and start recording; repeated calls are safe."""
        if self._entered:
            return
        self._handle.__enter__()
        self._entered = True
        try:
            self._handle.start_capture(
                self._capture_file, self._capture_id, self._attributes
            )
            self._capture_started = True
        except Exception:
            self._handle.__exit__(None, None, None)
            self._entered = False
            raise

    def stop(self, reason: str = "normal_completion") -> None:
        """Stop recording and always release the background handle."""
        try:
            if self._capture_started:
                self._handle.stop_capture(reason)
                self._capture_started = False
        finally:
            if self._entered:
                self._handle.__exit__(None, None, None)
                self._entered = False

    def start_segment(self, segment_id: str, kind: str, attributes: Attributes) -> None:
        """Forward a validated segment start to the background worker."""
        self._handle.start_segment(segment_id, kind, dict(attributes))

    def stop_segment(self, segment_id: str, reason: str) -> None:
        """Forward a segment stop to the background worker."""
        self._handle.stop_segment(segment_id, reason)

    def marker(self, marker_id: str, kind: str, attributes: Attributes) -> None:
        """Forward a validated marker in ``marker_id, kind`` order."""
        self._handle.add_marker(marker_id, kind, dict(attributes))


def create_sifi_capture(
    capture_file: Path,
    capture_id: str,
    attributes: Mapping[str, Scalar] | None = None,
    *,
    bridge_executable: str | Path = "bin/sifibridge.exe",
    host: str = "127.0.0.1",
    port: int = 5000,
    transport: BridgeTransport | str = BridgeTransport.TCP,
    emg_sample_rate: int = 1600,
    synthetic: bool = False,
    config: StreamerConfig | None = None,
) -> CaptureController:
    """Compose a ready-to-start controller for real or synthetic SiFi capture.

    No device, process, or output file is created until the returned controller
    is started. The synthetic path needs no bridge executable.

    Args:
        capture_file: New authoritative capture path.
        capture_id: Identifier for this capture occurrence.
        attributes: Optional scalar capture metadata.
        bridge_executable: Preinstalled vendor bridge path for hardware capture.
        host: Bridge TCP destination or local UDP bind interface.
        port: Bridge packet-output port.
        transport: Bridge packet-output transport.
        emg_sample_rate: Supported explicit EMG sample rate.
        synthetic: Use generated signals instead of the vendor bridge.
        config: Optional streamer settings; defaults to :class:`StreamerConfig`.

    Returns:
        A generic controller that owns the composed SiFi backend.

    Raises:
        ValueError: If ``emg_sample_rate`` is unsupported.
    """
    if emg_sample_rate not in EMG_SAMPLE_RATES:
        choices = ", ".join(map(str, sorted(EMG_SAMPLE_RATES)))
        raise ValueError(f"emg_sample_rate must be one of: {choices}")
    factory: DeviceFactory = (
        partial(SyntheticSiFiDevice, emg_sample_rate=emg_sample_rate)
        if synthetic
        else partial(
            SiFiBridgeDevice,
            host=host,
            port=port,
            executable=bridge_executable,
            transport=transport,
            emg_sample_rate=emg_sample_rate,
        )
    )
    return CaptureController(
        SiFiCaptureBackend(
            config or StreamerConfig(), factory, capture_file, capture_id, attributes
        )
    )
