# sifi-streamer

[![CI](https://github.com/ShriramRaghu-UofA/sifi-streamer/actions/workflows/main.yml/badge.svg)](https://github.com/ShriramRaghu-UofA/sifi-streamer/actions/workflows/main.yml)

Pluggable Python 3.14+ acquisition and capture infrastructure with a bundled
SiFi integration. Inject any device that satisfies the structural acquisition
protocols to reuse the background worker, shared-memory live signals, health
monitoring, generic annotations, and authoritative append-only capture logs.
Device implementations use composition and dependency injection; they do not
inherit from package base classes.

The package is organized by responsibility:

- `sifi_streamer.capture`: device-neutral records, controllers, and runners;
- `sifi_streamer.acquisition`: device protocols, worker process, shared memory,
  monitoring, and the generic capture backend;
- `sifi_streamer.sifi`: SiFi Bridge integration, devices, profiles, and
  composition helpers;
- `sifi_streamer.web`: the optional local dashboard and its presentation
  configuration.

## Installation

```powershell
uv add sifi-streamer
```

From the private Git repository:

```toml
[project]
dependencies = ["sifi-streamer"]

[tool.uv.sources]
sifi-streamer = { git = "https://github.com/ShriramRaghu-UofA/sifi-streamer.git", tag = "v0.6.4" }
```

For this private repository, authenticate HTTPS access through Git's credential
manager or a GitHub personal access token. Use an immutable tag or commit in
production.

## License

The code in this repository is licensed under the [MIT License](LICENSE).
SiFi Bridge is a separate proprietary binary supplied by SiFi Labs Inc. It is
not included in this repository and is not covered by the MIT License. See the
vendor terms below before downloading or using it.

## Bridge download

### Vendor dependency and license

This project uses [SiFi Bridge](https://github.com/SiFiLabs/sifi-bridge-pub),
proprietary vendor software provided by **SiFi Labs Inc.** SiFi Bridge is free
to use but is not open-source software. It is distributed separately and is
not covered by this repository's MIT License. Software built on it should
credit it as "SiFi Bridge (SiFi Labs Inc.)." The tested bridge release is
pinned below because the bridge is evolving and later releases may
introduce incompatible changes.

SiFi Labs Inc. grants permission to download, install, and run the SiFi Bridge
CLI, including in academic, commercial, and publicly demonstrated projects,
subject to these terms:

- Redistribute the binary only unmodified and with this notice included;
  linking to the [official release](https://github.com/SiFiLabs/sifi-bridge-pub/releases)
  is preferred.
- Do not modify, decompile, reverse engineer, or attempt to derive the source
  code of the binary.
- Including SiFi Bridge in an open-source project does not place it under that
  project's license.
- Use of the SiFi Labs name or logo in a way that suggests endorsement,
  sponsorship, or validation requires written permission.
- SiFi devices and SiFi Bridge are intended for research and development use.
  They are not medical devices and must not be used for diagnosis, treatment,
  or any application where failure could cause injury.
- SiFi Bridge is provided as is, without warranty of any kind. SiFi Labs Inc.
  accepts no liability for its use.

Questions about a specific use case should be sent to
[contact@sifilabs.com](mailto:contact@sifilabs.com).

Bridge acquisition is always an explicit user action. Installing this package
or starting a capture never downloads or updates vendor software.

Install the pinned `2.0.1` release into the default `bin`
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
uv run sifi-download-bridge --tag 2.0.1 --output-directory C:\tools\sifi
```

The utility auto-detects supported Windows, macOS, and Linux architectures,
uses only Python's standard-library `urllib`, verifies the release asset's
SHA-256 before installation, and extracts the nested `sifibridge` executable.
Capture commands default to `bin/sifibridge.exe` on Windows and
`bin/sifibridge` on macOS and Linux.
It writes `sifibridge-manifest.json` beside the executable with the release
version, asset, verified SHA-256, source URL, and installation time. Latest and
specific-tag modes refuse to install an asset if GitHub does not publish a
valid SHA-256 digest. Existing files are not replaced unless `--force` is
supplied. Tested mode uses the same tag resolver with the version and hashes
pinned by the maintainer.

## API capture

```python
from pathlib import Path

from sifi_streamer.capture import CaptureLogReader
from sifi_streamer.sifi import create_sifi_capture
from sifi_streamer.sifi.sensor_profile import EMG_IMU_PROFILE

capture = create_sifi_capture(
    Path("session.capture.jsonl.zst"),
    "session-001",
    {"site": "lab-a"},
    bridge_executable=Path(r"C:\tools\sifibridge.exe"),
    sensor_profile=EMG_IMU_PROFILE,
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
from sifi_streamer.capture import CaptureController


def run_trial(capture: CaptureController, trial_id: str) -> None:
    capture.start_segment(trial_id, "trial", difficulty=2)
    try:
        capture.marker(f"{trial_id}-stimulus", "stimulus_presented")
    finally:
        capture.stop_segment(trial_id)
```

The example's trial vocabulary belongs to the consumer, not this package.

## Pluggable acquisition

Custom hardware implements the structural `AcquisitionDevice` and
`AcquisitionPacket` protocols by shape; explicit inheritance or registration is
not required. A connected device declares one fixed ordered stream registry and
each packet contributes samples to at most one declared stream while optionally
providing its complete raw document for capture.

```python
from pathlib import Path

from sifi_streamer.acquisition import SignalChannelSpec, SignalStreamSpec
from sifi_streamer.acquisition.backend import create_capture_runtime


class MyoDevice:
    @property
    def streams(self) -> tuple[SignalStreamSpec, ...]:
        return (
            SignalStreamSpec(
                "emg",
                tuple(SignalChannelSpec(f"emg{i}") for i in range(8)),
                200.0,
            ),
        )

    def connect(self) -> None: ...

    def disconnect(self) -> None: ...

    def read_packet(self) -> "MyoPacket": ...

    @property
    def device_info(self) -> dict[str, object]: ...


runtime = create_capture_runtime(
    Path("myo.capture.jsonl.zst"),
    "myo-session-001",
    MyoDevice,  # top-level, zero-argument, and picklable for the worker process
)
```

`MyoPacket` supplies `stream_id`, `timestamps`, channel `data`, health fields,
and `capture_document()`. See [api.md](api.md) for the complete protocol shape.
The included SiFi support uses this same boundary; it is an integration rather
than a privileged acquisition path.

## CLI capture

Both `sifi-capture` and `sifi-capture-web` use **stdout** for bridge data by
default. The managed bridge runs as a child process; stdout is read internally
and saved to the capture, rather than printed as raw data in your terminal.
Select `--transport tcp` or `--transport udp` to use a socket instead. This
option controls the bridge data transport, independently of the dashboard's
HTTP server. Python helpers likewise default to `transport="stdout"`.

Run these commands from the repository with `uv run` (or omit `uv run` when the
console scripts are installed on your PATH). Choose a new output filename for
each capture; existing captures cannot be overwritten.

Record EMG and IMU over stdout until Ctrl+C:

```powershell
uv run sifi-capture recording.capture.jsonl.zst --capture-id session-001 `
  --bridge-executable C:\tools\sifibridge.exe --sensor-preset emg-imu
```

Record for five minutes over TCP, or enter annotations interactively over UDP:

```powershell
uv run sifi-capture timed.capture.jsonl.zst --capture-id baseline `
  --duration 300 --transport tcp --host 127.0.0.1 --port 5000
uv run sifi-capture notes.capture.jsonl.zst --capture-id annotated `
  --interactive --transport udp --host 127.0.0.1 --port 5000
```

Without `--duration` or `--interactive`, recording continues until Ctrl+C.
`--duration` and `--interactive` are mutually exclusive. The package launches
and configures the bridge for all three transports; you do not start another
bridge manually. `--host` and `--port` specify the bridge TCP listener or UDP
destination and are unused for stdout. Keep the host at `127.0.0.1` for local
capture.

Hardware capture defaults to a complete all-sensors profile. Every supported
sensor setting is sent explicitly before acquisition, including settings for
disabled sensors. Built-in profiles are `all`, `emg-only`, and `emg-imu`.
Those preset names describe the five switchable sensors; temperature remains
device-controlled because the bridge exposes its rate but no enabled switch.
Generate an editable, versioned JSON profile and use it from either launcher:

```powershell
sifi-sensor-profile create sensors.json --preset all
sifi-sensor-profile validate sensors.json
sifi-capture recording.capture.jsonl.zst --capture-id session-001 `
  --sensor-profile sensors.json
```

Frequent state and rate settings can be overridden directly with `--ecg on`,
`--emg-fs 1600`, `--ppg-sps 200`, and `--ppg-avg 4`. PPG has no `fs` setting:
its effective output rate is `sps / avg`, so the default is `200 / 4 = 50 Hz`.
Sensor profile options are hardware-only and cannot be combined with
`--synthetic`.

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

## Local capture dashboard

Start the dashboard with the default stdout transport:

```powershell
uv run sifi-capture-web monitored.capture.jsonl.zst --capture-id session-001 `
  --bridge-executable C:\tools\sifibridge.exe --sensor-preset emg-imu
```

Start web capture with **TCP** on port 5000 and the dashboard on port 8080:

```powershell
uv run sifi-capture-web tcp.capture.jsonl.zst --capture-id tcp-session `
  --transport tcp --host 127.0.0.1 --port 5000 --web-port 8080 `
  --bridge-executable C:\tools\sifibridge.exe --sensor-preset emg-imu
```

Use UDP, a saved profile, scalar session metadata, and a reusable kinds file:

```powershell
uv run sifi-capture-web udp.capture.jsonl.zst --capture-id udp-session `
  --transport udp --port 5000 --sensor-profile sensors.json `
  --attribute operator=Shriram --attribute session=1 --kinds-file kinds.json `
  --no-open --web-port 8080
```

`--web-port` selects the dashboard HTTP port; its default `0` asks the OS to
assign a free port. It is separate from the bridge's `--port`. Open the printed
URL, including its token, and use the page's start/stop controls to record.
`--no-open` suppresses automatic browser opening. For a hardware-free dashboard,
use `uv run sifi-capture-web demo.capture.jsonl.zst --synthetic`; omit hardware
sensor options. Run either launcher with `--help` for all flags, including sensor
overrides and web health thresholds.

`sifi-capture-web` starts a loopback-only Python server, prints its URL, and
opens the default browser unless `--no-open` is supplied. The launcher fixes
the output and device configuration; the page displays the complete resolved
sensor profile, starts/stops one capture, displays all declared streams, shows
advertised/reported/observed rates and missing-data warnings, and provides
marker/segment controls.

Capture and annotation metadata are JSON objects containing simple scalar
values: text, numbers, booleans, or `null`. They attach searchable facts such as
an operator, condition, or session number to a record; nested objects and lists
are rejected. The dashboard provides a field editor with explicit value types;
you do not need to write JSON. Duplicate names and invalid numbers are rejected
before sending commands. Signals, Annotations, Health, and Session views keep
live plots, reusable kinds, diagnostic rules, and setup distinct; capture
controls stay visible. Stopping asks for confirmation and flushes open segments.

The dashboard uses Skeleton's Svelte components and design system. Cerberus in
dark mode is the default, with Catppuccin, Concord, Dracula, Mint, Modern,
Rosepine, and Wintry themes. Appearance offers light, dark, and system modes;
both theme and mode persist in your browser. Plots retain null gaps and show
configured, packet-reported, and received window-estimated rates. Health diagnostics
also show the packet timestamp-derived rate. Received rates count sample rows over
host elapsed time in the rolling evaluation window (5 seconds by default, shorter
during startup); timestamp-derived rates use per-sample timestamps supplied in
packets. Live connection errors and view overruns
are visible independently of capture status.

Marker and segment kinds have independent generated IDs. A segment kind named
`Task` uses `Task_01`, `Task_02`, and so on by default. Pass `--kinds-file` to
load reusable definitions; operators may adjust them for the current capture.
Health thresholds remain editable while recording. A non-authoritative
`.health.jsonl` sidecar is enabled by default and can be disabled.

Both capture CLIs configure console logging. The foreground, worker, bridge,
recorder, annotations, shutdown, and health warning/recovery transitions are
reported without logging raw packets or routine dashboard polling.

The dashboard requires its per-launch URL token and bundles Svelte, Skeleton,
and uPlot assets in the wheel for offline use. Installing from a wheel or Git
does not require Node.js because the compiled dashboard assets are committed
under `sifi_streamer/web/assets`. Node.js is needed only to change and rebuild
the frontend.

See [frontend/README.md](frontend/README.md) for development, browser tests,
and the component choices used by the dashboard.

## Synthetic capture

```powershell
sifi-capture synthetic.capture.jsonl.zst --capture-id dev --synthetic --duration 1
```

For live access, enter a `BackgroundHandle`, inspect `handle.streams`, and read
the corresponding entries in `handle.stream_readers`. Gap-aware incremental
reads return source timestamps, native values, and an explicit validity mask.
`SyntheticSiFiDevice` uses the same generic worker and shared-memory path as
hardware and custom injected devices.

The `.capture.jsonl.zst` file is authoritative, append-only schema-v3 JSONL in
concatenated Zstandard frames. New files use exclusive creation and are never
rewritten. When supplied by the connected device, its complete startup-info
document is preserved in an explicit `device_info` record; for SiFi
hardware this includes the bridge-reported firmware, configuration, and sample
rates.

Every decoded vendor data packet received while recording is stored as a
complete `raw_packet`, including `event`, `status`, `memory`, `device_info`,
`start_time`, and unknown future packet types and fields. Only declared signal
streams enter shared memory and signal tables; other packets remain available
through `CaptureLogReader`. Vendor `event` packets remain vendor raw packets;
application markers and segments have their own authoritative records. The
latest Start Time packet is retained across acquisition startup and inserted
after device info when recording starts. Other packets received before the
capture starts are not backfilled. Bridge command acknowledgements, stderr,
and malformed/non-object JSON are not capture data. The capture preserves what
the selected transport delivers; it cannot recover missing BLE samples or UDP
datagrams (vendor null gaps and loss counts are preserved).

Install `sifi-streamer[parquet]` for canonical SiFi pandas tables and derived
Parquet datasets:

```python
from pathlib import Path

from sifi_streamer.sifi import Modality
from sifi_streamer.sifi.export import read_sifi_capture_tables

tables = read_sifi_capture_tables(Path("session.capture.jsonl.zst"))
emg = tables.signals[Modality.EMG]
```

```powershell
sifi-capture-to-parquet session.capture.jsonl.zst
```

The exporter writes a dataset directory containing capture, stream, marker,
segment, launch configuration, device report, diagnostic, and per-modality signal
tables. Nested metadata documents are preserved as JSON string columns, including
empty and repeated reports. Startup failures can export with empty signal views.
It preserves record sequence and all
recorded clock domains but does not interpret application-defined kinds or
filter incomplete/superseded attempts. Conversion is not authoritative.

Third-party integrations can use the public `sifi_streamer.capture.CaptureLogReader`
and record types to parse complete vendor packet/report JSON themselves, without
the Parquet extra. See the reader example in [api.md](api.md#capturelogreader).

See the [Python API reference](api.md), [architecture.md](architecture.md),
[compatibility.md](compatibility.md), and [contribution guide](CONTRIBUTING.md).

Both capture launchers accept optional `--device-handle HANDLE`: a BLE name
such as `BioPoint_AA92`, a MAC address on Windows/Linux, or a BLE UUID on macOS.
Python callers pass `device_handle` to `SiFiBridgeDevice`, `create_sifi_capture`,
or `create_sifi_capture_runtime`. Omission retains first-matching-device
selection. A supplied handle is sent as `connect HANDLE`; bridge errors abort
startup without falling back to automatic selection. Synthetic capture rejects
this option. Capture records are unchanged.

## Capture provenance and diagnostics (schema 3)

A composed capture starts before hardware connection. Its first records are
`capture_started` and `launch_configuration`, containing resolved integration and
acquisition settings. The web launcher also includes health rules and annotation
configuration; the standalone launcher records its mode and requested duration.
Failed hardware startup leaves a readable capture with the requested settings,
available reports, a `diagnostic`, and `capture_stopped` (`startup_failure`).

Device metadata is an explicit `device_info` record with `stage` and `info`.
SiFi records reports at `before_configuration`, `after_configuration`, and
`after_start`. Device reports and raw packets preserve the parsed vendor JSON
without modifying its contents. `{}` is an explicit empty report; no event means
no record. Packet `capture_document()` may still return `None` to omit a document.

Third-party integrations return a JSON object from `device_info`. To emit staged
or during-capture reports, structurally implement
`CaptureEventSource.set_capture_event_sink(sink)` and call the supplied sink:

```python
from dataclasses import asdict
from sifi_streamer.capture import DeviceInfoEvent, DiagnosticEvent

# Inside the integration, after receiving an explicit report:
sink(DeviceInfoEvent("during_capture", asdict(my_report)))
sink(
    DiagnosticEvent(
        "warning",
        "my-device",
        "during_capture",
        "quality",
        "Check sensor placement",
        {},
    )
)
```

Keep dataclasses and payload schemas in the integration. Supply finite JSON
objects; nested objects and lists are supported. Consumer code can similarly call
`controller.record_event(DiagnosticEvent(...))` while the controller is started.
Severity (`info`, `warning`, `error`) describes the event and does not itself stop
recording. Periodic health snapshots and routine Python logs remain separate.

The dashboard shows the requested device selection before startup and reported
SiFi identity afterward. Expand Device report or Recorded launch configuration to
inspect the complete JSON. Reported metadata remains available after stopping.

Schema 3 intentionally breaks the earlier wire format before v1. See
[compatibility.md](compatibility.md) for API changes and migration details.
