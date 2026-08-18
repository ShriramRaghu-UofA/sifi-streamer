# Compatibility choices

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
