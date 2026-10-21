"""Storage errors retain the committed prefix and never report success."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import duckdb

from sifi_streamer.capture import CaptureLogReader, CaptureLogWriter


class StorageFailureTests(unittest.TestCase):
    def test_commit_io_failure_preserves_prior_commit_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "failed.capture.duckdb"
            writer = CaptureLogWriter(path, "failed")
            writer.append_marker("committed", "example")
            writer.flush()
            writer.append_marker("pending", "example")
            failing = Mock(wraps=writer._connection)
            failing.execute.side_effect = duckdb.IOException("simulated disk full")
            try:
                with (
                    patch.object(writer, "_connection", failing),
                    self.assertRaisesRegex(duckdb.IOException, "disk full"),
                ):
                    writer.flush()
            finally:
                writer.abort()
            reader = CaptureLogReader(path)
            self.assertEqual(
                reader.query("SELECT marker_id FROM markers"), [("committed",)]
            )
            self.assertEqual(
                reader.query("SELECT outcome FROM capture_status"), [("incomplete",)]
            )
