# DuckDB capture architecture

This document records the approved pre-v1 replacement of JSONL/Zstandard capture
storage. Old capture files and APIs receive no compatibility path.

## Ownership

`CaptureController` composes an `AcquisitionCaptureBackend`. The backend owns a
coordinator handle, one recorder process, and one worker per configured device.
Device workers own connections, acquisition threads, and shared-memory rings.
Only the recorder owns a writable DuckDB connection. Acquisition and capture
remain independent of SiFi and web integration namespaces.

## Declarations and submissions

Devices declare all streams after connection, before a shared acquisition-start
gate opens. Stream identity is `(device_id, stream_id)`. Layouts cannot change.
The existing structural device/packet protocols acquire explicit per-channel
validity, descriptive source clocks and timestamp units/dtypes, and a thread-safe
`request_stop()` operation that unblocks polling. Each packet contributes to at
most one declared stream and may independently supply a complete raw document.
Malformed shapes, undeclared streams, and unhandled exceptions fail the capture.

Producers assign local sequences and host monotonic/Unix nanosecond receipt times
before transport. The recorder assigns global acceptance order. Neither order
nor host receipt time asserts source measurement synchronization. Source clocks
are producer-owned descriptions; no inferred clock mapping is performed.

## Storage

One `.capture.duckdb` artifact contains typed capture/producer/device/stream/channel
catalogs, events, packets, annotations, reports, diagnostics, lifecycle, and one
typed sample table per stream. Related rows share an event sequence and commit
atomically. Batched packet samples also have a sample index. Explicit validity is
independent of null values. Complete raw documents and dynamic metadata use
VARIANT; modeled envelope fields are ordinary typed columns. Segment views join
start and stop occurrences. A missing terminal event means incomplete capture.

New artifacts refuse existing paths. Completed artifacts are finalized before
success is reported. Crash recovery preserves DuckDB's database and WAL together;
no reader silently repairs, upgrades, or rewrites an authoritative capture.

## Recorder policy

Initial defaults: independent one-second commits; an 8 MiB batch-size trigger;
32 MiB pending-byte budget per producer; 8 MiB maximum submission; two-second
enqueue deadline. Bounded shared-memory channels publish only complete messages.
Transport budgets include queued data. Acceptance ACKs do
not promise durability. Overload, write failure, process failure, and timeout are
explicit terminal errors. There are no automatic retries or silent packet drops.

## Lifecycle

Create and commit initial capture metadata before connecting hardware. Record
startup reports/failures. Open the acquisition gate only after every stream is
registered. An unhandled failure stops the entire capture; integrations must
handle recoverable conditions explicitly themselves.

On shutdown, reject external commands, request device stop, finish/disconnect
producers, observe ordered producer-completion messages, drain accepted data,
close remaining segments in reverse order, record terminal outcome, and commit.
Timeouts are failures, never successful drains. Console Ctrl+C stays with the
foreground launcher; child processes ignore console interrupts.

## Inspection and export

The database is directly queryable. Read-only discovery/query helpers and SQL
examples expose declarations, events, segments, and typed samples. Parquet and
CSV are derived selections. Ordinary Parquet uses JSON-text dynamic payloads for
portability; native VARIANT may be offered explicitly. No pandas/PyArrow runtime
dependency is required for acquisition or database export.

## Acceptance

Cover atomic rollback and process-crash recovery, exact integer and timestamp
preservation, typed null/validity distinctions, idle timer commits, bounded
backpressure, write failures, multi-device startup barriers, autonomous terminal
failure propagation, final packet draining, partial startup cleanup, lifecycle
validation, exports, synthetic capture, and clean-wheel operation. Process-kill
tests do not establish power-loss durability. The commit interval is an operating
target, not a guarantee under stalled storage.
