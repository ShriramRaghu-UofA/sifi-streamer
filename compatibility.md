# Compatibility choices

Managed SiFi capture now defaults to the bridge's stdout data transport in the
Python composition helpers, `SiFiBridgeDevice`, and both capture CLIs. Select
`transport="tcp"` or `--transport tcp` explicitly to retain the previous default.
TCP and UDP remain supported; raw `SiFiBandDevice` remains a TCP client.

Both source `sifi_streamer` trees were compared file-by-file, excluding
`__pycache__`. The cognitive-load-validation implementation was selected where
it contained deliberate fixes:

- marker arguments are `marker_id, marker_kind` throughout;
- optional mappings replace mutable defaults and boundaries make copies;
- the foreground owns Ctrl+C, the worker ignores it, and bridge children use a
  new Windows process group or POSIX session;
- bridge stdin, stdout, and stderr streams close during teardown;
- acquisition, command, and socket boundaries handle specific expected errors;
- the complete all-sensors profile defaults SiFiBand EMG to 1600 Hz and PPG to
  200 SPS with averaging 4 for a 50 Hz effective output rate;
- oversized packet writes retain the newest samples in a ring;
- modern annotations are retained under the explicit `capture`, `acquisition`,
  `sifi`, and `web` namespaces.

The capture format remains schema version 2 with unchanged record names and
field meanings. Existing raw packet documents remain intact. Readers accept
unknown fields but reject unknown record types, bad sequences, invalid JSON,
invalid scalar attributes, and invalid lifecycles. Existing files are opened
read-only; new files use exclusive creation.

The cognitive controller's task-specific trial/presentation state, counters,
generated IDs, and automatic cognitive markers are intentionally absent.
Segment records are authoritative, so the generic controller does not emit
duplicate boundary markers.

One source behavior stored bridge `device_info` as a string capture attribute.
That encoding remains omitted because serialized state blobs violate the
scalar-annotation contract. When available, the complete startup device-info
document is instead written as the first schema-v2 `raw_packet`; live
`BackgroundHandle.device_info` also remains available.

Live acquisition accepts generic injected `AcquisitionDevice` implementations
with fixed startup stream registries. SiFi-facing `Modality`, `Modalities`,
`ModalitySpec`, and `SiFiPacket` live under `sifi_streamer.sifi`; generic live
access uses `SignalStreamSpec`, `BackgroundHandle.streams`, and
`stream_readers`. Shared memory is a runtime boundary rather than a persisted
wire format. Python import compatibility with the two former internal copies is
not retained; capture-wire compatibility remains unchanged.

Health sidecars and web annotation-kind definitions do not add records to or
change the meaning of schema-v2 captures.

The optional SiFi table schema is versioned independently from capture schema
v2. It preserves generic annotations and known SiFi samples but does not promise
the column layout of earlier consumer-owned Parquet scripts. Adding or changing
the exporter does not change the authoritative capture wire contract.

The former `emg_sample_rate` factory argument was deliberately replaced by the
complete `sensor_profile` API. The standalone `--emg-sample-rate` option was
replaced by profile selection plus `--emg-fs`. This is an API/CLI break but not
a capture-format change. Sensor profiles use strict versioned JSON and bridge
startup sends every supported setting explicitly for reproducibility.

## Bridge 2.0.1 migration

Live acquisition targets bridge 2.0.1 and requires its `info.configuration` and
physical `info.configuration.sensors` report. There is no legacy live command
dialect. Historical
schema-v2 captures remain readable and exportable: the SiFi metadata parser
accepts both the old `info.device` configuration block and `info.configuration`.
Neither reading nor export rewrites captures or converts their timestamp origin.

Sensor profiles now use JSON version 2. `ImuConfiguration` no longer accepts
`gyroscope_range_dps`; the device's IMU fixes that range. Accelerometer range
must be 8 or 16 g, with a default of 16 g. To migrate a version-1 JSON profile,
remove `imu.gyroscope_range_dps`, explicitly choose an allowed accelerometer
range, and set `version` to 2. Version-1 profiles fail with migration guidance
rather than silently changing requested settings. The web configuration summary
also removes the obsolete gyroscope setting.

Startup checks connect, configure, and start acknowledgements and reports bridge
errors immediately, including JSON prefixed by the bridge's piped REPL prompt.
Requested physically absent sensors are rejected; absent
disabled sensors are not configured. TCP subscribers connect before acquisition
starts so the transport can receive the initial Start Time packet. The complete
post-start info document supplies capture startup metadata, and enabled states
and rates are checked again after start before publishing the fixed registry.

SiFi rates retain fractional values, including low temperature rates and PPG
`sps / avg`. Export keeps measured packet rates per sample without requiring
them to equal the configured nominal rate or each other. With no recorded
configuration, the first available measured rate remains the stream's estimate
(`rate_source = packet`); otherwise the existing default is used. Missing first
packet rates, empty signal packets, and null sample gaps are supported. Invalid
nonpositive rates and non-finite capture JSON remain errors.

Capture schema version 2 and SiFi table schema version 1 are unchanged. The
downloader pins bridge 2.0.1 with GitHub-published hashes for all five platforms;
hardware and firmware validation remains a maintainer responsibility.

BioPoint EMG uses `Modality.EMG_SINGLE` (`emg`) and the `Modalities.emg_single`
slot, with one `emg` channel. SiFiBand retains `Modality.EMG` (`emg_armband`)
and its eight-channel layout. Live registries select the layout from device
metadata; table export recognizes both packet types without changing its schema.

The recorder retains the latest SiFi Start Time document and includes it after
device info when each capture starts, even if acquisition began earlier. These
are ordinary schema-v2 raw packets. Original device fields and receipt times
are preserved; record host timestamps describe insertion into the capture.
Signal samples received before capture startup are not backfilled. Other device
integrations can opt into this behavior through the structural
`CaptureContextPacket.capture_context_key` property; existing packet protocols
remain unchanged.

TCP reads use bounded waits to allow local shutdown on Windows, including when
the bridge sends no further packets. A failed device connection or registry
validation disconnects the partially started device before acknowledging failure.
Profile JSON versions must be integers; floats, strings, and booleans are rejected.

Downloaded bridge executables and generated comparison schemas in `bin`,
`bin-tested`, `schemas`, and `schemas-old` are local inputs and are excluded
from distribution archives.

Runtime duration settings and timed captures reject non-finite values before
acquisition starts. Partial foreground startup releases the worker and attached
readers; interrupted bridge startup also runs teardown. These validation and
ownership fixes leave the public API and schema-v2 capture records unchanged.

Both capture launchers accept optional `--device-handle HANDLE`: a BLE name
such as `BioPoint_AA92`, a MAC address on Windows/Linux, or a BLE UUID on macOS.
Python callers pass `device_handle` to `SiFiBridgeDevice`, `create_sifi_capture`,
or `create_sifi_capture_runtime`. Omission retains first-matching-device
selection. A supplied handle is sent as `connect HANDLE`; bridge errors abort
startup without falling back to automatic selection. Synthetic capture rejects
this option. Capture records are unchanged.
