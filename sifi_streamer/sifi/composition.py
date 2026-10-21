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


def _device_factories(
    *,
    device_handles: Mapping[str, str] | None,
    bridge_executable: str | Path,
    device_handle: str | None,
    host: str,
    port: int,
    transport: BridgeTransport | str,
    sensor_profile: SiFiSensorProfile | None,
    synthetic: bool,
) -> tuple[DeviceFactory | dict[str, DeviceFactory], dict[str, object]]:
    if device_handles is not None:
        if device_handle is not None or synthetic:
            raise ValueError(
                "device_handles cannot be combined with device_handle or synthetic"
            )
        if not device_handles:
            raise ValueError("device_handles must contain at least one device")
        if any(
            not name or name != name.strip() or "/" in name for name in device_handles
        ):
            raise ValueError("device IDs must be non-empty trimmed strings without '/'")
        handles = list(device_handles.values())
        if any(
            not handle.strip() or any(c in handle for c in "\r\n\0")
            for handle in handles
        ):
            raise ValueError(
                "device handles must be nonempty and contain no CR, LF, or NUL"
            )
        if len(set(handles)) != len(handles):
            raise ValueError("each device must have a distinct handle")
    selections = (
        dict(device_handles)
        if device_handles is not None
        else {"device": device_handle}
    )
    transport = BridgeTransport(transport)
    if not 1 <= port <= 65535 or (
        transport != BridgeTransport.STDOUT and port + len(selections) - 1 > 65535
    ):
        raise ValueError("device ports must be in the range 1..65535")
    factories: dict[str, DeviceFactory] = {}
    configurations: dict[str, dict[str, object]] = {}
    for index, (name, handle) in enumerate(selections.items()):
        device_port = port if transport == BridgeTransport.STDOUT else port + index
        factories[name] = _device_factory(
            bridge_executable=bridge_executable,
            device_handle=handle,
            host=host,
            port=device_port,
            transport=transport,
            sensor_profile=sensor_profile,
            synthetic=synthetic,
        )
        configurations[name] = _launch_configuration(
            bridge_executable,
            handle,
            host,
            device_port,
            transport,
            sensor_profile,
            synthetic,
        )
    if device_handles is None:
        return factories["device"], configurations["device"]
    return factories, {"devices": configurations}


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
    device_handles: Mapping[str, str] | None = None,
    host: str = "127.0.0.1",
    port: int = 5000,
    transport: BridgeTransport | str = BridgeTransport.STDOUT,
    sensor_profile: SiFiSensorProfile | None = None,
    synthetic: bool = False,
    config: StreamerConfig | None = None,
    launch_configuration: Packet | None = None,
) -> CaptureController:
    """Compose a ready-to-start controller for real or synthetic SiFi capture."""
    factory, integration = _device_factories(
        device_handles=device_handles,
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
                "integration": integration,
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
    device_handles: Mapping[str, str] | None = None,
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
    factory, integration = _device_factories(
        device_handles=device_handles,
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
            "integration": integration,
            **dict(launch_configuration or {}),
        },
    )
