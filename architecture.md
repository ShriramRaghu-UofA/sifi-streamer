# Architecture

```text
consumer functions
       |
CaptureController -> CaptureBackend protocol -> AcquisitionCaptureBackend
                                                |
                                         BackgroundHandle
                                                |
                    acquisition + shared memory + CaptureLogWriter
                                                |
                           AcquisitionDevice / SiFiBridgeDevice
```

`CaptureController` knows only captures, segments, markers, IDs, kinds,
reasons, and scalar attributes. It validates lifecycle, tracks arbitrary nested
segments, closes them in reverse order, and stops its backend exactly once. It
does not know trials, presentations, participants, stimuli, responses, tasks,
suites, or application artifact layouts. `NoCaptureController` provides the
same generic surface for deliberate no-hardware operation.

`CaptureBackend` is structural. `SiFiCaptureBackend` composes it with exactly
one entered `BackgroundHandle`; it is not a controller subclass. The handle
owns the spawned worker and shared-memory readers. The worker owns the device,
ring buffers, and recorder. The recorder serializes packets and annotations.

Live acquisition uses an ordered registry of string stream IDs declared once
after an injected device connects. Each `SignalStreamSpec` fixes its channels,
nominal rate, dtype, and optional display metadata. Each packet contributes to
at most one stream and may independently provide one raw capture document.
Shared-memory rings retain timestamps, native values, explicit validity, and
an absolute cursor.

`Modality`, `Modalities`, `SiFiPacket`, and `SiFiDevice` remain supported SiFi
compatibility projections. Stream addition or removal after startup remains
out of scope because live layouts are fixed for a capture.

The authoritative artifact is an append-only `*.capture.jsonl.zst`.
`CaptureLogWriter` exclusively creates schema-v2 JSONL in concatenated
Zstandard frames. Raw packet documents retain all JSON fields. Readers accept
unknown fields while validating record version, sequence, capture lifecycle,
segment lifecycle, JSON finiteness, and scalar annotations. A crashed,
unterminated log remains readable; readers never mutate it.

When a connected acquisition device supplies a startup information document,
the recorder writes that complete document as the first `raw_packet` after
`capture_started`. For SiFi hardware this preserves the bridge `info` response,
including firmware, configuration, and reported sample rates, in the
authoritative artifact without changing schema v2.

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
