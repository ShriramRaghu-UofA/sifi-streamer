"""The SDK-neutral example implements the real structural protocol."""

import unittest

import numpy as np

from examples.myo_adapter import MyoDevice
from sifi_streamer.acquisition.devices import AcquisitionDevice, AcquisitionPacket


class Transport:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def connect(self) -> None:
        self.calls.append("connect")

    def receive(self) -> tuple[int | None, ...]:
        return (1, 2, 3, 4, 5, 6, 7, None)

    def request_stop(self) -> None:
        self.calls.append("request_stop")

    def disconnect(self) -> None:
        self.calls.append("disconnect")


class MyoExampleTests(unittest.TestCase):
    def test_adapter_declares_clock_and_records_without_raw_document(self) -> None:
        transport = Transport()
        device = MyoDevice(lambda: transport)
        self.assertIsInstance(device, AcquisitionDevice)
        device.connect()
        packet = device.read_packet()
        self.assertIsInstance(packet, AcquisitionPacket)
        self.assertIsNone(packet.capture_document())
        self.assertEqual(packet.data["emg7"], (None,))
        self.assertEqual(packet.validity["emg7"], (False,))
        self.assertEqual(device.streams[0].timestamp_dtype, np.int64)
        self.assertEqual(device.streams[0].timestamp_unit, "nanoseconds")
        device.request_stop()
        device.disconnect()
        self.assertEqual(transport.calls, ["connect", "request_stop", "disconnect"])
