"""Device-neutral SQL inspection and dependency-free derived exports."""

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import duckdb

from sifi_streamer.acquisition.devices import SignalChannelSpec, SignalStreamSpec
from sifi_streamer.capture import CaptureLogReader, CaptureLogWriter
from sifi_streamer.capture.export import main


class DatabaseExportTests(unittest.TestCase):
    def test_catalog_samples_metadata_and_parquet_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "session.capture.duckdb"
            with CaptureLogWriter(path, "session") as writer:
                writer.register_device("myo", "example")
                writer.register_stream(
                    "myo",
                    SignalStreamSpec(
                        "emg",
                        (SignalChannelSpec("emg0", unit="raw"),),
                        200,
                        source_clock="adapter perf_counter()",
                        timestamp_unit="seconds",
                    ),
                )
                writer.append_packet_data(
                    "myo",
                    "emg",
                    [1.0, 1.005],
                    {"emg0": [0.5, None]},
                    {"emg0": [True, True]},
                    {"extra": [1, None]},
                )
                writer.append_marker("m", "example", {"enabled": True})
            reader = CaptureLogReader(path)
            self.assertEqual(reader.query("SELECT count(*) FROM markers"), [(1,)])
            stream = reader.streams()[0]
            self.assertEqual(stream["source_clock"], "adapter perf_counter()")
            table = str(stream["table_name"])
            before = path.read_bytes()
            output = root / "tables"
            reader.export(output, tables=("events", "packets", table))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                sorted(p.name for p in output.iterdir()),
                sorted(["events.parquet", "packets.parquet", f"{table}.parquet"]),
            )
            with duckdb.connect() as connection:
                rows = connection.execute(
                    "SELECT source_time, value_0, valid_0 FROM read_parquet(?) "
                    "ORDER BY sample_index",
                    [str(output / f"{table}.parquet")],
                ).fetchall()
                self.assertEqual(rows, [(1.0, 0.5, True), (1.005, None, True)])
            with self.assertRaises(FileExistsError):
                reader.export(output)

    def test_cli_csv_table_selection_and_output_protection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "empty.capture.duckdb"
            with CaptureLogWriter(path, "empty") as writer:
                writer.append_marker("m", "kind", {"x": 1})
            output = root / "csv"
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(
                        [
                            str(path),
                            str(output),
                            "--format",
                            "csv",
                            "--table",
                            "markers",
                        ]
                    ),
                    0,
                )
            self.assertEqual([p.name for p in output.iterdir()], ["markers.csv"])
            with duckdb.connect() as connection:
                row = connection.execute(
                    "SELECT marker_id, attributes FROM read_csv(?)",
                    [str(output / "markers.csv")],
                ).fetchall()[0]
            self.assertEqual(row[0], "m")
            self.assertEqual(json.loads(row[1]), {"x": 1})

    def test_unknown_table_fails_before_publishing_export(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "capture.duckdb"
            with CaptureLogWriter(path, "empty"):
                pass
            output = root / "export"
            with self.assertRaises(ValueError):
                CaptureLogReader(path).export(output, tables=("not_declared",))
            self.assertFalse(output.exists())
