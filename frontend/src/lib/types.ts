export type Scalar = string | number | boolean | null;
export type Attributes = Record<string, Scalar>;
export type Phase = 'setup' | 'starting' | 'recording' | 'stopping' | 'stopped' | 'failed';
export type Severity = 'healthy' | 'warming_up' | 'warning' | 'fatal';
export type Thresholds = {
  window_seconds: number;
  stale_after_seconds: number | null;
  minimum_rate_ratio: number | null;
  maximum_rate_ratio: number | null;
  maximum_missing_fraction: number | null;
  maximum_lost_samples: number | null;
};
export type Kind = {
  target: 'marker' | 'segment';
  kind: string;
  label: string | null;
  color: string | null;
  id_prefix: string | null;
  separator: string;
  padding: number;
  start: number;
  default_attributes: Attributes;
};
export type Stream = {
  stream_id: string;
  label: string | null;
  channels: string[];
  channel_labels: (string | null)[];
  channel_units: (string | null)[];
  nominal_rate_hz: number;
  n_samples: number;
};
export type StreamHealth = {
  stream_id: string;
  severity: Severity;
  nominal_rate_hz: number;
  reported_rate_hz: number | null;
  observed_rate_hz: number | null;
  source_rate_hz: number | null;
  last_packet_age_seconds: number | null;
  packet_count: number;
  sample_count: number;
  lost_samples: number;
  missing_by_channel: number[];
  missing_fraction: number;
  non_ok_packets: number;
  malformed_packets: number;
  misaligned_packets: number;
  timestamp_errors: number;
  warnings: string[];
};
export type Health = {
  sequence: number;
  monotonic_time: number;
  severity: Severity;
  acquisition_alive: boolean;
  streams: StreamHealth[];
};
export type HealthEvent = {
  sequence: number;
  monotonic_time: number;
  stream_id: string | null;
  code: string;
  active: boolean;
  severity: Severity;
  message: string;
};
export type Bootstrap = {
  state: Phase;
  error: string | null;
  output: string;
  configuration: Attributes;
  default_capture_id: string;
  default_attributes: Attributes;
  thresholds: Thresholds;
  health_log_enabled: boolean;
  kinds: Kind[];
  active_segments: string[];
  streams: Stream[];
};
export type Trace = { timestamps: number[]; samples: (number | null)[][] };
export type Live = {
  state: Phase;
  error: string | null;
  health: Health | null;
  events: HealthEvent[];
  batches: Record<string, Trace & { start_index: number; end_index: number; overrun: boolean }>;
  active_segments: string[];
  thresholds: Thresholds;
  kinds: Kind[];
};
export const defaultThresholds: Thresholds = {
  window_seconds: 5,
  stale_after_seconds: 2,
  minimum_rate_ratio: 0.9,
  maximum_rate_ratio: 1.1,
  maximum_missing_fraction: 0,
  maximum_lost_samples: 0,
};
export const themes = [
  'cerberus',
  'catppuccin',
  'concord',
  'dracula',
  'mint',
  'modern',
  'rosepine',
  'wintry',
] as const;
export type Theme = (typeof themes)[number];
export type ColorMode = 'system' | 'light' | 'dark';
export function formatRate(value: number | null | undefined): string {
  return value == null
    ? '—'
    : `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value)} Hz`;
}
export function title(value: string): string {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}
export function thresholdsValid(value: Thresholds): boolean {
  return Object.entries(value).every(([key, item]) => {
    if (item === null) return key !== 'window_seconds';
    if (!Number.isFinite(item)) return false;
    if (key === 'window_seconds' || key === 'stale_after_seconds') return item > 0;
    return item >= 0 && (key !== 'maximum_lost_samples' || Number.isInteger(item));
  });
}
