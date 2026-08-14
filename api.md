# Python API reference

The public API is exported from `sifi_streamer`. It separates device-neutral
capture concepts from SiFi-specific acquisition. Application code normally uses
`create_sifi_capture`, `CaptureController`, and `CaptureLogReader`; lower layers
are available for custom launchers and integrations.

## Capture vocabulary and annotations

`Scalar` is `str | int | float | bool | None`. `Attributes` is a mapping from
non-empty string keys to scalar values. Floats must be finite. Lists, nested
mappings, serialized state blobs, and non-string keys are rejected.

`validate_attributes(value)` validates this contract and returns a defensive
`dict` copy. `CaptureDecodeError` identifies malformed schema values;
`CaptureLifecycleError` identifies invalid record ordering or state.

IDs identify occurrences. Kinds identify stable categories. A segment is a
duration with one start and one stop record. A marker is a point fact. Segment
boundaries are authoritative and do not automatically create duplicate markers.

## Capture records

Every record contains these inherited fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Wire schema version; readers currently accept version 2 |
| `sequence` | Zero-based, contiguous record position |
| `host_monotonic_ns` | Host monotonic timestamp at record creation |
| `host_unix_ns` | Host Unix timestamp at record creation |
| `record_type` | Stable record discriminator |

The `CaptureRecord` union contains:

| Type | Additional fields |
| --- | --- |
| `CaptureStarted` | `capture_id`, `attributes` |
| `RawPacket` | complete finite-JSON `packet` object |
| `SegmentStarted` | `segment_id`, `segment_kind`, `attributes` |
| `SegmentStopped` | `segment_id`, optional `reason` |
| `Marker` | `marker_id`, `marker_kind`, `attributes`, optional `source_time_ns` and `source_clock` |
| `CaptureStopped` | `reason` |

`record_to_wire_map(record)` produces the schema-v2 mapping. `encode_record`
validates it and returns newline-terminated UTF-8 JSON bytes. `decode_record`
accepts a parsed JSON object and returns a typed record. Known record types
ignore unknown fields for forward compatibility; unknown record types and schema
versions are rejected.

### `CaptureLogWriter`

`CaptureLogWriter(path, capture_id, attributes=None, ...)` exclusively creates a
new capture and immediately appends `CaptureStarted`. It never opens an existing
file for append or replacement. Packet batches become concatenated Zstandard
frames; capture and segment boundaries flush immediately.

Public operations are:

- `append_packet(packet) -> int`: append the complete finite-JSON packet and
  return its sequence number.
- `start_segment(segment_id, segment_kind, attributes=None) -> int`: start a
  uniquely identified open segment.
- `stop_segment(segment_id, reason=None) -> int`: stop an open segment.
- `append_marker(marker_id, marker_kind, attributes=None, *, source_time_ns=None,
  source_clock=None) -> int`: add a marker in stable ID/kind order.
- `flush(*, boundary=False)`: write buffered records as a compressed frame.
- `close(reason="normal_completion")`: require all segments to be closed, append
  `CaptureStopped`, flush, and close. A repeated successful close is harmless.

The writer is a context manager, but its normal context exit does not infer an
abnormal reason. Lifecycle owners that need reason mapping should use a runner.

### `CaptureLogReader`

Iterating `CaptureLogReader(path)` streams a capture without mutation. It checks
Zstandard decoding, newline termination, finite JSON, schema version, contiguous
sequences, exactly one initial capture start, no records after capture stop, and
valid segment pairing. A capture that ended in a crash may omit `CaptureStopped`
and remains readable up to its last complete valid record.

## Controllers and ownership

`CaptureBackend` is a runtime-checkable structural protocol with `start`, `stop`,
`start_segment`, `stop_segment`, and `marker`. Implementations use composition;
they do not inherit application behavior from the controller.

`CaptureController(backend)` owns generic lifecycle validation:

- `start()` starts once. Failed startup is cleaned up and raised as
  `CaptureInitializationError` with the original cause.
- `marker(marker_id, kind, attributes=None, **extra)` rejects calls before start,
  merges scalar metadata, and rejects duplicate keys.
- `start_segment(segment_id, kind, attributes=None, **extra) -> str` rejects a
  duplicate active ID and returns the ID.
- `stop_segment(segment_id, reason="completed")` requires reverse start order.
- `close(reason="normal_completion")` automatically closes remaining segments in
  reverse order, using `completed` for normal completion and `aborted` otherwise,
  then stops the backend exactly once.

The controller is also a context manager. A clean exit uses
`normal_completion`; an exceptional exit uses `aborted`.

Only the launcher or component that starts a capture closes it. Code receiving
an already-started controller may annotate it but does not close it.

`NoCaptureController` has the same annotation surface for deliberate no-hardware
paths. It validates scalar metadata but performs no I/O and does not track
segments.

## Creating a SiFi capture

`create_sifi_capture(capture_file, capture_id, attributes=None, *,
bridge_executable=..., host="127.0.0.1", port=5000, transport="tcp",
sensor_profile=None, synthetic=False, config=None)` returns an unstarted generic
controller composed with `SiFiCaptureBackend`. No process, device, or file is
created until `start()`.

Hardware capture requires a preinstalled bridge. `sensor_profile` accepts a
complete immutable `SiFiSensorProfile`; omission selects `ALL_SENSORS_PROFILE`.
`EMG_ONLY_PROFILE` and `EMG_IMU_PROFILE` provide reduced enabled sets. Every
sensor option is concrete and is sent on every connection, including settings
for disabled sensors. The final sensor-state command is followed by `info`
validation before acquisition starts.

Preset names refer to ECG, EMG, EDA, IMU, and PPG. Temperature configuration
contains a concrete rate but no enabled field because the bridge does not
expose a temperature enable switch.

PPG configuration contains raw `samples_per_second` and `averaging`; its
effective output rate is their quotient. JSON profiles can be round-tripped
with `load_sensor_profile()` and `write_sensor_profile()`. With
`synthetic=True`, the same background, shared-memory, and recording path runs
without the bridge, and hardware profiles are rejected.

`SiFiCaptureBackend` is the concrete composition adapter. It owns exactly one
entered `BackgroundHandle` and the capture started on that handle, including
cleanup of partially entered handles.

## Live acquisition

### Generic injected devices

`AcquisitionDevice` is the generic structural boundary. After `connect()`, its
ordered `streams` tuple is fixed for the capture. Each `SignalStreamSpec` has a
string ID, `SignalChannelSpec` entries, a positive nominal rate, a native NumPy
dtype, and optional display labels and units.

`AcquisitionPacket` contributes timestamps and channel data to zero or one
declared stream. `capture_document()` independently returns the complete raw
packet to record, or `None` for an adapter-only contribution. This permits an
adapter to split a multi-stream message without duplicating its raw record.

`create_capture_runtime(path, capture_id, device_factory, ...)` composes an
injected device with a controller and `AcquisitionMonitor`.
`create_sifi_capture_runtime(...)` is the SiFi convenience equivalent.

### Modalities and packets

`Modality` identifies EMG, IMU, ECG, EDA, PPG, and temperature packet streams.
`SIGNAL_MODALITIES` contains all enum members. `DEFAULT_MODALITIES` supplies the
default channel layouts and rates.

`ModalitySpec(channels, sample_rate, dtype=numpy.float32)` describes one signal
matrix. Its `n_channels` and `numpy_dtype` properties normalize the layout.

`Modalities[T]` is an immutable fixed-shape collection with optional `emg`, `imu`,
`ecg`, `eda`, `ppg`, and `temperature` values. Use:

- `get(modality)` to return a value or `None`;
- `require(modality)` to return a value or raise `LookupError`;
- `with_value(modality, value)` to create a modified copy;
- `enabled()` to iterate enabled pairs in modality order;
- `from_enabled(values)` to construct a collection from pairs.

`modalities_from_device_info(info)` converts a vendor bridge info document into
enabled `ModalitySpec` values. `packet_from_json_line(line)` parses a bridge JSON
line, returning `None` for malformed/non-object JSON.

`SiFiPacket` exposes typed packet fields and retains the original document.
`modality` maps a known `packet_type` to `Modality`; non-signal packets return
`None`. `capture_document()` returns the original complete JSON object when one
was parsed, preserving unknown device fields.

`SiFiDevice` is the runtime-checkable acquisition protocol. Implementations
provide `connect`, `disconnect`, `read_packet`, `modalities`, and `device_info`.
`DeviceFactory` is a zero-argument callable returning such a device.
`PacketReader` is the narrower bridge transport protocol.

`SiFiBandDevice(host="127.0.0.1", port=5000)` reads newline-delimited packets from
an already-running TCP endpoint and assumes default modality layouts.
`SyntheticSiFiDevice(emg_sample_rate=1600, amplitude=100.0)` generates
deterministic eight-channel sinusoidal EMG for development and tests.

### `SiFiBridgeDevice`

`SiFiBridgeDevice` manages one vendor process. `connect()` checks that the
explicit executable exists, launches it outside the foreground signal group,
configures the EMG rate, reads device info, and starts the chosen transport.
`modalities` is available after connection; `device_info` exposes the complete
info document. `read_packet()` delegates to the selected packet transport.
`disconnect()` sends an orderly stop/exit request, escalates if the process does
not exit, and closes stdin, stdout, and stderr.

### `BackgroundHandle`

`BackgroundHandle(config, device_factory)` is the foreground context manager for
one spawned acquisition worker. Entering waits for readiness and attaches
`SharedMemoryReader` objects. The compatibility `reader` property returns EMG;
`readers` and `modalities` provide per-modality collections. `device_info`
returns optional startup metadata.

The annotation methods `start_capture`, `stop_capture`, `start_segment`,
`stop_segment`, and `add_marker` send typed commands and wait for matching worker
acknowledgements. Attribute mappings are copied before crossing the process
boundary. Capture stop waits without the normal acknowledgement timeout so the
worker can flush safely.

`SharedMemoryReader.read_window(n_samples, *, raise_on_stale=False)` returns the
newest coherent `(time, channels)` NumPy copy in chronological order. It returns
`None` if no fresh coherent snapshot is available, or raises `StaleDataError`
for unchanged data when requested. `n_samples`, `n_channels`, `dtype`, and
`has_new_data` describe the attached ring. `close()` releases only the local
attachment; the worker owns unlinking.

`read_signal_window(n_samples)` and `read_since(cursor, *, max_samples=None)`
return `SignalWindow(start_index, end_index, timestamps, samples, validity,
overrun)`. Validity is independent of payload dtype, so integer and floating
streams represent missing values identically. `BackgroundHandle.streams` and
`stream_readers` expose arbitrary injected streams; existing modality
properties remain SiFi compatibility views.

### Health and web capture

`HealthThresholds` controls rolling rate, staleness, missingness, and loss
warnings. `AcquisitionMonitor.latest()` returns immutable evaluated snapshots;
`events` retains warning/recovery transitions, `fatal()` reports reliable
worker failure, and `read_since()` supplies live signal batches.

`AnnotationKindDefinition` provides separate marker or segment shortcuts with
display metadata, ID prefix/separator/padding/start, and default scalar
attributes. `AnnotationKindRegistry` generates collision-free IDs in Python.

`serve_capture_web(output, runtime_factory, ...)` owns one controller behind a
loopback-only dashboard. Downstream launchers retain dependency injection by
supplying the runtime factory and a read-only scalar configuration summary.

## Runners and interactive input

`run_capture(controller, action)` owns startup and exactly one controlled close.
It maps Ctrl+C to `operator_interrupt`, propagates other exceptions after closing
with `aborted`, and otherwise returns `normal_completion`.

`run_timed_capture`, `run_until_interrupt`, and `run_interactive_capture` build on
that ownership boundary. `interactive_annotations` accepts only:

```text
segment start ID KIND [key=value ...]
segment stop ID [reason]
marker ID KIND [key=value ...]
help
stop
```

`parse_scalar` recognizes booleans, nulls, integers, and floats without expression
evaluation. `parse_attributes` parses unique `key=value` tokens.

## Configuration

`StreamerConfig` is a frozen settings object. It controls ring history,
acknowledgement timeout, capture enablement, compression frame size and interval,
compression level, and optional boundary fsync. Invalid non-positive values and
unsupported compression levels fail during construction.

## Explicit bridge installation

Bridge acquisition never occurs during package installation, import, capture
creation, or device startup. Invoke `sifi-download-bridge` or call
`install_bridge(output_directory, *, latest=False, tag=None, force=False)`.

The default uses maintainer-pinned `TESTED_VERSION` and platform SHA-256 values.
`tagged_asset` resolves a specific release; unpinned tag and latest resolution
require a valid digest in GitHub metadata and fail closed otherwise. Downloads
and extracted executables are size-constrained, only the expected nested
executable is read, and overwrite is refused unless `force=True`.

`BridgeAsset` describes the selected version, archive, URL, digest, and source.
`BridgeInstallManifest` records durable installation provenance.
`BridgeDownloadError` reports resolution, network, verification, extraction, and
installation failures.

## Exceptions

| Exception | Meaning |
| --- | --- |
| `StreamerError` | Base for streamer operational failures |
| `DeviceError` | Device, bridge, socket, or packet transport failure |
| `AckTimeoutError` | Worker acknowledgement did not arrive in time |
| `AckError` | Worker startup or command was explicitly rejected |
| `RecordingError` | Recording command failed or received an unexpected ACK |
| `StaleDataError` | No new shared-memory data was available when required |
| `CaptureInitializationError` | Controller backend startup failed |
| `CaptureError` | Base for capture schema/lifecycle value errors |
| `CaptureDecodeError` | Capture record or stream could not be safely decoded |
| `CaptureLifecycleError` | Capture or segment ordering was invalid |

## Worker protocol messages

Advanced integrations may use the exported frozen command messages
`StartCapture`, `StopCapture`, `StartSegment`, `StopSegment`, `AddMarker`, and
`Shutdown`, represented by `CommandMessage`. Acknowledgements are `Ready`,
`CaptureStarted`, `CaptureStopped`, `SegmentStarted`, `SegmentStopped`,
`MarkerAdded`, and `ErrorAck`, represented by `AckMessage`. `ModalityInfo`
describes one shared-memory stream and converts seconds to sample counts with
`samples_for_seconds`.

These messages are an implementation-level multiprocessing contract. Most
applications should use `BackgroundHandle` or `CaptureController` instead of
placing messages on queues directly.
