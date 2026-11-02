# Use the local capture dashboard

Install the package using the [README](README.md#getting-started). Try a
hardware-free session first:

```powershell
uv run sifi-capture-web demo.capture.duckdb --synthetic
```

Open the printed URL, including its token, and press Start. Use Stop to finalize
the capture; closing the browser tab does not stop recording. Choose a fresh
output filename for each launch. For hardware, [install the bridge](bridge.md)
and follow device and sensor setup in the [capture guide](capture-guide.md).

## Launch options and live views

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

## Reusable annotation kinds

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
[Python API reference](api.md#dashboard-annotation-definitions).

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
