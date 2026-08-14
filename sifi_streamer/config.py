"""System-level streamer configuration."""

from compression import zstd
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StreamerConfig:
    """Immutable settings shared by foreground and acquisition processes.

    Attributes:
        ring_buffer_seconds: Per-modality shared-memory history duration.
        ack_timeout_s: Default foreground wait for worker acknowledgements.
        capture_log_enabled: Whether the worker permits authoritative recording.
        capture_frame_target_bytes: Uncompressed bytes buffered per target frame.
        capture_flush_interval_s: Maximum interval between packet-batch flushes.
        capture_compression_level: Optional Zstandard compression level.
        capture_fsync_on_boundary: Synchronize capture and segment boundaries to
            storage before acknowledging them.

    Raises:
        ValueError: If a duration or size is non-positive, or the compression
            level is unsupported by the installed Zstandard implementation.
    """

    ring_buffer_seconds: float = 10.0
    ack_timeout_s: float = 2.0
    capture_log_enabled: bool = True
    capture_frame_target_bytes: int = 1 << 20
    capture_flush_interval_s: float = 1.0
    capture_compression_level: int | None = None
    capture_fsync_on_boundary: bool = False

    def __post_init__(self) -> None:
        if self.ring_buffer_seconds <= 0:
            raise ValueError("ring_buffer_seconds must be positive")
        if self.ack_timeout_s <= 0:
            raise ValueError("ack_timeout_s must be positive")
        if self.capture_frame_target_bytes <= 0:
            raise ValueError("capture_frame_target_bytes must be positive")
        if self.capture_flush_interval_s <= 0:
            raise ValueError("capture_flush_interval_s must be positive")
        if self.capture_compression_level is not None:
            try:
                zstd.ZstdCompressor(level=self.capture_compression_level)
            except (TypeError, ValueError, zstd.ZstdError) as exc:
                raise ValueError("capture_compression_level is not supported") from exc
