"""Foreground context-manager API over the background process."""

import logging
import multiprocessing
import queue
import uuid
from multiprocessing import Queue
from pathlib import Path
from typing import Self

from sifi_streamer.acquisition.config import StreamerConfig
from sifi_streamer.acquisition.devices import DeviceFactory
from sifi_streamer.acquisition.health import RawHealthSnapshot, WorkerFatal
from sifi_streamer.acquisition.ipc import (
    AddCaptureEvent,
    AddMarker,
    CaptureEventAdded,
    CaptureStarted,
    CaptureStopped,
    ErrorAck,
    MarkerAdded,
    Ready,
    SegmentStarted,
    SegmentStopped,
    Shutdown,
    StartCapture,
    StartSegment,
    StopCapture,
    StopSegment,
    StreamInfo,
)
from sifi_streamer.acquisition.reader import SharedMemoryReader
from sifi_streamer.acquisition.worker.process import background_main
from sifi_streamer.capture.events import CaptureEvent
from sifi_streamer.capture.records import Attributes, Packet, validate_document
from sifi_streamer.exceptions import AckError, AckTimeoutError, RecordingError

logger = logging.getLogger(__name__)


class BackgroundHandle:
    """Own one spawned acquisition worker and its live shared-memory readers.

    Use this class as a context manager. Entering starts the worker, waits for its
    ready acknowledgement, and attaches readers. Exiting requests orderly
    shutdown, then terminates a worker that does not exit promptly. The worker
    owns the device, shared-memory blocks, ring buffers, and recorder.

    Args:
        config: Settings shared with the worker process.
        device_factory: Picklable zero-argument factory invoked in the worker.
        background_log_level: Logging level configured inside the worker.
    """

    def __init__(
        self,
        config: StreamerConfig,
        device_factory: DeviceFactory,
        *,
        background_log_level: int = logging.INFO,
    ) -> None:
        self._config = config
        context = multiprocessing.get_context("spawn")
        self._cmd_queue: Queue = context.Queue()
        self._ack_queue: Queue = context.Queue()
        self._health_queue: Queue = context.Queue(maxsize=1)
        self._device_info_queue: Queue = context.Queue()
        self._startup_capture: StartCapture | None = None
        self._fatal_queue: Queue = context.Queue()
        self._process_kwargs: dict[str, object] = {
            "config": config,
            "device_factory": device_factory,
            "cmd_queue": self._cmd_queue,
            "ack_queue": self._ack_queue,
            "health_queue": self._health_queue,
            "fatal_queue": self._fatal_queue,
            "device_info_queue": self._device_info_queue,
            "shm_prefix": f"acq_{uuid.uuid4().hex[:12]}",
            "log_level": background_log_level,
        }
        self._process = context.Process(
            target=background_main,
            kwargs=self._process_kwargs,
            daemon=True,
            name="acquisition-worker",
        )
        self._stream_readers: dict[str, SharedMemoryReader] = {}
        self._streams: tuple[StreamInfo, ...] = ()
        self._device_info: dict[str, object] = {}
        self._device_info_revision = 0
        self._entered = False

    def __enter__(self) -> Self:
        """Start the worker and attach live readers.

        Returns:
            This entered handle. Re-entering an active handle is a no-op.

        Raises:
            AckError: If worker initialization reports a specific failure.
            AckTimeoutError: If the worker is not ready within 30 seconds.
        """
        if self._entered:
            return self
        if self._startup_capture is not None:
            self._process_kwargs["startup_capture"] = self._startup_capture
            self._process = multiprocessing.get_context("spawn").Process(
                target=background_main,
                kwargs=self._process_kwargs,
                daemon=True,
                name="acquisition-worker",
            )
        self._process.start()
        logger.info("Started acquisition worker process (pid=%s)", self._process.pid)
        try:
            return self._attach_readers()
        except BaseException:
            self._shutdown()
            raise

    def _attach_readers(self) -> Self:
        """Wait for startup and attach readers under the entry cleanup boundary."""
        ack = self._wait_ack(timeout=30)
        if isinstance(ack, Ready):
            self._streams, self._device_info = ack.streams, ack.device_info
            self._device_info_revision = ack.device_info_revision
            for info in ack.streams:
                reader = SharedMemoryReader(
                    info.shm_name,
                    info.n_samples,
                    info.n_channels,
                    dtype=info.payload_dtype,
                )
                self._stream_readers[info.stream_id] = reader
            self._entered = True
            logger.info("Acquisition worker ready with %d stream(s)", len(ack.streams))
            return self
        logger.error("Acquisition worker failed before becoming ready")
        if isinstance(ack, ErrorAck):
            raise AckError(f"Background process failed during startup: {ack.message}")
        raise AckTimeoutError("Background process did not send Ready within 30s")

    def __exit__(self, *_: object) -> None:
        """Stop the worker and close all attached readers."""
        if not self._entered:
            return
        self._shutdown()

    def _shutdown(self) -> None:
        """Release the started process and any readers, including partial entry."""
        try:
            self._cmd_queue.put(Shutdown())
            self._process.join(timeout=5)
        finally:
            if self._process.is_alive():
                logger.warning(
                    "Acquisition worker did not stop in time; terminating it"
                )
                self._process.terminate()
                self._process.join(timeout=2)
            for reader in self._stream_readers.values():
                reader.close()
            (
                self._stream_readers,
                self._streams,
                self._entered,
            ) = (
                {},
                (),
                False,
            )
            logger.info("Acquisition worker and shared-memory readers closed")

    @property
    def streams(self) -> tuple[StreamInfo, ...]:
        """Return every declared generic stream while entered."""
        if not self._entered:
            raise RuntimeError("streams are available only inside the context manager")
        return self._streams

    @property
    def stream_readers(self) -> dict[str, SharedMemoryReader]:
        """Return a copy of the dynamic stream-to-reader mapping."""
        if not self._entered:
            raise RuntimeError(
                "stream_readers are available only inside the context manager"
            )
        return dict(self._stream_readers)

    def poll_health(self) -> RawHealthSnapshot | None:
        """Return the newest available cumulative health snapshot without blocking."""
        latest = None
        while True:
            try:
                latest = self._health_queue.get_nowait()
            except queue.Empty:
                return latest

    def poll_fatal(self) -> WorkerFatal | None:
        """Return one reliable worker fatal event without blocking."""
        try:
            return self._fatal_queue.get_nowait()
        except queue.Empty:
            return None

    @property
    def device_info(self) -> dict[str, object]:
        """Return a defensive snapshot of the latest explicit device report."""
        while True:
            try:
                update = self._device_info_queue.get_nowait()
                if update.revision > self._device_info_revision:
                    self._device_info = update.info
                    self._device_info_revision = update.revision
            except queue.Empty:
                return validate_document(self._device_info)

    @property
    def launch_configuration(self) -> dict[str, object]:
        """Resolved settings supplied to the worker, retained after closure."""
        return validate_document(
            self._startup_capture.configuration
            if self._startup_capture is not None
            else {}
        )

    def prepare_capture(
        self,
        capture_file: Path,
        capture_id: str,
        attributes: Attributes,
        configuration: Packet,
    ) -> None:
        """Arrange worker-owned recording before hardware startup."""
        if self._entered or self._process.pid is not None:
            raise RuntimeError("capture must be prepared before worker startup")
        self._startup_capture = StartCapture(
            capture_file, capture_id, dict(attributes), validate_document(configuration)
        )

    def record_event(self, event: CaptureEvent) -> None:
        """Persist an explicit event and wait for acknowledgement."""
        if not self._entered:
            raise RuntimeError("capture events require an entered handle")
        from sifi_streamer.capture.events import copy_event

        self._cmd_queue.put(AddCaptureEvent(copy_event(event)))
        self._expect(CaptureEventAdded, "record_event")

    def start_capture(
        self, capture_file: Path, capture_id: str, attributes: Attributes | None = None
    ) -> None:
        """Create and start an authoritative capture in the worker.

        The mapping is copied before crossing the process boundary. This method
        returns only after the worker acknowledges creation.
        """
        self._cmd_queue.put(
            StartCapture(capture_file, capture_id, dict(attributes or {}))
        )
        self._expect(CaptureStarted, "start_capture")

    def stop_capture(self, reason: str = "normal_completion") -> None:
        """Flush and stop the active capture, waiting without an ACK timeout."""
        self._cmd_queue.put(StopCapture(reason))
        self._expect(CaptureStopped, "stop_capture", timeout=None)

    def start_segment(
        self, segment_id: str, segment_kind: str, attributes: Attributes | None = None
    ) -> None:
        """Record a segment start and wait for worker acknowledgement."""
        self._cmd_queue.put(
            StartSegment(segment_id, segment_kind, dict(attributes or {}))
        )
        self._expect(SegmentStarted, "start_segment")

    def stop_segment(self, segment_id: str, reason: str | None = None) -> None:
        """Record a segment stop and wait for worker acknowledgement."""
        self._cmd_queue.put(StopSegment(segment_id, reason))
        self._expect(SegmentStopped, "stop_segment")

    def add_marker(
        self,
        marker_id: str,
        marker_kind: str,
        attributes: Attributes | None = None,
        *,
        source_time_ns: int | None = None,
        source_clock: str | None = None,
    ) -> None:
        """Record a marker and wait for worker acknowledgement.

        Args follow the stable ``marker_id, marker_kind`` positional order.
        ``source_time_ns`` may identify time on the optional ``source_clock``.
        """
        self._cmd_queue.put(
            AddMarker(
                marker_id,
                marker_kind,
                dict(attributes or {}),
                source_time_ns,
                source_clock,
            )
        )
        self._expect(MarkerAdded, "add_marker")

    def _expect(self, expected: type, name: str, *, timeout: float | None = -1) -> None:
        ack = self._wait_ack(
            timeout=self._config.ack_timeout_s if timeout == -1 else timeout
        )
        if isinstance(ack, expected):
            return
        if isinstance(ack, ErrorAck):
            raise RecordingError(f"{name} failed: {ack.message}")
        if ack is None:
            raise AckTimeoutError(f"{name} ACK was not received")
        raise RecordingError(f"Unexpected ACK for {name}: {ack!r}")

    def _wait_ack(self, *, timeout: float | None):
        try:
            return self._ack_queue.get(timeout=timeout)
        except queue.Empty:
            return None
