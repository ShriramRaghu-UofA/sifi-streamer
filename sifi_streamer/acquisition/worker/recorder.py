"""Thread-safe serialization point for the authoritative capture."""

import logging
import threading
from pathlib import Path

from sifi_streamer.acquisition.config import StreamerConfig
from sifi_streamer.acquisition.devices import AcquisitionPacket
from sifi_streamer.capture.records import Attributes, CaptureLogWriter

logger = logging.getLogger(__name__)


class RecorderFSM:
    """Serialize packet and annotation writes to one optional capture writer.

    Acquisition callbacks and foreground commands may arrive on different worker
    threads. A single lock makes capture creation, writes, and closure atomic with
    respect to each other.

    Args:
        config: Capture writer settings and logging enablement.
        device_info: Optional connected-device metadata retained by the worker.
    """

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
        """Create the authoritative writer if recording is enabled and inactive."""
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
            if self._device_info is not None:
                # The bridge info response is itself a complete raw device
                # document. Preserve it in the authoritative stream before any
                # acquired packets without changing the schema-v2 vocabulary.
                self._writer.append_packet(self._device_info)
            logger.info(
                "Opened authoritative capture %s (id=%r)", capture_file, capture_id
            )

    def stop_capture(self, reason: str = "normal_completion") -> None:
        """Close and discard the active writer.

        Raises:
            RuntimeError: If no capture is active.
        """
        with self._lock:
            if self._writer is None:
                raise RuntimeError("capture is not active")
            self._writer.close(reason)
            self._writer = None
            logger.info("Closed authoritative capture with reason %r", reason)

    def start_segment(
        self, segment_id: str, kind: str, attributes: Attributes | None = None
    ) -> None:
        """Write a segment start to the active capture."""
        with self._lock:
            if self._writer is None:
                raise RuntimeError("capture is not active")
            self._writer.start_segment(segment_id, kind, attributes)
            logger.info("Wrote segment start %r (kind %r)", segment_id, kind)

    def stop_segment(self, segment_id: str, reason: str | None = None) -> None:
        """Write a segment stop to the active capture."""
        with self._lock:
            if self._writer is None:
                raise RuntimeError("capture is not active")
            self._writer.stop_segment(segment_id, reason)
            logger.info("Wrote segment stop %r (reason %r)", segment_id, reason)

    def marker(
        self,
        marker_id: str,
        kind: str,
        attributes: Attributes | None = None,
        *,
        source_time_ns: int | None = None,
        source_clock: str | None = None,
    ) -> None:
        """Write a marker to the active capture."""
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
            logger.info("Wrote marker %r (kind %r)", marker_id, kind)

    def on_packet(self, packet: AcquisitionPacket) -> None:
        """Append a complete packet document when capture is active."""
        with self._lock:
            if (
                self._writer is not None
                and (document := packet.capture_document()) is not None
            ):
                self._writer.append_packet(document)

    def close(self) -> None:
        """Close an active writer for ``operator_request``; otherwise do nothing."""
        with self._lock:
            if self._writer is not None:
                self._writer.close("operator_request")
                self._writer = None
                logger.warning("Closed active capture during worker teardown")
