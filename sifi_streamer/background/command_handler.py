"""IPC command dispatcher."""

import queue
from multiprocessing import Queue

from sifi_streamer.background.recorder import RecorderFSM
from sifi_streamer.protocol import (
    AddMarker,
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


class CommandHandler:
    def __init__(
        self,
        cmd_queue: Queue,
        ack_queue: Queue,
        recorder: RecorderFSM,
        *,
        poll_timeout_s: float = 0.02,
    ) -> None:
        self._cmd, self._ack, self._rec, self._poll = (
            cmd_queue,
            ack_queue,
            recorder,
            poll_timeout_s,
        )

    def tick(self) -> bool:
        try:
            command: CommandMessage = self._cmd.get(timeout=self._poll)
        except queue.Empty:
            return False
        try:
            match command:
                case StartCapture(
                    capture_file=file, capture_id=identifier, attributes=attributes
                ):
                    self._rec.start_capture(file, identifier, attributes)
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
                case Shutdown():
                    return True
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._ack.put(ErrorAck(str(exc)))
        return False
