"""Pluggable device acquisition, shared memory, monitoring, and capture adapters."""

from sifi_streamer.acquisition.backend import (
    AcquisitionCaptureBackend,
    create_capture_runtime,
)
from sifi_streamer.acquisition.config import StreamerConfig
from sifi_streamer.acquisition.devices import (
    AcquisitionDevice,
    AcquisitionPacket,
    DeviceFactory,
    SignalChannelSpec,
    SignalStreamSpec,
    StreamId,
)
from sifi_streamer.acquisition.handle import BackgroundHandle
from sifi_streamer.acquisition.health import (
    HealthEvent,
    HealthSeverity,
    HealthSnapshot,
    HealthThresholds,
    StreamHealth,
)
from sifi_streamer.acquisition.reader import SharedMemoryReader, SignalWindow
from sifi_streamer.acquisition.runtime import AcquisitionMonitor, CaptureRuntime

__all__ = [
    "AcquisitionCaptureBackend",
    "AcquisitionDevice",
    "AcquisitionMonitor",
    "AcquisitionPacket",
    "BackgroundHandle",
    "CaptureRuntime",
    "DeviceFactory",
    "HealthEvent",
    "HealthSeverity",
    "HealthSnapshot",
    "HealthThresholds",
    "SharedMemoryReader",
    "SignalChannelSpec",
    "SignalStreamSpec",
    "SignalWindow",
    "StreamHealth",
    "StreamId",
    "StreamerConfig",
    "create_capture_runtime",
]
