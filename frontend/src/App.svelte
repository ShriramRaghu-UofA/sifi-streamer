<script lang="ts">
  import { onMount } from 'svelte';
  import Plot from './Plot.svelte';

  type Thresholds = {
    window_seconds: number;
    stale_after_seconds: number | null;
    minimum_rate_ratio: number | null;
    maximum_rate_ratio: number | null;
    maximum_missing_fraction: number | null;
    maximum_lost_samples: number | null;
  };
  type Kind = {
    target: 'marker' | 'segment';
    kind: string;
    label: string | null;
    color: string | null;
    id_prefix: string | null;
    separator: string;
    padding: number;
    start: number;
    default_attributes: Record<string, string | number | boolean | null>;
  };
  type Stream = {
    stream_id: string;
    label: string | null;
    channels: string[];
    channel_labels: (string | null)[];
    channel_units: (string | null)[];
    nominal_rate_hz: number;
    n_samples: number;
  };
  type Bootstrap = {
    state: string;
    error: string | null;
    output: string;
    configuration: Record<string, unknown>;
    default_capture_id: string;
    default_attributes: Record<string, unknown>;
    thresholds: Thresholds;
    health_log_enabled: boolean;
    kinds: Kind[];
    active_segments: string[];
    streams: Stream[];
  };
  type Live = {
    state: string;
    error: string | null;
    health: any;
    events: any[];
    batches: Record<string, { end_index: number; timestamps: number[]; samples: (number | null)[][]; overrun: boolean }>;
    active_segments: string[];
    thresholds: Thresholds;
    kinds: Kind[];
  };

  const token = location.hash.slice(1);
  let bootstrap = $state.raw<Bootstrap | null>(null);
  let live = $state.raw<Live | null>(null);
  let error = $state('');
  let busy = $state(false);
  let captureId = $state('capture');
  let attributesText = $state('{}');
  let healthLogEnabled = $state(true);
  let thresholds = $state<Thresholds>({
    window_seconds: 5,
    stale_after_seconds: 2,
    minimum_rate_ratio: 0.9,
    maximum_rate_ratio: 1.1,
    maximum_missing_fraction: 0,
    maximum_lost_samples: 0
  });
  let cursors = $state<Record<string, number>>({});
  let traces = $state.raw<Record<string, { timestamps: number[]; samples: (number | null)[][] }>>({});
  let newKind = $state<Kind>({ target: 'marker', kind: '', label: null, color: '#42d3ff', id_prefix: null, separator: '_', padding: 2, start: 1, default_attributes: {} });
  let newKindDefaults = $state('{}');
  let kindAttributes = $state<Record<string, string>>({});

  let phase = $derived(live?.state ?? bootstrap?.state ?? 'loading');
  let streams = $derived(bootstrap?.streams ?? []);
  let kinds = $derived(live?.kinds ?? bootstrap?.kinds ?? []);

  async function api<T>(path: string, body?: unknown): Promise<T> {
    const response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'X-SiFi-Session-Token': token, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      body: body === undefined ? undefined : JSON.stringify(body)
    });
    const value = await response.json();
    if (!response.ok) throw new Error(value.error ?? response.statusText);
    return value;
  }

  async function initialize() {
    try {
      bootstrap = await api<Bootstrap>('/api/bootstrap');
      captureId = bootstrap.default_capture_id;
      attributesText = JSON.stringify(bootstrap.default_attributes, null, 2);
      thresholds = { ...bootstrap.thresholds };
      healthLogEnabled = bootstrap.health_log_enabled;
    } catch (cause) {
      error = String(cause);
    }
  }

  async function command(path: string, body: unknown = {}) {
    busy = true;
    error = '';
    try {
      await api(path, body);
      bootstrap = await api('/api/bootstrap');
    } catch (cause) {
      error = String(cause);
    } finally {
      busy = false;
    }
  }

  async function startCapture() {
    await command('/api/capture/start', {
      capture_id: captureId,
      attributes: JSON.parse(attributesText),
      thresholds,
      health_log_enabled: healthLogEnabled
    });
  }

  async function poll() {
    if (!bootstrap || phase === 'setup' || phase === 'loading') return;
    try {
      const update = await api<Live>('/api/live', { cursors });
      live = update;
      const next = { ...traces };
      for (const [streamId, batch] of Object.entries(update.batches)) {
        cursors[streamId] = batch.end_index;
        const prior = batch.overrun ? { timestamps: [], samples: [] } : (next[streamId] ?? { timestamps: [], samples: [] });
        const stream = streams.find((item) => item.stream_id === streamId);
        const limit = Math.round((stream?.nominal_rate_hz ?? 1) * 10);
        next[streamId] = {
          timestamps: [...prior.timestamps, ...batch.timestamps].slice(-limit),
          samples: [...prior.samples, ...batch.samples].slice(-limit)
        };
      }
      traces = next;
    } catch (cause) {
      error = String(cause);
    }
  }

  async function emit(kind: Kind) {
    const text = kindAttributes[`${kind.target}:${kind.kind}`] ?? '{}';
    await command(kind.target === 'marker' ? '/api/marker' : '/api/segment/start', {
      kind: kind.kind,
      attributes: JSON.parse(text)
    });
  }

  async function saveKind() {
    await command('/api/kinds/set', { ...newKind, label: newKind.label || null, id_prefix: newKind.id_prefix || null, default_attributes: JSON.parse(newKindDefaults) });
    newKind = { ...newKind, kind: '', label: null, id_prefix: null };
  }

  onMount(() => {
    initialize();
    const timer = window.setInterval(poll, 250);
    return () => window.clearInterval(timer);
  });
</script>

<svelte:head><title>Capture monitor</title></svelte:head>

<header class="navbar sticky top-0 z-10 border-b border-base-content/10 bg-base-100/95 px-4 shadow-sm backdrop-blur">
  <div class="flex-1"><div><span class="text-info text-xs font-bold tracking-[.18em]">LOCAL ACQUISITION</span><h1 class="text-xl font-semibold">Capture monitor</h1></div></div>
  <div class={['badge gap-2', phase === 'recording' && 'badge-success', phase === 'failed' && 'badge-error']}><span class={['status', phase === 'recording' && 'status-success', phase === 'failed' && 'status-error']}></span>{phase.replace('_', ' ')}</div>
</header>

{#if error || live?.error}
  <div class="alert alert-error mx-auto mt-4 max-w-[1440px]">{error || live?.error}</div>
{/if}

{#if bootstrap}
  <main class="mx-auto grid max-w-[1440px] gap-4 p-4">
    <section class="card gap-3 bg-base-100 p-5 shadow-sm">
      <h2 class="card-title">Session</h2>
      <div class="grid gap-1"><span class="text-xs uppercase opacity-60">Output</span><code>{bootstrap.output}</code></div>
      <div class="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
        {#each Object.entries(bootstrap.configuration) as [key, value] (key)}
          <div class="grid gap-1"><span class="text-xs uppercase opacity-60">{key.replaceAll('_', ' ')}</span><strong>{String(value)}</strong></div>
        {/each}
      </div>
      {#if phase === 'setup'}
        <label class="form-control gap-1 text-sm">Capture ID<input class="input input-bordered" bind:value={captureId} /></label>
        <label class="form-control gap-1 text-sm">Scalar attributes (JSON)<textarea class="textarea textarea-bordered font-mono" rows="4" bind:value={attributesText}></textarea></label>
        <label class="label cursor-pointer justify-start gap-3"><input class="checkbox checkbox-sm" type="checkbox" bind:checked={healthLogEnabled} /> Write health sidecar</label>
        <button class="btn btn-primary" disabled={busy} onclick={startCapture}>Start capture</button>
      {:else if phase === 'recording'}
        <button class="btn btn-error" disabled={busy} onclick={() => command('/api/capture/stop')}>Stop capture</button>
      {:else}
        <button class="btn" onclick={() => command('/api/server/stop')}>Exit dashboard</button>
      {/if}
    </section>

    <section class="card bg-base-100 p-5 shadow-sm">
      <div class="flex items-center justify-between gap-4"><h2 class="card-title">Health thresholds</h2><button class="btn btn-sm" disabled={busy} onclick={() => command('/api/thresholds', thresholds)}>Apply</button></div>
      <div class="mt-3 grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-3">
        <label class="form-control gap-1 text-sm">Window (s)<input class="input input-bordered input-sm" type="number" min="1" bind:value={thresholds.window_seconds} /></label>
        <label class="form-control gap-1 text-sm">Stale after (s)<input class="input input-bordered input-sm" type="number" min="0.1" step="0.1" bind:value={thresholds.stale_after_seconds} /></label>
        <label class="form-control gap-1 text-sm">Minimum rate ratio<input class="input input-bordered input-sm" type="number" min="0" step="0.01" bind:value={thresholds.minimum_rate_ratio} /></label>
        <label class="form-control gap-1 text-sm">Maximum rate ratio<input class="input input-bordered input-sm" type="number" min="0" step="0.01" bind:value={thresholds.maximum_rate_ratio} /></label>
        <label class="form-control gap-1 text-sm">Missing fraction<input class="input input-bordered input-sm" type="number" min="0" max="1" step="0.001" bind:value={thresholds.maximum_missing_fraction} /></label>
        <label class="form-control gap-1 text-sm">Lost samples<input class="input input-bordered input-sm" type="number" min="0" bind:value={thresholds.maximum_lost_samples} /></label>
      </div>
    </section>

    {#if phase !== 'setup'}
      <section class="grid grid-cols-[repeat(auto-fit,minmax(230px,1fr))] gap-4">
        {#each live?.health?.streams ?? [] as health (health.stream_id)}
          <article class={['card border-l-4 bg-base-100 p-4 shadow-sm', health.severity === 'warning' ? 'border-warning' : 'border-success']}>
            <div><h3>{health.stream_id}</h3><span>{health.severity}</span></div>
            <dl>
              <dt>Nominal</dt><dd>{health.nominal_rate_hz.toFixed(1)} Hz</dd>
              <dt>Observed</dt><dd>{health.observed_rate_hz?.toFixed(1) ?? 'warming'} Hz</dd>
              <dt>Source</dt><dd>{health.source_rate_hz?.toFixed(1) ?? '—'} Hz</dd>
              <dt>Missing</dt><dd>{(health.missing_fraction * 100).toFixed(3)}%</dd>
              <dt>Lost</dt><dd>{health.lost_samples}</dd>
            </dl>
            {#if health.warnings.length}<p>{health.warnings.join(' · ')}</p>{/if}
          </article>
        {/each}
      </section>

      <section class="grid grid-cols-[repeat(auto-fit,minmax(min(100%,540px),1fr))] gap-4">
        {#each streams as stream (stream.stream_id)}
          <div class="card overflow-hidden bg-base-100 p-3 shadow-sm">
            <Plot {stream} timestamps={traces[stream.stream_id]?.timestamps ?? []} samples={traces[stream.stream_id]?.samples ?? []} />
          </div>
        {/each}
      </section>
    {/if}

    <section class="card bg-base-100 p-5 shadow-sm">
      <div class="flex items-center justify-between gap-4"><h2 class="card-title">Annotation kinds</h2><span class="text-sm opacity-60">IDs are generated by Python</span></div>
      <div class="my-4 grid grid-cols-[repeat(auto-fit,minmax(230px,1fr))] gap-3">
        {#each kinds as kind (`${kind.target}:${kind.kind}`)}
          <article class="card gap-2 border-l-4 bg-base-200 p-4" style:border-left-color={kind.color ?? '#42d3ff'}>
            <div><span>{kind.target}</span><h3>{kind.label ?? kind.kind}</h3><code>{kind.id_prefix ?? kind.kind}{kind.separator}{String(kind.start).padStart(kind.padding, '0')}</code></div>
            <textarea class="textarea textarea-bordered font-mono" rows="2" placeholder="attributes JSON" bind:value={kindAttributes[`${kind.target}:${kind.kind}`]}></textarea>
            <div class="flex gap-2"><button class="btn btn-sm flex-1" disabled={phase !== 'recording' || busy} onclick={() => emit(kind)}>{kind.target === 'marker' ? 'Add marker' : 'Start segment'}</button><button class="btn btn-ghost btn-sm" disabled={busy} onclick={() => command('/api/kinds/remove', { target: kind.target, kind: kind.kind })}>Remove</button></div>
          </article>
        {/each}
      </div>
      <div class="grid grid-cols-[110px_repeat(3,minmax(120px,1fr))_55px] gap-2 max-md:grid-cols-2">
        <select class="select select-bordered" bind:value={newKind.target}><option value="marker">Marker</option><option value="segment">Segment</option></select>
        <input class="input input-bordered" placeholder="Kind" bind:value={newKind.kind} />
        <input class="input input-bordered" placeholder="Display label" bind:value={newKind.label} />
        <input class="input input-bordered" placeholder="ID prefix (defaults to kind)" bind:value={newKind.id_prefix} />
        <input class="input input-bordered p-1" type="color" bind:value={newKind.color} />
        <input class="input input-bordered" placeholder="Separator" bind:value={newKind.separator} />
        <label class="form-control gap-1 text-xs">Padding<input class="input input-bordered" type="number" min="1" max="9" bind:value={newKind.padding} /></label>
        <label class="form-control gap-1 text-xs">Start<input class="input input-bordered" type="number" min="0" bind:value={newKind.start} /></label>
        <input class="input input-bordered font-mono md:col-span-2" placeholder="Default attributes JSON" bind:value={newKindDefaults} />
        <button class="btn md:col-span-2" disabled={!newKind.kind || busy} onclick={saveKind}>Add / update kind</button>
      </div>
    </section>

    {#if live?.active_segments.length}
      <section class="card bg-base-100 p-5 shadow-sm">
        <h2 class="card-title">Active segments</h2>
        <ol class="grid gap-2">
          {#each live.active_segments as segment, index (segment)}
            <li class="flex items-center justify-between"><code>{segment}</code>{#if index === live.active_segments.length - 1}<button class="btn btn-sm" onclick={() => command('/api/segment/stop', { id: segment })}>Close</button>{/if}</li>
          {/each}
        </ol>
      </section>
    {/if}

    {#if live?.events.length}
      <section class="card bg-base-100 p-5 shadow-sm"><h2 class="card-title">Health events</h2><ul class="max-h-64 overflow-auto">{#each live.events.slice(-30).reverse() as event (event.sequence)}<li class="border-b border-base-content/10 py-2 text-sm"><span class={['badge mr-2', event.active ? 'badge-warning' : 'badge-success']}>{event.active ? 'WARN' : 'OK'}</span>{event.stream_id ?? 'acquisition'} — {event.message}</li>{/each}</ul></section>
    {/if}
  </main>
{/if}
