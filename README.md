# sifi-streamer

[![CI](https://github.com/ShriramRaghu-UofA/sifi-streamer/actions/workflows/main.yml/badge.svg)](https://github.com/ShriramRaghu-UofA/sifi-streamer/actions/workflows/main.yml)

`sifi-streamer` records SiFi sensor data, displays live signals, and saves samples
and annotations in a self-contained DuckDB database. It provides command-line
capture, a local browser dashboard, and Python APIs for applications that need
to control recording. Custom device adapters can reuse the acquisition and
storage infrastructure.

A **marker** records a point fact; a **segment** records a duration. Both carry
an occurrence ID, a category called a kind, and optional scalar attributes.
The `.capture.duckdb` file is the authoritative recording; CSV and Parquet
exports are derived copies.

## Getting started

You need Python 3.14 or newer, `uv`, and Git access to this private repository.
No hardware or SiFi Bridge is needed for the synthetic examples. Wheels and
Git installations include the dashboard assets; Node.js is needed only for
frontend development.

From a fresh checkout, run these commands in a terminal:

```powershell
git clone https://github.com/ShriramRaghu-UofA/sifi-streamer.git
cd sifi-streamer
uv sync --locked
uv run sifi-capture demo.capture.duckdb --capture-id demo --synthetic --duration 1
uv run sifi-capture-export demo.capture.duckdb demo-tables --table capture_status --format csv
```

The capture command records generated data for one second and prints the output
path and close reason. Open `demo-tables/capture_status.csv` to inspect its final
status. Use a new capture filename and export directory each time; existing
outputs are refused.

To use the library in another uv project, add the private Git dependency:

```powershell
uv add "sifi-streamer @ git+https://github.com/ShriramRaghu-UofA/sifi-streamer.git"
```

Authenticate through Git's credential manager or your configured GitHub
credentials. For reproducible deployments, pin an immutable release tag or
commit using uv's `--tag` or `--rev` option.

## Record and annotate in the browser

```powershell
uv run sifi-capture-web dashboard-demo.capture.duckdb --synthetic
```

Open the printed URL, including its token, and press Start. The dashboard shows
live signals and health, and lets you add markers and segments. Press Stop to
flush and finalize the capture. Closing the browser tab does not stop recording.
See the [dashboard guide](dashboard.md) for reusable annotation kinds, metadata,
health settings, and launch options.

## Capture from SiFi hardware

Install the vendor bridge explicitly after reading its
[license notice and installation guide](bridge.md):

```powershell
uv run sifi-download-bridge --tested
uv run sifi-capture recording.capture.duckdb --capture-id session-001 --sensor-preset emg-imu --duration 30
```

Run both commands from the same working directory so the capture command finds
the bridge in `bin`. You need a SiFi device accessible over BLE. Without an
explicit handle, the bridge selects the first matching device. Use
`--device-handle` or repeatable `--device ID=HANDLE` options to select devices.
The same hardware options work with `sifi-capture-web`.

The default EMG rate is 1000 Hz and the default bridge transport is stdout.
The [capture guide](capture-guide.md) covers device selection, sensor profiles,
multiple devices, TCP/UDP, timed captures, and interactive annotations. Bridge
downloads and updates occur only when explicitly requested.

## Use the Python API

Save this as a Python script and run it with `uv run python YOUR_SCRIPT.py`.
The main guard is required because acquisition uses spawned processes:

```python
from pathlib import Path
from time import sleep

from sifi_streamer.capture import CaptureLogReader
from sifi_streamer.sifi import create_sifi_capture


def main() -> None:
    path = Path("api-demo.capture.duckdb")
    with create_sifi_capture(path, "api-demo", synthetic=True) as capture:
        capture.start_segment("rest-001", "rest", seated=True)
        sleep(1)
        capture.marker("note-001", "operator_note", text="Baseline complete")
        capture.stop_segment("rest-001")

    reader = CaptureLogReader(path)
    print(reader.query("SELECT * FROM capture_status"))
    print(reader.streams())


if __name__ == "__main__":
    main()
```

The context manager starts and closes the capture. Code receiving an already
started controller may annotate it; the component that started it owns closure.
Nested segments stop in reverse start order. Attributes accept strings,
integers, finite floats, booleans, and `None`.

For hardware, omit `synthetic=True` and supply the bridge path and sensor profile
as needed. See the [Python API reference](api.md) for controller lifecycle,
live monitoring, and custom device protocols.

## Inspect recordings and learn more

- [Inspect and export captures](inspecting-captures.md): check outcomes, discover
  channels, query samples and annotations, interpret clocks, and export tables.
- [Python API reference](api.md): composition, device adapters, reader contracts,
  and provenance events. See also the [adapter example](examples/myo_adapter.py).
- [Architecture](architecture.md) and [storage design](persistence-design.md):
  process ownership, data flow, persistence, and shutdown.
- [Compatibility](compatibility.md): current storage format and pre-v1 changes.
- [Contributing](CONTRIBUTING.md): development setup, validation, and packaging.

## License

The package is licensed under the [MIT License](LICENSE). SiFi Bridge is a
separately distributed proprietary binary from SiFi Labs Inc.; its
[vendor notice](bridge.md#vendor-dependency-and-license) applies independently.
