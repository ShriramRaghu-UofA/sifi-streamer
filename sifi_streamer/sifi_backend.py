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
    """Own exactly one entered handle and its authoritative capture."""

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
        try:
            if self._capture_started:
                self._handle.stop_capture(reason)
                self._capture_started = False
        finally:
            if self._entered:
                self._handle.__exit__(None, None, None)
                self._entered = False

    def start_segment(self, segment_id: str, kind: str, attributes: Attributes) -> None:
        self._handle.start_segment(segment_id, kind, dict(attributes))

    def stop_segment(self, segment_id: str, reason: str) -> None:
        self._handle.stop_segment(segment_id, reason)

    def marker(self, marker_id: str, kind: str, attributes: Attributes) -> None:
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
    if emg_sample_rate not in EMG_SAMPLE_RATES:
        choices = ", ".join(map(str, sorted(EMG_SAMPLE_RATES)))
        raise ValueError(
            f"emg_sample_rate must be one of: {choices}"
        )
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
