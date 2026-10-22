# Python design review — 2026-10-06

The review focused on assertions, reflective access, validation, and resource
ownership in the Python package. Changes preserve public signatures and capture
schema version 2.

## Fixed

- The acquisition worker accessed required `AcquisitionPacket` properties using
  `getattr` defaults. It now uses the protocol directly, so a broken integration
  cannot silently invent a healthy status or missing health metadata.
- Health thresholds, modality lookup, bridge sensor settings, and CLI profile
  overrides used reflective access to concrete types. They now use explicit
  fields. CLI option detection indexes `vars(args)` because every destination is
  required; no default hides an incorrectly constructed namespace.
- Bridge state assertions became explicit `DeviceError` checks. Physical sensor
  metadata is validated through the existing validator rather than assertions
  about nested dictionaries. Process IDs are accessed directly on `Popen`.
- Web serialization now uses `is_dataclass` and `StrEnum` rather than guessing
  object identity from attribute names.
- `AcquisitionThread._stop` shadowed `threading.Thread._stop`, breaking joins after
  thread exit. The event is now `_stop_event`. Missing required packet attributes
  are reported through the existing acquisition failure channel.
- A failed `BackgroundHandle` entry could leave its worker and already attached
  readers alive. Partial entry and normal exit now share cleanup. A failed ring
  construction also releases the reader's shared-memory attachment.
- Backend startup cleans up on interruption. Stop and exit ownership flags are
  consumed before the operation, preventing repeated stop attempts after failure.
- UDP bridge startup is inside the cleanup boundary. Interrupted bridge startup
  cleans up, failed shutdown writes still close stdin, and a killed process is
  waited for before teardown returns.
- Capture writer construction validates its identifier before exclusive file
  creation and closes its file if initial encoding or writing fails. Rejected
  segment records no longer change the active segment set before append succeeds.
- Duration checks now reject NaN and infinity in configuration, writer settings,
  timed runners, and the capture CLI. Mixed-type profile keys are rejected before
  sorting, so callers receive the intended validation error.

## Deliberately retained

- Three assertions in `sensor_profile_to_dict` verify dictionary shapes created
  internally by `dataclasses.asdict` from a validated `SiFiSensorProfile`. They
  catch implementation mistakes and narrow types. They do not accept or reject
  external input; removing them leaves the same serialization operations. The
  optimized test suite exercises profile serialization and parsing.
- Test assertions narrow values after explicit test expectations. They are not
  package runtime validation. `unittest` assertions remain active under `-O`.
- `Modalities.with_value` still uses `dataclasses.replace` with an enum-derived
  field name. This is an immutable update operation, not optional lookup or a
  fallback. `_modality_field` is confined to that update and translates the two
  EMG identifiers into their fixed dataclass slots.
- `argparse.Namespace` remains at the CLI boundary. Replacing it with a new
  argument model would require coordinated changes across launchers without
  improving the now-explicit profile translation.
- `Any` remains confined to web JSON translation and serialization. Parsed JSON
  is an integration boundary; this review does not introduce a new recursive JSON
  model or redesign the HTTP API. Existing domain validators still apply.
- Structural runtime checks for `CaptureContextPacket` remain: this extension is
  genuinely optional for injected devices and preserves the base packet API.
- Broad exception catches remain at cleanup boundaries that re-raise, and at the
  web monitor boundary that records a failed state. Expected queue races and
  already-closed transport errors remain narrowly suppressed during teardown.
  No retry or compatibility mechanism was added.
- No controller hierarchy, device registry, transport redesign, or capture schema
  change was introduced. Hardware operation was not exercised during this review.

## Verification

Regression tests cover partial attachment, startup timeout, reader construction,
thread joining, exactly-once stop after failure, invalid writer initialization,
rejected segment append, non-finite durations, and diagnostic profile rejection.
The complete suite passes both normally and with `python -O`. Ruff lint and
format checks, `ty check`, `npm ci`, frontend checking/build, `git diff --check`,
and `uv build` pass. A clean temporary Python 3.14.7 environment imports the
installed wheel, runs `sifi-capture --help`, writes and reads a synthetic capture,
and decodes representative schema-v2 wire records without changing file bytes.

Both consumer repositories are present, but neither contains usable legacy
capture reader Python source. The remaining old package directory contains
cache/background/client remnants. Independent old-reader/new-writer execution
could therefore not be performed. Existing representative-wire compatibility
tests pass; consumer repositories were not modified.
