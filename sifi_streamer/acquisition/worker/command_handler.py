"""IPC command dispatcher."""

import logging
import queue
from multiprocessing import Queue

from sifi_streamer.acquisition.ipc import (
    AddCaptureEvent,
    AddMarker,
    CaptureEventAdded,
    CaptureStarted,
    CaptureStopped,
    CommandMessage,
    ErrorAck,
    MarkerAdded,
    SegmentStarted,
    SegmentStopped,
    Shutdown,
    StartCapture,
    StartSegment,
    StopCapture,
    StopSegment,
)
from sifi_streamer.acquisition.worker.recorder import RecorderFSM
from sifi_streamer.capture.events import CaptureEventSink

logger = logging.getLogger(__name__)


class CommandHandler:
    """Dispatch one foreground command at a time to a recorder.

    Args:
        cmd_queue: Incoming multiprocessing command queue.
        ack_queue: Outgoing acknowledgement queue.
        recorder: Worker-local authoritative recorder state machine.
        poll_timeout_s: Maximum queue wait per :meth:`tick`.
    """

    def __init__(
        self,
        cmd_queue: Queue,
        ack_queue: Queue,
        recorder: RecorderFSM,
        *,
        poll_timeout_s: float = 0.02,
        event_sink: CaptureEventSink | None = None,
    ) -> None:
        self._event_sink = event_sink or recorder.record_event
        self._cmd, self._ack, self._rec, self._poll = (
            cmd_queue,
            ack_queue,
            recorder,
            poll_timeout_s,
        )

    def tick(self) -> bool:
        """Process at most one command and report whether shutdown was requested.

        Recorder validation and I/O errors become :class:`ErrorAck` messages so
        the foreground receives a specific failure rather than losing the worker.
        """
        try:
            command: CommandMessage = self._cmd.get(timeout=self._poll)
        except queue.Empty:
            return False
        try:
            match command:
                case StartCapture(
                    capture_file=file,
                    capture_id=identifier,
                    attributes=attributes,
                    configuration=configuration,
                ):
                    self._rec.start_capture(file, identifier, attributes, configuration)
                    self._ack.put(CaptureStarted(file))
                case StopCapture(reason=reason):
                    self._rec.stop_capture(reason)
                    self._ack.put(CaptureStopped(reason))
                case StartSegment(
                    segment_id=identifier, segment_kind=kind, attributes=attributes
                ):
                    self._rec.start_segment(identifier, kind, attributes)
                    self._ack.put(SegmentStarted(identifier))
                case StopSegment(segment_id=identifier, reason=reason):
                    self._rec.stop_segment(identifier, reason)
                    self._ack.put(SegmentStopped(identifier))
                case AddMarker(
                    marker_id=identifier,
                    marker_kind=kind,
                    attributes=attributes,
                    source_time_ns=source_time,
                    source_clock=clock,
                ):
                    self._rec.marker(
                        identifier,
                        kind,
                        attributes,
                        source_time_ns=source_time,
                        source_clock=clock,
                    )
                    self._ack.put(MarkerAdded(identifier))
                case AddCaptureEvent(event=event):
                    if not self._rec.active:
                        raise RuntimeError("capture is not active")
                    self._event_sink(event)
                    self._ack.put(CaptureEventAdded())
                case Shutdown():
                    logger.info("Worker received shutdown command")
                    return True
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning("Worker command %s failed: %s", type(command).__name__, exc)
            self._ack.put(ErrorAck(str(exc)))
        return False
