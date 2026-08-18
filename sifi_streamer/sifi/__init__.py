"""Bundled SiFi devices, bridge management, profiles, and composition helpers."""

from sifi_streamer.sifi.bridge import BridgeTransport, SiFiBridgeDevice
from sifi_streamer.sifi.composition import (
    create_sifi_capture,
    create_sifi_capture_runtime,
)
from sifi_streamer.sifi.devices import (
    DEFAULT_MODALITIES,
    SIGNAL_MODALITIES,
    Modalities,
    Modality,
    ModalitySpec,
    SiFiBandDevice,
    SiFiPacket,
    SyntheticSiFiDevice,
    modalities_from_device_info,
    packet_from_json_line,
    streams_from_modalities,
)
from sifi_streamer.sifi.sensor_profile import SiFiSensorProfile

__all__ = [
    "DEFAULT_MODALITIES",
    "SIGNAL_MODALITIES",
    "BridgeTransport",
    "Modalities",
    "Modality",
    "ModalitySpec",
    "SiFiBandDevice",
    "SiFiBridgeDevice",
    "SiFiPacket",
    "SiFiSensorProfile",
    "SyntheticSiFiDevice",
    "create_sifi_capture",
    "create_sifi_capture_runtime",
    "modalities_from_device_info",
    "packet_from_json_line",
    "streams_from_modalities",
]
