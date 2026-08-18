"""Device-neutral capture lifecycle orchestration.

Applications compose :class:`CaptureController` with a structural
:class:`CaptureBackend`.  The controller owns lifecycle validation and segment
ordering; the backend owns acquisition and persistence resources.
"""

import logging
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from sifi_streamer.capture.records import Attributes, Scalar, validate_attributes
from sifi_streamer.exceptions import CaptureInitializationError

logger = logging.getLogger(__name__)


@runtime_checkable
class CaptureBackend(Protocol):
    """Structural interface between a controller and capture implementation.

    Implementations start and stop exactly one capture and accept already
    validated, defensively copied annotations. They need not inherit from this
    protocol.
    """

    def start(self) -> None:
        """Acquire backend resources and start the capture."""
        ...

    def stop(self, reason: str = "normal_completion") -> None:
        """Flush and release backend resources using ``reason``."""
        ...

    def start_segment(self, segment_id: str, kind: str, attributes: Attributes) -> None:
        """Record the start boundary for one generic segment."""
        ...

    def stop_segment(self, segment_id: str, reason: str) -> None:
        """Record the stop boundary for an open segment."""
        ...

    def marker(self, marker_id: str, kind: str, attributes: Attributes) -> None:
        """Record one point marker."""
        ...


def _values(
    attributes: Mapping[str, Scalar] | None, extra: Mapping[str, Scalar]
) -> dict[str, Scalar]:
    values = dict(attributes or {})
    duplicate = values.keys() & extra.keys()
    if duplicate:
        raise ValueError(f"duplicate attributes: {', '.join(sorted(duplicate))}")
    values.update(extra)
    return validate_attributes(values)


class CaptureController:
    """Coordinate one capture through a composed backend.

    Markers and segments are rejected until :meth:`start` succeeds. Segments may
    nest, but must be stopped in reverse start order. :meth:`close` automatically
    closes any remaining segments in that order and stops the backend at most
    once.

    Args:
        backend: Structural backend that owns acquisition and capture resources.

    Attributes:
        started: Whether annotations are currently accepted.
    """

    def __init__(self, backend: CaptureBackend) -> None:
        self._backend = backend
        self._segments: list[str] = []
        self._start_attempted = False
        self._backend_stopped = False
        self.started = False

    def start(self) -> None:
        """Start the backend, cleaning it up if startup fails.

        A successful repeated call is a no-op. A failed startup cannot be retried
        on the same controller.

        Raises:
            CaptureInitializationError: If backend startup fails. The original
                exception is available as the cause.
            RuntimeError: If startup was already attempted and did not succeed.
        """
        if self.started:
            return
        if self._start_attempted:
            raise RuntimeError("capture startup has already been attempted")
        self._start_attempted = True
        try:
            self._backend.start()
        except Exception as exc:
            logger.exception("Capture backend startup failed")
            self._stop_backend("startup_failure")
            raise CaptureInitializationError("capture backend failed to start") from exc
        self.started = True
        logger.info("Capture controller started")

    def _require_started(self) -> None:
        if not self.started:
            raise RuntimeError("capture session has not started")

    def marker(
        self,
        marker_id: str,
        kind: str,
        attributes: Mapping[str, Scalar] | None = None,
        **extra: Scalar,
    ) -> None:
        """Record one marker in ``marker_id, kind`` positional order.

        ``attributes`` and keyword ``extra`` values are merged. Supplying the
        same key through both forms is an error.

        Raises:
            RuntimeError: If the capture has not started.
            ValueError: If annotations are duplicated or not scalar.
        """
        self._require_started()
        self._backend.marker(marker_id, kind, _values(attributes, extra))
        logger.info("Recorded marker %r (kind %r)", marker_id, kind)

    def start_segment(
        self,
        segment_id: str,
        kind: str,
        attributes: Mapping[str, Scalar] | None = None,
        **extra: Scalar,
    ) -> str:
        """Start a segment and return ``segment_id`` for caller convenience.

        Raises:
            RuntimeError: If the capture has not started or the identifier is
                already active.
            ValueError: If annotations are duplicated or invalid.
        """
        self._require_started()
        if segment_id in self._segments:
            raise RuntimeError(f"segment {segment_id!r} is already active")
        self._backend.start_segment(segment_id, kind, _values(attributes, extra))
        self._segments.append(segment_id)
        logger.info("Started segment %r (kind %r)", segment_id, kind)
        return segment_id

    def stop_segment(self, segment_id: str, reason: str = "completed") -> None:
        """Stop the most recently started segment.

        Raises:
            RuntimeError: If the capture is not started, the segment is unknown,
                or nested segments would be stopped out of order.
        """
        self._require_started()
        if not self._segments or self._segments[-1] != segment_id:
            raise RuntimeError("segments must be stopped in reverse start order")
        self._backend.stop_segment(segment_id, reason)
        self._segments.pop()
        logger.info("Stopped segment %r with reason %r", segment_id, reason)

    def _stop_backend(self, reason: str) -> None:
        if self._backend_stopped:
            return
        self._backend_stopped = True
        self._backend.stop(reason)
        logger.info("Stopped capture backend with reason %r", reason)

    def close(self, reason: str = "normal_completion") -> None:
        """Close open segments and stop the backend exactly once.

        Remaining segments receive ``"completed"`` after normal completion and
        ``"aborted"`` for every other capture close reason. Calling ``close``
        again is a no-op.
        """
        if self._backend_stopped:
            return
        segment_reason = "completed" if reason == "normal_completion" else "aborted"
        error: BaseException | None = None
        if self.started:
            while self._segments:
                try:
                    self.stop_segment(self._segments[-1], segment_reason)
                except BaseException as exc:
                    error = exc
                    break
        try:
            if self._start_attempted:
                self._stop_backend(reason)
        finally:
            self.started = False
        if error is not None:
            raise error

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> None:
        self.close("normal_completion" if exc_type is None else "aborted")


class NoCaptureController:
    """Validate annotations while deliberately performing no capture I/O.

    This object mirrors the controller surface for consumer paths where capture
    is intentionally disabled. It is always considered started and does not
    track segment lifecycle.
    """

    started = True

    def start(self) -> None:
        """Perform no work; the no-capture controller is always started."""
        pass

    def marker(
        self,
        marker_id: str,
        kind: str,
        attributes: Mapping[str, Scalar] | None = None,
        **extra: Scalar,
    ) -> None:
        """Validate marker attributes without recording a marker."""
        _values(attributes, extra)

    def start_segment(
        self,
        segment_id: str,
        kind: str,
        attributes: Mapping[str, Scalar] | None = None,
        **extra: Scalar,
    ) -> str:
        """Validate attributes and return ``segment_id`` without recording."""
        _values(attributes, extra)
        return segment_id

    def stop_segment(self, segment_id: str, reason: str = "completed") -> None:
        """Accept a segment stop without recording it."""
        pass

    def close(self, reason: str = "normal_completion") -> None:
        """Perform no work."""
        pass
