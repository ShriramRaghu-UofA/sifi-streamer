"""Versioned SQL layout for self-describing DuckDB captures."""

SCHEMA_SQL = """
CREATE TABLE capture (
    capture_id VARCHAR PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    package_version VARCHAR NOT NULL,
    attributes VARIANT NOT NULL,
    host_clock VARCHAR NOT NULL
);
CREATE TABLE producers (
    producer_id VARCHAR PRIMARY KEY, device_id VARCHAR, role VARCHAR NOT NULL
);
CREATE TABLE devices (
    device_id VARCHAR PRIMARY KEY, integration VARCHAR NOT NULL,
    configuration VARIANT NOT NULL
);
CREATE TABLE streams (
    device_id VARCHAR NOT NULL, stream_id VARCHAR NOT NULL,
    table_name VARCHAR UNIQUE NOT NULL, dtype VARCHAR NOT NULL,
    nominal_rate_hz DOUBLE NOT NULL, label VARCHAR,
    source_clock VARCHAR NOT NULL, timestamp_unit VARCHAR NOT NULL,
    timestamp_dtype VARCHAR NOT NULL,
    PRIMARY KEY(device_id, stream_id)
);
CREATE TABLE channels (
    device_id VARCHAR NOT NULL, stream_id VARCHAR NOT NULL,
    channel_id VARCHAR NOT NULL, channel_index INTEGER NOT NULL,
    column_name VARCHAR NOT NULL, validity_column VARCHAR NOT NULL,
    label VARCHAR, unit VARCHAR,
    PRIMARY KEY(device_id, stream_id, channel_id),
    UNIQUE(device_id, stream_id, channel_index)
);
CREATE TABLE events (
    event_sequence BIGINT PRIMARY KEY, producer_id VARCHAR NOT NULL,
    producer_sequence BIGINT NOT NULL, record_type VARCHAR NOT NULL,
    received_monotonic_ns BIGINT NOT NULL, received_unix_ns BIGINT NOT NULL,
    UNIQUE(producer_id, producer_sequence)
);
CREATE TABLE packets (
    event_sequence BIGINT PRIMARY KEY, device_id VARCHAR, stream_id VARCHAR,
    payload VARIANT, reported_rate_hz DOUBLE,
    samples_lost BIGINT NOT NULL CHECK(samples_lost >= 0), status VARCHAR NOT NULL
);
CREATE TABLE capture_started (event_sequence BIGINT PRIMARY KEY);
CREATE TABLE capture_stopped (
    event_sequence BIGINT PRIMARY KEY, reason VARCHAR NOT NULL,
    outcome VARCHAR NOT NULL
);
CREATE TABLE launch_configuration (
    event_sequence BIGINT PRIMARY KEY, configuration VARIANT NOT NULL
);
CREATE TABLE device_reports (
    event_sequence BIGINT PRIMARY KEY, device_id VARCHAR,
    stage VARCHAR NOT NULL, info VARIANT NOT NULL
);
CREATE TABLE diagnostics (
    event_sequence BIGINT PRIMARY KEY, device_id VARCHAR,
    severity VARCHAR NOT NULL, source VARCHAR NOT NULL, stage VARCHAR NOT NULL,
    code VARCHAR NOT NULL, message VARCHAR NOT NULL, details VARIANT NOT NULL
);
CREATE TABLE markers (
    event_sequence BIGINT PRIMARY KEY, marker_id VARCHAR NOT NULL,
    marker_kind VARCHAR NOT NULL, attributes VARIANT NOT NULL,
    source_time_ns BIGINT, source_clock VARCHAR
);
CREATE TABLE segment_starts (
    event_sequence BIGINT PRIMARY KEY, segment_id VARCHAR NOT NULL,
    segment_kind VARCHAR NOT NULL, attributes VARIANT NOT NULL
);
CREATE TABLE segment_stops (
    event_sequence BIGINT PRIMARY KEY,
    start_event_sequence BIGINT UNIQUE NOT NULL, reason VARCHAR
);
CREATE VIEW segments AS
SELECT s.segment_id, s.segment_kind, s.attributes,
       s.event_sequence AS start_sequence, t.event_sequence AS stop_sequence,
       e.received_monotonic_ns AS start_monotonic_ns,
       f.received_monotonic_ns AS stop_monotonic_ns,
       e.received_unix_ns AS start_unix_ns, f.received_unix_ns AS stop_unix_ns,
       t.reason
FROM segment_starts s
JOIN events e ON e.event_sequence = s.event_sequence
LEFT JOIN segment_stops t ON t.start_event_sequence = s.event_sequence
LEFT JOIN events f ON f.event_sequence = t.event_sequence;
CREATE VIEW capture_status AS
SELECT c.capture_id, c.schema_version,
       CASE WHEN t.event_sequence IS NULL THEN 'incomplete'
            ELSE t.outcome END AS outcome,
       t.reason
FROM capture c LEFT JOIN capture_stopped t ON TRUE;
CREATE VIEW record_bodies AS
SELECT event_sequence, json_object('packet', payload::JSON) AS body FROM packets
UNION ALL SELECT event_sequence,
    json_object('capture_id',capture_id,'attributes',attributes::JSON)
    FROM capture_started CROSS JOIN capture
UNION ALL SELECT event_sequence, json_object('reason',reason) FROM capture_stopped
UNION ALL SELECT event_sequence, json_object('configuration',configuration::JSON)
    FROM launch_configuration
UNION ALL SELECT event_sequence, json_object('stage',stage,'info',info::JSON)
    FROM device_reports
UNION ALL SELECT event_sequence,
    json_object('severity',severity,'source',source,'stage',stage,'code',code,
                'message',message,'details',details::JSON) FROM diagnostics
UNION ALL SELECT event_sequence,
    json_object('marker_id',marker_id,'marker_kind',marker_kind,'attributes',attributes::JSON,
                'source_time_ns',source_time_ns,'source_clock',source_clock)
    FROM markers
UNION ALL SELECT event_sequence,
    json_object('segment_id',segment_id,'segment_kind',segment_kind,
                'attributes',attributes::JSON) FROM segment_starts
UNION ALL SELECT t.event_sequence,
    json_object('segment_id',s.segment_id,'reason',t.reason)
    FROM segment_stops t JOIN segment_starts s
    ON t.start_event_sequence = s.event_sequence;
"""


def quote_identifier(value: str) -> str:
    """Quote a SQL identifier without changing its spelling."""
    return '"' + value.replace('"', '""') + '"'
