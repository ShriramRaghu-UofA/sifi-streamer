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
    AcquisitionPacket,
    DeviceFactory,
    SignalStreamSpec,
)
from sifi_streamer.acquisition.health import WorkerFatal, WorkerHealthCollector
from sifi_streamer.acquisition.ipc import ErrorAck, Ready, StreamInfo
from sifi_streamer.acquisition.ring_buffer import SeqlockRingBuffer
from sifi_streamer.acquisition.worker.acquisition import AcquisitionThread
from sifi_streamer.acquisition.worker.command_handler import CommandHandler
from sifi_streamer.acquisition.worker.recorder import RecorderFSM
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
    try:
        device = device_factory()
        device.connect()
        streams: tuple[SignalStreamSpec, ...] = tuple(device.streams)
        if not streams or len({item.stream_id for item in streams}) != len(streams):
            raise ValueError("device streams must be non-empty and uniquely identified")
        logger.info(
            "Device connected with streams: %s",
            ", ".join(item.stream_id for item in streams),
        )
    except (DeviceError, OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.exception("Acquisition worker startup failed")
        ack_queue.put(ErrorAck(str(exc)))
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
        ack_queue.put(ErrorAck(str(exc)))
        device.disconnect()
        for shm in shms.values():
            try:
                shm.close()
                shm.unlink()
            except OSError:
                pass
        return
    recorder = RecorderFSM(config, device.device_info)
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
                reported_rate_hz=getattr(packet, "reported_rate_hz", None),
                samples_lost=getattr(packet, "samples_lost", 0),
                status=getattr(packet, "status", "ok"),
                misaligned=misaligned,
            )
        recorder.on_packet(packet)

    stop_event = threading.Event()
    acquisition = AcquisitionThread(
        device, on_packet, stop_event, already_connected=True
    )
    handler = CommandHandler(cmd_queue, ack_queue, recorder)
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
            device.device_info,
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
