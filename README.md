# sifi-streamer

[![CI](https://github.com/BLINCdev/sifi-streamer/actions/workflows/main.yml/badge.svg)](https://github.com/BLINCdev/sifi-streamer/actions/workflows/main.yml)

Private Python 3.14+ package for SiFi acquisition, shared-memory live signals,
and append-only capture logs. The `sifi_streamer` namespace is preserved so
existing consumers can replace their copied package with a dependency.

## Installation

```powershell
uv add sifi-streamer
```

From the private Git repository:

```toml
[project]
dependencies = ["sifi-streamer"]

[tool.uv.sources]
sifi-streamer = { git = "https://github.com/BLINCdev/sifi-streamer.git", tag = "v0.2.2" }
```

For this private repository, authenticate HTTPS access through Git's credential
manager or a GitHub personal access token. Use an immutable tag or commit in
production.

## Bridge download

Bridge acquisition is always an explicit user action. Installing this package
or starting a capture never downloads or updates vendor software.

Install the maintainer-tested `2.0.0-b21` release into the default `bin`
directory:

```powershell
uv run sifi-download-bridge --tested
```

Choose another destination if desired:

```powershell
uv run sifi-download-bridge --tested --output-directory C:\tools\sifi
```

The latest GitHub release may also work, but it has not necessarily been tested
with this package and therefore requires an explicit opt-in:

```powershell
uv run sifi-download-bridge --latest --output-directory C:\tools\sifi
```

A specific untested release tag can be selected explicitly:

```powershell
uv run sifi-download-bridge --tag 2.0.0-b21 --output-directory C:\tools\sifi
```

The utility auto-detects supported Windows, macOS, and Linux architectures,
uses only Python's standard-library `urllib`, verifies the release asset's
SHA-256 before installation, and extracts the nested `sifibridge` executable.
It writes `sifibridge-manifest.json` beside the executable with the release
version, asset, verified SHA-256, source URL, and installation time. Latest and
specific-tag modes refuse to install an asset if GitHub does not publish a
valid SHA-256 digest. Existing files are not replaced unless `--force` is
supplied. Tested mode uses the same tag resolver with the version and hashes
pinned by the maintainer.

## API capture

```python
from pathlib import Path

from sifi_streamer import CaptureLogReader, create_sifi_capture

capture = create_sifi_capture(
    Path("session.capture.jsonl.zst"),
    "session-001",
    {"site": "lab-a"},
    bridge_executable=Path(r"C:\tools\sifibridge.exe"),
    emg_sample_rate=1600,
)
capture.start()
try:
    capture.start_segment("rest-001", "rest", condition="eyes_open")
    capture.marker("prompt-001", "prompt_presented")
    capture.stop_segment("rest-001")
finally:
    capture.close()

for record in CaptureLogReader(Path("session.capture.jsonl.zst")):
    print(record)
```

IDs identify occurrences; kinds identify stable categories. Segment boundaries
are records themselves and do not create duplicate markers. Attributes must be
scalar `str | int | finite float | bool | None` values. Nested containers and
non-finite floats are rejected.

> Only the launcher or component that starts a capture closes it. Code
> receiving an already-started controller may annotate it but does not close
> it.

Consumer composition can define its own vocabulary:

```python
from sifi_streamer import CaptureController


def run_trial(capture: CaptureController, trial_id: str) -> None:
    capture.start_segment(trial_id, "trial", difficulty=2)
    try:
        capture.marker(f"{trial_id}-stimulus", "stimulus_presented")
    finally:
        capture.stop_segment(trial_id)
```

The example's trial vocabulary belongs to the consumer, not this package.

## CLI capture

```powershell
sifi-capture recording.capture.jsonl.zst --capture-id session-001 `
  --bridge-executable C:\tools\sifibridge.exe --emg-sample-rate 1600
sifi-capture timed.capture.jsonl.zst --capture-id baseline --duration 300
sifi-capture notes.capture.jsonl.zst --capture-id annotated --interactive
```

Interactive commands:

```text
segment start ID KIND [key=value ...]
segment stop ID [reason]
marker ID KIND [key=value ...]
help
stop
```

Ctrl+C becomes `operator_interrupt`; the worker and bridge are stopped
orderly so the capture is flushed.

## Synthetic capture

```powershell
sifi-capture synthetic.capture.jsonl.zst --capture-id dev --synthetic --duration 1
```

For live access, enter a `BackgroundHandle` and read `handle.reader` (EMG) or
the typed `handle.readers` collection. `SyntheticSiFiDevice` uses the same
background and shared-memory path as hardware.

The `.capture.jsonl.zst` file is authoritative, append-only schema-v2 JSONL in
concatenated Zstandard frames. New files use exclusive creation and are never
rewritten. Optional Parquet dependencies are available with
`sifi-streamer[parquet]`; conversion is not authoritative.

See the [Python API reference](api.md), [architecture.md](architecture.md),
[migration.md](migration.md), and [compatibility.md](compatibility.md).
