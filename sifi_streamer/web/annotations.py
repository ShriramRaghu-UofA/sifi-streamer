"""Reusable web-frontend definitions for ergonomic generic annotations."""

import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from sifi_streamer.capture.records import Scalar, validate_attributes


class AnnotationTarget(StrEnum):
    MARKER = "marker"
    SEGMENT = "segment"


@dataclass(frozen=True, slots=True)
class AnnotationKindDefinition:
    target: AnnotationTarget
    kind: str
    label: str | None = None
    color: str | None = None
    id_prefix: str | None = None
    separator: str = "_"
    padding: int = 2
    start: int = 1
    default_attributes: Mapping[str, Scalar] | None = None

    def __post_init__(self) -> None:
        if not self.kind or self.kind != self.kind.strip():
            raise ValueError("annotation kind must be a non-empty trimmed string")
        if (
            self.color is not None
            and re.fullmatch(r"#[0-9A-Fa-f]{6}", self.color) is None
        ):
            raise ValueError("annotation color must use #RRGGBB")
        if self.id_prefix is not None and not self.id_prefix:
            raise ValueError("id_prefix must be non-empty or null")
        if len(self.separator) > 4:
            raise ValueError("separator must contain at most four characters")
        if not 1 <= self.padding <= 9:
            raise ValueError("padding must be between 1 and 9")
        if self.start < 0:
            raise ValueError("start must be non-negative")
        object.__setattr__(
            self,
            "default_attributes",
            validate_attributes(self.default_attributes or {}),
        )

    @property
    def prefix(self) -> str:
        return self.kind if self.id_prefix is None else self.id_prefix


class AnnotationKindRegistry:
    """Thread-safe definitions and collision-free per-target ID counters."""

    def __init__(self, definitions: Sequence[AnnotationKindDefinition] = ()) -> None:
        self._lock = threading.Lock()
        self._definitions: dict[
            tuple[AnnotationTarget, str], AnnotationKindDefinition
        ] = {}
        self._next: dict[tuple[AnnotationTarget, str], int] = {}
        self._used: dict[AnnotationTarget, set[str]] = {
            AnnotationTarget.MARKER: set(),
            AnnotationTarget.SEGMENT: set(),
        }
        for definition in definitions:
            self.set(definition)

    @property
    def definitions(self) -> tuple[AnnotationKindDefinition, ...]:
        with self._lock:
            return tuple(self._definitions.values())

    def set(self, definition: AnnotationKindDefinition) -> None:
        key = (definition.target, definition.kind)
        with self._lock:
            self._definitions[key] = definition
            self._next[key] = max(
                self._next.get(key, definition.start), definition.start
            )

    def remove(self, target: AnnotationTarget, kind: str) -> None:
        with self._lock:
            self._definitions.pop((target, kind), None)

    def require(self, target: AnnotationTarget, kind: str) -> AnnotationKindDefinition:
        with self._lock:
            try:
                return self._definitions[(target, kind)]
            except KeyError as exc:
                raise LookupError(f"unknown {target.value} kind {kind!r}") from exc

    def reserve(self, target: AnnotationTarget, identifier: str) -> str:
        if not identifier:
            raise ValueError("annotation ID must be non-empty")
        with self._lock:
            if identifier in self._used[target]:
                raise ValueError(f"annotation ID {identifier!r} is already used")
            self._used[target].add(identifier)
        return identifier

    def generate(self, target: AnnotationTarget, kind: str) -> str:
        key = (target, kind)
        with self._lock:
            try:
                definition = self._definitions[key]
            except KeyError as exc:
                raise LookupError(f"unknown {target.value} kind {kind!r}") from exc
            counter = self._next[key]
            while True:
                identifier = (
                    f"{definition.prefix}{definition.separator}"
                    f"{counter:0{definition.padding}d}"
                )
                counter += 1
                if identifier not in self._used[target]:
                    self._next[key] = counter
                    self._used[target].add(identifier)
                    return identifier

    def merge_attributes(
        self,
        target: AnnotationTarget,
        kind: str,
        attributes: Mapping[str, Scalar] | None,
    ) -> dict[str, Scalar]:
        values = dict(self.require(target, kind).default_attributes or {})
        values.update(validate_attributes(attributes or {}))
        return validate_attributes(values)
