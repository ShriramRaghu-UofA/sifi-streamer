import { createToaster } from '@skeletonlabs/skeleton-svelte';
import type { Attributes, Bootstrap, Kind, Live, Phase, Stream, Trace } from './types';
import { defaultThresholds } from './types';

/** One browser session. Polls never overlap and old responses cannot undo commands. */
export class Dashboard {
  bootstrap = $state.raw<Bootstrap | null>(null);
  live = $state.raw<Live | null>(null);
  traces = $state.raw<Record<string, Trace>>({});
  busy = $state(false);
  error = $state('');
  connectionError = $state('');
  closed = $state(false);
  captureId = $state('capture');
  attributes = $state<Attributes>({});
  healthLogEnabled = $state(true);
  thresholds = $state({ ...defaultThresholds });
  overrunStreams = $state.raw<string[]>([]);
  readonly toaster = createToaster({ placement: 'bottom-end', max: 3 });
  readonly phase: Phase | 'loading' | 'closed' = $derived(
    this.closed ? 'closed' : (this.live?.state ?? this.bootstrap?.state ?? 'loading'),
  );
  readonly kinds: Kind[] = $derived(this.live?.kinds ?? this.bootstrap?.kinds ?? []);
  readonly streams: Stream[] = $derived(this.bootstrap?.streams ?? []);
  readonly segments: string[] = $derived(
    this.live?.active_segments ?? this.bootstrap?.active_segments ?? [],
  );
  private cursors: Record<string, number> = {};
  private epoch = 0;
  private disposed = false;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private readonly abort = new AbortController();
  private readonly token: string;
  constructor(token: string) {
    this.token = token;
  }

  private async api<T>(path: string, body?: unknown): Promise<T> {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: {
        'X-SiFi-Session-Token': this.token,
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.any([this.abort.signal, AbortSignal.timeout(60000)]),
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error ?? `Request failed (${response.status})`);
    return value as T;
  }
  async initialize() {
    this.error = '';
    try {
      const bootstrap = await this.api<Bootstrap>('/api/bootstrap');
      if (this.disposed) return;
      this.bootstrap = bootstrap;
      this.captureId = bootstrap.default_capture_id;
      this.attributes = { ...bootstrap.default_attributes };
      this.thresholds = { ...bootstrap.thresholds };
      this.healthLogEnabled = bootstrap.health_log_enabled;
    } catch (cause) {
      if (!this.disposed) this.error = this.message(cause);
    }
  }
  private message(cause: unknown) {
    return cause instanceof Error ? cause.message : String(cause);
  }
  async command(path: string, body: unknown = {}, success = 'Changes saved'): Promise<boolean> {
    if (this.busy || this.closed || this.disposed) return false;
    this.busy = true;
    this.error = '';
    ++this.epoch;
    try {
      const result = await this.api<Bootstrap | { id?: string }>(path, body);
      if (this.disposed) return false;
      // Shutdown must be acknowledged; a failed request must not claim it saved.
      if (path === '/api/server/stop') {
        this.closed = true;
        clearTimeout(this.timer);
      } else {
        const bootstrap = 'state' in result ? result : await this.api<Bootstrap>('/api/bootstrap');
        this.bootstrap = bootstrap;
        this.connectionError = '';
        if (this.live)
          this.live = {
            ...this.live,
            state: bootstrap.state,
            error: bootstrap.error,
            kinds: bootstrap.kinds,
            thresholds: bootstrap.thresholds,
            active_segments: bootstrap.active_segments,
          };
        this.toaster.success({
          title: success,
          description: 'id' in result && result.id ? `ID: ${result.id}` : undefined,
        });
      }
      return true;
    } catch (cause) {
      if (!this.disposed) this.error = this.message(cause);
      return false;
    } finally {
      this.busy = false;
      ++this.epoch;
    }
  }
  start() {
    return this.command(
      '/api/capture/start',
      {
        capture_id: this.captureId,
        attributes: this.attributes,
        thresholds: this.thresholds,
        health_log_enabled: this.healthLogEnabled,
      },
      'Capture started',
    );
  }
  startPolling() {
    const tick = async () => {
      await this.poll();
      if (!this.disposed && !this.closed) this.timer = setTimeout(tick, 250);
    };
    void tick();
  }
  private async poll() {
    if (this.busy || !this.bootstrap || !['recording', 'starting', 'stopping'].includes(this.phase))
      return;
    const epoch = this.epoch;
    try {
      const update = await this.api<Live>('/api/live', { cursors: this.cursors });
      if (this.disposed || epoch !== this.epoch) return;
      this.connectionError = '';
      this.live = update;
      const next = { ...this.traces };
      const overruns = new Set(this.overrunStreams);
      for (const [id, batch] of Object.entries(update.batches)) {
        this.cursors[id] = batch.end_index;
        if (batch.overrun) overruns.add(id);
        const previous = batch.overrun
          ? { timestamps: [], samples: [] }
          : (next[id] ?? { timestamps: [], samples: [] });
        const rate = this.streams.find((stream) => stream.stream_id === id)?.nominal_rate_hz ?? 1;
        const limit = Math.max(1, Math.ceil(rate * 10));
        next[id] = {
          timestamps: [...previous.timestamps, ...batch.timestamps].slice(-limit),
          samples: [...previous.samples, ...batch.samples].slice(-limit),
        };
      }
      this.traces = next;
      this.overrunStreams = [...overruns];
    } catch (cause) {
      if (!this.disposed && epoch === this.epoch) this.connectionError = this.message(cause);
    }
  }
  dispose() {
    this.disposed = true;
    clearTimeout(this.timer);
    this.abort.abort();
    this.toaster.dismiss();
  }
}
