# Pre-v1 capture replacement

The authoritative capture is now a `.capture.duckdb` database. This is a deliberate
breaking change before v1. There is no reader, migration, or dual-write path for
compressed JSONL. Use the earlier package revision to inspect old captures.
Existing artifacts are never rewritten.

DuckDB is a runtime dependency. CSV and Parquet exports do not require pandas or
PyArrow. The old SiFi pandas extraction API and `sifi-capture-to-parquet` command
are removed. Use `sifi-capture-export SOURCE OUTPUT [--format parquet|csv]`
with optional repeated `--table NAME` selections.

Integrations must supply explicit per-channel validity, fixed stream source-clock
text/unit/timestamp dtype, and thread-safe `request_stop()` to unblock reads.
`disconnect()` performs final cleanup after polling finishes. All stream layouts
register before polling starts; dynamic registration and implicit shape repair
are unsupported.

Composition accepts named device factories. Stream identity is device ID plus
local stream ID. The recorder starts before hardware connection. Producers stamp
local sequences and receipt clocks before transport; the recorder assigns global
acceptance order. ACKs mean accepted, not durable. Independent one-second commits
are an operating target, not a bounded-loss promise during storage stalls.

Any unhandled error fails the whole capture. Device producers finish before the
recorder drains, closes remaining segments, and finalizes. Missing terminal state
means incomplete capture. Preserve a crashed database and WAL together. Explicit
recovery writes a new artifact rather than mutating the original.

Annotations remain device/application-neutral and strictly scalar. Marker order
remains `marker_id, marker_kind`. Consumer artifact paths, interpretation of source
clocks, and application selection policy remain consumer responsibilities.
