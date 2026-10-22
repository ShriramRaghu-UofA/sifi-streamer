"""Foreground owner of a recorder and a fixed collection of device workers."""

import logging
import multiprocessing
import queue
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Self

from sifi_streamer.acquisition.config import StreamerConfig
from sifi_streamer.acquisition.devices import DeviceFactory
from sifi_streamer.acquisition.health import RawHealthSnapshot, WorkerFatal
from sifi_streamer.acquisition.ipc import (
    CaptureConfiguration,
    DeviceInfoUpdate,
    ErrorAck,
    ProcessSignal,
    Ready,
    StreamInfo,
    WorkerStopped,
)
from sifi_streamer.acquisition.reader import SharedMemoryReader
from sifi_streamer.acquisition.worker.process import background_main
from sifi_streamer.capture.events import CaptureEvent
from sifi_streamer.capture.recorder import RecorderHandle, RecorderStatus
from sifi_streamer.capture.records import Attributes, Packet, validate_document
from sifi_streamer.capture.transport import MessageChannel, RecorderConfig
from sifi_streamer.exceptions import AckError, AckTimeoutError, RecordingError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Worker:
    device_id: str
    process: BaseProcess
    stop: ProcessSignal
    acknowledgements: MessageChannel[Ready | WorkerStopped | ErrorAck]
    health: MessageChannel[RawHealthSnapshot]
    fatal: MessageChannel[WorkerFatal]
    reports: MessageChannel[DeviceInfoUpdate]
    ready: bool = False


class BackgroundHandle:
    """Own one recorder plus one spawned worker per named device.

    A capture must be prepared before entry. A single factory is shorthand for
    ``{"device": factory}``; all paths use the same multi-device implementation.
    """

    def __init__(
        self,
        config: StreamerConfig,
        device_factory: DeviceFactory | Mapping[str, DeviceFactory],
        *,
        background_log_level: int = logging.INFO,
    ) -> None:
        devices = (
            dict(device_factory)
            if isinstance(device_factory, Mapping)
            else {"device": device_factory}
        )
        if not devices:
            raise ValueError("at least one device factory is required")
        if any(not name or name != name.strip() or "/" in name for name in devices):
            raise ValueError("device IDs must be non-empty trimmed strings without '/'")
        self._config = config
        self._factories = devices
        self._log_level = background_log_level
        self._startup_capture: CaptureConfiguration | None = None
        self._recorder: RecorderHandle | None = None
        self._recorder_status: RecorderStatus | None = None
        self._workers: dict[str, _Worker] = {}
        self._stream_readers: dict[str, SharedMemoryReader] = {}
        self._streams: tuple[StreamInfo, ...] = ()
        self._device_infos: dict[str, dict[str, object]] = {}
        self._report_revisions: dict[str, int] = {}
        self._latest_health: dict[str, RawHealthSnapshot] = {}
        self._fatal_events: queue.Queue[WorkerFatal] = queue.Queue()
        self._failure: WorkerFatal | None = None
        self._recorder_failed = False
        self._incomplete_producers = False
        self._entered = False
        self._attempted = False
        self._devices_stopped = False
        self._closed = False
        self._supervisor_stop = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._shutdown_lock = threading.RLock()

    def prepare_capture(
        self,
        capture_file: Path,
        capture_id: str,
        attributes: Attributes,
        configuration: Packet,
    ) -> None:
        if self._attempted:
            raise RuntimeError("capture must be prepared before worker startup")
        self._startup_capture = CaptureConfiguration(
            capture_file, capture_id, dict(attributes), validate_document(configuration)
        )

    def __enter__(self) -> Self:
        if self._entered:
            return self
        if self._attempted:
            raise RuntimeError("an acquisition handle cannot be restarted")
        if self._startup_capture is None:
            raise RuntimeError("prepare_capture must precede acquisition startup")
        self._attempted = True
        capture = self._startup_capture
        config = self._config
        context = multiprocessing.get_context("spawn")
        recorder_config = RecorderConfig(
            commit_interval_s=config.commit_interval_s,
            transaction_target_bytes=config.transaction_target_bytes,
            producer_buffer_bytes=config.producer_buffer_bytes,
            max_submission_bytes=config.max_submission_bytes,
            enqueue_timeout_s=config.enqueue_timeout_s,
            shutdown_timeout_s=config.shutdown_timeout_s,
        )
        self._recorder = RecorderHandle(
            capture.capture_file,
            capture.capture_id,
            capture.attributes,
            configuration=capture.configuration,
            producer_devices={
                "foreground": None,
                **{f"device:{name}": name for name in self._factories},
            },
            config=recorder_config,
        )
        try:
            self._recorder.start()
            gate = ProcessSignal(context.Semaphore(0), readers=len(self._factories))
            for name, factory in self._factories.items():
                self._recorder.register_device(
                    name,
                    integration=getattr(factory, "__name__", type(factory).__name__),
                )
                acknowledgements, health, fatal, reports = (
                    MessageChannel[Ready | WorkerStopped | ErrorAck].create(
                        context,
                        capacity_bytes=max(4 * config.max_submission_bytes, 64 << 10),
                        max_message_bytes=config.max_submission_bytes,
                        timeout_s=config.enqueue_timeout_s,
                    ),
                    MessageChannel[RawHealthSnapshot].create(
                        context,
                        capacity_bytes=512 << 10,
                        max_message_bytes=256 << 10,
                        timeout_s=config.enqueue_timeout_s,
                    ),
                    MessageChannel[WorkerFatal].create(
                        context,
                        capacity_bytes=64 << 10,
                        max_message_bytes=16 << 10,
                        timeout_s=config.enqueue_timeout_s,
                    ),
                    MessageChannel[DeviceInfoUpdate].create(
                        context,
                        capacity_bytes=max(2 * config.max_submission_bytes, 64 << 10),
                        max_message_bytes=config.max_submission_bytes,
                        timeout_s=config.enqueue_timeout_s,
                    ),
                )
                stop = ProcessSignal(context.Semaphore(0))
                process = context.Process(
                    target=background_main,
                    kwargs={
                        "config": config,
                        "device_factory": factory,
                        "device_id": name,
                        "producer": self._recorder.producer(f"device:{name}"),
                        "start_event": gate,
                        "stop_event": stop,
                        "ack_queue": acknowledgements,
                        "health_queue": health,
                        "fatal_queue": fatal,
                        "device_info_queue": reports,
                        "shm_prefix": f"acq_{uuid.uuid4().hex[:12]}",
                        "log_level": self._log_level,
                    },
                    name=f"acquisition-{name}",
                )
                worker = _Worker(
                    name, process, stop, acknowledgements, health, fatal, reports
                )
                self._workers[name] = worker
                process.start()
            infos: list[StreamInfo] = []
            for worker in self._workers.values():
                ready = self._wait_ready(worker)
                worker.ready = True
                infos.extend(ready.streams)
                self._device_infos[worker.device_id] = ready.device_info
                self._report_revisions[worker.device_id] = ready.device_info_revision
                for spec in ready.registry:
                    self._recorder.register_stream(worker.device_id, spec)
                for info in ready.streams:
                    self._stream_readers[info.stream_id] = SharedMemoryReader(
                        info.shm_name,
                        info.n_samples,
                        info.n_channels,
                        dtype=info.payload_dtype,
                    )
            self._recorder.seal_registration()
            self._streams = tuple(infos)
            self._entered = True
            gate.set()
            self._supervisor = threading.Thread(
                target=self._supervise, name="capture-supervisor", daemon=True
            )
            self._supervisor.start()
            return self
        except BaseException:
            self._close("startup_failure", raise_failure=False)
            raise

    def _wait_ready(self, worker: _Worker) -> Ready:
        deadline = time.monotonic() + self._config.startup_timeout_s
        while time.monotonic() < deadline:
            try:
                message = worker.acknowledgements.get(timeout=0.05)
            except queue.Empty:
                if not worker.process.is_alive():
                    raise AckError(
                        f"device {worker.device_id} exited during startup"
                    ) from None
                self._check_recorder()
                continue
            if isinstance(message, Ready):
                return message
            if isinstance(message, ErrorAck):
                raise AckError(
                    f"device {worker.device_id} startup failed: {message.message}"
                )
            raise AckError(
                f"device {worker.device_id} stopped before registration: {message}"
            )
        raise AckTimeoutError(f"device {worker.device_id} startup timed out")

    def _remember_failure(self, failure: WorkerFatal) -> None:
        if self._failure is None:
            self._failure = failure
            self._fatal_events.put(failure)

    def _check_recorder(self) -> None:
        if self._recorder is not None:
            failure = self._recorder.poll_failure()
            if failure is not None:
                self._recorder_failed = True
                raise RecordingError(str(failure))

    def _supervise(self) -> None:
        while not self._supervisor_stop.wait(0.05):
            try:
                with self._shutdown_lock:
                    if self._supervisor_stop.is_set():
                        return
                    self._check_recorder()
                    for worker in self._workers.values():
                        if not self._devices_stopped and not worker.process.is_alive():
                            raise RecordingError(
                                f"device {worker.device_id} exited unexpectedly"
                            )
                        try:
                            failure = worker.fatal.get_nowait()
                        except queue.Empty:
                            failure = None
                        if failure is not None:
                            self._remember_failure(failure)
                            raise RecordingError(failure.message)
            except BaseException as exc:
                self._remember_failure(WorkerFatal("acquisition_failure", str(exc)))
                self._close("acquisition_failure", raise_failure=False)
                return

    def stop_devices(self) -> None:
        """Finish all producers before permitting recorder finalization."""
        with self._shutdown_lock:
            if self._devices_stopped:
                return
            self._devices_stopped = True
            for worker in self._workers.values():
                worker.stop.set()
            deadline = time.monotonic() + self._config.shutdown_timeout_s
            for worker in self._workers.values():
                if worker.process.pid is None:
                    continue
                worker.process.join(max(0, deadline - time.monotonic()))
                if worker.process.is_alive():
                    self._incomplete_producers = True
                    worker.process.terminate()
                    worker.process.join(2)
                    self._remember_failure(
                        WorkerFatal(
                            "shutdown_failure",
                            f"device {worker.device_id} did not stop",
                        )
                    )
                if worker.process.exitcode not in (0, None):
                    self._incomplete_producers = True
                    self._remember_failure(
                        WorkerFatal(
                            "shutdown_failure",
                            f"device {worker.device_id} exited with "
                            f"{worker.process.exitcode}",
                        )
                    )
                    continue
                while True:
                    try:
                        stopped = worker.acknowledgements.get_nowait()
                    except queue.Empty:
                        break
                    if isinstance(stopped, WorkerStopped) and stopped.failure:
                        self._remember_failure(
                            WorkerFatal("shutdown_failure", stopped.failure)
                        )
                    if isinstance(stopped, WorkerStopped):
                        if not stopped.producer_completed:
                            self._incomplete_producers = True
                        self._device_infos[worker.device_id] = stopped.device_info
                        self._report_revisions[worker.device_id] = (
                            stopped.device_info_revision
                        )
            if self._failure is not None:
                raise RecordingError(self._failure.message)

    def _close(self, reason: str, *, raise_failure: bool = True) -> None:
        with self._shutdown_lock:
            if self._closed:
                if raise_failure and self._failure:
                    raise RecordingError(self._failure.message)
                return
            self._closed = True
            self._supervisor_stop.set()
            try:
                self.stop_devices()
            except BaseException as exc:
                self._remember_failure(WorkerFatal("shutdown_failure", str(exc)))
            if self._recorder is not None:
                try:
                    if self._recorder_failed or self._incomplete_producers:
                        self._recorder.abort()
                    else:
                        for name in self._factories:
                            worker = self._workers.get(name)
                            if worker is None or worker.process.pid is None:
                                self._recorder.producer(f"device:{name}").complete()
                        final_reason = reason
                        if self._failure and reason != "startup_failure":
                            final_reason = "acquisition_failure"
                        self._recorder.stop(final_reason)
                except BaseException as exc:
                    self._remember_failure(WorkerFatal("recorder_failure", str(exc)))
                    try:
                        self._recorder.abort()
                    except BaseException as abort_error:
                        logger.error("Recorder cleanup failed: %s", abort_error)
            try:
                self._drain_reports()
            except BaseException as exc:
                self._remember_failure(WorkerFatal("shutdown_failure", str(exc)))
            for reader in self._stream_readers.values():
                try:
                    reader.close()
                except BaseException as exc:
                    self._remember_failure(WorkerFatal("shutdown_failure", str(exc)))
            self._stream_readers.clear()
            self._entered = False
            if raise_failure and self._failure:
                raise RecordingError(self._failure.message)

    def __exit__(self, *_: object) -> None:
        self._close("operator_request")
        if (
            self._supervisor is not None
            and self._supervisor is not threading.current_thread()
        ):
            self._supervisor.join(self._config.shutdown_timeout_s)

    def stop_capture(self, reason: str = "normal_completion") -> None:
        self._close(reason)

    def _require_recorder(self) -> RecorderHandle:
        if not self._entered or self._recorder is None or self._closed:
            raise RuntimeError("capture is not active")
        if self._failure:
            raise RecordingError(self._failure.message)
        return self._recorder

    def record_event(self, event: CaptureEvent) -> None:
        self._require_recorder().record_event(event)

    def start_segment(
        self, segment_id: str, segment_kind: str, attributes: Attributes | None = None
    ) -> None:
        self._require_recorder().start_segment(
            segment_id, segment_kind, dict(attributes or {})
        )

    def stop_segment(self, segment_id: str, reason: str | None = None) -> None:
        self._require_recorder().stop_segment(segment_id, reason or "completed")

    def add_marker(
        self,
        marker_id: str,
        marker_kind: str,
        attributes: Attributes | None = None,
        *,
        source_time_ns: int | None = None,
        source_clock: str | None = None,
    ) -> None:
        self._require_recorder().add_marker(
            marker_id,
            marker_kind,
            dict(attributes or {}),
            source_time_ns=source_time_ns,
            source_clock=source_clock,
        )

    @property
    def streams(self) -> tuple[StreamInfo, ...]:
        return self._streams

    @property
    def stream_readers(self) -> dict[str, SharedMemoryReader]:
        return dict(self._stream_readers)

    def _drain_reports(self) -> None:
        if self._closed and not self._entered:
            return
        for name, worker in self._workers.items():
            if worker.process.pid is None or worker.process.exitcode not in (0, None):
                continue
            while True:
                try:
                    update: DeviceInfoUpdate = worker.reports.get_nowait()
                except queue.Empty:
                    break
                if update.revision > self._report_revisions.get(name, 0):
                    self._device_infos[name] = update.info
                    self._report_revisions[name] = update.revision

    @property
    def device_infos(self) -> dict[str, dict[str, object]]:
        with self._shutdown_lock:
            self._drain_reports()
            return {
                name: validate_document(info)
                for name, info in self._device_infos.items()
            }

    @property
    def device_info(self) -> dict[str, object]:
        reports = self.device_infos
        if len(reports) == 1:
            return next(iter(reports.values()))
        return {"devices": reports}

    @property
    def launch_configuration(self) -> dict[str, object]:
        return validate_document(
            self._startup_capture.configuration if self._startup_capture else {}
        )

    @property
    def recorder_stats(self) -> RecorderStatus | None:
        if self._recorder is not None:
            status = self._recorder.poll_status()
            if status is not None:
                self._recorder_status = status
        return self._recorder_status

    def poll_fatal(self) -> WorkerFatal | None:
        try:
            return self._fatal_events.get_nowait()
        except queue.Empty:
            return None

    def poll_health(self) -> RawHealthSnapshot | None:
        with self._shutdown_lock:
            return self._poll_health()

    def _poll_health(self) -> RawHealthSnapshot | None:
        if self._closed:
            return None
        for name, worker in self._workers.items():
            while True:
                try:
                    self._latest_health[name] = worker.health.get_nowait()
                except queue.Empty:
                    break
        if not self._latest_health:
            return None
        return RawHealthSnapshot(
            time.monotonic(),
            self._failure is None and not self._devices_stopped,
            tuple(
                stream
                for snapshot in self._latest_health.values()
                for stream in snapshot.streams
            ),
        )
