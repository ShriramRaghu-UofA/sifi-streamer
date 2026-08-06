"""Thread-safe serialization point for the authoritative capture."""

import threading
from pathlib import Path

from sifi_streamer.capture import Attributes, CaptureLogWriter
from sifi_streamer.config import StreamerConfig
from sifi_streamer.devices import SiFiPacket


class RecorderFSM:
    def __init__(
        self, config: StreamerConfig, device_info: dict[str, object] | None
    ) -> None:
        self._config, self._device_info, self._lock, self._writer = (
            config,
            device_info,
            threading.Lock(),
            None,
        )

    def start_capture(
        self, capture_file: Path, capture_id: str, attributes: Attributes | None = None
    ) -> None:
        with self._lock:
            if self._writer is not None:
                raise RuntimeError("capture is already active")
            if not self._config.capture_log_enabled:
                raise RuntimeError("capture logging is disabled")
            # Device info remains in raw packet documents; attributes stay scalar.
            self._writer = CaptureLogWriter(
                capture_file,
                capture_id,
                attributes,
                frame_target_bytes=self._config.capture_frame_target_bytes,
                flush_interval_s=self._config.capture_flush_interval_s,
                compression_level=self._config.capture_compression_level,
                fsync_on_boundary=self._config.capture_fsync_on_boundary,
            )

    def stop_capture(self, reason: str = "normal_completion") -> None:
        with self._lock:
            if self._writer is None:
                raise RuntimeError("capture is not active")
            self._writer.close(reason)
            self._writer = None

    def start_segment(
        self, segment_id: str, kind: str, attributes: Attributes | None = None
    ) -> None:
        with self._lock:
            if self._writer is None:
                raise RuntimeError("capture is not active")
            self._writer.start_segment(segment_id, kind, attributes)

    def stop_segment(self, segment_id: str, reason: str | None = None) -> None:
        with self._lock:
            if self._writer is None:
                raise RuntimeError("capture is not active")
            self._writer.stop_segment(segment_id, reason)

    def marker(
        self,
        marker_id: str,
        kind: str,
        attributes: Attributes | None = None,
        *,
        source_time_ns: int | None = None,
        source_clock: str | None = None,
    ) -> None:
        with self._lock:
            if self._writer is None:
                raise RuntimeError("capture is not active")
            self._writer.append_marker(
                marker_id,
                kind,
                attributes,
                source_time_ns=source_time_ns,
                source_clock=source_clock,
            )

    def on_packet(self, packet: SiFiPacket) -> None:
        with self._lock:
            if self._writer is not None:
                self._writer.append_packet(packet.capture_document())

    def close(self) -> None:
        with self._lock:
            if self._writer is not None:
                self._writer.close("operator_request")
                self._writer = None
