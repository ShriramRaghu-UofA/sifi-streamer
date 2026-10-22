"""Byte-bounded producer submissions for the single capture recorder.

Endpoints are created before spawning workers. Each producer has a shared byte
ring divided into credit-controlled slots. OS semaphores publish only complete
frames and return credits after consumption; no reader mutex can be stranded by
producer death and no feeder thread can bypass the allocated byte budget.
"""

import math
import pickle
import queue
import struct
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from ctypes import Array
from dataclasses import dataclass, field
from multiprocessing.context import BaseContext
from multiprocessing.synchronize import Lock, Semaphore
from typing import Protocol, cast

from sifi_streamer.capture.events import CaptureEvent, copy_event
from sifi_streamer.capture.records import (
    Attributes,
    validate_attributes,
    validate_document,
)
from sifi_streamer.exceptions import RecordingError


@dataclass(frozen=True, slots=True)
class RecorderConfig:
    """Finite limits for recording, independent from live ring-buffer settings."""

    commit_interval_s: float = 1.0
    transaction_target_bytes: int = 8 << 20
    producer_buffer_bytes: int = 32 << 20
    max_submission_bytes: int = 8 << 20
    enqueue_timeout_s: float = 2.0
    shutdown_timeout_s: float = 30.0

    def __post_init__(self) -> None:
        for name in ("commit_interval_s", "enqueue_timeout_s", "shutdown_timeout_s"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in (
            "transaction_target_bytes",
            "producer_buffer_bytes",
            "max_submission_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_submission_bytes > self.producer_buffer_bytes:
            raise ValueError("maximum submission exceeds producer buffer capacity")


@dataclass(frozen=True, slots=True)
class PacketSubmission:
    stream_id: str | None
    timestamps: tuple[int | float, ...]
    data: dict[str, tuple[int | float | None, ...]]
    validity: dict[str, tuple[bool, ...]]
    document: dict[str, object] | None
    reported_rate_hz: float | None = None
    samples_lost: int = 0
    status: str = "ok"


@dataclass(frozen=True, slots=True)
class MarkerSubmission:
    marker_id: str
    marker_kind: str
    attributes: Attributes
    source_time_ns: int | None = None
    source_clock: str | None = None


@dataclass(frozen=True, slots=True)
class SegmentStartSubmission:
    segment_id: str
    segment_kind: str
    attributes: Attributes


@dataclass(frozen=True, slots=True)
class SegmentStopSubmission:
    segment_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class ProducerComplete:
    """Ordered barrier following a producer's last contribution."""


type SubmissionBody = (
    PacketSubmission
    | MarkerSubmission
    | SegmentStartSubmission
    | SegmentStopSubmission
    | CaptureEvent
    | ProducerComplete
)


@dataclass(frozen=True, slots=True)
class Submission:
    producer_id: str
    producer_sequence: int
    received_monotonic_ns: int
    received_unix_ns: int
    body: SubmissionBody
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class FailureFlag:
    """Nonblocking semaphore latch with no process-owned lock.

    A process killed while inspecting the latch cannot strand a mutex. Process
    liveness remains authoritative; the latch is a prompt shutdown notification.
    """

    signal: Semaphore

    def set(self) -> None:
        self.signal.release()

    def is_set(self) -> bool:
        if not self.signal.acquire(False):
            return False
        self.signal.release()
        return True


class _FrameRing(Protocol):
    """Shared frame mechanics; writer and reader cursors have separate owners."""

    buffer: Array
    occupied: Array
    slot_bytes: int
    slot_count: int
    available: Semaphore
    free_slots: Semaphore
    _write_slot: int
    _read_slot: int

    def _write_frame(self, frame: bytes, slots: int) -> None: ...


def _copy_frame(ring: _FrameRing, frame: bytes, slots: int) -> None:
    view = memoryview(ring.buffer).cast("B")
    start = ring._write_slot * ring.slot_bytes
    first = min(len(frame), len(view) - start)
    view[start : start + first] = frame[:first]
    view[: len(frame) - first] = frame[first:]
    ring._write_slot = (ring._write_slot + slots) % ring.slot_count


def _publish_frame(
    ring: _FrameRing,
    serialized: bytes,
    maximum: int,
    timeout: float,
    failed: Callable[[], bool] | None = None,
) -> None:
    size = len(serialized)
    slots = math.ceil((size + 8) / ring.slot_bytes)
    if size > maximum or slots > ring.slot_count:
        raise RecordingError(
            f"message is {size} bytes; maximum is {maximum} bytes "
            f"within {len(ring.buffer)} framed buffer bytes"
        )
    deadline = time.monotonic() + timeout
    acquired = 0
    first_slot = ring._write_slot
    marked = False
    try:
        while acquired < slots:
            if failed is not None and failed():
                raise RecordingError("capture recorder has failed")
            if ring.free_slots.acquire(False):
                acquired += 1
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Full
            if ring.free_slots.acquire(timeout=min(remaining, 0.05)):
                acquired += 1
        marked = True
        for offset in range(slots):
            ring.occupied[(first_slot + offset) % ring.slot_count] = 1
        ring._write_frame(struct.pack("<Q", size) + serialized, slots)
        # OS semaphore release/acquire publishes all preceding frame writes.
        ring.available.release()
    except BaseException:
        ring._write_slot = first_slot
        if marked:
            for offset in range(slots):
                ring.occupied[(first_slot + offset) % ring.slot_count] = 0
        for _ in range(acquired):
            ring.free_slots.release()
        raise


def _receive_frame(ring: _FrameRing, maximum: int) -> bytes | None:
    if not ring.available.acquire(False):
        return None
    view = memoryview(ring.buffer).cast("B")
    start = ring._read_slot * ring.slot_bytes
    size = struct.unpack("<Q", view[start : start + 8])[0]
    if size > maximum or size + 8 > len(view):
        raise RecordingError("invalid published transport frame length")
    total = size + 8
    first = min(total, len(view) - start)
    frame = bytes(view[start : start + first]) + bytes(view[: total - first])
    return frame[8:]


def _release_frame(ring: _FrameRing, size: int) -> None:
    slots = math.ceil((size + 8) / ring.slot_bytes)
    for offset in range(slots):
        ring.occupied[(ring._read_slot + offset) % ring.slot_count] = 0
    ring._read_slot = (ring._read_slot + slots) % ring.slot_count
    for _ in range(slots):
        ring.free_slots.release()


class _ReceiverLock:
    """Receiver-thread serialization recreated locally when a channel is spawned."""

    def __init__(self) -> None:
        self.lock = threading.Lock()

    def __getstate__(self) -> tuple[()]:
        return ()

    def __setstate__(self, state: tuple[()]) -> None:
        self.lock = threading.Lock()


@dataclass(slots=True)
class MessageChannel[T]:
    """Bounded complete-message IPC with one sending and one receiving process.

    Sender threads serialize through a finite-deadline lock. The sole receiver
    never acquires it. A sender killed midway through copying cannot publish a
    partial frame or strand the receiver. There are no feeder threads or queue
    cleanup joins. Receiving threads serialize through a process-local lock;
    killing the sending process cannot strand that local receiver lock.
    """

    buffer: Array
    occupied: Array
    slot_bytes: int
    slot_count: int
    available: Semaphore
    free_slots: Semaphore
    send_lock: Lock
    max_message_bytes: int
    timeout_s: float
    _write_slot: int = 0
    _read_slot: int = 0
    _receiver: _ReceiverLock = field(default_factory=_ReceiverLock, repr=False)

    @classmethod
    def create(
        cls,
        context: BaseContext,
        *,
        capacity_bytes: int = 32 << 20,
        max_message_bytes: int = 8 << 20,
        timeout_s: float = 2.0,
    ) -> MessageChannel[T]:
        for name, value in (
            ("capacity_bytes", capacity_bytes),
            ("max_message_bytes", max_message_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 16:
                raise ValueError(f"{name} must be an integer of at least 16 bytes")
        if max_message_bytes + 8 > capacity_bytes:
            raise ValueError("maximum framed message exceeds channel capacity")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        slot_bytes = min(64 << 10, max_message_bytes + 8)
        # Allocate whole slots while respecting the requested maximum capacity.
        slot_count = capacity_bytes // slot_bytes
        if math.ceil((max_message_bytes + 8) / slot_bytes) > slot_count:
            raise ValueError("maximum framed message does not fit whole channel slots")
        return cls(
            context.RawArray("B", slot_bytes * slot_count),
            context.RawArray("b", slot_count),
            slot_bytes,
            slot_count,
            context.Semaphore(0),
            context.Semaphore(slot_count),
            context.Lock(),
            max_message_bytes,
            timeout_s,
        )

    def _write_frame(self, frame: bytes, slots: int) -> None:
        _copy_frame(self, frame, slots)

    def put(self, value: T, block: bool = True, timeout: float | None = None) -> None:
        duration = self.timeout_s if timeout is None else timeout
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("message timeout must be finite and non-negative")
        if not block:
            duration = 0
        deadline = time.monotonic() + duration
        acquired = (
            self.send_lock.acquire(False)
            if duration == 0
            else self.send_lock.acquire(timeout=duration)
        )
        if not acquired:
            raise queue.Full
        try:
            serialized = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            _publish_frame(
                self,
                serialized,
                self.max_message_bytes,
                max(0.0, deadline - time.monotonic()),
            )
        finally:
            self.send_lock.release()

    def put_nowait(self, value: T) -> None:
        self.put(value, block=False)

    def get(self, block: bool = True, timeout: float | None = None) -> T:
        duration = self.timeout_s if timeout is None else timeout
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("message timeout must be finite and non-negative")
        if not block:
            duration = 0
        deadline = time.monotonic() + duration
        acquired = (
            self._receiver.lock.acquire(False)
            if duration == 0
            else self._receiver.lock.acquire(timeout=duration)
        )
        if not acquired:
            raise queue.Empty
        try:
            while True:
                serialized = _receive_frame(self, self.max_message_bytes)
                if serialized is not None:
                    try:
                        return cast(T, pickle.loads(serialized))
                    finally:
                        _release_frame(self, len(serialized))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue.Empty
                time.sleep(min(remaining, 0.005))
        finally:
            self._receiver.lock.release()

    def get_nowait(self) -> T:
        return self.get(block=False)


@dataclass(slots=True)
class ProducerEndpoint:
    """Picklable, single ordered producer endpoint.

    One process owns the sending side and one owns the receiving side. Their
    ring cursors are private, not shared counters. Calls copy inputs immediately.
    Packet sends return after bounded transport admission; recorder acceptance
    is reported cumulatively. A foreground command may request an explicit ack.
    """

    producer_id: str
    device_id: str | None
    config: RecorderConfig
    buffer: Array
    occupied: Array
    slot_bytes: int
    slot_count: int
    available: Semaphore
    free_slots: Semaphore
    send_lock: Lock
    failed_event: FailureFlag
    error_buffer: Array
    error_ready: Semaphore
    _write_slot: int = 0
    _read_slot: int = 0
    _sequence: int = 0
    _completed: bool = False

    @property
    def failed(self) -> bool:
        return self.failed_event.is_set()

    @property
    def pending_bytes(self) -> int:
        """Approximate reserved capacity, rounded to transport slots.

        Occupancy flags are telemetry only. Semaphore credits, not these reads,
        enforce capacity and publication ordering.
        """
        return sum(self.occupied) * self.slot_bytes

    @property
    def failure_message(self) -> str | None:
        if not self.error_ready.acquire(False):
            return None
        message = bytes(self.error_buffer).split(b"\0", 1)[0].decode("utf-8")
        self.error_ready.release()
        return message

    def fail(self, message: str) -> None:
        """Fail the whole capture; the recorder/coordinator owns teardown."""
        text = message.encode("utf-8")[:4092].decode("utf-8", errors="ignore").encode()
        view = memoryview(self.error_buffer).cast("B")
        view[:] = b"\0" * len(view)
        view[: len(text)] = text
        self.error_ready.release()
        self.failed_event.set()

    def _write_frame(self, frame: bytes, slots: int) -> None:
        """Copy before publication; terminating here exposes no partial frame."""
        _copy_frame(self, frame, slots)

    def receive(self) -> bytes | None:
        """Receive one fully published frame without a blocking pipe read."""
        return _receive_frame(self, self.config.max_submission_bytes)

    def send(
        self,
        body: SubmissionBody,
        *,
        received_monotonic_ns: int | None = None,
        received_unix_ns: int | None = None,
        request_id: str | None = None,
    ) -> None:
        """Submit under a byte budget and fail explicitly on pressure timeout."""
        monotonic_ns = (
            time.monotonic_ns()
            if received_monotonic_ns is None
            else received_monotonic_ns
        )
        unix_ns = time.time_ns() if received_unix_ns is None else received_unix_ns
        with self.send_lock:
            if self.failed:
                raise RecordingError("capture recorder has failed")
            if self._completed:
                raise RecordingError(f"producer {self.producer_id!r} is complete")
            submission = Submission(
                self.producer_id,
                self._sequence,
                monotonic_ns,
                unix_ns,
                body,
                request_id,
            )
            serialized = pickle.dumps(submission, protocol=pickle.HIGHEST_PROTOCOL)
            try:
                _publish_frame(
                    self,
                    serialized,
                    self.config.max_submission_bytes,
                    self.config.enqueue_timeout_s,
                    lambda: self.failed,
                )
            except queue.Full as exc:
                message = f"producer {self.producer_id!r} enqueue deadline exceeded"
                self.fail(message)
                raise RecordingError(message) from exc
            except RecordingError as exc:
                self.fail(str(exc))
                raise
            self._sequence += 1
            if isinstance(body, ProducerComplete):
                self._completed = True

    def send_packet(
        self,
        *,
        stream_id: str | None,
        timestamps: Sequence[int | float],
        data: Mapping[str, Sequence[int | float | None]],
        validity: Mapping[str, Sequence[bool]],
        document: Mapping[str, object] | None,
        reported_rate_hz: float | None = None,
        samples_lost: int = 0,
        status: str = "ok",
        received_monotonic_ns: int | None = None,
        received_unix_ns: int | None = None,
    ) -> None:
        self.send(
            PacketSubmission(
                stream_id,
                tuple(timestamps),
                {name: tuple(values) for name, values in data.items()},
                {name: tuple(values) for name, values in validity.items()},
                None if document is None else validate_document(document),
                reported_rate_hz,
                samples_lost,
                status,
            ),
            received_monotonic_ns=received_monotonic_ns,
            received_unix_ns=received_unix_ns,
        )

    def record_event(
        self,
        event: CaptureEvent,
        *,
        received_monotonic_ns: int | None = None,
        received_unix_ns: int | None = None,
    ) -> None:
        self.send(
            copy_event(event),
            received_monotonic_ns=received_monotonic_ns,
            received_unix_ns=received_unix_ns,
        )

    def complete(self) -> None:
        """Send the final ordered producer barrier exactly once."""
        if not self._completed:
            self.send(ProducerComplete())

    def marker(
        self,
        marker_id: str,
        marker_kind: str,
        attributes: Attributes,
        *,
        request_id: str,
        source_time_ns: int | None = None,
        source_clock: str | None = None,
    ) -> None:
        self.send(
            MarkerSubmission(
                marker_id,
                marker_kind,
                validate_attributes(attributes),
                source_time_ns,
                source_clock,
            ),
            request_id=request_id,
        )

    def release(self, size: int) -> None:
        """Return reserved slots only after the recorder finishes consumption."""
        _release_frame(self, size)
