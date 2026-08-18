"""Local monitored-capture dashboard and presentation configuration."""

from sifi_streamer.web.annotations import (
    AnnotationKindDefinition,
    AnnotationKindRegistry,
    AnnotationTarget,
)
from sifi_streamer.web.coordinator import WebCaptureCoordinator, serve_capture_web

__all__ = [
    "AnnotationKindDefinition",
    "AnnotationKindRegistry",
    "AnnotationTarget",
    "WebCaptureCoordinator",
    "serve_capture_web",
]
