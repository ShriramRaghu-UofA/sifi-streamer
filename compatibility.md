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
- EMG defaults explicitly to 1600 Hz and permits 500, 1000, 1600, or 2000 Hz;
- oversized packet writes retain the newest samples in a ring;
- modern annotations and useful package-root exports are retained.

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
It was omitted because serialized state blobs violate the scalar-annotation
contract. Complete device fields remain in raw packet documents, and live
`BackgroundHandle.device_info` remains available for separate metadata.
