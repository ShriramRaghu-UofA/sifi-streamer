# sifi-streamer

[![CI](https://github.com/ShriramRaghu-UofA/sifi-streamer/actions/workflows/main.yml/badge.svg)](https://github.com/ShriramRaghu-UofA/sifi-streamer/actions/workflows/main.yml)

Pluggable Python 3.14+ acquisition and capture infrastructure with a bundled
SiFi integration. Inject any device that satisfies the structural acquisition
protocols to reuse the background worker, shared-memory live signals, health
monitoring, generic annotations, and authoritative DuckDB capture databases.
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
    Path("session.capture.duckdb"),
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

for record in CaptureLogReader(Path("session.capture.duckdb")):
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


from functools import partial
from examples.myo_adapter import MyoDevice

# Your SDK adapter implements the small MyoTransport protocol in the example.
# MyoDevice itself needs no inheritance or registration.
# runtime = create_capture_runtime(
#     Path("myo.capture.duckdb"), "capture-001",
#     {"left": partial(MyoDevice, YourMyoTransport),
#      "right": partial(MyoDevice, YourOtherMyoTransport)},
# )
```

See [examples/myo_adapter.py](examples/myo_adapter.py) for the complete SDK-neutral
packet/device implementation. It records samples directly, without requiring a
raw document. It uses adapter receipt `time.perf_counter_ns()` as its declared
source clock; that is an adapter observation time, not a hardware measurement
clock. Each packet supplies timestamps, channel values, explicit validity, and
health metadata. The integration's thread-safe `request_stop()` unblocks pending
reads before final `disconnect()`. Factories must be top-level/picklable.

All device layouts register before acquisition starts. Stream identity is
`(device_id, stream_id)`, and live monitoring exposes device-scoped keys. Dynamic
stream registration and silent padding/truncation are unsupported. Any unhandled
device, transport, or recorder error stops the entire capture.

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
uv run sifi-capture recording.capture.duckdb --capture-id session-001 `
  --bridge-executable C:\tools\sifibridge.exe --sensor-preset emg-imu
```

Record for five minutes over TCP, or enter annotations interactively over UDP:

```powershell
uv run sifi-capture timed.capture.duckdb --capture-id baseline `
  --duration 300 --transport tcp --host 127.0.0.1 --port 5000
uv run sifi-capture notes.capture.duckdb --capture-id annotated `
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
sifi-capture recording.capture.duckdb --capture-id session-001 `
  --sensor-profile sensors.json
```

The default EMG rate is 1000 Hz. This avoids the 1600 Hz firmware issue reported
by the manufacturer. Use `--emg-fs 2000` for the other safe rate confirmed by the
manufacturer; 1600 Hz remains available only through explicit configuration.

Frequent state and rate settings can be overridden directly with `--ecg on`,
`--emg-fs 1000`, `--ppg-sps 200`, and `--ppg-avg 4`. PPG has no `fs` setting:
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
uv run sifi-capture-web monitored.capture.duckdb --capture-id session-001 `
  --bridge-executable C:\tools\sifibridge.exe --sensor-preset emg-imu
```

Start web capture with **TCP** on port 5000 and the dashboard on port 8080:

```powershell
uv run sifi-capture-web tcp.capture.duckdb --capture-id tcp-session `
  --transport tcp --host 127.0.0.1 --port 5000 --web-port 8080 `
  --bridge-executable C:\tools\sifibridge.exe --sensor-preset emg-imu
```

Use UDP, a saved profile, scalar session metadata, and a reusable kinds file:

```powershell
uv run sifi-capture-web udp.capture.duckdb --capture-id udp-session `
  --transport udp --port 5000 --sensor-profile sensors.json `
  --attribute operator=Shriram --attribute session=1 --kinds-file kinds.json `
  --no-open --web-port 8080
```

`--web-port` selects the dashboard HTTP port; its default `0` asks the OS to
assign a free port. It is separate from the bridge's `--port`. Open the printed
URL, including its token, and use the page's start/stop controls to record.
`--no-open` suppresses automatic browser opening. For a hardware-free dashboard,
use `uv run sifi-capture-web demo.capture.duckdb --synthetic`; omit hardware
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

### Reusable annotation kinds

Save the following as `kinds.json` (UTF-8), then pass it to
`sifi-capture-web` with `--kinds-file kinds.json`:

```json
[
  {
    "target": "marker",
    "kind": "Note",
    "label": "Operator note",
    "color": "#38BDF8",
    "default_attributes": {"source": "operator"}
  },
  {
    "target": "segment",
    "kind": "Rest",
    "label": "Rest interval",
    "color": "#34D399",
    "id_prefix": "rest",
    "separator": "-",
    "padding": 3,
    "start": 1,
    "default_attributes": {"condition": "rest", "seated": true}
  }
]
```

For a hardware-free example:

```powershell
uv run sifi-capture-web demo.capture.duckdb --synthetic --kinds-file kinds.json
```

The top level is an array, with no wrapper object or schema-version field.
Only `target` and `kind` are required for each entry; for example,
`{"target": "marker", "kind": "Note"}` is a complete definition.

| Field | Meaning and default |
| --- | --- |
| `target` | Required: `"marker"` for a point annotation or `"segment"` for a duration. |
| `kind` | Required: non-empty category name with no leading or trailing whitespace. |
| `label` | Optional display label; defaults to `null`. Does not change the recorded kind. |
| `color` | Optional `"#RRGGBB"` display color; defaults to `null`. |
| `id_prefix` | Generated ID prefix; omitted or `null` uses `kind`. An explicit prefix must be non-empty. |
| `separator` | Text between prefix and counter; defaults to `"_"`, at most four characters (empty is allowed). |
| `padding` | Minimum counter width, from 1 to 9; defaults to `2`. |
| `start` | Non-negative starting counter; defaults to `1`. |
| `default_attributes` | Scalar metadata copied onto each annotation; defaults to `{}`. |

This example generates marker IDs `Note_01`, `Note_02`, etc. and segment IDs
`rest-001`, `rest-002`, etc. Marker and segment IDs are tracked independently;
generated IDs skip already-used IDs. Definitions are keyed by `(target, kind)`;
a later definition with the same pair replaces the earlier one.

Default attributes may contain strings, finite numbers, booleans, or `null`;
nested objects and arrays are rejected. Attributes supplied for an individual
annotation override matching defaults. Labels and colors are presentation
settings; annotations record the kind, occurrence ID, and merged attributes.
Dashboard edits apply to the current launch and do not rewrite `kinds.json`.

Python launchers supply `definitions=` rather than a kinds-file path; see the
[Python API example](api.md#reusable-annotation-kinds).

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
sifi-capture synthetic.capture.duckdb --capture-id dev --synthetic --duration 1
```

For live access, enter a `BackgroundHandle`, inspect `handle.streams`, and read
the corresponding entries in `handle.stream_readers`. Gap-aware incremental
reads return source timestamps, native values, and an explicit validity mask.
`SyntheticSiFiDevice` uses the same generic worker and shared-memory path as
hardware and custom injected devices.

The `.capture.duckdb` database is authoritative. One recorder process owns all
writes; each named device has an independent worker and bounded producer buffer.
Events carry producer-local and global recording sequences plus host receipt
monotonic/Unix nanosecond clocks. Typed sample tables preserve source timestamps,
channel values, and explicit validity. Complete raw documents and dynamic reports
use VARIANT. Missing raw documents do not suppress typed samples.

The recorder commits independently of incoming traffic, normally every second.
Acknowledgements mean accepted, not durable. Shutdown finishes device producers
before draining the recorder and closing open segments. Any unhandled failure
fails the whole capture. Preserve a crashed database together with its `.wal`
file; explicit `recover_capture(source, destination)` creates a recovered copy
without rewriting the original. No legacy JSONL reader or migration exists.

## SQL inspection and export

```python
from pathlib import Path
from sifi_streamer.capture import CaptureLogReader

reader = CaptureLogReader(Path("session.capture.duckdb"))
print(reader.query("SELECT marker_kind, count(*) FROM markers GROUP BY marker_kind"))
print(reader.streams())  # discover typed sample table names and source clocks
reader.export(Path("session-tables"), tables=("events", "markers", "segments"))
```

```powershell
sifi-capture-export session.capture.duckdb session-tables
sifi-capture-export session.capture.duckdb selected --table markers --table segments
sifi-capture-export session.capture.duckdb csv-tables --format csv
```

DuckDB provides Parquet/CSV export directly; pandas/PyArrow are not required.
Dynamic columns export as JSON text by default. `--native-variant` explicitly
requests native VARIANT in Parquet. Export directories must be new. Exports are
derived artifacts and never alter the database. SQL clients can also query the
self-describing database directly without importing this package.
See [examples/inspect_capture.sql](examples/inspect_capture.sql) for catalog,
event, annotation, and segment queries.

### Finding your way around a capture

Start with the `.capture.duckdb` file. It contains the entire recording, including
device settings, signal layouts, samples, annotations, and lifecycle events.
An optional `.health.jsonl` file contains monitoring diagnostics, not authoritative
samples. Export directories such as `session-tables/` and `csv-tables/` are derived
copies: by default, each exported table or view gets a file with the same name,
such as `events.parquet` or `events.csv`. A selected-table export may omit files.

For a first inspection, look at these tables/files in this order:

| Table / exported filename stem | What it tells you |
| --- | --- |
| `capture_status` | Whether capture completed, failed, or is incomplete, and its stop reason. |
| `capture` | Capture ID, format/package versions, attributes, and host clock description. |
| `streams` | Which device owns each stream, its nominal rate, source clock/unit, and sample table name. |
| `channels` | Ordered channel names and their physical value/validity column names, labels, and units when supplied. |
| `samples_0000`, `samples_0001`, … | Actual measurements, one row per sample in a particular stream. Discover their meaning through `streams`; the numeric suffix has no intrinsic meaning. |
| `events` | Global recording order, producer-local sequence, and host timestamps for every recorded event. |
| `packets` | Device/stream identity, reported rate/loss/status, and optional original document for each packet. |
| `capture_started`, `capture_stopped` | Lifecycle event IDs and stop outcome/reason; join to `events` for timestamps. |
| `markers`, `segments` | Point annotations and duration annotations. The `segments` view already includes boundary timestamps. |
| `launch_configuration`, `device_reports`, `diagnostics` | Requested settings, bridge reports at startup stages, and recorded problems. Join to `events` for their times. |
| `devices`, `producers` | Device registration and the identities of components submitting events. |

The `segment_starts` and `segment_stops` tables contain the underlying segment
events. Most analysis can use the joined `segments` view instead. In the database,
`record_bodies` is a convenience view used by the package reader; it is not an
additional sample stream.

Open the completed database in a DuckDB-compatible SQL client. Alternatively,
run each query below with `reader.query(...)` using the Python example above.

**1. Check the outcome and capture boundaries.**

```sql
SELECT * FROM capture_status;

SELECT event_sequence, record_type,
       make_timestamp_ns(received_unix_ns) AS time_utc,
       received_monotonic_ns
FROM events
WHERE record_type IN ('capture_started', 'capture_stopped')
ORDER BY event_sequence;
```

Timestamps are stored once in `events`, rather than repeated in each event's body
table. For example, `capture_started.csv` contains only an `event_sequence`;
find that same sequence in `events.csv` to obtain its time. Capture start includes
hardware startup, and capture stop follows producer shutdown and draining. These
boundaries are not the first and last measurement times. `incomplete` means no
terminal event was recorded; a terminal event can also report failure.

**2. Find a device's stream and channels.**

```sql
SELECT s.device_id, s.stream_id, s.table_name, s.nominal_rate_hz,
       s.source_clock, s.timestamp_unit,
       c.channel_id, c.column_name, c.validity_column, c.unit
FROM streams s
JOIN channels c USING (device_id, stream_id)
ORDER BY s.device_id, s.stream_id, c.channel_index;
```

For a recording made with `--device left=... --device right=...`, `device_id`
contains `left` or `right`. A SiFiBand might have stream `emg_armband` with
channels `emg0` through `emg7`, while a BioPoint has stream `emg` with one channel.
Use the catalog to discover the actual layout. Both devices can have channels
named `emg`; the device and stream IDs disambiguate them.

**3. Read measurements with packet arrival times.** After the previous query
identifies a sample table and channel columns, substitute them in this example:

```sql
SELECT p.device_id, p.stream_id, t.event_sequence, t.sample_index,
       t.source_time, t.value_0, t.valid_0,
       e.received_monotonic_ns,
       make_timestamp_ns(e.received_unix_ns) AS packet_arrival_utc
FROM samples_0000 t
JOIN packets p USING (event_sequence)
JOIN events e USING (event_sequence)
ORDER BY t.event_sequence, t.sample_index
LIMIT 20;
```

`value_0` is the channel mapped to that column in `channels`, not necessarily a
channel called "0". Its corresponding `valid_0` explicitly identifies validity:
a null value can be marked valid, so nullness alone does not establish invalidity.
One packet may batch many samples. Those rows share `event_sequence` and packet
arrival time; `sample_index` orders samples within that packet.

`source_time` is the producer's measurement timestamp, interpreted using the
stream's `source_clock` and `timestamp_unit`. It is not necessarily Unix time.
Two devices' source clocks are not automatically synchronized. Host Unix time
provides calendar time; host monotonic nanoseconds provide elapsed-time comparisons
within the same host boot. Neither packet arrival time nor recorder order proves
simultaneous measurement across devices.

**4. Count events, packets, and samples separately.**

```sql
SELECT record_type, count(*) AS event_count
FROM events GROUP BY record_type ORDER BY record_type;

SELECT device_id, stream_id, count(*) AS packet_count,
       sum(samples_lost) AS producer_reported_lost_samples
FROM packets GROUP BY device_id, stream_id ORDER BY device_id, stream_id;

SELECT count(*) AS sample_count FROM samples_0000;
```

Packet counts are not sample counts. Some packets are status or metadata reports
with no stream or samples. `samples_lost` is the producer's report, not an
independent completeness guarantee. `event_sequence` orders recorder acceptance;
`producer_id` plus `producer_sequence` identifies each producer's local order.
Use explicit `ORDER BY` when ordering matters.

**5. Place annotations on the timeline.**

```sql
SELECT m.marker_id, m.marker_kind, m.attributes,
       make_timestamp_ns(e.received_unix_ns) AS time_utc
FROM markers m JOIN events e USING (event_sequence)
ORDER BY m.event_sequence;

SELECT segment_id, segment_kind, start_sequence, stop_sequence,
       start_monotonic_ns, stop_monotonic_ns, reason
FROM segments ORDER BY start_sequence;
```

Segment boundaries describe annotation timing; use host arrival times or recording
order to select received packets within them. Mapping boundaries into a device's
measurement clock requires a clock relationship supplied by the producer/user.

The same joins work on exported files once loaded into a SQL tool. For example,
in a new DuckDB connection, load the event export into a temporary view:

```sql
CREATE TEMP VIEW events AS
SELECT * FROM read_parquet('session-tables/events.parquet');
```

Use `read_csv('csv-tables/events.csv', header = true)` for CSV, and load other
tables in the same way. This `CREATE VIEW` example requires a writable scratch
connection, rather than the SELECT-only `reader.query` helper. Parquet preserves
column types; when importing CSV into spreadsheets, keep nanosecond timestamps
and large sequence values as exact integers or text to avoid rounding. Dynamic
payload columns are JSON text in ordinary exports; typed sample columns can be
analyzed directly without parsing those payloads.

See the [Python API reference](api.md), [architecture.md](architecture.md),
[compatibility.md](compatibility.md), and [contribution guide](CONTRIBUTING.md).

Both capture launchers accept optional `--device-handle HANDLE`: a BLE name
such as `BioPoint_AA92`, a MAC address on Windows/Linux, or a BLE UUID on macOS.
Python callers pass `device_handle` to `SiFiBridgeDevice`, `create_sifi_capture`,
or `create_sifi_capture_runtime`. Omission retains first-matching-device
selection. A supplied handle is sent as `connect HANDLE`; bridge errors abort
startup without falling back to automatic selection. Synthetic capture rejects
this option.

For explicit device names, both `sifi-capture` and `sifi-capture-web` accept
repeatable `--device ID=HANDLE` options. Use one option for a single band, or two
for simultaneous recording into the same database:

```powershell
uv run sifi-capture one.capture.duckdb --capture-id one --device left=BioPoint_AA92 --duration 30
uv run sifi-capture two.capture.duckdb --capture-id two --device left=BioPoint_AA92 --device right=BioPoint_BB93 --duration 30
uv run sifi-capture-web two-live.capture.duckdb --device left=BioPoint_AA92 --device right=BioPoint_BB93
```

Replace the example handles with your devices' BLE names, MAC addresses, or UUIDs.
Each band gets its own bridge process and streams such as `left/emg` and
`right/emg`. The bridge executable, transport, and sensor profile apply to all
devices. Stdout is the default transport and needs no separate ports. TCP/UDP
assign consecutive ports starting at `--port`, in option order. Device IDs and
handles must be unique. `--device` cannot be combined with `--device-handle` or
`--synthetic`. Any unhandled device failure stops the entire capture.

The dashboard shows a connection card for each device ID, including its requested
handle before startup and its reported identity after connection. Each card has
its own expandable device report. Requested handles and reports remain visible
after stopping. The sensor settings summary is shared; the available signal
streams are discovered separately for each connected device.

Python callers can pass `device_handles={"left": "BioPoint_AA92", "right":
"BioPoint_BB93"}` to either SiFi composition function. Device declarations and
per-device bridge settings are saved in the database launch configuration.

## Capture provenance and diagnostics

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
