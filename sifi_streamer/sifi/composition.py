"""Compose the generic acquisition stack with bundled SiFi devices."""

from collections.abc import Mapping
from dataclasses import asdict
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
from sifi_streamer.capture.records import Packet, Scalar
from sifi_streamer.sifi.bridge import (
    DEFAULT_BRIDGE_EXECUTABLE,
    BridgeTransport,
    SiFiBridgeDevice,
)
from sifi_streamer.sifi.devices import SyntheticSiFiDevice
from sifi_streamer.sifi.sensor_profile import SiFiSensorProfile


def _device_factory(
    *,
    bridge_executable: str | Path,
    device_handle: str | None,
    host: str,
    port: int,
    transport: BridgeTransport | str,
    sensor_profile: SiFiSensorProfile | None,
    synthetic: bool,
) -> DeviceFactory:
    if synthetic and device_handle is not None:
        raise ValueError("device_handle cannot be used with synthetic acquisition")
    if synthetic and sensor_profile is not None:
        raise ValueError("sensor_profile cannot be used with synthetic acquisition")
    if synthetic:
        return SyntheticSiFiDevice
    return partial(
        SiFiBridgeDevice,
        host=host,
        port=port,
        executable=bridge_executable,
        device_handle=device_handle,
        transport=transport,
        **({"sensor_profile": sensor_profile} if sensor_profile is not None else {}),
    )


def _launch_configuration(
    bridge_executable: str | Path,
    device_handle: str | None,
    host: str,
    port: int,
    transport: BridgeTransport | str,
    sensor_profile: SiFiSensorProfile | None,
    synthetic: bool,
) -> dict[str, object]:
    return {
        "device": "synthetic" if synthetic else "SiFi bridge",
        "device_handle": device_handle,
        "bridge_executable": None if synthetic else str(bridge_executable),
        "host": host,
        "port": port,
        "transport": str(transport),
        "sensor_profile": None
        if synthetic
        else asdict(sensor_profile or SiFiSensorProfile()),
    }


def create_sifi_capture(
    capture_file: Path,
    capture_id: str,
    attributes: Mapping[str, Scalar] | None = None,
    *,
    bridge_executable: str | Path = DEFAULT_BRIDGE_EXECUTABLE,
    device_handle: str | None = None,
    host: str = "127.0.0.1",
    port: int = 5000,
    transport: BridgeTransport | str = BridgeTransport.STDOUT,
    sensor_profile: SiFiSensorProfile | None = None,
    synthetic: bool = False,
    config: StreamerConfig | None = None,
    launch_configuration: Packet | None = None,
) -> CaptureController:
    """Compose a ready-to-start controller for real or synthetic SiFi capture."""
    factory = _device_factory(
        bridge_executable=bridge_executable,
        device_handle=device_handle,
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
            launch_configuration={
                "integration": _launch_configuration(
                    bridge_executable,
                    device_handle,
                    host,
                    port,
                    transport,
                    sensor_profile,
                    synthetic,
                ),
                **dict(launch_configuration or {}),
            },
        )
    )


def create_sifi_capture_runtime(
    capture_file: Path,
    capture_id: str,
    attributes: Mapping[str, Scalar] | None = None,
    *,
    bridge_executable: str | Path = DEFAULT_BRIDGE_EXECUTABLE,
    device_handle: str | None = None,
    host: str = "127.0.0.1",
    port: int = 5000,
    transport: BridgeTransport | str = BridgeTransport.STDOUT,
    sensor_profile: SiFiSensorProfile | None = None,
    synthetic: bool = False,
    config: StreamerConfig | None = None,
    thresholds: HealthThresholds | None = None,
    launch_configuration: Packet | None = None,
) -> CaptureRuntime:
    """Compose the standard SiFi device with controller and monitor access."""
    factory = _device_factory(
        bridge_executable=bridge_executable,
        device_handle=device_handle,
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
        launch_configuration={
            "integration": _launch_configuration(
                bridge_executable,
                device_handle,
                host,
                port,
                transport,
                sensor_profile,
                synthetic,
            ),
            **dict(launch_configuration or {}),
        },
    )
