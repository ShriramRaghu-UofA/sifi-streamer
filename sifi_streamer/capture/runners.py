"""Reusable capture execution and terminal annotation functions."""

import shlex
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sifi_streamer.capture.controller import CaptureController
from sifi_streamer.capture.records import Scalar

INTERACTIVE_HELP = """Commands:
  segment start ID KIND [key=value ...]
  segment stop ID [reason]
  marker ID KIND [key=value ...]
  help
  stop"""


def parse_scalar(value: str) -> Scalar:
    """Parse a command-line scalar without evaluating arbitrary expressions.

    Case-insensitive booleans and nulls are recognized first, followed by base-10
    integers and floats. Every other value remains a string.
    """
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.lower() in {"none", "null"}:
        return None
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def parse_attributes(tokens: Sequence[str]) -> dict[str, Scalar]:
    """Parse unique ``key=value`` tokens into scalar annotations.

    Raises:
        ValueError: If a token has no non-empty key or a key occurs more than once.
    """
    result: dict[str, Scalar] = {}
    for token in tokens:
        key, separator, value = token.partition("=")
        if not separator or not key:
            raise ValueError("attributes must use key=value syntax")
        if key in result:
            raise ValueError(f"duplicate attribute {key!r}")
        result[key] = parse_scalar(value)
    return result


def interactive_annotations(
    controller: CaptureController,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> None:
    """Read interactive marker and segment commands until ``stop`` or EOF.

    Invalid commands and controller validation errors are reported through
    ``output_fn`` and do not end the loop. ``shlex`` parsing permits quoted IDs,
    kinds, and values.

    Args:
        controller: Already-started controller owned by the caller.
        input_fn: Injectable prompt function, primarily for tests and UIs.
        output_fn: Injectable line-output function.
    """
    output_fn(INTERACTIVE_HELP)
    while True:
        try:
            tokens = shlex.split(
                input_fn(f"{datetime.now(UTC).isoformat(timespec='seconds')} capture> ")
            )
        except EOFError:
            return
        if not tokens:
            continue
        try:
            if tokens == ["help"]:
                output_fn(INTERACTIVE_HELP)
            elif tokens == ["stop"]:
                return
            elif len(tokens) >= 4 and tokens[:2] == ["segment", "start"]:
                controller.start_segment(
                    tokens[2], tokens[3], parse_attributes(tokens[4:])
                )
            elif 3 <= len(tokens) <= 4 and tokens[:2] == ["segment", "stop"]:
                controller.stop_segment(
                    tokens[2], tokens[3] if len(tokens) == 4 else "completed"
                )
            elif len(tokens) >= 3 and tokens[0] == "marker":
                controller.marker(tokens[1], tokens[2], parse_attributes(tokens[3:]))
            else:
                output_fn("Invalid command; enter 'help' for syntax.")
        except (RuntimeError, ValueError) as exc:
            output_fn(f"Command failed: {exc}")


def run_capture(
    controller: CaptureController, action: Callable[[CaptureController], None]
) -> str:
    """Own startup and exactly one controlled close around ``action``.

    A keyboard interrupt is consumed and mapped to ``"operator_interrupt"``.
    Other exceptions propagate after closing with ``"aborted"``.

    Returns:
        The reason passed to :meth:`CaptureController.close`.
    """
    reason = "normal_completion"
    try:
        controller.start()
        action(controller)
    except KeyboardInterrupt:
        reason = "operator_interrupt"
    except BaseException:
        reason = "aborted"
        raise
    finally:
        controller.close(reason)
    return reason


def run_timed_capture(
    controller: CaptureController,
    duration_s: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Run a capture for a positive duration and return its close reason.

    ``sleep`` is injectable so tests need not wait in real time.
    """
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    return run_capture(controller, lambda _: sleep(duration_s))


def run_until_interrupt(
    controller: CaptureController, *, sleep: Callable[[float], None] = time.sleep
) -> str:
    """Run until Ctrl+C, then close cleanly with ``operator_interrupt``."""

    def wait(_: CaptureController) -> None:
        """Sleep cooperatively until interrupted by the operator."""
        while True:
            sleep(1)

    return run_capture(controller, wait)


def run_interactive_capture(
    controller: CaptureController,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> str:
    """Own a capture around :func:`interactive_annotations`."""
    return run_capture(
        controller,
        lambda capture: interactive_annotations(
            capture, input_fn=input_fn, output_fn=output_fn
        ),
    )
