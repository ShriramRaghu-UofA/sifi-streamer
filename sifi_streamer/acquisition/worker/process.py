"""Device worker: fixed stream registration, bounded recording, and live rings."""

import contextlib
import logging
import math
import queue
import signal
import threading
import time
from multiprocessing.shared_memory import SharedMemory

import numpy as np

from sifi_streamer.acquisition.config import StreamerConfig
from sifi_streamer.acquisition.devices import AcquisitionPacket, DeviceFactory
from sifi_streamer.acquisition.events import CaptureEventSource
from sifi_streamer.acquisition.health import (
    RawHealthSnapshot,
    WorkerFatal,
    WorkerHealthCollector,
)
from sifi_streamer.acquisition.ipc import (
    DeviceInfoUpdate,
    ErrorAck,
    ProcessSignal,
    Ready,
    StreamInfo,
    WorkerStopped,
)
from sifi_streamer.acquisition.ring_buffer import SeqlockRingBuffer
from sifi_streamer.acquisition.worker.acquisition import AcquisitionThread
from sifi_streamer.capture.events import CaptureEvent, DeviceInfoEvent, DiagnosticEvent
from sifi_streamer.capture.records import validate_document, validate_sample_value
from sifi_streamer.capture.transport import MessageChannel, ProducerEndpoint

logger = logging.getLogger(__name__)


def _ignore_console_interrupts() -> None:
    """The foreground launcher alone handles console Ctrl+C."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def background_main(
    config: StreamerConfig,
    device_factory: DeviceFactory,
    device_id: str,
    producer: ProducerEndpoint,
    start_event: ProcessSignal,
    stop_event: ProcessSignal,
    ack_queue: MessageChannel[Ready | WorkerStopped | ErrorAck],
    shm_prefix: str,
    health_queue: MessageChannel[RawHealthSnapshot],
    fatal_queue: MessageChannel[WorkerFatal],
    device_info_queue: MessageChannel[DeviceInfoUpdate],
    log_level: int = logging.INFO,
) -> None:
    """Own one device until final submissions and producer completion are sent."""
    logging.basicConfig(level=log_level)
    _ignore_console_interrupts()
    device = None
    acquisition = None
    rings: dict[str, SeqlockRingBuffer] = {}
    shms: dict[str, SharedMemory] = {}
    latest_info: dict[str, object] = {}
    revision = 0
    report_received = False
    local_stop = threading.Event()
    startup = True
    failure: BaseException | None = None
    report_lock = threading.Lock()

    def emit(event: CaptureEvent) -> None:
        nonlocal latest_info, revision, report_received
        with report_lock:
            producer.record_event(event)
            if isinstance(event, DeviceInfoEvent):
                latest_info = validate_document(event.info)
                revision += 1
                report_received = True
                update = DeviceInfoUpdate(revision, latest_info, device_id)
                with contextlib.suppress(queue.Full):
                    device_info_queue.put_nowait(update)

    try:
        device = device_factory()
        if isinstance(device, CaptureEventSource):
            device.set_capture_event_sink(emit)
        device.connect()
        if not report_received:
            emit(DeviceInfoEvent("after_connection", device.device_info))
        streams = tuple(device.streams)
        if len({spec.stream_id for spec in streams}) != len(streams):
            raise ValueError("duplicate stream IDs in device registry")
        specs = {spec.stream_id: spec for spec in streams}
        for index, spec in enumerate(streams):
            count = max(round(config.ring_buffer_seconds * spec.nominal_rate_hz), 16)
            shm = SharedMemory(
                name=f"{shm_prefix}_{index}",
                create=True,
                size=SeqlockRingBuffer.required_bytes(
                    count, spec.n_channels, dtype=spec.numpy_dtype
                ),
            )
            shms[spec.stream_id] = shm
            rings[spec.stream_id] = SeqlockRingBuffer(
                count, spec.n_channels, shm, dtype=spec.numpy_dtype, is_owner=True
            )
        health = WorkerHealthCollector(
            tuple(
                (f"{device_id}/{spec.stream_id}", spec.nominal_rate_hz, spec.n_channels)
                for spec in streams
            )
        )

        def on_packet(
            packet: AcquisitionPacket, monotonic_ns: int, unix_ns: int
        ) -> None:
            stream_id = packet.stream_id
            timestamps = tuple(
                value.item() if isinstance(value, np.generic) else value
                for value in packet.timestamps
            )
            data = {
                name: tuple(
                    value.item() if isinstance(value, np.generic) else value
                    for value in values
                )
                for name, values in packet.data.items()
            }
            validity = {
                name: tuple(
                    value.item() if isinstance(value, np.generic) else value
                    for value in values
                )
                for name, values in packet.validity.items()
            }
            if stream_id is not None:
                if stream_id not in specs:
                    raise ValueError(
                        f"packet references undeclared stream {stream_id!r}"
                    )
                spec = specs[stream_id]
                channels = tuple(channel.channel_id for channel in spec.channels)
                if set(data) != set(channels) or set(validity) != set(channels):
                    raise ValueError(
                        "packet channels differ from the fixed stream layout"
                    )
                count = len(timestamps)
                if any(
                    len(data[c]) != count or len(validity[c]) != count for c in channels
                ):
                    raise ValueError(
                        "timestamps, values, and validity must have equal lengths"
                    )
                if any(
                    type(flag) is not bool for c in channels for flag in validity[c]
                ):
                    raise TypeError("validity values must be bool")
                if any(
                    isinstance(value, bool)
                    or not isinstance(value, int | float)
                    or not math.isfinite(value)
                    for value in timestamps
                ):
                    raise ValueError("source timestamps must be finite numbers")
                for timestamp in timestamps:
                    validate_sample_value(
                        timestamp, np.dtype(spec.timestamp_dtype), "timestamp"
                    )
                for channel in channels:
                    for value in data[channel]:
                        if value is not None:
                            validate_sample_value(value, spec.numpy_dtype, channel)
                # Only the live path narrows timestamps to float64 for display.
                producer.send_packet(
                    stream_id=stream_id,
                    timestamps=timestamps,
                    data=data,
                    validity=validity,
                    document=packet.capture_document(),
                    reported_rate_hz=packet.reported_rate_hz,
                    samples_lost=packet.samples_lost,
                    status=packet.status,
                    received_monotonic_ns=monotonic_ns,
                    received_unix_ns=unix_ns,
                )
                matrix = np.zeros((count, spec.n_channels), dtype=spec.numpy_dtype)
                display_validity = np.zeros((count, spec.n_channels), dtype=np.bool_)
                for column, channel in enumerate(channels):
                    for row, value in enumerate(data[channel]):
                        if value is not None:
                            matrix[row, column] = value
                            display_validity[row, column] = validity[channel][row]
                if count:
                    rings[stream_id].write_samples(
                        matrix,
                        timestamps=np.asarray(timestamps, dtype=np.float64),
                        validity=display_validity,
                    )
                unit_scale = {
                    "seconds": 1.0,
                    "milliseconds": 1e-3,
                    "microseconds": 1e-6,
                    "nanoseconds": 1e-9,
                }.get(spec.timestamp_unit)
                health.observe(
                    f"{device_id}/{stream_id}",
                    timestamps=[float(t) * (unit_scale or 1.0) for t in timestamps],
                    validity=display_validity.tolist(),
                    reported_rate_hz=packet.reported_rate_hz,
                    samples_lost=packet.samples_lost,
                    status=packet.status,
                    misaligned=False,
                    evaluate_source_timing=unit_scale is not None,
                )
            else:
                producer.send_packet(
                    stream_id=None,
                    timestamps=(),
                    data={},
                    validity={},
                    document=packet.capture_document(),
                    reported_rate_hz=packet.reported_rate_hz,
                    samples_lost=packet.samples_lost,
                    status=packet.status,
                    received_monotonic_ns=monotonic_ns,
                    received_unix_ns=unix_ns,
                )

        infos = tuple(
            StreamInfo(
                f"{device_id}/{spec.stream_id}",
                rings[spec.stream_id].shm_name,
                rings[spec.stream_id].n_samples,
                tuple(c.channel_id for c in spec.channels),
                spec.nominal_rate_hz,
                spec.numpy_dtype.str,
                spec.label,
                tuple(c.label for c in spec.channels),
                tuple(c.unit for c in spec.channels),
                device_id,
                spec.stream_id,
                spec.source_clock,
                spec.timestamp_unit,
            )
            for spec in streams
        )
        with report_lock:
            ack_queue.put(Ready(infos, latest_info, revision, streams))
        while not start_event.wait(0.05):
            if stop_event.is_set() or producer.failed:
                return
        if stop_event.is_set():
            return
        startup = False
        acquisition = AcquisitionThread(device, on_packet, local_stop)
        acquisition.start()
        last_health = 0.0
        while not stop_event.wait(0.02):
            if producer.failed:
                raise RuntimeError("recorder failed")
            if not acquisition.is_alive():
                if acquisition.failure is not None:
                    raise acquisition.failure
                raise RuntimeError("device polling exited without a shutdown request")
            now = time.monotonic()
            if now - last_health >= 0.25:
                snapshot = health.snapshot(acquisition_alive=True)
                with contextlib.suppress(queue.Full):
                    health_queue.put_nowait(snapshot)
                last_health = now
    except BaseException as exc:
        failure = exc
        logger.exception("Device worker %s failed", device_id)
        code = "startup_failure" if startup else "acquisition_failure"
        try:
            emit(
                DiagnosticEvent(
                    "error",
                    device_id,
                    "startup" if startup else "acquisition",
                    code,
                    str(exc),
                    {"exception_type": type(exc).__name__},
                )
            )
        except BaseException as diagnostic_error:
            logger.error("Could not record worker failure: %s", diagnostic_error)
        fatal_queue.put(WorkerFatal(code, f"{device_id}: {exc}"))
        if startup:
            ack_queue.put(ErrorAck(str(exc)))
    finally:
        local_stop.set()
        cleanup_errors: list[BaseException] = []
        if device is not None:
            try:
                device.request_stop()
            except BaseException as exc:
                failure = failure or exc
                cleanup_errors.append(exc)
                fatal_queue.put(WorkerFatal("shutdown_failure", f"{device_id}: {exc}"))
            if acquisition is not None:
                acquisition.join(config.shutdown_timeout_s)
                if acquisition.is_alive():
                    timeout = TimeoutError("device read did not stop")
                    failure = failure or timeout
                    cleanup_errors.append(timeout)
                elif acquisition.failure is not None:
                    failure = failure or acquisition.failure
            try:
                device.disconnect()
            except BaseException as exc:
                failure = failure or exc
                cleanup_errors.append(exc)
                fatal_queue.put(WorkerFatal("shutdown_failure", f"{device_id}: {exc}"))
        for ring in rings.values():
            try:
                ring.close()
            except BaseException as exc:
                failure = failure or exc
                cleanup_errors.append(exc)
        for shm in shms.values():
            for release in (shm.close, shm.unlink):
                try:
                    release()
                except BaseException as exc:
                    failure = failure or exc
                    cleanup_errors.append(exc)
        if cleanup_errors:
            message = "; ".join(str(exc) for exc in cleanup_errors)
            fatal_queue.put(WorkerFatal("shutdown_failure", f"{device_id}: {message}"))
            try:
                emit(
                    DiagnosticEvent(
                        "error",
                        device_id,
                        "shutdown",
                        "shutdown_failure",
                        message,
                        {"errors": [type(exc).__name__ for exc in cleanup_errors]},
                    )
                )
            except BaseException as exc:
                failure = failure or exc
                logger.error("Could not record shutdown diagnostic: %s", exc)
        completed = False
        try:
            if acquisition is not None and acquisition.is_alive():
                raise TimeoutError("cannot complete producer while polling is alive")
            producer.complete()
            completed = True
        except BaseException as exc:
            failure = failure or exc
            fatal_queue.put(WorkerFatal("producer_failure", f"{device_id}: {exc}"))
        ack_queue.put(
            WorkerStopped(
                str(failure) if failure else None, latest_info, revision, completed
            )
        )
