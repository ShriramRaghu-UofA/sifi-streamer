"""One spawned recorder process owning the authoritative DuckDB connection."""

import multiprocessing as mp
import pickle
import queue
import signal
import time
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from sifi_streamer.capture.events import CaptureEvent, DeviceInfoEvent, DiagnosticEvent
from sifi_streamer.capture.records import (
    Attributes,
    CaptureLogWriter,
    StreamDeclaration,
    validate_attributes,
    validate_document,
)
from sifi_streamer.capture.transport import (
    FailureFlag,
    MarkerSubmission,
    MessageChannel,
    PacketSubmission,
    ProducerComplete,
    ProducerEndpoint,
    RecorderConfig,
    SegmentStartSubmission,
    SegmentStopSubmission,
    Submission,
)
from sifi_streamer.exceptions import RecordingError


@dataclass(frozen=True, slots=True)
class RecorderFailure:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class RecorderStatus:
    accepted_events: int
    committed_events: int
    pending_bytes: int
    oldest_uncommitted_age_s: float
    producer_pending_bytes: tuple[tuple[str, int], ...]
    completed_producers: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _RegisterDevice:
    request_id: str
    device_id: str
    integration: str
    configuration: dict[str, object]


@dataclass(frozen=True, slots=True)
class _RegisterStream:
    request_id: str
    device_id: str
    spec: StreamDeclaration


@dataclass(frozen=True, slots=True)
class _Seal:
    request_id: str


@dataclass(frozen=True, slots=True)
class _Stop:
    request_id: str
    reason: str


type _RecorderCommand = _RegisterDevice | _RegisterStream | _Seal | _Stop


@dataclass(frozen=True, slots=True)
class _Reply:
    request_id: str
    sequence: int | None = None
    status: RecorderStatus | None = None


def _append(
    writer: CaptureLogWriter, producer: ProducerEndpoint, submission: Submission
) -> int:
    """Dispatch one immutable producer contribution inside its event envelope."""
    with writer.submission(
        submission.producer_id,
        submission.producer_sequence,
        submission.received_monotonic_ns,
        submission.received_unix_ns,
    ):
        match submission.body:
            case PacketSubmission() as packet:
                if producer.device_id is None:
                    raise ValueError("packet producer has no device identity")
                return writer.append_packet_data(
                    producer.device_id,
                    packet.stream_id,
                    packet.timestamps,
                    packet.data,
                    packet.validity,
                    packet.document,
                    reported_rate_hz=packet.reported_rate_hz,
                    samples_lost=packet.samples_lost,
                    status=packet.status,
                )
            case MarkerSubmission() as marker:
                return writer.append_marker(
                    marker.marker_id,
                    marker.marker_kind,
                    marker.attributes,
                    source_time_ns=marker.source_time_ns,
                    source_clock=marker.source_clock,
                )
            case SegmentStartSubmission() as segment:
                return writer.start_segment(
                    segment.segment_id, segment.segment_kind, segment.attributes
                )
            case SegmentStopSubmission() as segment:
                return writer.stop_segment(segment.segment_id, segment.reason)
            case DeviceInfoEvent() as event:
                return writer.append_device_info(
                    event.stage, event.info, device_id=producer.device_id
                )
            case DiagnosticEvent() as event:
                return writer.append_diagnostic(
                    event.severity,
                    event.source,
                    event.stage,
                    event.code,
                    event.message,
                    event.details,
                    device_id=producer.device_id,
                )
            case _:
                raise TypeError("unsupported recorder submission")


def _recorder_main(
    path: Path,
    capture_id: str,
    attributes: Attributes,
    configuration: dict[str, object],
    producers: tuple[ProducerEndpoint, ...],
    config: RecorderConfig,
    commands: MessageChannel[_RecorderCommand],
    replies: MessageChannel[_Reply],
    status: MessageChannel[RecorderStatus],
    failures: MessageChannel[RecorderFailure],
) -> None:
    """Spawn target; the foreground launcher retains Ctrl+C ownership."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    writer: CaptureLogWriter | None = None
    accepted = committed = pending_bytes = 0
    oldest_pending: float | None = None
    completed: set[str] = set()
    expected = {producer.producer_id: 0 for producer in producers}
    sealed = False
    stopping: _Stop | None = None
    last_commit = last_status = time.monotonic()
    parent = mp.parent_process()

    def publish() -> RecorderStatus:
        snapshot = RecorderStatus(
            accepted,
            committed,
            pending_bytes,
            0.0 if oldest_pending is None else time.monotonic() - oldest_pending,
            tuple((p.producer_id, p.pending_bytes) for p in producers),
            tuple(sorted(completed)),
        )
        # Progress is latest-value telemetry, never authoritative data.
        with suppress(queue.Full):
            status.put_nowait(snapshot)
        return snapshot

    try:
        writer = CaptureLogWriter(path, capture_id, attributes)
        writer.append_launch_configuration(configuration)
        for producer in producers:
            writer.register_producer(
                producer.producer_id,
                producer.device_id,
                "foreground" if producer.device_id is None else "device",
            )
        writer.flush()
        replies.put(_Reply("startup"))
        while True:
            if parent is not None and not parent.is_alive():
                raise RecordingError("capture launcher exited before recorder shutdown")
            if any(producer.failed for producer in producers):
                messages = [
                    f"producer {producer.producer_id!r}: {message}"
                    for producer in producers
                    if (message := producer.failure_message) is not None
                ]
                raise RecordingError("; ".join(messages) or "capture producer failed")
            worked = False
            try:
                command = commands.get_nowait()
            except queue.Empty:
                command = None
            match command:
                case _RegisterDevice():
                    if sealed:
                        raise ValueError("device registry is sealed")
                    writer.register_device(
                        command.device_id, command.integration, command.configuration
                    )
                    replies.put(_Reply(command.request_id))
                    worked = True
                case _RegisterStream():
                    if sealed:
                        raise ValueError("stream registry is sealed")
                    writer.register_stream(command.device_id, command.spec)
                    replies.put(_Reply(command.request_id))
                    worked = True
                case _Seal():
                    if sealed:
                        raise ValueError("stream registry is already sealed")
                    sealed = True
                    writer.flush()
                    committed, pending_bytes, oldest_pending = accepted, 0, None
                    last_commit = time.monotonic()
                    replies.put(_Reply(command.request_id))
                    worked = True
                case _Stop():
                    if stopping is not None:
                        raise RuntimeError("recorder stop already requested")
                    stopping = command
                    worked = True
                case None:
                    pass
                case _:
                    raise TypeError("unknown recorder command")
            for producer in producers:
                if producer.producer_id in completed:
                    continue
                serialized = producer.receive()
                if serialized is None:
                    continue
                worked = True
                try:
                    submission = pickle.loads(serialized)
                    if not isinstance(submission, Submission):
                        raise TypeError("invalid recorder transport message")
                    if submission.producer_id != producer.producer_id:
                        raise ValueError("producer identity mismatch")
                    if submission.producer_sequence != expected[producer.producer_id]:
                        raise ValueError("producer sequence discontinuity")
                    expected[producer.producer_id] += 1
                    if isinstance(submission.body, ProducerComplete):
                        completed.add(producer.producer_id)
                        continue
                    if isinstance(submission.body, PacketSubmission) and not sealed:
                        raise ValueError(
                            "packet recording requires sealed stream registry"
                        )
                    sequence = _append(writer, producer, submission)
                    accepted += 1
                    pending_bytes += len(serialized)
                    if oldest_pending is None:
                        oldest_pending = time.monotonic()
                    if submission.request_id is not None:
                        replies.put(_Reply(submission.request_id, sequence))
                finally:
                    producer.release(len(serialized))
            now = time.monotonic()
            if pending_bytes and (
                now - last_commit >= config.commit_interval_s
                or pending_bytes >= config.transaction_target_bytes
            ):
                writer.flush()
                committed, pending_bytes, oldest_pending = accepted, 0, None
                last_commit = time.monotonic()
                publish()
            if stopping is not None and len(completed) == len(producers):
                writer.close(stopping.reason)
                writer = None
                committed, pending_bytes, oldest_pending = accepted, 0, None
                final_status = publish()
                replies.put(_Reply(stopping.request_id, status=final_status))
                return
            if now - last_status >= 0.1:
                publish()
                last_status = now
            if not worked:
                # Do not sleep while holding a shared synchronization primitive:
                # forced process death must not strand its lock in another process.
                time.sleep(0.005)
    except BaseException as exc:
        message = f"{type(exc).__name__}: {exc}"
        if writer is not None:
            try:
                writer.abort()
            except BaseException as cleanup:
                message += (
                    f"; recorder cleanup failed: {type(cleanup).__name__}: {cleanup}"
                )
        if len(message.encode("utf-8")) > 400_000:
            message = (
                message.encode("utf-8")[:400_000].decode("utf-8", errors="ignore")
                + " [recorder failure diagnostic truncated]"
            )
        failures.put(RecorderFailure("recorder_failure", message))
        for producer in producers:
            producer.failed_event.set()


class RecorderHandle:
    """Foreground owner of one recorder and a fixed collection of producers."""

    def __init__(
        self,
        path: Path,
        capture_id: str,
        attributes: Attributes | None = None,
        configuration: Mapping[str, object] | None = None,
        *,
        producer_devices: Mapping[str, str | None] | None = None,
        config: RecorderConfig | None = None,
    ) -> None:
        self.config = config or RecorderConfig()
        registry = dict(producer_devices or {"foreground": None})
        if registry.get("foreground", "missing") is not None:
            raise ValueError("producer registry must contain foreground with no device")
        if any(not name or name != name.strip() for name in registry):
            raise ValueError("producer IDs must be non-empty trimmed strings")
        context = mp.get_context("spawn")
        self._commands = MessageChannel[_RecorderCommand].create(
            context,
            capacity_bytes=self.config.producer_buffer_bytes,
            max_message_bytes=min(
                self.config.max_submission_bytes, self.config.producer_buffer_bytes - 8
            ),
            timeout_s=self.config.enqueue_timeout_s,
        )
        self._replies = MessageChannel[_Reply].create(
            context,
            capacity_bytes=512 << 10,
            max_message_bytes=256 << 10,
            timeout_s=self.config.enqueue_timeout_s,
        )
        self._statuses = MessageChannel[RecorderStatus].create(
            context,
            capacity_bytes=512 << 10,
            max_message_bytes=256 << 10,
        )
        self._failures = MessageChannel[RecorderFailure].create(
            context,
            capacity_bytes=1 << 20,
            max_message_bytes=512 << 10,
        )
        failed = FailureFlag(context.Semaphore(0))
        slot_bytes = min(
            64 << 10,
            self.config.max_submission_bytes + 8,
            self.config.producer_buffer_bytes,
        )
        slot_count = self.config.producer_buffer_bytes // slot_bytes
        self._producers = {
            name: ProducerEndpoint(
                name,
                device,
                self.config,
                context.RawArray("B", slot_bytes * slot_count),
                context.RawArray("b", slot_count),
                slot_bytes,
                slot_count,
                context.Semaphore(0),
                context.Semaphore(slot_count),
                context.Lock(),
                failed,
                context.RawArray("B", 4096),
                context.Semaphore(0),
            )
            for name, device in registry.items()
        }
        self._process = context.Process(
            target=_recorder_main,
            args=(
                path,
                capture_id,
                validate_attributes(attributes or {}),
                validate_document(dict(configuration or {})),
                tuple(self._producers.values()),
                self.config,
                self._commands,
                self._replies,
                self._statuses,
                self._failures,
            ),
            name="capture-recorder",
        )
        self._started = self._closed = False
        self._stopping = False
        self._failure: RecorderFailure | None = None
        self._status: RecorderStatus | None = None
        self._pending_replies: dict[str, _Reply] = {}

    def producer(self, producer_id: str) -> ProducerEndpoint:
        return self._producers[producer_id]

    def start(self) -> None:
        if self._started:
            raise RuntimeError("recorder has already started")
        if self._closed:
            raise RuntimeError("recorder is closed")
        self._process.start()
        self._started = True
        try:
            self._wait("startup", 30)
        except BaseException:
            self.abort()
            raise

    def _wait(self, request_id: str, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while True:
            if request_id in self._pending_replies:
                reply = self._pending_replies.pop(request_id)
                if reply.status is not None:
                    self._status = reply.status
                return reply.sequence
            try:
                reply = self._replies.get_nowait()
            except queue.Empty:
                pass
            else:
                self._pending_replies[reply.request_id] = reply
                continue
            failure = self.poll_failure()
            if failure is not None:
                raise RecordingError(failure.message)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._producers["foreground"].fail(
                    f"recorder acknowledgement deadline exceeded: {request_id}"
                )
                raise RecordingError("recorder acknowledgement deadline exceeded")
            try:
                reply = self._replies.get(timeout=min(remaining, 0.05))
            except queue.Empty:
                continue
            self._pending_replies[reply.request_id] = reply

    def _command(self, command: _RegisterDevice | _RegisterStream | _Seal) -> None:
        if not self._started or self._closed or self._stopping:
            raise RuntimeError("recorder is not running")
        self._commands.put(command)
        self._wait(command.request_id, self.config.enqueue_timeout_s + 30)

    def register_device(
        self,
        device_id: str,
        integration: str = "",
        configuration: Mapping[str, object] | None = None,
    ) -> None:
        self._command(
            _RegisterDevice(
                uuid.uuid4().hex,
                device_id,
                integration,
                validate_document(dict(configuration or {})),
            )
        )

    def register_stream(self, device_id: str, spec: StreamDeclaration) -> None:
        self._command(_RegisterStream(uuid.uuid4().hex, device_id, spec))

    def seal_registration(self) -> None:
        self._command(_Seal(uuid.uuid4().hex))

    def _submit(
        self,
        body: MarkerSubmission
        | SegmentStartSubmission
        | SegmentStopSubmission
        | CaptureEvent,
    ) -> int:
        if not self._started or self._closed or self._stopping:
            raise RuntimeError("recorder is not running")
        request_id = uuid.uuid4().hex
        self._producers["foreground"].send(body, request_id=request_id)
        sequence = self._wait(request_id, self.config.enqueue_timeout_s + 30)
        if sequence is None:
            raise RecordingError("recorder omitted accepted event sequence")
        return sequence

    def start_segment(
        self, segment_id: str, kind: str, attributes: Attributes | None = None
    ) -> int:
        return self._submit(
            SegmentStartSubmission(
                segment_id, kind, validate_attributes(attributes or {})
            )
        )

    def stop_segment(self, segment_id: str, reason: str = "completed") -> int:
        return self._submit(SegmentStopSubmission(segment_id, reason))

    def add_marker(
        self,
        marker_id: str,
        kind: str,
        attributes: Attributes | None = None,
        *,
        source_time_ns: int | None = None,
        source_clock: str | None = None,
    ) -> int:
        return self._submit(
            MarkerSubmission(
                marker_id,
                kind,
                validate_attributes(attributes or {}),
                source_time_ns,
                source_clock,
            )
        )

    def record_event(self, event: CaptureEvent) -> int:
        from sifi_streamer.capture.events import copy_event

        return self._submit(copy_event(event))

    def poll_status(self) -> RecorderStatus | None:
        if self._closed:
            return self._status
        while True:
            try:
                self._status = self._statuses.get_nowait()
            except queue.Empty:
                return self._status

    def poll_failure(self) -> RecorderFailure | None:
        if self._failure is not None or self._closed:
            return self._failure
        try:
            self._failure = self._failures.get_nowait()
        except queue.Empty:
            if (
                self._started
                and not self._process.is_alive()
                and not (self._stopping and self._process.exitcode == 0)
            ):
                self._failure = RecorderFailure(
                    "recorder_exit",
                    f"recorder exited with code {self._process.exitcode}",
                )
        return self._failure

    def stop(self, reason: str = "normal_completion") -> None:
        if self._closed:
            return
        if not self._started:
            self.abort()
            return
        try:
            self._stopping = True
            self._producers["foreground"].complete()
            request_id = uuid.uuid4().hex
            self._commands.put(_Stop(request_id, reason))
            self._wait(request_id, self.config.shutdown_timeout_s)
            self._process.join(self.config.shutdown_timeout_s)
            if self._process.is_alive():
                raise RecordingError("recorder did not exit after final commit")
            if self._process.exitcode != 0:
                raise RecordingError(
                    f"recorder exited with code {self._process.exitcode}"
                )
        finally:
            self.abort()

    def abort(self) -> None:
        """Release transport after failure; never fabricate successful closure."""
        if self._closed:
            return
        # Do not acquire a dead sender's synchronization primitives. Shared frame
        # channels have no feeder threads or cleanup joins. Preserve observed stats.
        if self._started:
            if self._process.is_alive():
                self._process.terminate()
            self._process.join(5)
            if self._process.is_alive():
                raise RecordingError("recorder could not be terminated")
            self._process.close()
        self._closed = True
