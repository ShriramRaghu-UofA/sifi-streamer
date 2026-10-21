-- Open a completed .capture.duckdb database with DuckDB or a SQL client.
SELECT * FROM capture_status;

SELECT record_type, count(*) AS events
FROM events GROUP BY record_type ORDER BY record_type;

SELECT s.device_id, s.stream_id, s.table_name, s.dtype,
       s.source_clock, s.timestamp_unit, s.timestamp_dtype,
       c.channel_id, c.column_name, c.validity_column, c.unit
FROM streams s JOIN channels c USING (device_id, stream_id)
ORDER BY s.device_id, s.stream_id, c.channel_index;

SELECT marker_kind, count(*) AS markers
FROM markers GROUP BY marker_kind;

SELECT s.segment_id, s.segment_kind, s.reason, count(p.event_sequence) AS packets
FROM segments s LEFT JOIN packets p
  ON p.event_sequence > s.start_sequence
 AND (s.stop_sequence IS NULL OR p.event_sequence < s.stop_sequence)
GROUP BY s.start_sequence, s.segment_id, s.segment_kind, s.reason;

-- Discover the table and channel columns above before selecting measurements:
-- SELECT e.received_monotonic_ns, t.source_time, t.value_0, t.valid_0
-- FROM samples_0000 t JOIN events e USING (event_sequence)
-- ORDER BY t.event_sequence, t.sample_index;
