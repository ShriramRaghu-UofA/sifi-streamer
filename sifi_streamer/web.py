"""Loopback-only local web launcher for monitored captures."""

import json
import logging
import mimetypes
import secrets
import threading
import webbrowser
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sifi_streamer.annotation_kinds import (
    AnnotationKindDefinition,
    AnnotationKindRegistry,
    AnnotationTarget,
)
from sifi_streamer.capture import Attributes, Scalar, validate_attributes
from sifi_streamer.health import HealthThresholds
from sifi_streamer.health_log import HealthLogWriter, default_health_path
from sifi_streamer.monitor import CaptureRuntime

type RuntimeFactory = Callable[[str, Attributes], CaptureRuntime]

logger = logging.getLogger(__name__)


def _wire(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return _wire(asdict(value))
    if hasattr(value, "value") and isinstance(value.value, str):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _wire(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_wire(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


class WebCaptureCoordinator:
    """Serialize browser commands and own exactly one capture lifecycle."""

    def __init__(
        self,
        output: Path,
        runtime_factory: RuntimeFactory,
        *,
        configuration_summary: Mapping[str, Scalar] | None = None,
        default_capture_id: str = "capture",
        default_attributes: Mapping[str, Scalar] | None = None,
        thresholds: HealthThresholds | None = None,
        definitions: Sequence[AnnotationKindDefinition] = (),
        health_log_enabled: bool = True,
    ) -> None:
        self.output = output
        self._factory = runtime_factory
        self._configuration = validate_attributes(configuration_summary or {})
        self.default_capture_id = default_capture_id
        self.default_attributes = validate_attributes(default_attributes or {})
        self._thresholds = thresholds or HealthThresholds()
        self._kinds = AnnotationKindRegistry(definitions)
        self._health_log_default = health_log_enabled
        self._lock = threading.RLock()
        self._runtime: CaptureRuntime | None = None
        self._health_log: HealthLogWriter | None = None
        self._active_segments: list[str] = []
        self._state = "setup"
        self._error: str | None = None
        self._monitor_thread: threading.Thread | None = None
        self._stop_monitor = threading.Event()
        self._last_logged_health = 0.0
        self._logged_events = 0

    def bootstrap(self) -> dict[str, object]:
        with self._lock:
            return {
                "state": self._state,
                "error": self._error,
                "output": str(self.output),
                "configuration": self._configuration,
                "default_capture_id": self.default_capture_id,
                "default_attributes": self.default_attributes,
                "thresholds": _wire(self._thresholds),
                "health_log_enabled": self._health_log_default,
                "kinds": _wire(self._kinds.definitions),
                "active_segments": list(self._active_segments),
                "streams": self._streams(),
            }

    def start(
        self,
        capture_id: str,
        attributes: Mapping[str, Scalar] | None,
        *,
        thresholds: HealthThresholds | None = None,
        health_log_enabled: bool | None = None,
    ) -> None:
        with self._lock:
            if self._state != "setup":
                raise RuntimeError("this dashboard has already started a capture")
            if not capture_id:
                raise ValueError("capture_id must be non-empty")
            if self.output.exists():
                raise FileExistsError(f"output already exists: {self.output}")
            enabled = (
                self._health_log_default
                if health_log_enabled is None
                else health_log_enabled
            )
            health_path = default_health_path(self.output)
            if enabled and health_path.exists():
                raise FileExistsError(f"health log already exists: {health_path}")
            self._state = "starting"
            logger.info("Starting capture %r at %s", capture_id, self.output)
            runtime = self._factory(capture_id, validate_attributes(attributes or {}))
            if thresholds is not None:
                self._thresholds = thresholds
            runtime.monitor.update_thresholds(self._thresholds)
            try:
                runtime.controller.start()
                _ = runtime.monitor.streams
                if enabled:
                    self._health_log = HealthLogWriter(health_path)
                    self._health_log.append(
                        "health_started",
                        {
                            "capture": self.output,
                            "capture_id": capture_id,
                            "thresholds": self._thresholds,
                            "kinds": self._kinds.definitions,
                        },
                    )
            except BaseException:
                logger.exception("Capture %r failed during startup", capture_id)
                runtime.controller.close("startup_failure")
                self._state = "failed"
                raise
            self._runtime = runtime
            self._state = "recording"
            logger.info("Capture %r is recording", capture_id)
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name="sifi-web-monitor",
            )
            self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        while not self._stop_monitor.wait(0.25):
            with self._lock:
                if self._state != "recording" or self._runtime is None:
                    return
                self._refresh_health()
                if (fatal := self._runtime.monitor.fatal()) is not None:
                    logger.error("Worker failure [%s]: %s", fatal.code, fatal.message)
                    self._error = fatal.message
                    try:
                        self._close_locked(fatal.code)
                    except BaseException as exc:
                        self._error = f"{fatal.message}; close failed: {exc}"
                    self._state = "failed"
                    return

    def _refresh_health(self) -> object:
        if self._runtime is None:
            return None
        snapshot = self._runtime.monitor.latest()
        if snapshot is not None:
            if (
                self._health_log is not None
                and snapshot.monotonic_time - self._last_logged_health >= 1
            ):
                self._health_log.append("health_snapshot", snapshot)
                self._last_logged_health = snapshot.monotonic_time
            events = self._runtime.monitor.events
            for event in events[self._logged_events :]:
                if self._health_log is not None:
                    self._health_log.append("health_event", event)
                message = "%s health %s [%s]: %s"
                arguments = (
                    event.stream_id or "acquisition",
                    "warning" if event.active else "recovered",
                    event.code,
                    event.message,
                )
                (logger.warning if event.active else logger.info)(message, *arguments)
            self._logged_events = len(events)
        return snapshot

    def stop(self, reason: str = "normal_completion") -> None:
        with self._lock:
            if self._state not in {"recording", "starting"}:
                return
            self._close_locked(reason)

    def _close_locked(self, reason: str) -> None:
        self._state = "stopping"
        logger.info("Stopping capture with reason %r", reason)
        self._stop_monitor.set()
        try:
            if self._runtime is not None:
                self._runtime.controller.close(reason)
        finally:
            if self._health_log is not None:
                self._health_log.append(
                    "health_stopped", {"reason": reason, "error": self._error}
                )
                self._health_log.close()
                self._health_log = None
            self._state = "stopped" if self._error is None else "failed"
            logger.info("Capture stopped with state %s", self._state)

    def update_thresholds(self, thresholds: HealthThresholds) -> None:
        with self._lock:
            self._thresholds = thresholds
            logger.info("Updated signal health thresholds")
            if self._runtime is not None:
                self._runtime.monitor.update_thresholds(thresholds)
            if self._health_log is not None:
                self._health_log.append("thresholds_changed", thresholds)

    def set_kind(self, definition: AnnotationKindDefinition) -> None:
        with self._lock:
            self._kinds.set(definition)
            logger.info("Set %s annotation kind %r", definition.target, definition.kind)
            if self._health_log is not None:
                self._health_log.append("annotation_kind_set", definition)

    def remove_kind(self, target: AnnotationTarget, kind: str) -> None:
        with self._lock:
            self._kinds.remove(target, kind)
            logger.info("Removed %s annotation kind %r", target, kind)
            if self._health_log is not None:
                self._health_log.append(
                    "annotation_kind_removed", {"target": target, "kind": kind}
                )

    def marker(
        self,
        kind: str,
        attributes: Mapping[str, Scalar] | None,
        identifier: str | None = None,
    ) -> str:
        with self._lock:
            runtime = self._require_recording()
            identifier = (
                self._kinds.generate(AnnotationTarget.MARKER, kind)
                if identifier is None
                else self._kinds.reserve(AnnotationTarget.MARKER, identifier)
            )
            values = self._kind_attributes(AnnotationTarget.MARKER, kind, attributes)
            runtime.controller.marker(identifier, kind, values)
            logger.info("Added marker %r (kind %r)", identifier, kind)
            return identifier

    def start_segment(
        self,
        kind: str,
        attributes: Mapping[str, Scalar] | None,
        identifier: str | None = None,
    ) -> str:
        with self._lock:
            runtime = self._require_recording()
            identifier = (
                self._kinds.generate(AnnotationTarget.SEGMENT, kind)
                if identifier is None
                else self._kinds.reserve(AnnotationTarget.SEGMENT, identifier)
            )
            values = self._kind_attributes(AnnotationTarget.SEGMENT, kind, attributes)
            runtime.controller.start_segment(identifier, kind, values)
            self._active_segments.append(identifier)
            logger.info("Started segment %r (kind %r)", identifier, kind)
            return identifier

    def stop_segment(self, identifier: str, reason: str = "completed") -> None:
        with self._lock:
            runtime = self._require_recording()
            if not self._active_segments or self._active_segments[-1] != identifier:
                raise RuntimeError("only the most recently started segment can stop")
            runtime.controller.stop_segment(identifier, reason)
            self._active_segments.pop()
            logger.info("Stopped segment %r with reason %r", identifier, reason)

    def _kind_attributes(
        self,
        target: AnnotationTarget,
        kind: str,
        attributes: Mapping[str, Scalar] | None,
    ) -> dict[str, Scalar]:
        try:
            return self._kinds.merge_attributes(target, kind, attributes)
        except LookupError:
            return validate_attributes(attributes or {})

    def live(self, cursors: Mapping[str, int]) -> dict[str, object]:
        with self._lock:
            snapshot = self._refresh_health()
            batches: dict[str, object] = {}
            if self._runtime is not None and self._state == "recording":
                for stream in self._runtime.monitor.streams:
                    cursor = int(cursors.get(stream.stream_id, 0))
                    window = self._runtime.monitor.read_since(
                        stream.stream_id, cursor, max_samples=stream.n_samples
                    )
                    if window is None:
                        continue
                    samples: list[list[float | int | None]] = []
                    for row, valid in zip(
                        window.samples.tolist(), window.validity.tolist(), strict=True
                    ):
                        samples.append(
                            [
                                value if is_valid else None
                                for value, is_valid in zip(row, valid, strict=True)
                            ]
                        )
                    batches[stream.stream_id] = {
                        "start_index": window.start_index,
                        "end_index": window.end_index,
                        "timestamps": window.timestamps.tolist(),
                        "samples": samples,
                        "overrun": window.overrun,
                    }
            return {
                "state": self._state,
                "error": self._error,
                "health": _wire(snapshot),
                "events": _wire(
                    self._runtime.monitor.events if self._runtime is not None else ()
                ),
                "batches": batches,
                "active_segments": list(self._active_segments),
                "thresholds": _wire(self._thresholds),
                "kinds": _wire(self._kinds.definitions),
            }

    def _streams(self) -> list[object]:
        if self._runtime is None or self._state == "setup":
            return []
        return [_wire(item) for item in self._runtime.monitor.streams]

    def _require_recording(self) -> CaptureRuntime:
        if self._state != "recording" or self._runtime is None:
            raise RuntimeError("capture is not recording")
        return self._runtime


class _WebServer(ThreadingHTTPServer):
    coordinator: WebCaptureCoordinator
    token: str
    origin: str


class _Handler(BaseHTTPRequestHandler):
    server: _WebServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/bootstrap":
            self._api(lambda: self.server.coordinator.bootstrap())
            return
        assets = {"/": "index.html", "/app.js": "app.js", "/app.css": "app.css"}
        if path not in assets:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        resource = files("sifi_streamer.web_assets").joinpath(assets[path])
        data = resource.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            mimetypes.guess_type(assets[path])[0] or "application/octet-stream",
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path

        def action() -> object:
            body = self._body()
            coordinator = self.server.coordinator
            if path == "/api/live":
                return coordinator.live(body.get("cursors", {}))
            if path == "/api/capture/start":
                coordinator.start(
                    str(body["capture_id"]),
                    body.get("attributes", {}),
                    thresholds=_thresholds(body.get("thresholds")),
                    health_log_enabled=body.get("health_log_enabled"),
                )
                return coordinator.bootstrap()
            if path == "/api/capture/stop":
                coordinator.stop("normal_completion")
                return coordinator.bootstrap()
            if path == "/api/marker":
                identifier = coordinator.marker(
                    str(body["kind"]), body.get("attributes"), body.get("id")
                )
                return {"id": identifier}
            if path == "/api/segment/start":
                identifier = coordinator.start_segment(
                    str(body["kind"]), body.get("attributes"), body.get("id")
                )
                return {"id": identifier}
            if path == "/api/segment/stop":
                coordinator.stop_segment(
                    str(body["id"]), str(body.get("reason", "completed"))
                )
                return {}
            if path == "/api/thresholds":
                coordinator.update_thresholds(_thresholds(body) or HealthThresholds())
                return {}
            if path == "/api/kinds/set":
                coordinator.set_kind(_kind(body))
                return {}
            if path == "/api/kinds/remove":
                coordinator.remove_kind(
                    AnnotationTarget(str(body["target"])), str(body["kind"])
                )
                return {}
            if path == "/api/server/stop":
                coordinator.stop("operator_request")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return {}
            raise LookupError("unknown API route")

        self._api(action)

    def _body(self) -> dict[str, Any]:
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            raise ValueError("Content-Type must be application/json")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 65536:
            raise ValueError("request body is too large")
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError("request JSON must be an object")
        return value

    def _api(self, callback: Callable[[], object]) -> None:
        if self.headers.get("X-SiFi-Session-Token") != self.server.token:
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "invalid session token"})
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            self._json(HTTPStatus.FORBIDDEN, {"error": "invalid origin"})
            return
        try:
            value = callback()
        except (
            KeyError,
            LookupError,
            RuntimeError,
            TypeError,
            ValueError,
            OSError,
        ) as exc:
            logger.warning("API request %s failed: %s", self.path, exc)
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._json(HTTPStatus.OK, value)

    def _json(self, status: HTTPStatus, value: object) -> None:
        data = json.dumps(_wire(value), allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        logger.debug("HTTP %s - %s", self.address_string(), format % args)


def _thresholds(value: Any) -> HealthThresholds | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("thresholds must be an object")
    return HealthThresholds(**value)


def _kind(value: Mapping[str, Any]) -> AnnotationKindDefinition:
    return AnnotationKindDefinition(
        AnnotationTarget(str(value["target"])),
        str(value["kind"]),
        str(value["label"]) if value.get("label") is not None else None,
        str(value["color"]) if value.get("color") is not None else None,
        str(value["id_prefix"]) if value.get("id_prefix") is not None else None,
        str(value.get("separator", "_")),
        int(value.get("padding", 2)),
        int(value.get("start", 1)),
        value.get("default_attributes", {}),
    )


def serve_capture_web(
    output: Path,
    runtime_factory: RuntimeFactory,
    *,
    configuration_summary: Mapping[str, Scalar] | None = None,
    default_capture_id: str = "capture",
    default_attributes: Mapping[str, Scalar] | None = None,
    thresholds: HealthThresholds | None = None,
    definitions: Sequence[AnnotationKindDefinition] = (),
    health_log_enabled: bool = True,
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """Serve one local dashboard and close an active capture on Ctrl+C."""
    coordinator = WebCaptureCoordinator(
        output,
        runtime_factory,
        configuration_summary=configuration_summary,
        default_capture_id=default_capture_id,
        default_attributes=default_attributes,
        thresholds=thresholds,
        definitions=definitions,
        health_log_enabled=health_log_enabled,
    )
    token = secrets.token_urlsafe(24)
    server = _WebServer(("127.0.0.1", port), _Handler)
    server.coordinator = coordinator
    server.token = token
    server.origin = f"http://127.0.0.1:{server.server_port}"
    url = f"{server.origin}/#{token}"
    logger.info("Capture dashboard listening at %s", server.origin)
    print(f"Capture dashboard: {url}", flush=True)
    if open_browser:
        webbrowser.open(url, new=2)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Operator interrupt received")
        coordinator.stop("operator_interrupt")
    finally:
        server.server_close()
        logger.info("Capture dashboard stopped")
