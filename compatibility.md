# Pre-v1 API and capture format changes

SiFi derived table schema version 2 adds `launch_configuration`, `device_info`,
and `diagnostics` DataFrames to `SiFiCaptureTables` and corresponding Parquet
files. Direct construction of this value now requires the three additional
fields. Nested metadata documents use lossless JSON string columns. Valid
captures without SiFi streams now export with empty stream/signal views instead
of failing. The authoritative capture schema remains version 3. The public
device-neutral reader continues to expose complete vendor payloads for custom
third-party parsers without optional table dependencies.

Capture schema version 3 adds `launch_configuration`, `device_info`, and
`diagnostic` to the existing six record types. This is an intentional breaking
change: schema-v2 captures and older readers are not supported by the current
reader/writer. Existing artifacts are never rewritten. Use an earlier package
version to inspect earlier captures.

The record envelope retains sequence numbers and host monotonic/unix timestamps.
Raw packets and metadata payloads retain their complete finite JSON documents.
Marker/segment attributes remain strictly scalar. `launch_configuration`, when
present, must be the second record and appears once. Composed capture writes it
before device startup; the low-level writer can still be used without launcher
configuration.

The backend now starts recording before device connection. Failed attempts can
therefore leave a valid capture ending in `startup_failure`; output paths must
remain new and are never overwritten. The worker owns the writer throughout.
Observed terminal failures produce diagnostics, but abrupt process termination
or unavailable storage cannot guarantee a terminal diagnostic or stop record.

`AcquisitionDevice.device_info` returns `dict[str, object]`, including `{}`.
The optional `CaptureEventSource.set_capture_event_sink()` receives a callback
before `connect()`. Integrations emit `DeviceInfoEvent` and `DiagnosticEvent`
for explicit occurrences. Stages are integration-owned non-empty strings.
Payloads must be finite JSON objects: nested objects/lists are supported, while
non-string object keys, tuples, and non-JSON values are rejected. Dataclasses are
integration-owned and must be serialized by the integration before submission.

`CaptureBackend` now includes `record_event()`. `CaptureController` validates
and copies events, rejects them before startup/after close, and does not close
capture based on severity. `NoCaptureController` validates without recording.

Web runtime factories now accept `(capture_id, attributes, launch_configuration)`.
Pass the configuration into `create_capture_runtime()` or
`create_sifi_capture_runtime()`. The coordinator includes resolved requested
settings, health rules, health-log preference, and annotation definitions.
The API's bootstrap/live responses expose `launch_configuration`, `device_info`,
and `device_summary`. Optional `device_info_formatter` extracts presentation
fields; it never changes the recorded report.

SiFi connection reports are recorded before configuration, after configuration,
and after start. Export uses the configured reports for nominal stream layout,
ignoring the pre-configuration report. SiFi bridge 2.0.1, sensor-profile version 2,
explicit EMG rates, and explicit bridge-download ownership remain unchanged.
