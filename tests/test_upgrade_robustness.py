import socket
import threading
import unittest

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
