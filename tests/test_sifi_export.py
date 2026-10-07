import json
import tempfile
import unittest
from compression import zstd
from pathlib import Path

import pandas as pd

from sifi_streamer.capture import (
    CaptureLogWriter,
    CaptureStarted,
    RawPacket,
    SegmentStarted,
    encode_record,
)
from sifi_streamer.sifi import DEFAULT_MODALITIES, Modality
from sifi_streamer.sifi.export import (
    SiFiExportError,
    export_sifi_capture_to_parquet,
    read_sifi_capture_tables,
)


def emg_packet(
    values: tuple[float | None, ...] = (1.0, 2.0),
    *,
    sample_rate: float = 1600.0,
) -> dict[str, object]:
    return {
        "packet_type": "emg_armband",
        "timestamps": [index / sample_rate for index in range(len(values))],
        "data": {f"emg{channel}": list(values) for channel in range(8)},
        "received_at": 10.0,
        "sample_rate": sample_rate,
        "samples_lost": 0,
        "status": "ok",
        "vendor_extension": {"preserved_in_capture": True},
    }


class SiFiExportTests(unittest.TestCase):
    def test_every_sifi_modality_uses_its_canonical_channel_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "all.capture.jsonl.zst"
            with CaptureLogWriter(path, "all") as writer:
                for modality in Modality:
                    spec = DEFAULT_MODALITIES.require(modality)
                    writer.append_packet(
                        {
                            "packet_type": modality.value,
                            "timestamps": [1.0],
                            "data": {
                                channel: [float(index)]
                                for index, channel in enumerate(spec.channels)
                            },
                            "sample_rate": spec.sample_rate,
                        }
                    )
            tables = read_sifi_capture_tables(path)

        self.assertEqual(set(tables.signals), set(Modality))
        for modality, frame in tables.signals.items():
            expected = list(DEFAULT_MODALITIES.require(modality).channels)
            self.assertEqual(frame.columns[-len(expected) :].tolist(), expected)

    def test_tables_expand_samples_and_pair_generic_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.capture.jsonl.zst"
            with CaptureLogWriter(path, "session", {"operator": "one"}) as writer:
                writer.append_packet(
                    {
                        "info": {
                            "device": {
                                "emg": {"enabled": True, "fs": 1600},
                                "ecg": {"enabled": False, "fs": 500},
                            }
                        }
                    }
                )
                writer.start_segment(
                    "phase-1", "arbitrary", {"level": 2, "label": "rest"}
                )
                writer.append_marker(
                    "prompt-1",
                    "anything",
                    {"ready": True},
                    source_time_ns=123,
                    source_clock="display",
                )
                packet_sequence = writer.append_packet(emg_packet())
                writer.stop_segment("phase-1", "completed")

            tables = read_sifi_capture_tables(path)

        self.assertEqual(tables.capture.loc[0, "capture_id"], "session")
        self.assertEqual(tables.capture.loc[0, "attribute_operator"], "one")
        self.assertEqual(
            tables.streams["channel_id"].tolist(),
            [f"emg{channel}" for channel in range(8)],
        )
        self.assertEqual(
            tables.streams["rate_source"].unique().tolist(), ["device_info"]
        )
        signal = tables.signals[Modality.EMG]
        self.assertEqual(signal["packet_sequence"].tolist(), [packet_sequence] * 2)
        self.assertEqual(signal["sample_index_in_packet"].tolist(), [0, 1])
        self.assertEqual(signal["emg0"].tolist(), [1.0, 2.0])
        self.assertEqual(tables.markers.loc[0, "attribute_ready"], True)
        self.assertEqual(tables.markers.loc[0, "source_clock"], "display")
        self.assertEqual(str(tables.markers["source_time_ns"].dtype), "Int64")
        segment = tables.segments.iloc[0]
        self.assertLess(segment["start_sequence"], packet_sequence)
        self.assertGreater(segment["stop_sequence"], packet_sequence)
        self.assertEqual(segment["attribute_label"], "rest")

    def test_packet_rate_builds_manifest_without_device_info(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.capture.jsonl.zst"
            with CaptureLogWriter(path, "synthetic") as writer:
                writer.append_packet(emg_packet((1.0,), sample_rate=1000.0))
            tables = read_sifi_capture_tables(path)

        self.assertEqual(tables.streams["nominal_rate_hz"].unique().tolist(), [1000.0])
        self.assertEqual(tables.streams["rate_source"].unique().tolist(), ["packet"])

    def test_old_and_current_info_export_jitter_missingness_and_unchanged_clocks(
        self,
    ) -> None:
        configuration = {"emg": {"enabled": True, "fs": 1600}}
        for info in (
            {"device": configuration},
            {"device": "SiFiBand", "configuration": configuration},
        ):
            with self.subTest(info=info), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "rates.capture.jsonl.zst"
                first = emg_packet((None, 2.0), sample_rate=1597.15)
                first.pop("sample_rate")
                first.pop("samples_lost")
                first["timestamps"] = [0.0, 1 / 1600]
                with CaptureLogWriter(path, "rates") as writer:
                    writer.append_packet({"info": info})
                    writer.append_packet(
                        {"packet_type": "start_time", "start_time": 1_790_000_000.25}
                    )
                    writer.append_packet(first)
                    writer.append_packet(emg_packet((3.0,), sample_rate=1597.15))
                    writer.append_packet(emg_packet((4.0,), sample_rate=1601.2))
                original = path.read_bytes()
                tables = read_sifi_capture_tables(path)
                self.assertEqual(path.read_bytes(), original)
            signal = tables.signals[Modality.EMG]
            self.assertTrue(pd.isna(signal.loc[0, "emg0"]))
            self.assertTrue(pd.isna(signal.loc[0, "reported_sample_rate_hz"]))
            self.assertEqual(
                signal["reported_sample_rate_hz"].iloc[2:].tolist(), [1597.15, 1601.2]
            )
            self.assertEqual(signal["device_time_s"].iloc[:2].tolist(), [0.0, 1 / 1600])
            self.assertEqual(
                tables.streams["nominal_rate_hz"].unique().tolist(), [1600.0]
            )

    def test_empty_signal_packet_and_invalid_reported_rates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.capture.jsonl.zst"
            with CaptureLogWriter(path, "empty") as writer:
                writer.append_packet({"packet_type": "ecg"})
            tables = read_sifi_capture_tables(path)
            self.assertTrue(tables.signals[Modality.ECG].empty)
        for rate in (0, -1):
            with self.subTest(rate=rate), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bad.capture.jsonl.zst"
                with CaptureLogWriter(path, "bad") as writer:
                    writer.append_packet({**emg_packet((1.0,)), "sample_rate": rate})
                with self.assertRaisesRegex(SiFiExportError, "positive"):
                    read_sifi_capture_tables(path)

    def test_incompatible_attribute_types_and_normalized_names_fail(self) -> None:
        cases = (
            (("same", 1), ("same", "one")),
            (("A value", 1), ("a-value", 2)),
        )
        for first, second in cases:
            with (
                self.subTest(first=first, second=second),
                tempfile.TemporaryDirectory() as directory,
            ):
                path = Path(directory) / "bad.capture.jsonl.zst"
                with CaptureLogWriter(path, "bad") as writer:
                    writer.append_marker("one", "kind", {first[0]: first[1]})
                    writer.append_marker("two", "kind", {second[0]: second[1]})
                    writer.append_packet(emg_packet((1.0,)))
                with self.assertRaises(SiFiExportError):
                    read_sifi_capture_tables(path)

    def test_misaligned_known_packet_fails_instead_of_dropping_samples(self) -> None:
        packet = emg_packet()
        data = packet["data"]
        assert isinstance(data, dict)
        data["emg7"] = [1.0]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.capture.jsonl.zst"
            with CaptureLogWriter(path, "bad") as writer:
                writer.append_packet(packet)
            with self.assertRaisesRegex(SiFiExportError, "emg7"):
                read_sifi_capture_tables(path)

    def test_unknown_packets_leave_empty_signal_views(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unknown.capture.jsonl.zst"
            with CaptureLogWriter(path, "unknown") as writer:
                writer.append_packet({"packet_type": "future", "data": {}})
            tables = read_sifi_capture_tables(path)
            self.assertTrue(tables.streams.empty)
            self.assertEqual(dict(tables.signals), {})
            self.assertEqual(tables.capture.loc[0, "capture_id"], "unknown")

    def test_schema_three_metadata_survives_failed_capture_and_parquet(self) -> None:
        payload = {"vendor": {"values": [None, True, 2**60 + 1, "µ"]}}
        host_time = 1_790_000_000_000_000_001
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failed.capture.jsonl.zst"
            writer = CaptureLogWriter(
                path,
                "failed",
                monotonic_ns=lambda: host_time,
                unix_ns=lambda: host_time + 1,
            )
            writer.append_launch_configuration(payload)
            writer.append_device_info("before_configuration", {})
            writer.append_device_info("during_capture", payload)
            writer.append_device_info("during_capture", payload)
            for severity in ("info", "warning", "error"):
                writer.append_diagnostic(
                    severity,
                    "vendor",
                    "connect",
                    "failed",
                    "Connection failed",
                    payload,
                )
            writer.close("startup_failure")
            original = path.read_bytes()
            tables = read_sifi_capture_tables(path)
            output = export_sifi_capture_to_parquet(path)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(tables.capture.loc[0, "table_schema_version"], 2)
            self.assertEqual(tables.capture.loc[0, "stop_reason"], "startup_failure")
            self.assertTrue(tables.streams.empty)
            self.assertEqual(dict(tables.signals), {})
            for name in ("launch_configuration", "device_info", "diagnostics"):
                frame = getattr(tables, name)
                restored = pd.read_parquet(output / f"{name}.parquet")
                pd.testing.assert_frame_equal(frame, restored)
                self.assertEqual(frame["capture_id"].unique().tolist(), ["failed"])
                self.assertEqual(
                    frame["host_unix_ns"].unique().tolist(), [host_time + 1]
                )
            self.assertEqual(
                json.loads(tables.launch_configuration.loc[0, "configuration_json"]),
                payload,
            )
            self.assertEqual(tables.device_info["sequence"].tolist(), [2, 3, 4])
            self.assertEqual(
                [json.loads(value) for value in tables.device_info["info_json"]],
                [{}, payload, payload],
            )
            self.assertEqual(
                tables.diagnostics["severity"].tolist(), ["info", "warning", "error"]
            )
            self.assertEqual(
                json.loads(tables.diagnostics.loc[2, "details_json"]), payload
            )

    def test_configured_reports_determine_streams_and_all_stages_are_retained(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "configured.capture.jsonl.zst"
            with CaptureLogWriter(path, "configured") as writer:
                for stage, rate in (
                    ("before_configuration", 500),
                    ("after_configuration", 1000),
                    ("after_start", 1000),
                ):
                    writer.append_device_info(
                        stage,
                        {
                            "info": {
                                "configuration": {"emg": {"enabled": True, "fs": rate}}
                            }
                        },
                    )
                writer.append_packet(emg_packet((1.0,), sample_rate=1000))
            tables = read_sifi_capture_tables(path)
            self.assertEqual(
                tables.streams["nominal_rate_hz"].unique().tolist(), [1000.0]
            )
            self.assertEqual(
                tables.device_info["stage"].tolist(),
                ["before_configuration", "after_configuration", "after_start"],
            )

    def test_crash_truncated_capture_retains_open_segment_and_integer_clocks(
        self,
    ) -> None:
        host_time = 8_000_000_000_000_001
        records = (
            CaptureStarted(3, 0, host_time, host_time + 1, "crash", {}),
            SegmentStarted(3, 1, host_time + 2, host_time + 3, "phase", "kind", {}),
            RawPacket(
                3,
                2,
                host_time + 4,
                host_time + 5,
                emg_packet((1.0,)),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crash.capture.jsonl.zst"
            path.write_bytes(zstd.compress(b"".join(map(encode_record, records))))
            tables = read_sifi_capture_tables(path)

        self.assertEqual(
            tables.segments.loc[0, "start_host_monotonic_ns"], host_time + 2
        )
        self.assertTrue(pd.isna(tables.segments.loc[0, "stop_sequence"]))
        self.assertEqual(str(tables.segments["stop_sequence"].dtype), "Int64")
        self.assertTrue(pd.isna(tables.capture.loc[0, "stop_sequence"]))

    def test_reused_closed_segment_id_produces_distinct_intervals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reused.capture.jsonl.zst"
            with CaptureLogWriter(path, "reused") as writer:
                for value in (1.0, 2.0):
                    writer.start_segment("phase", "kind")
                    writer.append_packet(emg_packet((value,)))
                    writer.stop_segment("phase", "completed")
            tables = read_sifi_capture_tables(path)

        self.assertEqual(tables.segments["segment_id"].tolist(), ["phase", "phase"])
        self.assertEqual(tables.signals[Modality.EMG]["emg0"].tolist(), [1.0, 2.0])

    def test_parquet_dataset_is_complete_and_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.capture.jsonl.zst"
            with CaptureLogWriter(path, "session") as writer:
                writer.append_packet(emg_packet((1.0,)))

            output = export_sifi_capture_to_parquet(path)
            self.assertEqual(output, root / "session.parquet")
            self.assertTrue((output / "capture.parquet").is_file())
            self.assertTrue((output / "streams.parquet").is_file())
            self.assertTrue((output / "markers.parquet").is_file())
            self.assertTrue((output / "segments.parquet").is_file())
            for name in ("launch_configuration", "device_info", "diagnostics"):
                self.assertTrue(pd.read_parquet(output / f"{name}.parquet").empty)
            signal_path = output / "signals" / "emg_armband.parquet"
            self.assertEqual(pd.read_parquet(signal_path)["emg0"].tolist(), [1.0])
            with self.assertRaises(FileExistsError):
                export_sifi_capture_to_parquet(path)
            sentinel = output / "stale.txt"
            sentinel.write_text("old")
            self.assertEqual(export_sifi_capture_to_parquet(path, force=True), output)
            self.assertFalse(sentinel.exists())


if __name__ == "__main__":
    unittest.main()
