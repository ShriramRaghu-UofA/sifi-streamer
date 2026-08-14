"""Non-authoritative append-only health diagnostic sidecars."""

import json
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Self


def default_health_path(capture_path: Path) -> Path:
    suffix = ".capture.jsonl.zst"
    name = capture_path.name
    return capture_path.with_name(
        f"{name[: -len(suffix)]}.health.jsonl"
        if name.endswith(suffix)
        else f"{name}.health.jsonl"
    )


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    return value


class HealthLogWriter:
    """Write newline-delimited diagnostic records with exclusive creation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = path.open("x", encoding="utf-8", newline="\n")
        self._sequence = 0

    def append(self, record_type: str, value: object) -> None:
        record = {
            "health_schema_version": 1,
            "sequence": self._sequence,
            "record_type": record_type,
            "value": _json_value(value),
        }
        text = json.dumps(
            record, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        self._file.write(text + "\n")
        self._file.flush()
        self._sequence += 1

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
