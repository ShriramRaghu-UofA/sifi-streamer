"""Compose the generic acquisition stack with bundled SiFi devices."""

from collections.abc import Mapping
from functools import partial
from pathlib import Path

from sifi_streamer.acquisition.backend import (
    AcquisitionCaptureBackend,
    create_capture_runtime,
)
from sifi_streamer.acquisition.config import StreamerConfig
from sifi_streamer.acquisition.devices import DeviceFactory
from sifi_streamer.acquisition.health import HealthThresholds
from sifi_streamer.acquisition.runtime import CaptureRuntime
from sifi_streamer.capture.controller import CaptureController
from sifi_streamer.capture.records import Scalar
from sifi_streamer.sifi.bridge import BridgeTransport, SiFiBridgeDevice
from sifi_streamer.sifi.devices import SyntheticSiFiDevice
from sifi_streamer.sifi.sensor_profile import SiFiSensorProfile


def _device_factory(
    *,
    bridge_executable: str | Path,
    host: str,
    port: int,
    transport: BridgeTransport | str,
    sensor_profile: SiFiSensorProfile | None,
    synthetic: bool,
) -> DeviceFactory:
    if synthetic and sensor_profile is not None:
        raise ValueError("sensor_profile cannot be used with synthetic acquisition")
    if synthetic:
        return SyntheticSiFiDevice
    return partial(
        SiFiBridgeDevice,
        host=host,
        port=port,
        executable=bridge_executable,
        transport=transport,
        **({"sensor_profile": sensor_profile} if sensor_profile is not None else {}),
    )


def create_sifi_capture(
    capture_file: Path,
    capture_id: str,
    attributes: Mapping[str, Scalar] | None = None,
    *,
    bridge_executable: str | Path = "bin/sifibridge.exe",
    host: str = "127.0.0.1",
    port: int = 5000,
    transport: BridgeTransport | str = BridgeTransport.TCP,
    sensor_profile: SiFiSensorProfile | None = None,
    synthetic: bool = False,
    config: StreamerConfig | None = None,
) -> CaptureController:
    """Compose a ready-to-start controller for real or synthetic SiFi capture."""
    factory = _device_factory(
        bridge_executable=bridge_executable,
        host=host,
        port=port,
        transport=transport,
        sensor_profile=sensor_profile,
        synthetic=synthetic,
    )
    return CaptureController(
        AcquisitionCaptureBackend(
            config or StreamerConfig(),
            factory,
            capture_file,
            capture_id,
            attributes,
        )
    )


def create_sifi_capture_runtime(
    capture_file: Path,
    capture_id: str,
    attributes: Mapping[str, Scalar] | None = None,
    *,
    bridge_executable: str | Path = "bin/sifibridge.exe",
    host: str = "127.0.0.1",
    port: int = 5000,
    transport: BridgeTransport | str = BridgeTransport.TCP,
    sensor_profile: SiFiSensorProfile | None = None,
    synthetic: bool = False,
    config: StreamerConfig | None = None,
    thresholds: HealthThresholds | None = None,
) -> CaptureRuntime:
    """Compose the standard SiFi device with controller and monitor access."""
    factory = _device_factory(
        bridge_executable=bridge_executable,
        host=host,
        port=port,
        transport=transport,
        sensor_profile=sensor_profile,
        synthetic=synthetic,
    )
    return create_capture_runtime(
        capture_file,
        capture_id,
        factory,
        attributes,
        config=config,
        thresholds=thresholds,
    )
