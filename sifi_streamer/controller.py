"""Generic composition-oriented capture lifecycle."""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from sifi_streamer.capture import Attributes, Scalar, validate_attributes
from sifi_streamer.exceptions import CaptureInitializationError


@runtime_checkable
class CaptureBackend(Protocol):
    """Narrow structural boundary implemented by capture transports."""

    def start(self) -> None: ...
    def stop(self, reason: str = "normal_completion") -> None: ...
    def start_segment(
        self, segment_id: str, kind: str, attributes: Attributes
    ) -> None: ...
    def stop_segment(self, segment_id: str, reason: str) -> None: ...
    def marker(self, marker_id: str, kind: str, attributes: Attributes) -> None: ...


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
    """Validate one capture and close open segments in reverse start order."""

    def __init__(self, backend: CaptureBackend) -> None:
        self._backend = backend
        self._segments: list[str] = []
        self._start_attempted = False
        self._backend_stopped = False
        self.started = False

    def start(self) -> None:
        if self.started:
            return
        if self._start_attempted:
            raise RuntimeError("capture startup has already been attempted")
        self._start_attempted = True
        try:
            self._backend.start()
        except Exception as exc:
            self._stop_backend("startup_failure")
            raise CaptureInitializationError("capture backend failed to start") from exc
        self.started = True

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
        self._require_started()
        self._backend.marker(marker_id, kind, _values(attributes, extra))

    def start_segment(
        self,
        segment_id: str,
        kind: str,
        attributes: Mapping[str, Scalar] | None = None,
        **extra: Scalar,
    ) -> str:
        self._require_started()
        if segment_id in self._segments:
            raise RuntimeError(f"segment {segment_id!r} is already active")
        self._backend.start_segment(segment_id, kind, _values(attributes, extra))
        self._segments.append(segment_id)
        return segment_id

    def stop_segment(self, segment_id: str, reason: str = "completed") -> None:
        self._require_started()
        if not self._segments or self._segments[-1] != segment_id:
            raise RuntimeError("segments must be stopped in reverse start order")
        self._backend.stop_segment(segment_id, reason)
        self._segments.pop()

    def _stop_backend(self, reason: str) -> None:
        if self._backend_stopped:
            return
        self._backend_stopped = True
        self._backend.stop(reason)

    def close(self, reason: str = "normal_completion") -> None:
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
    """Structural no-op for deliberate operation without capture hardware."""

    started = True

    def start(self) -> None:
        pass

    def marker(
        self,
        marker_id: str,
        kind: str,
        attributes: Mapping[str, Scalar] | None = None,
        **extra: Scalar,
    ) -> None:
        _values(attributes, extra)

    def start_segment(
        self,
        segment_id: str,
        kind: str,
        attributes: Mapping[str, Scalar] | None = None,
        **extra: Scalar,
    ) -> str:
        _values(attributes, extra)
        return segment_id

    def stop_segment(self, segment_id: str, reason: str = "completed") -> None:
        pass

    def close(self, reason: str = "normal_completion") -> None:
        pass
