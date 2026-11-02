# Python API reference

The package targets Python 3.14+. DuckDB is a runtime dependency. The capture
format is a versioned DuckDB database with no compressed-JSONL compatibility.

## Composition and lifecycle

`CaptureController` composes a structural `CaptureBackend`; application functions
receive a started controller and annotate it. The launcher that starts a capture
is responsible for closing it. `NoCaptureController` supports deliberate
no-hardware paths. Marker argument order is `marker_id, marker_kind`.

`create_capture_runtime(path, capture_id, device_factory_or_mapping, attributes,
config=..., thresholds=..., launch_configuration=...)` returns a controller and
read-only monitor. Pass a picklable zero-argument factory or a mapping of explicit
device IDs to factories. IDs are scoped to the capture; device IDs may not contain
`/`. Each local stream is addressed live by its device-scoped key.

Controllers expose `start()`, `marker(id, kind, attributes=None, **extra)`,
`start_segment(id, kind, attributes=None, **extra)`, `stop_segment(id, reason)`,
`record_event(event)`, and `close(reason)`. Reject annotations before startup or
after close. Segments nest and close in reverse order. Automatic closure uses
`completed` for normal completion and `aborted` otherwise. `close()` first
quiesces device producers, then closes remaining segments and finalizes storage.
Backend stop is attempted exactly once. Any unhandled acquisition, transport, or
persistence error fails the whole capture.

## Device and packet protocols

Implement by structural shape, without inheritance or registration:

- `connect()` initializes the device and its fixed stream registry.
- `streams` returns an ordered tuple of `SignalStreamSpec`.
- `device_info` returns a complete finite JSON object, including `{}`.
- `read_packet()` returns an `AcquisitionPacket`.
- `request_stop()` thread-safely unblocks an outstanding read.
- `disconnect()` finishes cleanup after polling has stopped.

`SignalChannelSpec(channel_id, label=None, unit=None)` describes one column.
`SignalStreamSpec(stream_id, channels, nominal_rate_hz, dtype, label=None,
source_clock='producer-defined', timestamp_unit='seconds', timestamp_dtype=...)`
fixes layout and describes producer timing. Supported sample dtypes are boolean,
integer, and floating numeric scalars up to 64 bits. Timestamps use signed integer
or floating dtypes. The source-clock description is opaque text; no clock mapping
is inferred. See [examples/myo_adapter.py](examples/myo_adapter.py).

Packets supply `stream_id` (or None), `timestamps`, channel `data`, channel
`validity`, `reported_rate_hz`, `samples_lost`, `status`, and
`capture_document()` returning finite JSON or None. Each packet contributes to at
most one declared stream. Arrays have equal lengths and exactly the declared
channels. Explicit booleans express per-channel validity independently of nulls.
A valid null is distinct from an invalid null. A packet with samples and no raw
document is still recorded. No dynamic stream discovery or implicit repair occurs.

`DeviceInfoEvent(stage, info)` and `DiagnosticEvent(severity, source, stage, code,
message, details)` preserve explicit reports and diagnostics. Optional structural
`CaptureEventSource.set_capture_event_sink(sink)` receives the sink before
connection. Diagnostic severity describes data; integrations must propagate
unhandled operational failures rather than merely logging them.

## Storage and SQL

`CaptureLogReader(path)` opens only current-schema DuckDB databases. Iteration
returns validated typed record values. `RawPacket.packet` may be None for a
sample-only packet. Raw packet documents and device reports retain complete
finite JSON. Attributes are scalar strings, integers, finite floats, booleans,
or null; nested attributes and non-string keys are rejected.

Reader methods:

- `query(sql, parameters=())`: one SELECT query, returning row tuples.
- `tables()`: discover table and public view names.
- `streams()`: discover stream declarations and physical sample table names.
- `export(destination, format='parquet', native_variant=False, tables=None)`:
  export selected tables/views into a new directory, returning table/path pairs.

Core tables: `capture`, `producers`, `devices`, `streams`, `channels`, `events`,
`packets`, `markers`, `segment_starts`, `segment_stops`, `device_reports`,
`diagnostics`, `launch_configuration`, and capture lifecycle tables. Public views
`segments` and `capture_status` derive intervals and terminal state.

`events` stores `event_sequence`, `producer_id`, `producer_sequence`, `record_type`,
`received_monotonic_ns`, and `received_unix_ns`. Receipt clocks are assigned before
queueing; global sequence describes recorder acceptance, not physical measurement
order. Host clocks share a scope on one machine/run, not across machines/reboots.

`streams.table_name` locates the sample table. `channels` maps original channel IDs
to physical value and validity column names and preserves their order. Sample
rows contain `event_sequence`, `sample_index`, `source_time`, and channel columns.
One packet event can contribute many rows. Join to `events` for packet receipt
time. Source timestamps preserve their declared representation; live rings may
use floating timestamps for display only.

`packets` also retains `reported_rate_hz`, `samples_lost`, and `status` as typed
columns, including when no raw document is supplied. These fields are producer
reports; status strings do not replace explicit exception/failure propagation.

Events and related rows are committed atomically. Ordinary reads never silently
repair a database. Preserve the database plus WAL after a crash and call
`recover_capture(source, destination)` for an explicitly recovered new copy. A
missing committed capture stop means incomplete, not inferred success.

`CaptureLogWriter` is the low-level single-owner writer. The normal application
path uses the recorder process, not direct concurrent writer access. Device/stream
registration precedes sample submissions. `flush()` commits pending data;
`close(reason)` finishes open segments and terminal state. `abort()` discards the
open transaction. Existing paths are refused.

## Recorder policies and monitoring

`StreamerConfig` defines live history and finite recorder limits. Defaults include
one-second commits, an 8 MiB transaction target, 32 MiB producer budget, 8 MiB
maximum submission, and two-second enqueue deadline. Byte budgets include feeder
backlog. Timed commits work when acquisition is idle. ACKs promise acceptance,
not durability; stalled storage can violate the normal commit target and must
become an explicit failure. There are no silent drops or automatic retries.

The runtime monitor exposes stream declarations, gap-aware ring reads, cumulative
health, explicit device reports, recorder progress, and reliable terminal failure.
The backend supervises failures independently of consumers polling health. Live
rings are recent display buffers; recording/export never reconstruct samples from
those rings. Unknown source timestamp units disable source-rate estimation.

## SiFi and launchers

`create_sifi_capture` and `create_sifi_capture_runtime` accept an optional
`device_handles: Mapping[str, str]` mapping stable device IDs to explicit BLE
handles. It is mutually exclusive with `device_handle` and synthetic acquisition.
Each device has an independent worker and bridge. Sensor settings are shared;
TCP/UDP ports increment from the supplied base port in mapping order. The CLIs
expose this as repeatable `--device ID=HANDLE` options.

`create_sifi_capture` and `create_sifi_capture_runtime` compose the same generic
path with `SiFiBridgeDevice` or `SyntheticSiFiDevice`. Pass a bridge executable,
transport, optional device handle, and complete sensor profile. The managed bridge
is never downloaded implicitly. `sifi-download-bridge` is the explicit verified
download operation; TCP, UDP, and stdout transports remain supported.

`sifi-capture` provides timed, interrupt-driven, and interactive annotation modes.
`sifi-capture-web` adds the local dashboard; closing a browser tab does not stop
recording. Built assets ship in the wheel. `sifi-capture-export SOURCE OUTPUT`
exports generic tables with `--format parquet|csv`, repeatable `--table`, and
optional `--native-variant`. All exports are derived and refuse existing outputs.

## Integrate custom hardware

Custom hardware implements the structural `AcquisitionDevice` and
`AcquisitionPacket` protocols by shape; explicit inheritance or registration is
not required. A connected device declares one fixed ordered stream registry and
each packet contributes samples to at most one declared stream while optionally
providing its complete raw document for capture.

Use `create_capture_runtime` with a zero-argument device factory or a mapping
of device IDs to factories. The [adapter example](examples/myo_adapter.py)
defines the transport interface your SDK wrapper must implement.


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

## Dashboard annotation definitions

The CLI loads definitions from `--kinds-file`. Python callers pass a sequence
of `AnnotationKindDefinition` values through `definitions=` to `serve_capture_web`.
Import both from `sifi_streamer.web`.
See [reusable annotation kinds](dashboard.md#reusable-annotation-kinds) for the
JSON format, defaults, ID generation, and merge rules.
