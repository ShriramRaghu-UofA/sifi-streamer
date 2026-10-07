"""Background acquisition process entry point."""

import contextlib
import logging
import math
import queue
import signal
import threading
import time
from multiprocessing import Queue
from multiprocessing.shared_memory import SharedMemory

import numpy as np

from sifi_streamer.acquisition.config import StreamerConfig
from sifi_streamer.acquisition.devices import (
    AcquisitionDevice,
    AcquisitionPacket,
    DeviceFactory,
    SignalStreamSpec,
)
from sifi_streamer.acquisition.events import CaptureEventSource
from sifi_streamer.acquisition.health import WorkerFatal, WorkerHealthCollector
from sifi_streamer.acquisition.ipc import (
    DeviceInfoUpdate,
    ErrorAck,
    Ready,
    StartCapture,
    StreamInfo,
)
from sifi_streamer.acquisition.ring_buffer import SeqlockRingBuffer
from sifi_streamer.acquisition.worker.acquisition import AcquisitionThread
from sifi_streamer.acquisition.worker.command_handler import CommandHandler
from sifi_streamer.acquisition.worker.recorder import RecorderFSM
from sifi_streamer.capture.events import (
    CaptureEvent,
    DeviceInfoEvent,
    DiagnosticEvent,
)
from sifi_streamer.capture.records import validate_document
from sifi_streamer.exceptions import DeviceError

logger = logging.getLogger(__name__)


def _ignore_console_interrupts() -> None:
    """Leave console Ctrl+C ownership with the foreground launcher."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def background_main(
    config: StreamerConfig,
    device_factory: DeviceFactory,
    cmd_queue: Queue,
    ack_queue: Queue,
    shm_prefix: str,
    *,
    log_level: int = logging.INFO,
    health_queue: Queue | None = None,
    fatal_queue: Queue | None = None,
    startup_capture: StartCapture | None = None,
    device_info_queue: Queue | None = None,
) -> None:
    """Run device acquisition, shared-memory publication, and capture recording.

    This is the spawned worker entry point. It owns the device, shared-memory
    blocks, ring buffers, acquisition thread, and recorder for their full
    lifetimes, and reports startup success or failure through ``ack_queue``.

    Args:
        config: Shared-memory and recording settings.
        device_factory: Factory invoked in this process to create the device.
        cmd_queue: Commands from the foreground handle.
        ack_queue: Startup and command acknowledgements to the foreground.
        shm_prefix: Unique prefix for per-stream shared-memory names.
        log_level: Worker root logging level.
    """
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _ignore_console_interrupts()
    logger.info("Acquisition worker starting")
    recorder = RecorderFSM(config, None)
    latest_info: dict[str, object] = {}
    report_received = False
    report_revision = 0
    report_lock = threading.Lock()

    def emit(event: CaptureEvent) -> None:
        nonlocal latest_info, report_received, report_revision
        with report_lock:
            recorder.record_event(event)
            if isinstance(event, DeviceInfoEvent):
                latest_info = validate_document(event.info)
                report_received = True
                report_revision += 1
                if device_info_queue is not None:
                    device_info_queue.put(
                        DeviceInfoUpdate(report_revision, latest_info)
                    )

    if startup_capture is not None:
        try:
            recorder.start_capture(
                startup_capture.capture_file,
                startup_capture.capture_id,
                startup_capture.attributes,
                startup_capture.configuration,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            ack_queue.put(ErrorAck(str(exc)))
            return
    device: AcquisitionDevice | None = None
    try:
        device = device_factory()
        if isinstance(device, CaptureEventSource):
            device.set_capture_event_sink(emit)
        device.connect()
        if not report_received:
            emit(DeviceInfoEvent("after_start", device.device_info))
        streams: tuple[SignalStreamSpec, ...] = tuple(device.streams)
        if not streams or len({item.stream_id for item in streams}) != len(streams):
            raise ValueError("device streams must be non-empty and uniquely identified")
        logger.info(
            "Device connected with streams: %s",
            ", ".join(item.stream_id for item in streams),
        )
    except (DeviceError, OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.exception("Acquisition worker startup failed")
        message = str(exc)
        if device is not None:
            try:
                device.disconnect()
            except (
                DeviceError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as cleanup:
                logger.exception("Device cleanup after failed startup also failed")
                message += f"; device cleanup failed: {cleanup}"
        if startup_capture is not None:
            recorder.record_event(
                DiagnosticEvent(
                    "error",
                    "acquisition",
                    "startup",
                    "startup_failure",
                    message,
                    {"exception_type": type(exc).__name__},
                )
            )
            recorder.stop_capture("startup_failure")
        ack_queue.put(ErrorAck(message))
        return
    rings: dict[str, SeqlockRingBuffer] = {}
    shms: dict[str, SharedMemory] = {}
    try:
        for index, spec in enumerate(streams):
            count = max(round(config.ring_buffer_seconds * spec.nominal_rate_hz), 16)
            shm = SharedMemory(
                name=f"{shm_prefix}_{index}",
                create=True,
                size=SeqlockRingBuffer.required_bytes(
                    count, spec.n_channels, dtype=spec.numpy_dtype
                ),
            )
            ring = SeqlockRingBuffer(
                count, spec.n_channels, shm, dtype=spec.numpy_dtype, is_owner=True
            )
            shms[spec.stream_id] = shm
            rings[spec.stream_id] = ring
    except (OSError, ValueError) as exc:
        logger.exception("Could not allocate shared memory")
        if startup_capture is not None:
            recorder.record_event(
                DiagnosticEvent(
                    "error",
                    "acquisition",
                    "shared_memory",
                    "startup_failure",
                    str(exc),
                    {},
                )
            )
            recorder.stop_capture("startup_failure")
        ack_queue.put(ErrorAck(str(exc)))
        device.disconnect()
        for shm in shms.values():
            try:
                shm.close()
                shm.unlink()
            except OSError:
                pass
        return
    specs = {item.stream_id: item for item in streams}
    health = WorkerHealthCollector(
        tuple(
            (item.stream_id, item.nominal_rate_hz, item.n_channels) for item in streams
        )
    )

    def on_packet(packet: AcquisitionPacket) -> None:
        """Publish known signal samples and forward the full packet to recording."""
        stream_id = packet.stream_id
        if stream_id is not None and stream_id in rings:
            spec = specs[stream_id]
            rows: list[tuple[int, float]] = []
            for index, value in enumerate(packet.timestamps):
                try:
                    timestamp = float(value)
                except TypeError, ValueError:
                    continue
                if math.isfinite(timestamp):
                    rows.append((index, timestamp))
            matrix = np.zeros((len(rows), spec.n_channels), dtype=spec.numpy_dtype)
            validity = np.zeros((len(rows), spec.n_channels), dtype=np.bool_)
            misaligned = (
                not packet.timestamps
                or not packet.data
                or len(rows) != len(packet.timestamps)
            )
            for channel_index, channel in enumerate(spec.channels):
                values = packet.data.get(channel.channel_id)
                if values is None or isinstance(values, str | bytes):
                    misaligned = True
                    continue
                if len(values) != len(packet.timestamps):
                    misaligned = True
                for row_index, (source_index, _timestamp) in enumerate(rows):
                    if source_index >= len(values):
                        continue
                    value = values[source_index]
                    if value is None:
                        continue
                    try:
                        numeric = float(value)
                        if not math.isfinite(numeric):
                            continue
                        matrix[row_index, channel_index] = value
                    except TypeError, ValueError, OverflowError:
                        continue
                    validity[row_index, channel_index] = True
            timestamps = np.asarray([item[1] for item in rows], dtype=np.float64)
            if rows:
                rings[stream_id].write_samples(
                    matrix, timestamps=timestamps, validity=validity
                )
            health.observe(
                stream_id,
                timestamps=timestamps.tolist(),
                validity=validity.tolist(),
                reported_rate_hz=packet.reported_rate_hz,
                samples_lost=packet.samples_lost,
                status=packet.status,
                misaligned=misaligned,
            )
        recorder.on_packet(packet)

    stop_event = threading.Event()
    acquisition = AcquisitionThread(
        device, on_packet, stop_event, already_connected=True
    )
    handler = CommandHandler(cmd_queue, ack_queue, recorder, event_sink=emit)
    with report_lock:
        ready_info, ready_revision = latest_info, report_revision
    acquisition.start()
    ack_queue.put(
        Ready(
            tuple(
                StreamInfo(
                    spec.stream_id,
                    rings[spec.stream_id].shm_name,
                    rings[spec.stream_id].n_samples,
                    tuple(channel.channel_id for channel in spec.channels),
                    spec.nominal_rate_hz,
                    rings[spec.stream_id].dtype.str,
                    spec.label,
                    tuple(channel.label for channel in spec.channels),
                    tuple(channel.unit for channel in spec.channels),
                )
                for spec in streams
            ),
            ready_info,
            ready_revision,
        )
    )
    try:
        last_health = 0.0
        fatal_sent = False
        while not handler.tick():
            now = time.monotonic()
            if health_queue is not None and now - last_health >= 0.25:
                snapshot = health.snapshot(acquisition_alive=acquisition.is_alive())
                try:
                    health_queue.put_nowait(snapshot)
                except queue.Full:
                    with contextlib.suppress(queue.Empty):
                        health_queue.get_nowait()
                    with contextlib.suppress(queue.Full):
                        health_queue.put_nowait(snapshot)
                last_health = now
            if (
                not acquisition.is_alive()
                and acquisition.failure is not None
                and not fatal_sent
            ):
                if recorder.active:
                    recorder.record_event(
                        DiagnosticEvent(
                            "error",
                            "acquisition",
                            "during_capture",
                            "acquisition_failure",
                            str(acquisition.failure),
                            {"exception_type": type(acquisition.failure).__name__},
                        )
                    )
                if fatal_queue is not None:
                    logger.error("Acquisition thread failed: %s", acquisition.failure)
                    fatal_queue.put(
                        WorkerFatal("acquisition_failure", str(acquisition.failure))
                    )
                fatal_sent = True
    finally:
        logger.info("Acquisition worker shutting down")
        stop_event.set()
        device.disconnect()
        acquisition.join(timeout=5)
        recorder.close()
        for stream_id, ring in rings.items():
            ring.close()
            shm = shms[stream_id]
            try:
                shm.close()
                shm.unlink()
            except OSError:
                pass
        logger.info("Acquisition worker stopped")
