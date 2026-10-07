import type { Page } from '@playwright/test';
import type { Attributes, Bootstrap, Health, Kind, Live } from '../src/lib/types';
import { defaultThresholds } from '../src/lib/types';

export function bootstrap(): Bootstrap {
  return {
    state: 'setup',
    device_info: {},
    device_summary: {},
    launch_configuration: {},
    error: null,
    output: 'C:\\captures\\pilot.capture.jsonl.zst',
    configuration: {
      device: 'SiFi bridge',
      transport: 'stdout',
      emg_fs_hz: 1600,
      imu_fs_hz: 100,
      emg_enabled: true,
    },
    default_capture_id: 'pilot-session-01',
    default_attributes: { operator: 'Shriram' },
    thresholds: { ...defaultThresholds },
    health_log_enabled: true,
    active_segments: [],
    streams: [],
    kinds: [kind('marker', 'note'), kind('segment', 'rest')],
  };
}
export function kind(target: Kind['target'], name: string): Kind {
  return {
    target,
    kind: name,
    label: name === 'note' ? 'Note' : 'Rest',
    color: '#10b981',
    id_prefix: null,
    separator: '_',
    padding: 2,
    start: 1,
    default_attributes: { condition: 'baseline' },
  };
}
const streams: Bootstrap['streams'] = [
  {
    stream_id: 'emg_armband',
    label: 'EMG',
    channels: Array.from({ length: 8 }, (_, index) => `emg${index}`),
    channel_labels: Array(8).fill(null),
    channel_units: Array(8).fill('V'),
    nominal_rate_hz: 1600,
    n_samples: 16000,
  },
];
export function health(count: number): Health {
  return {
    sequence: count,
    monotonic_time: count,
    severity: 'healthy',
    acquisition_alive: true,
    streams: [
      {
        stream_id: 'emg_armband',
        severity: 'healthy',
        nominal_rate_hz: 1600,
        reported_rate_hz: 1599.8,
        observed_rate_hz: 1600,
        source_rate_hz: 1600,
        last_packet_age_seconds: 0.01,
        packet_count: count,
        sample_count: count * 100,
        lost_samples: 0,
        missing_by_channel: Array(8).fill(0),
        missing_fraction: 0,
        non_ok_packets: 0,
        malformed_packets: 0,
        misaligned_packets: 0,
        timestamp_errors: 0,
        warnings: [],
      },
    ],
  };
}
export async function mockSession(page: Page) {
  const session = bootstrap();
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  let count = 0;
  let markerNumber = 0;
  let segmentNumber = 0;
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const body = request.postDataJSON() ?? {};
    calls.push({ path, body });
    if (request.headers()['x-sifi-session-token'] !== 'test-token') {
      await route.fulfill({ status: 401, json: { error: 'invalid session token' } });
      return;
    }
    let response: unknown = {};
    switch (path) {
      case '/api/bootstrap':
        response = session;
        break;
      case '/api/capture/start':
        session.state = 'recording';
        session.streams = streams;
        session.default_capture_id = String(body.capture_id);
        session.default_attributes = body.attributes as Attributes;
        session.health_log_enabled = Boolean(body.health_log_enabled);
        response = session;
        break;
      case '/api/live': {
        const start = count++ * 100;
        response = {
          state: session.state,
          device_info: session.device_info,
          device_summary: session.device_summary,
          launch_configuration: session.launch_configuration,
          error: null,
          health: health(count),
          active_segments: session.active_segments,
          kinds: session.kinds,
          thresholds: session.thresholds,
          events: [
            {
              sequence: 1,
              monotonic_time: 1,
              stream_id: 'emg_armband',
              code: 'warming_up',
              active: false,
              severity: 'healthy',
              message: 'EMG stream recovered',
            },
          ],
          batches: {
            emg_armband: {
              start_index: start,
              end_index: start + 100,
              overrun: false,
              timestamps: Array.from({ length: 100 }, (_, i) => (start + i) / 1600),
              samples: Array.from({ length: 100 }, (_, i) =>
                Array.from({ length: 8 }, (_, j) =>
                  i === 30 ? null : Math.sin((start + i) / 10 + j),
                ),
              ),
            },
          },
        } satisfies Live;
        break;
      }
      case '/api/marker':
        response = { id: `note_${String(++markerNumber).padStart(2, '0')}` };
        break;
      case '/api/segment/start': {
        const id = `rest_${String(++segmentNumber).padStart(2, '0')}`;
        session.active_segments.push(id);
        response = { id };
        break;
      }
      case '/api/segment/stop':
        session.active_segments.pop();
        break;
      case '/api/kinds/set':
        session.kinds = [
          ...session.kinds.filter((item) => item.target !== body.target || item.kind !== body.kind),
          body as Kind,
        ];
        break;
      case '/api/kinds/remove':
        session.kinds = session.kinds.filter(
          (item) => item.target !== body.target || item.kind !== body.kind,
        );
        break;
      case '/api/thresholds':
        session.thresholds = body as Bootstrap['thresholds'];
        break;
      case '/api/capture/stop':
        session.state = 'stopped';
        session.active_segments = [];
        response = session;
        break;
    }
    await route.fulfill({ json: response });
  });
  return { session, calls };
}
