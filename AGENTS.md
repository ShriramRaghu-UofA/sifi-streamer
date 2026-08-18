# Contributor and agent guidance

This guidance applies to every human and coding agent working in this
repository. Read [architecture.md](architecture.md) and
[compatibility.md](compatibility.md) before changing public APIs, capture
records, acquisition lifecycle, or process ownership.

## Scope

This repository is the private reusable `sifi-streamer` distribution. It owns:

- SiFi device and managed bridge integration;
- pluggable background acquisition and generic stream-shaped shared memory;
- authoritative append-only capture logging;
- generic segments and markers;
- the composition-oriented `CaptureController` and `CaptureBackend` protocol;
- the generic `AcquisitionCaptureBackend`;
- reusable capture runners and interactive annotation parsing;
- the standalone `sifi-capture` command;
- the synthetic SiFi device.

It must not depend on either consumer repository or on `cognitive_platform`.
Do not modify consumer repositories as part of work here unless a task
explicitly expands the scope.

## Architecture invariants

Use composition, not inheritance, for application behavior:

```text
CaptureController -> CaptureBackend protocol -> AcquisitionCaptureBackend
                                              -> BackgroundHandle
```

- Keep one generic `CaptureController` composed with a narrow structural
  `CaptureBackend`.
- Do not add device-specific controller subclasses, task controller
  subclasses, or inheritance hierarchies for application concepts.
- `AcquisitionCaptureBackend` owns one entered `BackgroundHandle` and its
  capture. It does not inherit from `CaptureController`.
- Device integrations satisfy structural `AcquisitionDevice` and
  `AcquisitionPacket` protocols without inheritance or registration. Keep
  composition and dependency injection as the extension mechanism.
- The capture log, annotations, controller, and runners should remain
  device-neutral where practical.
- Live acquisition accepts injected `AcquisitionDevice` implementations with a
  fixed startup registry of string-keyed `SignalStreamSpec` values. Each packet
  contributes to at most one declared stream. Do not support streams appearing
  or disappearing after startup without another explicitly approved change.
- Keep `Modality`, `Modalities`, `SiFiPacket`, and bridge transports contained
  in `sifi_streamer.sifi`. The generic acquisition layer must not import the
  SiFi or web namespaces. Do not add a meta-package or device registry.

## Generic capture vocabulary

Package behavior understands only:

- a capture/session;
- raw packets;
- segments representing durations;
- markers representing point facts;
- IDs, kinds, reasons, timestamps, and scalar attributes.

It must not understand cognitive tasks, trials, presentations, stimuli,
responses, participants, suites, task metrics, or project-specific artifacts.
Consumers may use those words as their own segment or marker kinds, but they do
not belong in package control flow or APIs.

An ID identifies one occurrence. A kind identifies its stable category.

```python
type Scalar = str | int | float | bool | None
type Attributes = Mapping[str, Scalar]
```

Reject nested mappings, lists, state blobs serialized into JSON strings,
non-finite floats, non-string keys, and other invalid annotation values at the
appropriate boundary. Make defensive copies of mappings at mutable or
cross-process boundaries. Never use mutable default arguments.

## Capture ownership and lifecycle

> Only the launcher or component that starts a capture closes it. Code
> receiving an already-started controller may annotate it but does not close
> it.

- Reject markers and segments before controller startup.
- Reject duplicate active segment IDs.
- Stop nested segments in reverse start order.
- On controller close, close every open segment in reverse order before
  stopping the backend.
- Use `completed` for automatically closed segments after normal completion
  and `aborted` after a controlled abnormal exit.
- Stop the backend exactly once.
- Safely clean up partially entered backends when startup fails.
- Treat Ctrl+C as a controlled `operator_interrupt` and allow the backend to
  acknowledge, flush, and stop before the launcher exits.
- On Windows, keep console Ctrl+C ownership in the foreground launcher. The
  worker ignores console interrupts, and the bridge remains outside the
  launcher's terminal signal path.
- Use `NoCaptureController` for deliberate no-hardware paths. Do not add
  task-specific `if capture` branches.
- Starting and stopping a segment emits its authoritative segment record. Do
  not automatically add duplicate boundary markers.

## Capture format compatibility

`*.capture.jsonl.zst` is authoritative and append-only.

- Preserve schema version 2 record names, fields, and meanings unless a schema
  correction is explicitly approved and documented with compatibility tests.
- Never rewrite an existing capture. Writers use exclusive creation.
- Never make CSV or Parquet authoritative.
- Readers must decode existing compatible captures from both source projects.
- Existing readers must decode newly written compatible captures.
- Preserve lifecycle validation, sequence validation, finite JSON values, and
  newline-terminated JSONL records.
- Preserve complete raw packet documents, including unknown device fields.
- Accept unknown fields on known record types for forward compatibility.
- Do not silently accept unknown record types or unsupported schema versions.
- Keep marker ordering consistently `marker_id, marker_kind` through every API,
  IPC message, recorder, and wire record.
- Keep optional timestamp/source-clock fields generic. Do not invent
  application-specific timestamp attribute names.
- Heavy conversion dependencies such as pandas and PyArrow must remain behind
  the `parquet` optional dependency. Acquisition and capture must not require
  them.

## SiFi and bridge behavior

- The managed bridge must configure an explicit EMG sample rate. The default
  is 1600 Hz; supported values are 500, 1000, 1600, and 2000 Hz.
- Bridge downloads must remain an explicitly invoked user operation through
  `uv run sifi-download-bridge`. Never download or update a bridge during
  package installation, import, capture creation, or device startup.
- Keep the maintainer-tested bridge version and all platform SHA-256 values in
  `sifi/bridge_install.py`. Updating them is a deliberate manual maintainer
  action.
- The optional latest-release and specific-tag paths must require a valid
  SHA-256 digest from GitHub release metadata and fail closed when it is absent
  or malformed. Tested mode routes through the tag resolver with the
  maintainer-pinned version and digest table.
- Use `urllib` and Python archive modules for bridge downloads. Do not add an
  HTTP client dependency solely for this utility.
- Auto-detect only supported vendor platforms, verify the archive before
  extraction, extract only the expected nested executable, constrain download
  and extracted size, refuse overwrites by default, and write a provenance
  manifest beside the executable.
- Validate rates before launching hardware.
- Accept an explicit bridge executable path; do not rely on a consumer's
  project-relative layout.
- Preserve TCP, UDP, and stdout transport command construction.
- Translate socket and expected bridge I/O failures into specific package
  exceptions.
- Close bridge stdin, stdout, and stderr streams during teardown.
- Use a new Windows process group or POSIX session for the bridge so foreground
  Ctrl+C can produce an orderly command-driven shutdown.
- The background handle owns its process and readers only while entered.
- The worker owns the device, shared-memory blocks, ring buffers, acquisition
  thread, and recorder.
- A ring write larger than its capacity retains the newest samples.
- Keep dependency injection available for devices, clocks, handles, and I/O so
  tests do not require hardware.

## Runner and CLI boundaries

Reusable runners may provide:

- timed capture;
- capture until interrupt;
- interactive marker and segment entry;
- scalar `key=value` parsing;
- controlled close with a meaningful reason.

The supported interactive vocabulary is:

```text
segment start ID KIND [key=value ...]
segment stop ID [reason]
marker ID KIND [key=value ...]
help
stop
```

Runners and the `sifi-capture` CLI must not know about participants, cognitive
session directories, manifests, task suites, task names, or consumer artifact
layouts. Those belong in thin consumer wrappers.

## Python and packaging standards

Target Python 3.14+ and use `uv` for environments, dependency management,
running commands, and builds.

- Use aggressive type hints on public APIs and non-obvious boundaries.
- Prefer modern syntax: `list[str]`, `X | None`, PEP 695 type aliases and type
  parameters, `Self`, `StrEnum`, pattern matching, and narrow protocols.
- Import collection interfaces such as `Mapping`, `Callable`, `Iterator`, and
  `Sequence` from `collections.abc`.
- Prefer frozen, slotted dataclasses for fixed-shape values. Use mutable data
  classes only where lifecycle state is intrinsically mutable.
- Contain `Any` at unavoidable integration edges; prefer precise types over
  `Any`, `object`, string flags, ad-hoc tuples, and unstructured dictionaries.
- Use specific exceptions. Do not broadly swallow `Exception` except where a
  lifecycle boundary must clean up and immediately re-raise.
- Keep functions small and semantic. Use the canonical `capture`,
  `acquisition`, `sifi`, and `web` namespaces; keep the package root minimal.
- Keep runtime dependencies minimal and development dependencies separate.
- Ensure wheel configuration includes the complete `sifi_streamer` package and
  the `sifi-capture` console script.
- Do not add dependencies on either source repository.
- Do not commit unless explicitly requested.

## Testing requirements

Use temporary directories for all capture artifacts. Add regression coverage
for each meaningful behavioral divergence or lifecycle fix. At minimum retain
coverage for:

- capture decoding and round trips;
- invalid record and lifecycle rejection;
- nested and non-finite attribute rejection;
- marker ID/kind ordering;
- capture and segment start/stop;
- reverse-order nested segment closure;
- duplicate segment IDs and annotation-before-start rejection;
- normal, controlled-abort, and partially failed startup closure;
- exactly-once backend stop behavior;
- no-capture behavior;
- background handle lifecycle and synthetic acquisition;
- shared-memory reads and oversized writes;
- modality and packet parsing;
- bridge command construction, process ownership, stream closure, and socket
  error translation;
- bridge downloader platform selection, pinned and latest release resolution,
  checksum rejection, safe ZIP/TAR extraction, overwrite protection, and
  manifest contents without live network access;
- supported and invalid EMG rates;
- interactive parsing and CLI mode conflicts;
- controlled Ctrl+C shutdown where practical;
- old-reader/new-writer and representative old-wire/new-reader compatibility.

Before handoff, run:

```powershell
uv run python -m unittest discover -s tests -v
uv run ruff check .
uv run ruff format --check .
uv run ty check
Push-Location frontend
npm ci
npm run check
npm run build
Pop-Location
git diff --check
uv build
```

Frontend changes must include the regenerated, committed files under
`sifi_streamer/web/assets`. Do not commit `frontend/node_modules`. A Git or
wheel installation consumes the compiled assets and must not require Node.js.

Also install the built wheel into a clean temporary Python 3.14 environment and
confirm:

- `import sifi_streamer` resolves from the installed wheel without either
  consumer repository on `sys.path`;
- `sifi-capture --help` succeeds;
- a short synthetic capture can be written and read back;
- representative compatible captures decode without mutation;
- unchanged readers from both source repositories can read new captures when
  those repositories are available locally.

## Documentation and migration

Update documentation whenever public concepts, commands, dependencies,
capture compatibility, lifecycle, or ownership changes. Keep private Git
dependency examples pointed at `BLINCdev/sifi-streamer` and recommend immutable
tags or commits.

When handing off consumer migration, call out deliberate API changes:

- task-specific trial and presentation methods are consumer-owned functions;
- generic segments no longer generate duplicate markers;
- marker positional order is `marker_id, marker_kind`;
- attributes are strictly scalar;
- project-specific manifests, paths, and conversion policy remain in the
  consumer repositories.
