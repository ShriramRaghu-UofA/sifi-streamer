"""Reusable capture execution and terminal annotation functions."""

import shlex
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sifi_streamer.capture import Scalar
from sifi_streamer.controller import CaptureController

INTERACTIVE_HELP = """Commands:
  segment start ID KIND [key=value ...]
  segment stop ID [reason]
  marker ID KIND [key=value ...]
  help
  stop"""


def parse_scalar(value: str) -> Scalar:
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
    """Own startup and exactly one controlled close around *action*."""
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
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    return run_capture(controller, lambda _: sleep(duration_s))


def run_until_interrupt(
    controller: CaptureController, *, sleep: Callable[[float], None] = time.sleep
) -> str:
    def wait(_: CaptureController) -> None:
        while True:
            sleep(1)

    return run_capture(controller, wait)


def run_interactive_capture(
    controller: CaptureController,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> str:
    return run_capture(
        controller,
        lambda capture: interactive_annotations(
            capture, input_fn=input_fn, output_fn=output_fn
        ),
    )
