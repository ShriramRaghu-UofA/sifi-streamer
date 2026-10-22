# Architecture

```text
CaptureController -> CaptureBackend -> AcquisitionCaptureBackend
                                              |
                                      coordinator handle
                                  /           |           \
                           device A        device B     annotations
                                  \           |           /
                                  bounded producer transport
                                              |
                                      recorder process
                                              |
                                    capture DuckDB database

device workers -> shared-memory rings -> live monitor -> web/consumer
```

`capture` owns authoritative storage, recording transport, controller lifecycle,
and runners. `acquisition` owns generic device protocols, coordination, workers,
and live buffers. `sifi` supplies concrete devices and managed bridge composition;
`web` supplies a local monitored launcher. Generic capture and acquisition never
import integration namespaces. Runtime acquisition does not require pandas or
PyArrow.

The coordinator owns one recorder and one process per device. Factories are
explicitly injected and picklable. Workers own devices, polling threads, and
shared-memory rings. `request_stop()` unblocks reads; `disconnect()` completes
teardown. Console Ctrl+C belongs to the foreground launcher; children ignore it
and the bridge runs in its own process group/session.

The recorder creates and commits metadata before hardware connection. Devices
report complete finite JSON and declare their fixed layouts after connection.
All layouts register before a shared acquisition gate opens. Undeclared streams,
shape changes, and unhandled exceptions fail the entire capture. Source clocks
are descriptive producer-owned text with declared units and timestamp dtypes.
The package does not infer clock synchronization.

Each submission carries producer identity, local sequence, and host monotonic/Unix
receipt clocks assigned before transport. The recorder assigns global acceptance
order. Each packet may contain many samples and an optional complete raw document.
Samples share event identity plus a sample index. Nullness and validity remain
independent; authoritative recording precedes lossy display conversion.

Only the recorder writes DuckDB. Events and their body/sample rows commit
atomically. Dynamic metadata and raw documents use VARIANT; modeled columns are
typed. Acceptance is not durability. An independent timer commits pending data,
with an additional batch-size trigger. Bounded budgets include transport backlog.
Deadlines and overload are failures; there are no silent drops or automatic retries.

Shutdown rejects external commands, finishes device producers, drains accepted
data, closes open segments in reverse order, writes a terminal event, and commits.
Queue emptiness is not producer completion. Any unhandled device/transport/storage
failure initiates capture-wide cleanup; timeouts never become successful drains.

One `.capture.duckdb` database represents one capture. There is no legacy storage
reader or migration. Read-only SQL and discovery helpers expose events, catalogs,
samples, and derived segment/status views. Parquet and CSV are derived exports.
Preserve the WAL beside an abruptly terminated database. Explicit recovery creates
a new recovered artifact; ordinary readers never silently repair originals.

Live rings and health are separate from persistence. Their recent-window retention
and display conversion must not affect authoritative samples. Optional health JSONL
sidecars remain non-authoritative.

See [persistence-design.md](persistence-design.md) and [api.md](api.md).
