import io
import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from unittest.mock import patch

import numpy as np

from sifi_streamer.acquisition import (
    BackgroundHandle,
    SharedMemoryReader,
    StreamerConfig,
)
from sifi_streamer.acquisition.ring_buffer import SeqlockRingBuffer
from sifi_streamer.acquisition.worker.recorder import RecorderFSM
from sifi_streamer.capture import CaptureLogReader, RawPacket
from sifi_streamer.exceptions import DeviceError
from sifi_streamer.sifi import SyntheticSiFiDevice
from sifi_streamer.sifi.bridge import (
    DEFAULT_BRIDGE_EXECUTABLE,
    BridgeTransport,
    SiFiBridgeDevice,
    _UdpPacketReader,
    bridge_executable_name,
)
from sifi_streamer.sifi.cli.capture import build_parser as capture_parser
from sifi_streamer.sifi.devices import (
    Modality,
    SiFiBandDevice,
    modalities_from_device_info,
    packet_from_json_line,
)
from sifi_streamer.sifi.sensor_profile import ALL_SENSORS_PROFILE, EMG_ONLY_PROFILE
from sifi_streamer.web.cli import build_parser as web_parser

PACKET = (
    '{"packet_type":"ecg","timestamps":[1.0],"data":{"ecg":[2.5]},"received_at":3.0}'
)


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 123
        self.stdin = io.StringIO()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.returncode = 0

    def poll(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


class DeviceTests(unittest.TestCase):
    def test_capture_transport_defaults_and_explicit_socket_options(self) -> None:
        self.assertEqual(SiFiBridgeDevice()._transport, BridgeTransport.STDOUT)
        for parser_factory in (capture_parser, web_parser):
            for transport in (None, "tcp", "udp", "stdout"):
                with self.subTest(parser=parser_factory, transport=transport):
                    arguments = ["session.capture.jsonl.zst", "--capture-id", "session"]
                    if transport is not None:
                        arguments += ["--transport", transport]
                    args = parser_factory().parse_args(arguments)
                    self.assertEqual(args.transport, transport or "stdout")
        args = web_parser().parse_args(
            [
                "session.capture.jsonl.zst",
                "--transport",
                "tcp",
                "--port",
                "5000",
                "--web-port",
                "8080",
            ]
        )
        self.assertEqual((args.port, args.web_port), (5000, 8080))

    def test_stdout_capture_preserves_events_metadata_and_unknown_packets(self) -> None:
        documents = [
            {
                "packet_type": kind,
                "received_at": 1.0,
                "vendor_extension": {"nested": [1, None]},
                **fields,
            }
            for kind, fields in (
                ("event", {"timestamps": [1.0], "data": {"event": [7]}}),
                ("status", {"device_state": "connected_ble_only_working"}),
                ("memory", {"status": "memory_download_completed"}),
                ("device_info", {"firmware": "new"}),
                ("start_time", {"start_time": 1790000000.25}),
                ("future_packet", {"new_field": True}),
            )
        ]
        process = FakeProcess()
        process.stdout = io.StringIO(
            '{"configure":{}}\n'
            + "\n".join("> " + json.dumps(document) for document in documents)
            + "\n"
        )
        device = SiFiBridgeDevice()
        with patch.object(device, "_process", process):
            device._read_stdout()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.capture.jsonl.zst"
            recorder = RecorderFSM(StreamerConfig(), None)
            recorder.start_capture(path, "events")
            while (packet := device._stdout_packets.get_nowait()) is not None:
                recorder.on_packet(packet)
            recorder.stop_capture()
            captured = [
                record.packet
                for record in CaptureLogReader(path)
                if isinstance(record, RawPacket)
            ]
        self.assertEqual(captured, documents)

    def test_bridge_executable_name_is_platform_specific(self) -> None:
        self.assertEqual(bridge_executable_name("Windows"), "sifibridge.exe")
        self.assertEqual(bridge_executable_name("Linux"), "sifibridge")
        self.assertEqual(bridge_executable_name("Darwin"), "sifibridge")
        self.assertEqual(SiFiBridgeDevice()._executable, DEFAULT_BRIDGE_EXECUTABLE)

    def test_modality_parsing_and_packet_preservation(self) -> None:
        modalities = modalities_from_device_info(
            {
                "info": {
                    "device": {
                        "emg": {"enabled": True, "fs": 2000},
                        "ppg": {"enabled": True, "sps": 400, "avg": 4},
                    }
                }
            }
        )
        emg = modalities.emg
        ppg = modalities.ppg
        assert emg is not None
        assert ppg is not None
        self.assertEqual(emg.sample_rate, 2000)
        self.assertEqual(ppg.sample_rate, 100)
        self.assertIsNone(modalities.imu)
        packet = packet_from_json_line(PACKET)
        assert packet is not None
        self.assertEqual(packet.document, json.loads(PACKET))
        self.assertIs(packet.modality, Modality.ECG)

    def test_socket_read_errors_are_translated(self) -> None:
        class Resetting:
            def readline(self) -> bytes:
                raise ConnectionResetError("reset")

            def close(self) -> None:
                pass

        device = SiFiBandDevice()
        device._file = Resetting()
        with self.assertRaisesRegex(DeviceError, "TCP receive failed"):
            device.read_packet()

    def test_current_info_preserves_fractional_rates_and_physical_capabilities(
        self,
    ) -> None:
        modalities = modalities_from_device_info(
            {
                "info": {
                    "device": "SiFiBandFocus",
                    "configuration": {
                        "sensors": {"ecg": False},
                        "ecg": {"enabled": True, "fs": 500},
                        "ppg": {"enabled": True, "sps": 50, "avg": 32},
                        "temperature": {"fs": 0.1},
                    },
                }
            }
        )
        self.assertIsNone(modalities.ecg)
        self.assertEqual(modalities.require(Modality.PPG).sample_rate, 1.5625)
        self.assertEqual(modalities.require(Modality.TEMPERATURE).sample_rate, 0.1)

    def test_optional_packet_fields_and_new_metadata_are_preserved(self) -> None:
        for document in (
            {"packet_type": "start_time", "start_time": 1_790_000_000.25},
            {"packet_type": "status", "device_state": "connected_ble_only_working"},
            {"packet_type": "ecg", "timestamps": [0.0], "data": {"ecg": [None]}},
        ):
            with self.subTest(document=document):
                packet = packet_from_json_line(json.dumps(document))
                assert packet is not None
                self.assertEqual(packet.capture_document(), document)
                self.assertIsNone(packet.sample_rate)
                self.assertEqual(packet.samples_lost, 0)

    def test_bridge_errors_unexpected_responses_and_eof_fail_promptly(self) -> None:
        cases: tuple[tuple[dict[str, object] | None, str], ...] = (
            ({"error": {"message": "invalid configuration"}}, "invalid configuration"),
            ({"start": {}}, "Unexpected bridge response"),
            (None, "stdout closed"),
        )
        for response, message in cases:
            with self.subTest(response=response):
                device = SiFiBridgeDevice()
                device._control.put(response)
                with self.assertRaisesRegex(DeviceError, message):
                    device._wait_for_response("configure")

    def test_stdout_demultiplexes_commands_packets_and_eof(self) -> None:
        process = FakeProcess()
        process.stdout = io.StringIO(
            '> {"configure":{}}\n' + PACKET + '\n> {"error":{"message":"rejected"}}\n'
        )
        device = SiFiBridgeDevice(transport=BridgeTransport.STDOUT)
        with patch.object(device, "_process", process):
            device._read_stdout()
        self.assertEqual(device._wait_for_response("configure"), {"configure": {}})
        with self.assertRaisesRegex(DeviceError, "rejected"):
            device._wait_for_info()
        packet = device._stdout_packets.get_nowait()
        assert packet is not None
        self.assertEqual(packet.packet_type, "ecg")
        self.assertIsNone(device._stdout_packets.get_nowait())

    def test_bridge_stdin_failure_is_translated(self) -> None:
        class BrokenInput(io.StringIO):
            def write(self, value: str) -> int:
                raise BrokenPipeError("closed")

        process = FakeProcess()
        process.stdin = BrokenInput()
        device = SiFiBridgeDevice()
        with (
            patch.object(device, "_process", process),
            self.assertRaisesRegex(DeviceError, "Unable to send bridge command"),
        ):
            device._send("start")

    def test_udp_reader(self) -> None:
        reader = _UdpPacketReader("127.0.0.1", 0)
        reader.connect()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            reader_socket = reader._sock
            assert reader_socket is not None
            address = "127.0.0.1", reader_socket.getsockname()[1]
            sender.sendto(PACKET.encode(), address)
            self.assertEqual(reader.read_packet().packet_type, "ecg")
        finally:
            sender.close()
            reader.disconnect()

    def test_bridge_command_and_rates(self) -> None:
        self.assertEqual(SiFiBridgeDevice()._sensor_profile, ALL_SENSORS_PROFILE)
        self.assertEqual(
            SiFiBridgeDevice(sensor_profile=EMG_ONLY_PROFILE)._sensor_profile,
            EMG_ONLY_PROFILE,
        )
        for transport, expected in (
            (BridgeTransport.TCP, ["--tcp-out", "127.0.0.1:5000", "--no-stdout-data"]),
            (BridgeTransport.UDP, ["--udp-out", "127.0.0.1:5000", "--no-stdout-data"]),
            (BridgeTransport.STDOUT, []),
        ):
            with (
                self.subTest(transport=transport),
                patch(
                    "sifi_streamer.sifi.bridge.subprocess.Popen",
                    return_value=(process := FakeProcess()),
                ) as popen,
            ):
                device = SiFiBridgeDevice(transport=transport)
                device._launch()
                self.assertEqual(popen.call_args.args[0][1:], expected)
                self.assertEqual(
                    popen.call_args.kwargs["creationflags"],
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
                device.disconnect()
                self.assertTrue(process.stdin.closed)

    def test_shared_memory_reads_and_oversized_write(self) -> None:
        shm = SharedMemory(create=True, size=SeqlockRingBuffer.required_bytes(4, 2))
        owner = SeqlockRingBuffer(4, 2, shm, is_owner=True)
        reader = SharedMemoryReader(shm.name, 4, 2)
        try:
            owner.write_samples(np.arange(12, dtype=np.float32).reshape(6, 2))
            np.testing.assert_array_equal(
                reader.read_window(4), np.arange(4, 12, dtype=np.float32).reshape(4, 2)
            )
        finally:
            reader.close()
            owner.close()
            shm.close()
            shm.unlink()

    def test_background_synthetic_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.capture.jsonl.zst"
            with BackgroundHandle(
                StreamerConfig(ack_timeout_s=3), SyntheticSiFiDevice
            ) as handle:
                handle.start_capture(path, "synthetic")
                time.sleep(0.03)
                window = handle.stream_readers[Modality.EMG].read_window(4)
                assert window is not None
                self.assertEqual(window.shape, (4, 8))
                handle.stop_capture()
            self.assertTrue(
                any(isinstance(record, RawPacket) for record in CaptureLogReader(path))
            )


if __name__ == "__main__":
    unittest.main()
