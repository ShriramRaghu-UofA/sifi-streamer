import unittest

from sifi_streamer.capture import Attributes, Scalar
from sifi_streamer.controller import CaptureController, NoCaptureController
from sifi_streamer.exceptions import CaptureInitializationError
from sifi_streamer.runner import run_capture

type EventPayload = (
    str | tuple[str, str] | tuple[str, str, dict[str, Scalar]] | None
)


class Backend:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.events: list[tuple[str, EventPayload]] = []
        self.marker_attributes: dict[str, Scalar] | None = None
        self.fail_start = fail_start

    def start(self) -> None:
        self.events.append(("start", None))
        if self.fail_start:
            raise RuntimeError("broken")

    def stop(self, reason: str = "normal_completion") -> None:
        self.events.append(("stop", reason))

    def start_segment(
        self, segment_id: str, kind: str, attributes: Attributes
    ) -> None:
        self.events.append(("segment_start", (segment_id, kind, dict(attributes))))

    def stop_segment(self, segment_id: str, reason: str) -> None:
        self.events.append(("segment_stop", (segment_id, reason)))

    def marker(self, marker_id: str, kind: str, attributes: Attributes) -> None:
        copied = dict(attributes)
        self.events.append(("marker", (marker_id, kind, copied)))
        self.marker_attributes = copied


class ControllerTests(unittest.TestCase):
    def test_before_start_and_duplicate_rejection(self) -> None:
        controller = CaptureController(Backend())
        with self.assertRaises(RuntimeError):
            controller.marker("id", "kind")
        controller.start()
        controller.start_segment("a", "outer")
        with self.assertRaises(RuntimeError):
            controller.start_segment("a", "duplicate")

    def test_nested_segments_close_in_reverse_order_without_markers(self) -> None:
        backend = Backend()
        controller = CaptureController(backend)
        controller.start()
        controller.start_segment("outer", "phase")
        controller.start_segment("inner", "phase")
        controller.close()
        self.assertEqual(
            backend.events,
            [
                ("start", None),
                ("segment_start", ("outer", "phase", {})),
                ("segment_start", ("inner", "phase", {})),
                ("segment_stop", ("inner", "completed")),
                ("segment_stop", ("outer", "completed")),
                ("stop", "normal_completion"),
            ],
        )

    def test_controlled_abort_and_exactly_once_stop(self) -> None:
        backend = Backend()
        controller = CaptureController(backend)
        reason = run_capture(
            controller, lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        controller.close()
        self.assertEqual(reason, "operator_interrupt")
        self.assertEqual(
            [event for event in backend.events if event[0] == "stop"],
            [("stop", "operator_interrupt")],
        )

    def test_partial_startup_failure_is_cleaned_once(self) -> None:
        backend = Backend(fail_start=True)
        controller = CaptureController(backend)
        with self.assertRaises(CaptureInitializationError):
            controller.start()
        controller.close()
        self.assertEqual(backend.events, [("start", None), ("stop", "startup_failure")])

    def test_attribute_copy_and_no_capture(self) -> None:
        backend = Backend()
        controller = CaptureController(backend)
        controller.start()
        attributes = {"value": 1}
        controller.marker("id", "kind", attributes)
        attributes["value"] = 2
        self.assertEqual(backend.marker_attributes, {"value": 1})
        no_capture = NoCaptureController()
        no_capture.start()
        no_capture.start_segment("x", "kind", ok=True)
        no_capture.marker("m", "kind")
        no_capture.stop_segment("x")
        no_capture.close()


if __name__ == "__main__":
    unittest.main()
