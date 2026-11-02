# Record from SiFi devices

Install the package using the [README](README.md#getting-started), then
[download SiFi Bridge](bridge.md). You need a SiFi device accessible to the
bridge over BLE. Examples use PowerShell; replace device handles and paths
with your own.

## Choose a capture mode

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

## Select one or more devices

Both capture launchers accept optional`--device-handle HANDLE`: a BLE name
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

## Inspect the result

After stopping, use the [inspection guide](inspecting-captures.md) to check the
outcome, find streams, and export samples. For browser controls and live plots,
use the [dashboard guide](dashboard.md).
