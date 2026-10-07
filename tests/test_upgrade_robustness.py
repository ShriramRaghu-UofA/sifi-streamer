import socket
import tempfile
import threading
import unittest
from multiprocessing import get_context
from pathlib import Path
from unittest.mock import patch

from sifi_streamer.acquisition.config import StreamerConfig
from sifi_streamer.acquisition.ipc import ErrorAck
from sifi_streamer.acquisition.worker.process import background_main
from sifi_streamer.acquisition.worker.recorder import RecorderFSM
from sifi_streamer.capture import CaptureLogReader, RawPacket
from sifi_streamer.exceptions import DeviceError
from sifi_streamer.sifi.devices import (
    Modality,
    SiFiBandDevice,
    SiFiPacket,
    _SocketLineReader,
    modalities_from_device_info,
)
from sifi_streamer.sifi.sensor_profile import (
    ALL_SENSORS_PROFILE,
    sensor_profile_from_dict,
    sensor_profile_to_dict,
)


class UpgradeRobustnessTests(unittest.TestCase):
    def test_disconnect_unblocks_tcp_read(self) -> None:
        local, peer = socket.socketpair()
        device = SiFiBandDevice()
        device._sock = local
        device._file = _SocketLineReader(local)
        entered = threading.Event()
        errors: list[DeviceError] = []

        def read() -> None:
            entered.set()
            try:
                device.read_packet()
            except DeviceError as exc:
                errors.append(exc)

        reader = threading.Thread(target=read, daemon=True)
        closer = threading.Thread(target=device.disconnect, daemon=True)
        reader.start()
        self.assertTrue(entered.wait(1))
        try:
            closer.start()
            closer.join(1)
            self.assertFalse(closer.is_alive(), "disconnect blocked on readline")
            reader.join(1)
            self.assertFalse(reader.is_alive())
            self.assertEqual(len(errors), 1)
            device.disconnect()
        finally:
            peer.close()
            reader.join(1)
            closer.join(1)

    def test_failed_registry_startup_disconnects_before_ack(self) -> None:
        device = SiFiBandDevice()
        events: list[str] = []
        context = get_context("spawn")
        commands, acknowledgements = context.Queue(), context.Queue()
        try:
            with (
                patch.object(
                    device, "connect", side_effect=lambda: events.append("connect")
                ),
                patch.object(
                    device,
                    "disconnect",
                    side_effect=lambda: events.append("disconnect"),
                ),
                patch.object(SiFiBandDevice, "streams", new=property(lambda _: ())),
                patch(
                    "sifi_streamer.acquisition.worker.process._ignore_console_interrupts"
                ),
            ):
                background_main(
                    StreamerConfig(),
                    lambda: device,
                    commands,
                    acknowledgements,
                    "unused",
                )
            ack = acknowledgements.get(timeout=1)
            self.assertIsInstance(ack, ErrorAck)
            self.assertEqual(events, ["connect", "disconnect"])
        finally:
            commands.close()
            acknowledgements.close()
            commands.join_thread()
            acknowledgements.join_thread()

    def test_latest_context_survives_capture_boundaries_without_signal_backlog(
        self,
    ) -> None:
        recorder = RecorderFSM(StreamerConfig(), {"info": {"device": "SiFiBand"}})
        context: dict[str, object] = {
            "packet_type": "start_time",
            "start_time": 1,
            "extension": {"value": 1},
        }
        recorder.on_packet(SiFiPacket("start_time", [], {}, 1, document=context))
        context["extension"] = {"value": 99}
        recorder.on_packet(SiFiPacket("emg_armband", [0], {"emg0": [1]}, 1))
        with tempfile.TemporaryDirectory() as directory:
            for index in range(2):
                path = Path(directory) / f"{index}.capture.jsonl.zst"
                recorder.start_capture(path, str(index))
                recorder.stop_capture()
                packets = [
                    record.packet
                    for record in CaptureLogReader(path)
                    if isinstance(record, RawPacket)
                ]
                self.assertEqual(len(packets), 1)
                self.assertEqual(packets[0]["extension"], {"value": 1})
            recorder.on_packet(
                SiFiPacket(
                    "start_time",
                    [],
                    {},
                    2,
                    document={"packet_type": "start_time", "start_time": 2},
                )
            )
            path = Path(directory) / "latest.capture.jsonl.zst"
            recorder.start_capture(path, "latest")
            recorder.on_packet(
                SiFiPacket(
                    "start_time",
                    [],
                    {},
                    3,
                    document={"packet_type": "start_time", "start_time": 3},
                )
            )
            recorder.stop_capture()
            packets = [
                record.packet
                for record in CaptureLogReader(path)
                if isinstance(record, RawPacket)
            ]
            self.assertEqual([packet.get("start_time") for packet in packets], [2, 3])

    def test_biopoint_registry_and_packet_use_single_emg_channel(self) -> None:
        modalities = modalities_from_device_info(
            {
                "info": {
                    "device": "BioPoint",
                    "configuration": {"emg": {"enabled": True, "fs": 2000}},
                }
            }
        )
        self.assertIsNone(modalities.emg)
        spec = modalities.require(Modality.EMG_SINGLE)
        self.assertEqual(spec.channels, ("emg",))
        self.assertEqual(spec.sample_rate, 2000)
        packet = SiFiPacket("emg", [0], {"emg": [1]}, 1)
        self.assertEqual(packet.modality, Modality.EMG_SINGLE)

    def test_profile_version_requires_integer(self) -> None:
        for version in (2.0, True, "2"):
            with self.subTest(version=version):
                document = sensor_profile_to_dict(ALL_SENSORS_PROFILE)
                document["version"] = version
                with self.assertRaises(ValueError):
                    sensor_profile_from_dict(document)

    def test_socket_lines_preserve_fragmented_and_batched_documents(self) -> None:
        local, peer = socket.socketpair()
        reader = _SocketLineReader(local)
        try:
            peer.sendall(b'{"first":')
            peer.sendall(b'1}\n{"second":2}\npartial')
            self.assertEqual(reader.readline(), b'{"first":1}\n')
            self.assertEqual(reader.readline(), b'{"second":2}\n')
            peer.shutdown(socket.SHUT_WR)
            self.assertEqual(reader.readline(), b"partial")
            self.assertEqual(reader.readline(), b"")
        finally:
            reader.close()
            local.close()
            peer.close()
