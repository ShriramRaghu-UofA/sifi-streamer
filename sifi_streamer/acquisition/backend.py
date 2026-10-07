"""Composition adapter from generic acquisition to capture control."""

import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path

from sifi_streamer.acquisition.config import StreamerConfig
from sifi_streamer.acquisition.devices import DeviceFactory
from sifi_streamer.acquisition.handle import BackgroundHandle
from sifi_streamer.acquisition.health import HealthThresholds
from sifi_streamer.acquisition.runtime import AcquisitionMonitor, CaptureRuntime
from sifi_streamer.capture.controller import CaptureController
from sifi_streamer.capture.events import CaptureEvent
from sifi_streamer.capture.records import (
    Attributes,
    Packet,
    Scalar,
    validate_attributes,
    validate_document,
)

logger = logging.getLogger(__name__)


class AcquisitionCaptureBackend:
    """Adapt one background acquisition handle to the capture backend protocol."""

    def __init__(
        self,
        config: StreamerConfig,
        device_factory: DeviceFactory,
        capture_file: Path,
        capture_id: str,
        attributes: Attributes | None = None,
        *,
        launch_configuration: Packet | None = None,
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
        self._launch_configuration = validate_document(
            {
                **dict(launch_configuration or {}),
                "acquisition": asdict(config),
            }
        )
        self._entered = self._capture_started = False

    @property
    def handle(self) -> BackgroundHandle:
        """Return the owned handle for composition with a read-only monitor."""
        return self._handle

    def start(self) -> None:
        """Enter the handle and start recording; repeated calls are safe."""
        if self._entered:
            return
        self._handle.prepare_capture(
            self._capture_file,
            self._capture_id,
            self._attributes,
            self._launch_configuration,
        )
        self._handle.__enter__()
        self._entered = self._capture_started = True
        logger.info("Authoritative capture started at %s", self._capture_file)

    def stop(self, reason: str = "normal_completion") -> None:
        """Stop recording and always release the background handle."""
        try:
            if self._capture_started:
                self._capture_started = False
                self._handle.stop_capture(reason)
                logger.info("Authoritative capture stopped with reason %r", reason)
        finally:
            if self._entered:
                self._entered = False
                self._handle.__exit__(None, None, None)

    def record_event(self, event: CaptureEvent) -> None:
        """Persist a report or diagnostic through the owned worker."""
        self._handle.record_event(event)

    def start_segment(self, segment_id: str, kind: str, attributes: Attributes) -> None:
        self._handle.start_segment(segment_id, kind, dict(attributes))

    def stop_segment(self, segment_id: str, reason: str) -> None:
        self._handle.stop_segment(segment_id, reason)

    def marker(self, marker_id: str, kind: str, attributes: Attributes) -> None:
        self._handle.add_marker(marker_id, kind, dict(attributes))


def create_capture_runtime(
    capture_file: Path,
    capture_id: str,
    device_factory: DeviceFactory,
    attributes: Mapping[str, Scalar] | None = None,
    *,
    config: StreamerConfig | None = None,
    thresholds: HealthThresholds | None = None,
    launch_configuration: Packet | None = None,
) -> CaptureRuntime:
    """Compose an injected device with a capture controller and live monitor."""
    backend = AcquisitionCaptureBackend(
        config or StreamerConfig(),
        device_factory,
        capture_file,
        capture_id,
        attributes,
        launch_configuration=launch_configuration,
    )
    return CaptureRuntime(
        CaptureController(backend), AcquisitionMonitor(backend.handle, thresholds)
    )
