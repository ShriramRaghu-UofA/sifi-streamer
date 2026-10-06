import io
import tempfile
import threading
import unittest
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import Mock, patch

from sifi_streamer.exceptions import DeviceError
from sifi_streamer.sifi import create_sifi_capture
from sifi_streamer.sifi.bridge import BridgeTransport, SiFiBridgeDevice
from sifi_streamer.sifi.cli.capture import build_parser
from sifi_streamer.sifi.cli.sensor_options import (
    resolve_sensor_profile,
    sensor_profile_summary,
)
from sifi_streamer.sifi.devices import SiFiPacket
from sifi_streamer.sifi.sensor_profile import (
    ALL_SENSORS_PROFILE,
    EMG_IMU_PROFILE,
    EMG_ONLY_PROFILE,
    ImuConfiguration,
    PpgConfiguration,
    bridge_configuration_commands,
    load_sensor_profile,
    sensor_profile_from_dict,
    sensor_profile_to_dict,
    write_sensor_profile,
)
from sifi_streamer.web.cli import main as web_main

ALL_SENSOR_CONFIGURATION = {
    "sensors": dict.fromkeys(("ecg", "emg", "eda", "imu", "ppg", "temperature"), True),
    "ecg": {"enabled": True, "fs": 500},
    "emg": {"enabled": True, "fs": 1600},
    "eda": {"enabled": True, "fs": 50},
    "imu": {"enabled": True, "fs": 100},
    "ppg": {"enabled": True, "sps": 200, "avg": 4},
    "temperature": {"enabled": True, "fs": 1},
}
ALL_SENSOR_INFO = {
    "info": {
        "device": "SiFiBand",
        "configuration": ALL_SENSOR_CONFIGURATION,
    }
}


class SensorProfileTests(unittest.TestCase):
    def test_device_selection_commands(self) -> None:
        for handle, command in (
            (None, "connect"),
            ("BioPoint_AA92", "connect BioPoint_AA92"),
            ("E7:B5:AB:8B:9E:D0", "connect E7:B5:AB:8B:9E:D0"),
            (
                "00001122-3344-5566-7788-99AABBCCDDEE",
                "connect 00001122-3344-5566-7788-99AABBCCDDEE",
            ),
            ("Device name", 'connect "Device name"'),
        ):
            with self.subTest(handle=handle):
                device = SiFiBridgeDevice(device_handle=handle)
                with (
                    patch("pathlib.Path.exists", return_value=True),
                    patch.object(device, "_launch"),
                    patch.object(device, "_send") as send,
                    patch.object(device, "_wait_for_response") as acknowledge,
                    patch.object(
                        device, "_wait_for_info", return_value=ALL_SENSOR_INFO
                    ),
                ):
                    device.connect()
                self.assertEqual(send.call_args_list[0].args, (command,))
                self.assertEqual(acknowledge.call_args_list[0].args, ("connect",))

    def test_invalid_device_handles(self) -> None:
        for handle in ("", "  ", "name\nstart", "name\r", "name\0"):
            with self.subTest(handle=handle), self.assertRaises(ValueError):
                SiFiBridgeDevice(device_handle=handle)

    def test_missing_bridge_hint_uses_configured_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom bridge" / "sifibridge.exe"
            with self.assertRaises(DeviceError) as raised:
                SiFiBridgeDevice(executable=path).connect()
            self.assertIn(f'--output-directory "{path.parent}"', str(raised.exception))

    def test_stdout_reader_receives_packets_after_connect_and_reconnect(self) -> None:
        device = SiFiBridgeDevice(transport="stdout")

        def read(packets: list[SiFiPacket]) -> None:
            packets.append(device.read_packet())

        for _ in range(2):
            process = Mock(
                stdin=io.StringIO(),
                stdout=io.StringIO(
                    '> {"packet_type":"emg","timestamps":[1.0],"data":{"emg":[2.5]}}\n'
                ),
                stderr=io.StringIO(),
            )

            packets: list[SiFiPacket] = []
            with (
                patch("pathlib.Path.exists", return_value=True),
                patch.object(
                    device,
                    "_launch",
                    side_effect=partial(setattr, device, "_process", process),
                ),
                patch.object(device, "_send"),
                patch.object(device, "_wait_for_response"),
                patch.object(device, "_wait_for_info", return_value=ALL_SENSOR_INFO),
            ):
                device.connect()
            device._read_stdout()
            reader = threading.Thread(target=read, args=(packets,), daemon=True)
            reader.start()
            try:
                reader.join(1)
                self.assertFalse(reader.is_alive(), "stdout packet was not delivered")
                self.assertEqual(packets[0].packet_type, "emg")
                self.assertEqual(packets[0].data, {"emg": [2.5]})
            finally:
                device.disconnect()
                reader.join(1)

    def test_live_bridge_requires_nested_physical_sensor_report(self) -> None:
        device = SiFiBridgeDevice()
        for sensors in (None, [], {"ecg": "true"}):
            with self.subTest(sensors=sensors):
                device._device_info = {
                    "info": {
                        "configuration": {
                            **ALL_SENSOR_CONFIGURATION,
                            "sensors": sensors,
                        }
                    }
                }
                with self.assertRaises(DeviceError):
                    device._validate_sensor_capabilities()

    def test_default_profile_and_exhaustive_commands(self) -> None:
        profile = ALL_SENSORS_PROFILE
        self.assertEqual(profile.emg.sample_rate_hz, 1600)
        self.assertEqual(profile.ppg.samples_per_second, 200)
        self.assertEqual(profile.ppg.averaging, 4)
        self.assertEqual(profile.ppg.effective_sample_rate_hz, 50)
        commands = bridge_configuration_commands(profile)
        self.assertEqual(len(commands), 7)
        self.assertEqual(
            commands[0],
            "configure ecg --fs 500 --dc-notch on --mains-notch 60 "
            "--bandpass on --bandpass-low 0 --bandpass-high 30",
        )
        self.assertEqual(
            commands[3],
            "configure ppg --sps 200 --led-ir 9 --led-red 9 --led-green 9 "
            "--led-blue 9 --sens medium --avg 4",
        )
        self.assertEqual(
            commands[-1],
            "configure sensors --ecg on --emg on --eda on --imu on --ppg on",
        )

    def test_disabled_sensor_configuration_is_still_rendered(self) -> None:
        commands = bridge_configuration_commands(EMG_ONLY_PROFILE)
        self.assertTrue(commands[0].startswith("configure ecg --fs 500"))
        self.assertIn("--ppg off", commands[-1])
        self.assertIn("--imu off", commands[-1])
        self.assertIn("--imu on", bridge_configuration_commands(EMG_IMU_PROFILE)[-1])

    def test_bridge_sends_full_profile_before_info_and_start(self) -> None:
        device = SiFiBridgeDevice(transport=BridgeTransport.STDOUT)
        sent: list[str] = []
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch.object(device, "_launch"),
            patch.object(device, "_send", side_effect=sent.append),
            patch.object(device, "_wait_for_response"),
            patch.object(device, "_wait_for_info", return_value=ALL_SENSOR_INFO),
        ):
            device.connect()
        self.assertEqual(
            sent,
            [
                "connect",
                "info",
                *bridge_configuration_commands(ALL_SENSORS_PROFILE),
                "info",
                "start",
                "info",
            ],
        )

    def test_bridge_rejects_reported_configuration_mismatch(self) -> None:
        device = SiFiBridgeDevice(transport=BridgeTransport.STDOUT)
        bad_info = {
            "info": {
                **ALL_SENSOR_INFO["info"],
                "configuration": {
                    **ALL_SENSOR_CONFIGURATION,
                    "emg": {"enabled": True, "fs": 1000},
                },
            }
        }
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch.object(device, "_launch"),
            patch.object(device, "_send"),
            patch.object(device, "_wait_for_response"),
            patch.object(device, "_wait_for_info", return_value=bad_info),
            self.assertRaisesRegex(Exception, "expected 1600 Hz"),
        ):
            device.connect()

    def test_json_round_trip_and_exclusive_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            write_sensor_profile(path, EMG_IMU_PROFILE)
            self.assertEqual(load_sensor_profile(path), EMG_IMU_PROFILE)
            self.assertTrue(path.read_bytes().endswith(b"\n"))
            with self.assertRaises(FileExistsError):
                write_sensor_profile(path, ALL_SENSORS_PROFILE)
            write_sensor_profile(path, ALL_SENSORS_PROFILE, overwrite=True)
            self.assertEqual(load_sensor_profile(path), ALL_SENSORS_PROFILE)

    def test_profile_v2_removes_unsupported_imu_settings(self) -> None:
        document = sensor_profile_to_dict(ALL_SENSORS_PROFILE)
        self.assertEqual(document["version"], 2)
        imu = document["imu"]
        assert isinstance(imu, dict)
        self.assertNotIn("gyroscope_range_dps", imu)
        self.assertEqual(
            bridge_configuration_commands(ALL_SENSORS_PROFILE)[4],
            "configure imu --fs 100 --acc-range 16",
        )
        for value in (2, 4):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ImuConfiguration(accelerometer_range_g=value)
        document["version"] = 1
        with self.assertRaisesRegex(ValueError, "remove imu.gyroscope_range_dps"):
            sensor_profile_from_dict(document)

    def test_live_bridge_rejects_historical_info_and_unavailable_sensors(self) -> None:
        device = SiFiBridgeDevice()
        device._device_info = {"info": {"device": {}}}
        with self.assertRaisesRegex(DeviceError, "info.configuration"):
            device._validate_sensor_capabilities()
        device._device_info = {
            "info": {
                **ALL_SENSOR_INFO["info"],
                "configuration": {
                    **ALL_SENSOR_CONFIGURATION,
                    "sensors": {"ecg": False},
                },
            }
        }
        with self.assertRaisesRegex(DeviceError, "not physically available"):
            device._validate_sensor_capabilities()

    def test_tcp_subscribes_before_start_and_revalidates_after_start(self) -> None:
        device = SiFiBridgeDevice(transport=BridgeTransport.TCP)
        events: list[str] = []
        changed_info = {
            "info": {
                **ALL_SENSOR_INFO["info"],
                "configuration": {
                    **ALL_SENSOR_CONFIGURATION,
                    "emg": {"enabled": True, "fs": 1000},
                },
            }
        }
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch.object(device, "_launch"),
            patch.object(device, "_send", side_effect=events.append),
            patch.object(device, "_wait_for_response"),
            patch.object(
                device,
                "_wait_for_info",
                side_effect=[ALL_SENSOR_INFO, ALL_SENSOR_INFO, changed_info],
            ),
            patch.object(
                device,
                "_connect_tcp_when_ready",
                side_effect=lambda: events.append("subscribe"),
            ),
            patch.object(device, "disconnect") as disconnect,
            self.assertRaisesRegex(DeviceError, "expected 1600 Hz"),
        ):
            device.connect()
        self.assertLess(events.index("subscribe"), events.index("start"))
        disconnect.assert_called_once()

    def test_disabled_absent_sensors_are_not_configured(self) -> None:
        device = SiFiBridgeDevice(sensor_profile=EMG_ONLY_PROFILE, transport="stdout")
        info = {
            "info": {
                "device": "SiFiBand",
                "configuration": {
                    **ALL_SENSOR_CONFIGURATION,
                    "sensors": {
                        name: name == "emg"
                        for name in ALL_SENSOR_CONFIGURATION["sensors"]
                    },
                },
            }
        }
        sent: list[str] = []
        with (
            patch("pathlib.Path.exists", return_value=True),
            patch.object(device, "_launch"),
            patch.object(device, "_send", side_effect=sent.append),
            patch.object(device, "_wait_for_response"),
            patch.object(device, "_wait_for_info", return_value=info),
        ):
            device.connect()
        self.assertEqual(
            [
                command.split()[1]
                for command in sent
                if command.startswith("configure ")
            ],
            ["emg", "sensors"],
        )

    def test_strict_json_and_value_validation(self) -> None:
        document = sensor_profile_to_dict(ALL_SENSORS_PROFILE)
        document["future"] = True
        with self.assertRaisesRegex(ValueError, "unknown future"):
            sensor_profile_from_dict(document)
        document = sensor_profile_to_dict(ALL_SENSORS_PROFILE)
        ppg = document["ppg"]
        assert isinstance(ppg, dict)
        ppg["led_ir_ma"] = 51
        with self.assertRaisesRegex(ValueError, "led_ir_ma"):
            sensor_profile_from_dict(document)
        with self.assertRaisesRegex(ValueError, "led_ir_ma"):
            PpgConfiguration(led_ir_ma=0)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text('{"version": NaN}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-finite"):
                load_sensor_profile(path)

    def test_cli_preset_and_common_overrides(self) -> None:
        args = build_parser().parse_args(
            [
                "capture.zst",
                "--capture-id",
                "test",
                "--sensor-preset",
                "emg-only",
                "--imu",
                "on",
                "--imu-fs",
                "200",
                "--ppg-sps",
                "400",
                "--ppg-avg",
                "8",
            ]
        )
        profile = resolve_sensor_profile(args)
        self.assertTrue(profile.imu.enabled)
        self.assertEqual(profile.imu.sample_rate_hz, 200)
        self.assertFalse(profile.ppg.enabled)
        self.assertEqual(profile.ppg.effective_sample_rate_hz, 50)

    def test_web_launcher_displays_complete_resolved_profile(self) -> None:
        summary = sensor_profile_summary(EMG_IMU_PROFILE)
        self.assertEqual(summary["emg_fs_hz"], 1600)
        self.assertEqual(summary["emg_bandpass_high_hz"], 450)
        self.assertEqual(summary["ecg_mains_notch_hz"], "60")
        self.assertEqual(summary["eda_frequency_hz"], 0)
        self.assertEqual(summary["ppg_sps"], 200)
        self.assertEqual(summary["ppg_avg"], 4)
        self.assertEqual(summary["ppg_effective_rate_hz"], 50)
        self.assertEqual(summary["ppg_led_green_ma"], 9)
        self.assertEqual(summary["ppg_sensitivity"], "medium")
        self.assertEqual(summary["imu_accelerometer_range_g"], 16)
        self.assertEqual(summary["temperature_fs_hz"], 1)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "web.capture.jsonl.zst"
            with patch("sifi_streamer.web.cli.serve_capture_web") as serve:
                self.assertEqual(
                    web_main(
                        [
                            str(output),
                            "--sensor-preset",
                            "emg-imu",
                            "--no-open",
                        ]
                    ),
                    0,
                )
            displayed = serve.call_args.kwargs["configuration_summary"]
            self.assertEqual(displayed["emg_fs_hz"], 1600)
            self.assertEqual(displayed["ppg_sps"], 200)
            self.assertFalse(displayed["ppg_enabled"])
            self.assertTrue(displayed["imu_enabled"])

    def test_synthetic_rejects_hardware_profile(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(ValueError, "synthetic"),
        ):
            create_sifi_capture(
                Path(directory) / "capture.zst",
                "test",
                sensor_profile=replace(ALL_SENSORS_PROFILE),
                synthetic=True,
            )


if __name__ == "__main__":
    unittest.main()
