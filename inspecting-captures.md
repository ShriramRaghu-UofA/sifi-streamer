# Inspect and export a capture

Use a completed `.capture.duckdb` database produced by either launcher. Run commands
from the project environment with `uv run`, or use installed console scripts on
PATH. Keep the original database as the authoritative recording.

## Query and export

```python
from pathlib import Path
from sifi_streamer.capture import CaptureLogReader

reader = CaptureLogReader(Path("session.capture.duckdb"))
print(reader.query("SELECT marker_kind, count(*) FROM markers GROUP BY marker_kind"))
print(reader.streams())  # discover typed sample table names and source clocks
reader.export(Path("session-tables"), tables=("events", "markers", "segments"))
```

```powershell
sifi-capture-export session.capture.duckdb session-tables
sifi-capture-export session.capture.duckdb selected --table markers --table segments
sifi-capture-export session.capture.duckdb csv-tables --format csv
```

DuckDB provides Parquet/CSV export directly; pandas/PyArrow are not required.
Dynamic columns export as JSON text by default. `--native-variant` explicitly
requests native VARIANT in Parquet. Export directories must be new. Exports are
derived artifacts and never alter the database. SQL clients can also query the
self-describing database directly without importing this package.
See [examples/inspect_capture.sql](examples/inspect_capture.sql) for catalog,
event, annotation, and segment queries.

### Finding your way around a capture

Start with the `.capture.duckdb` file. It contains the entire recording, including
device settings, signal layouts, samples, annotations, and lifecycle events.
An optional `.health.jsonl` file contains monitoring diagnostics, not authoritative
samples. Export directories such as `session-tables/` and `csv-tables/` are derived
copies: by default, each exported table or view gets a file with the same name,
such as `events.parquet` or `events.csv`. A selected-table export may omit files.

For a first inspection, look at these tables/files in this order:

| Table / exported filename stem | What it tells you |
| --- | --- |
| `capture_status` | Whether capture completed, failed, or is incomplete, and its stop reason. |
| `capture` | Capture ID, format/package versions, attributes, and host clock description. |
| `streams` | Which device owns each stream, its nominal rate, source clock/unit, and sample table name. |
| `channels` | Ordered channel names and their physical value/validity column names, labels, and units when supplied. |
| `samples_0000`, `samples_0001`, … | Actual measurements, one row per sample in a particular stream. Discover their meaning through `streams`; the numeric suffix has no intrinsic meaning. |
| `events` | Global recording order, producer-local sequence, and host timestamps for every recorded event. |
| `packets` | Device/stream identity, reported rate/loss/status, and optional original document for each packet. |
| `capture_started`, `capture_stopped` | Lifecycle event IDs and stop outcome/reason; join to `events` for timestamps. |
| `markers`, `segments` | Point annotations and duration annotations. The `segments` view already includes boundary timestamps. |
| `launch_configuration`, `device_reports`, `diagnostics` | Requested settings, bridge reports at startup stages, and recorded problems. Join to `events` for their times. |
| `devices`, `producers` | Device registration and the identities of components submitting events. |

The `segment_starts` and `segment_stops` tables contain the underlying segment
events. Most analysis can use the joined `segments` view instead. In the database,
`record_bodies` is a convenience view used by the package reader; it is not an
additional sample stream.

Open the completed database in a DuckDB-compatible SQL client. Alternatively,
run each query below with `reader.query(...)` using the Python example above.

**1. Check the outcome and capture boundaries.**

```sql
SELECT * FROM capture_status;

SELECT event_sequence, record_type,
       make_timestamp_ns(received_unix_ns) AS time_utc,
       received_monotonic_ns
FROM events
WHERE record_type IN ('capture_started', 'capture_stopped')
ORDER BY event_sequence;
```

Timestamps are stored once in `events`, rather than repeated in each event's body
table. For example, `capture_started.csv` contains only an `event_sequence`;
find that same sequence in `events.csv` to obtain its time. Capture start includes
hardware startup, and capture stop follows producer shutdown and draining. These
boundaries are not the first and last measurement times. `incomplete` means no
terminal event was recorded; a terminal event can also report failure.

**2. Find a device's stream and channels.**

```sql
SELECT s.device_id, s.stream_id, s.table_name, s.nominal_rate_hz,
       s.source_clock, s.timestamp_unit,
       c.channel_id, c.column_name, c.validity_column, c.unit
FROM streams s
JOIN channels c USING (device_id, stream_id)
ORDER BY s.device_id, s.stream_id, c.channel_index;
```

For a recording made with `--device left=... --device right=...`, `device_id`
contains `left` or `right`. A SiFiBand might have stream `emg_armband` with
channels `emg0` through `emg7`, while a BioPoint has stream `emg` with one channel.
Use the catalog to discover the actual layout. Both devices can have channels
named `emg`; the device and stream IDs disambiguate them.

**3. Read measurements with packet arrival times.** After the previous query
identifies a sample table and channel columns, substitute them in this example:

```sql
SELECT p.device_id, p.stream_id, t.event_sequence, t.sample_index,
       t.source_time, t.value_0, t.valid_0,
       e.received_monotonic_ns,
       make_timestamp_ns(e.received_unix_ns) AS packet_arrival_utc
FROM samples_0000 t
JOIN packets p USING (event_sequence)
JOIN events e USING (event_sequence)
ORDER BY t.event_sequence, t.sample_index
LIMIT 20;
```

`value_0` is the channel mapped to that column in `channels`, not necessarily a
channel called "0". Its corresponding `valid_0` explicitly identifies validity:
a null value can be marked valid, so nullness alone does not establish invalidity.
One packet may batch many samples. Those rows share `event_sequence` and packet
arrival time; `sample_index` orders samples within that packet.

`source_time` is the producer's measurement timestamp, interpreted using the
stream's `source_clock` and `timestamp_unit`. It is not necessarily Unix time.
Two devices' source clocks are not automatically synchronized. Host Unix time
provides calendar time; host monotonic nanoseconds provide elapsed-time comparisons
within the same host boot. Neither packet arrival time nor recorder order proves
simultaneous measurement across devices.

**4. Count events, packets, and samples separately.**

```sql
SELECT record_type, count(*) AS event_count
FROM events GROUP BY record_type ORDER BY record_type;

SELECT device_id, stream_id, count(*) AS packet_count,
       sum(samples_lost) AS producer_reported_lost_samples
FROM packets GROUP BY device_id, stream_id ORDER BY device_id, stream_id;

SELECT count(*) AS sample_count FROM samples_0000;
```

Packet counts are not sample counts. Some packets are status or metadata reports
with no stream or samples. `samples_lost` is the producer's report, not an
independent completeness guarantee. `event_sequence` orders recorder acceptance;
`producer_id` plus `producer_sequence` identifies each producer's local order.
Use explicit `ORDER BY` when ordering matters.

**5. Place annotations on the timeline.**

```sql
SELECT m.marker_id, m.marker_kind, m.attributes,
       make_timestamp_ns(e.received_unix_ns) AS time_utc
FROM markers m JOIN events e USING (event_sequence)
ORDER BY m.event_sequence;

SELECT segment_id, segment_kind, start_sequence, stop_sequence,
       start_monotonic_ns, stop_monotonic_ns, reason
FROM segments ORDER BY start_sequence;
```

Segment boundaries describe annotation timing; use host arrival times or recording
order to select received packets within them. Mapping boundaries into a device's
measurement clock requires a clock relationship supplied by the producer/user.

The same joins work on exported files once loaded into a SQL tool. For example,
in a new DuckDB connection, load the event export into a temporary view:

```sql
CREATE TEMP VIEW events AS
SELECT * FROM read_parquet('session-tables/events.parquet');
```

Use `read_csv('csv-tables/events.csv', header = true)` for CSV, and load other
tables in the same way. This `CREATE VIEW` example requires a writable scratch
connection, rather than the SELECT-only `reader.query` helper. Parquet preserves
column types; when importing CSV into spreadsheets, keep nanosecond timestamps
and large sequence values as exact integers or text to avoid rounding. Dynamic
payload columns are JSON text in ordinary exports; typed sample columns can be
analyzed directly without parsing those payloads.

## Incomplete captures and recovery

A missing terminal event means the capture is incomplete. Preserve a crashed
database together with its `.wal` file. Ordinary readers do not repair captures.
Explicit recovery writes a new artifact:

```python
from pathlib import Path
from sifi_streamer.capture import recover_capture

recover_capture(Path("interrupted.capture.duckdb"), Path("recovered.capture.duckdb"))
```

See the [API reference](api.md#storage-and-sql) for reader contracts and
[storage design](persistence-design.md) for commit and failure behavior.
