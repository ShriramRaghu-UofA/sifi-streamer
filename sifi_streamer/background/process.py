"""Background acquisition process entry point."""

import logging
import signal
import threading
from multiprocessing import Queue
from multiprocessing.shared_memory import SharedMemory

import numpy as np

from sifi_streamer.background.acquisition import AcquisitionThread
from sifi_streamer.background.command_handler import CommandHandler
from sifi_streamer.background.recorder import RecorderFSM
from sifi_streamer.background.ring_buffer import SeqlockRingBuffer
from sifi_streamer.config import StreamerConfig
from sifi_streamer.devices import DeviceFactory, Modalities, SiFiPacket
from sifi_streamer.exceptions import DeviceError
from sifi_streamer.protocol import ErrorAck, ModalityInfo, Ready


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
        shm_prefix: Unique prefix for per-modality shared-memory names.
        log_level: Worker root logging level.
    """
    logging.basicConfig(level=log_level)
    _ignore_console_interrupts()
    try:
        device = device_factory()
        device.connect()
        modalities = device.modalities
    except (DeviceError, OSError, RuntimeError, TypeError, ValueError) as exc:
        ack_queue.put(ErrorAck(str(exc)))
        return
    rings: Modalities[SeqlockRingBuffer] = Modalities()
    shms: Modalities[SharedMemory] = Modalities()
    try:
        for modality, spec in modalities.enabled():
            count = max(round(config.ring_buffer_seconds * spec.sample_rate), 16)
            shm = SharedMemory(
                name=f"{shm_prefix}_{modality.value}",
                create=True,
                size=SeqlockRingBuffer.required_bytes(
                    count, spec.n_channels, dtype=spec.numpy_dtype
                ),
            )
            ring = SeqlockRingBuffer(
                count, spec.n_channels, shm, dtype=spec.numpy_dtype, is_owner=True
            )
            shms, rings = (
                shms.with_value(modality, shm),
                rings.with_value(modality, ring),
            )
    except (OSError, ValueError) as exc:
        ack_queue.put(ErrorAck(str(exc)))
        device.disconnect()
        for _, shm in shms.enabled():
            try:
                shm.close()
                shm.unlink()
            except OSError:
                pass
        return
    recorder = RecorderFSM(config, device.device_info)

    def on_packet(packet: SiFiPacket) -> None:
        """Publish known signal samples and forward the full packet to recording."""
        modality = packet.modality
        if modality is not None and packet.timestamps and packet.data:
            ring = rings.get(modality)
            if ring is not None:
                spec, length = modalities.require(modality), len(packet.timestamps)
                matrix = np.zeros((length, spec.n_channels), dtype=spec.numpy_dtype)
                for index, channel in enumerate(spec.channels):
                    values = packet.data.get(channel)
                    if values:
                        matrix[: min(length, len(values)), index] = values[:length]
                ring.write_samples(matrix)
        recorder.on_packet(packet)

    stop_event = threading.Event()
    acquisition = AcquisitionThread(device, on_packet, stop_event)
    handler = CommandHandler(cmd_queue, ack_queue, recorder)
    acquisition.start()
    ack_queue.put(
        Ready(
            Modalities.from_enabled(
                (
                    modality,
                    ModalityInfo(
                        ring.shm_name,
                        ring.n_samples,
                        ring.n_channels,
                        modalities.require(modality).channels,
                        modalities.require(modality).sample_rate,
                        ring.dtype.str,
                    ),
                )
                for modality, ring in rings.enabled()
            ),
            device.device_info,
        )
    )
    try:
        while not handler.tick():
            pass
    finally:
        stop_event.set()
        device.disconnect()
        acquisition.join(timeout=5)
        recorder.close()
        for modality, ring in rings.enabled():
            ring.close()
            shm = shms.require(modality)
            try:
                shm.close()
                shm.unlink()
            except OSError:
                pass
