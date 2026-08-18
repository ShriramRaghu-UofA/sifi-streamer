import unittest
from unittest.mock import patch

from sifi_streamer.capture import (
    CaptureController,
    interactive_annotations,
    parse_attributes,
    parse_scalar,
    run_until_interrupt,
)
from sifi_streamer.sifi.cli.capture import build_parser
from sifi_streamer.sifi.cli.export import build_parser as build_export_parser


class Backend:
    def __init__(self) -> None:
        self.events = []

    def start(self) -> None:
        self.events.append("start")

    def stop(self, reason: str = "normal_completion") -> None:
        self.events.append(reason)

    def start_segment(self, segment_id, kind, attributes) -> None:
        self.events.append(("start_segment", segment_id, kind, dict(attributes)))

    def stop_segment(self, segment_id, reason) -> None:
        self.events.append(("stop_segment", segment_id, reason))

    def marker(self, marker_id, kind, attributes) -> None:
        self.events.append(("marker", marker_id, kind, dict(attributes)))


class RunnerTests(unittest.TestCase):
    def test_scalar_and_attribute_parsing(self) -> None:
        self.assertEqual(
            [parse_scalar(value) for value in ["1", "1.5", "true", "null", "x"]],
            [1, 1.5, True, None, "x"],
        )
        self.assertEqual(parse_attributes(["a=1", "b=false"]), {"a": 1, "b": False})
        with self.assertRaises(ValueError):
            parse_attributes(["bad"])

    def test_interactive_id_kind_order(self) -> None:
        backend = Backend()
        controller = CaptureController(backend)
        controller.start()
        inputs = iter(
            [
                "marker prompt-1 stimulus modality=auditory",
                "segment start rest baseline",
                "segment stop rest",
                "stop",
            ]
        )
        interactive_annotations(
            controller, input_fn=lambda _: next(inputs), output_fn=lambda _: None
        )
        self.assertIn(
            ("marker", "prompt-1", "stimulus", {"modality": "auditory"}), backend.events
        )

    def test_ctrl_c_is_controlled(self) -> None:
        backend = Backend()
        reason = run_until_interrupt(
            CaptureController(backend),
            sleep=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        self.assertEqual(reason, "operator_interrupt")
        self.assertEqual(backend.events[-1], "operator_interrupt")

    def test_cli_modes_conflict_and_duration_validation(self) -> None:
        parser = build_parser()
        with patch("sys.stderr"), self.assertRaises(SystemExit):
            parser.parse_args(
                ["x.zst", "--capture-id", "x", "--duration", "1", "--interactive"]
            )

    def test_export_cli_help_parser_does_not_import_optional_dependencies(self) -> None:
        args = build_export_parser().parse_args(["capture.zst", "--force"])
        self.assertEqual(args.output, None)
        self.assertTrue(args.force)


if __name__ == "__main__":
    unittest.main()
