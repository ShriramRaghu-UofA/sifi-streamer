# Architecture

```text
consumer functions
       |
CaptureController -> CaptureBackend protocol -> AcquisitionCaptureBackend
                                                |
                                         BackgroundHandle
                                                |
                 generic worker + shared memory + CaptureLogWriter
                                                |
                                      AcquisitionDevice protocol
                                         /                \
                                  MyoDevice          SiFiBridgeDevice
```

The source tree makes those boundaries explicit: `capture` owns authoritative
records and lifecycle, `acquisition` owns the pluggable device boundary and
process machinery, `sifi` supplies one concrete integration, and `web` supplies
a monitored launcher. Dependencies point from integrations toward the generic
layers; `capture` and `acquisition` never import `sifi` or `web`.

`CaptureController` knows only captures, segments, markers, IDs, kinds,
reasons, and scalar attributes. It validates lifecycle, tracks arbitrary nested
segments, closes them in reverse order, and stops its backend exactly once. It
does not know trials, presentations, participants, stimuli, responses, tasks,
suites, or application artifact layouts. `NoCaptureController` provides the
same generic surface for deliberate no-hardware operation.

`CaptureBackend` is structural. `AcquisitionCaptureBackend` satisfies it by
owning exactly one entered `BackgroundHandle`; it is not a controller subclass.
The handle owns the spawned worker and shared-memory readers. The worker owns
the injected device, ring buffers, and recorder. The recorder serializes
packets and annotations. SiFi composition supplies a `SiFiBridgeDevice` factory
to this same generic backend rather than defining a device-specific subclass.

Live acquisition uses an ordered registry of string stream IDs declared once
after an injected device connects. Each `SignalStreamSpec` fixes its channels,
nominal rate, dtype, and optional display metadata. Each packet contributes to
at most one stream and may independently provide one raw capture document.
Shared-memory rings retain timestamps, native values, explicit validity, and
an absolute cursor.

`Modality`, `Modalities`, and `SiFiPacket` are contained in the `sifi`
integration. Generic acquisition consumes only `AcquisitionDevice`,
`AcquisitionPacket`, and fixed `SignalStreamSpec` declarations. Stream addition
or removal after startup remains out of scope because live layouts are fixed
for a capture.

Managed SiFi startup consumes one complete immutable `SiFiSensorProfile`.
It checks the bridge's physical sensor report and sends every ECG, EMG, EDA,
PPG, IMU, and temperature option for physically present sensors on every
connection—even for disabled sensors—then sends the complete sensor enabled
state last. Each command is acknowledged. The subsequent bridge `info` response
must agree with enabled states and rates both before and after start, before the
fixed stream registry is published. PPG declares
raw `sps` and averaging separately; its stream rate is `sps / avg`.
Fractional nominal rates are retained. TCP subscribers connect before start.
Live metadata requires `info.configuration`; historical capture metadata may
also use the old `info.device` configuration block.

The authoritative artifact is an append-only `*.capture.jsonl.zst`.
`CaptureLogWriter` exclusively creates schema-v3 JSONL in concatenated
Zstandard frames. Raw packet documents retain all JSON fields. Readers accept
unknown fields while validating record version, sequence, capture lifecycle,
segment lifecycle, JSON finiteness, and scalar annotations. A crashed,
unterminated log remains readable; readers never mutate it.

The composed backend prepares capture creation before entering its handle. The
worker exclusively opens the capture and writes `capture_started` followed by
`launch_configuration` before invoking the device factory or connecting hardware.
Connection, configuration, and stream-registry failures produce an error-severity
`diagnostic` and `capture_stopped` with `startup_failure`. The worker remains the
single writer owner; capture creation is never split across processes.

Device reports use `device_info` records with a stage and a complete, untouched
finite JSON object. The optional structural `CaptureEventSource` lets an
integration install a worker-owned event sink before connection. SiFi emits
`before_configuration`, `after_configuration`, and `after_start` reports; other
integrations may use their own stages and emit reports during acquisition. An
explicit `{}` report is recorded, and repeated explicit reports are preserved.
Devices without the event extension publish `device_info` once after connection.
The required property returns a JSON object, including `{}` when no information
is available. Ordinary packet `capture_document()` retains `None` to suppress a
record; `{}` is an actual raw packet document.

`DiagnosticEvent` carries severity, source, stage, code, message, and a JSON
object of details. Severity is descriptive and never automatically changes
lifecycle. Consumers use `CaptureController.record_event()` for non-terminal
errors, warnings, or information. Terminal acquisition failures are also recorded
before orderly shutdown. Automatic Python logging and periodic health snapshots
remain separate from authoritative diagnostics.

The monitor exposes the latest explicit device report, retained after shutdown.
The web API exposes it together with recorded launch settings. An injected
integration formatter supplies a scalar display summary without teaching generic
acquisition or the UI vendor field layouts. The UI shows requested selection
before connection and reported identity afterward, with complete JSON in details.

Optional SiFi table extraction is a derived, non-authoritative boundary.
`sifi_streamer.sifi.export` validates known SiFi packet layouts and exposes
capture, stream, marker, segment, and per-modality pandas tables. Capture
provenance and diagnostics have separate tables with lossless JSON document
columns, preserving every explicit report and its recorded stage. Valid captures
without streams still export their metadata and lifecycle. The generic public
reader remains the vendor-neutral extension point for consumer-owned parsers.
Capture sequence and the recorded clocks are preserved so consumers can join tables
without the package interpreting marker or segment kinds. Parquet datasets are
published only as a convenience representation; cognitive labeling, attempt
selection, supersession, and other application policies remain downstream.

## Ownership rule

> Only the launcher or component that starts a capture closes it. Code
> receiving an already-started controller may annotate it but does not close
> it.

Launchers should use `runner.py`; application functions should receive a
started controller and manage only their own segments.

The local web launcher's Python coordinator owns one controller. Browser tabs
send commands and display immutable state; closing a tab does not stop capture.

## Observability

The worker publishes cumulative health separately from command acknowledgements.
A lossy latest-value queue feeds rate and missingness display, while a reliable
fatal queue reports acquisition or recorder failure. Warnings do not alter
authoritative data. Optional `*.health.jsonl` files are non-authoritative.

## Vendor bridge installation

Bridge installation is deliberately outside capture startup. The user invokes
`sifi-download-bridge`, which selects the host asset, downloads with `urllib`,
verifies SHA-256, extracts only the expected executable, and writes a provenance
manifest. The default release is maintainer-tested; `--latest` is an explicit,
fail-closed opt-in, and `--tag TAG` selects a specific release through the same
verified metadata path. Capture code only consumes an executable path.
