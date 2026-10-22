"""Shared named-device selection for SiFi launchers."""

import argparse
from collections.abc import Sequence


def add_device_arguments(parser: argparse.ArgumentParser) -> None:
    """Add mutually exclusive single and named-device selections."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--device-handle",
        metavar="HANDLE",
        help="BLE name, MAC (Windows/Linux), or UUID (macOS); default: auto-connect",
    )
    group.add_argument(
        "--device",
        action="append",
        metavar="ID=HANDLE",
        help="Named SiFi device; repeat for multiple devices (shared sensor profile)",
    )


def resolve_devices(values: Sequence[str] | None) -> dict[str, str] | None:
    """Parse explicit device IDs, rejecting ambiguous duplicate selections."""
    if values is None:
        return None
    devices: dict[str, str] = {}
    for value in values:
        name, separator, handle = value.partition("=")
        if not separator or not name or name != name.strip() or "/" in name:
            raise ValueError(
                "--device requires ID=HANDLE; ID must be trimmed without '/'"
            )
        if not handle.strip() or any(c in handle for c in "\r\n\0"):
            raise ValueError(
                "--device requires a nonempty handle without CR, LF, or NUL"
            )
        if name in devices:
            raise ValueError(f"duplicate device ID: {name}")
        if handle in devices.values():
            raise ValueError(f"duplicate device handle: {handle}")
        devices[name] = handle
    return devices
