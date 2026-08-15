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
  type Theme = 'dracula' | 'nord' | 'light';

  const token = location.hash.slice(1);
  let bootstrap = $state.raw<Bootstrap | null>(null);
  let live = $state.raw<Live | null>(null);
  let error = $state('');
  let notice = $state('');
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
  let theme = $state<Theme>('dracula');
  let shuttingDown = $state(false);
  let dashboardClosed = $state(false);
  let closeBlocked = $state(false);

  let phase = $derived(dashboardClosed ? 'closed' : (live?.state ?? bootstrap?.state ?? 'loading'));
  let streams = $derived(bootstrap?.streams ?? []);
  let kinds = $derived(live?.kinds ?? bootstrap?.kinds ?? []);

  function applyTheme(value: Theme) {
    theme = value;
    document.documentElement.dataset.theme = value;
    localStorage.setItem('sifi-theme', value);
  }

  function parseAttributes(text: string, label: string): Record<string, unknown> | null {
    try {
      const value = JSON.parse(text);
      if (value === null || Array.isArray(value) || typeof value !== 'object') {
        throw new Error('must be a JSON object');
      }
      return value;
    } catch (cause) {
      error = `${label}: ${cause instanceof Error ? cause.message : String(cause)}`;
      return null;
    }
  }

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

  async function command(path: string, body: unknown = {}, success = 'Changes saved') {
    busy = true;
    error = '';
    notice = '';
    try {
      await api(path, body);
      bootstrap = await api('/api/bootstrap');
      notice = success;
    } catch (cause) {
      error = String(cause);
    } finally {
      busy = false;
    }
  }

  async function stopCapture() {
    busy = true;
    error = '';
    notice = '';
    try {
      bootstrap = await api<Bootstrap>('/api/capture/stop', {});
      live = null;
      notice = 'Capture stopped and flushed';
    } catch (cause) {
      error = String(cause);
    } finally {
      busy = false;
    }
  }

  async function exitDashboard() {
    busy = true;
    error = '';
    notice = '';
    shuttingDown = true;
    try {
      await api('/api/server/stop', {});
    } catch {
      // The local server may finish shutting down before the browser receives
      // its response. That disconnect is expected after an explicit exit.
    } finally {
      dashboardClosed = true;
      busy = false;
    }
  }

  function closeTab() {
    window.close();
    window.setTimeout(() => closeBlocked = true, 150);
  }

  async function startCapture() {
    const attributes = parseAttributes(attributesText, 'Capture metadata');
    if (!attributes) return;
    await command('/api/capture/start', {
      capture_id: captureId,
      attributes,
      thresholds,
      health_log_enabled: healthLogEnabled
    }, 'Capture started');
  }

  async function poll() {
    if (!bootstrap || shuttingDown || dashboardClosed || phase === 'setup' || phase === 'loading' || phase === 'stopped') return;
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
    const attributes = parseAttributes(text, `${kind.label ?? kind.kind} metadata`);
    if (!attributes) return;
    await command(kind.target === 'marker' ? '/api/marker' : '/api/segment/start', {
      kind: kind.kind,
      attributes
    }, kind.target === 'marker' ? `${kind.label ?? kind.kind} marker added` : `${kind.label ?? kind.kind} segment started`);
  }

  async function saveKind() {
    const defaultAttributes = parseAttributes(newKindDefaults, 'Default metadata');
    if (!defaultAttributes) return;
    await command('/api/kinds/set', { ...newKind, label: newKind.label || null, id_prefix: newKind.id_prefix || null, default_attributes: defaultAttributes }, 'Annotation kind saved');
    newKind = { ...newKind, kind: '', label: null, id_prefix: null };
  }

  onMount(() => {
    const savedTheme = localStorage.getItem('sifi-theme');
    applyTheme(savedTheme === 'light' || savedTheme === 'nord' ? savedTheme : 'dracula');
    initialize();
    const timer = window.setInterval(poll, 250);
    return () => window.clearInterval(timer);
  });
</script>

<svelte:head><title>Capture monitor</title></svelte:head>

<header class="sticky top-0 z-20 border-b border-base-content/10 bg-base-100/85 backdrop-blur-xl">
  <div class="navbar mx-auto max-w-[1500px] gap-4 px-4 py-2 sm:px-6">
    <div class="min-w-0 flex-1">
      <p class="eyebrow">Local acquisition</p>
      <h1 class="truncate text-xl font-semibold tracking-tight">Capture monitor</h1>
    </div>
    <label class="flex items-center gap-2 text-sm">
      <span class="hidden text-base-content/60 sm:inline">Theme</span>
      <select class="select select-sm select-bordered" aria-label="Color theme" value={theme} onchange={(event) => applyTheme(event.currentTarget.value as Theme)}>
        <option value="dracula">Dracula</option>
        <option value="nord">Nord</option>
        <option value="light">Light</option>
      </select>
    </label>
    <div class={['badge badge-lg gap-2 font-medium capitalize', phase === 'recording' && 'badge-success', phase === 'failed' && 'badge-error']}>
      <span class={['status', phase === 'recording' && 'status-success', phase === 'failed' && 'status-error']}></span>{phase.replace('_', ' ')}
    </div>
  </div>
</header>

<div class="pointer-events-none fixed inset-x-0 top-20 z-30 mx-auto grid max-w-2xl gap-2 px-4" aria-live="polite">
  {#if error || live?.error}<div class="alert alert-error pointer-events-auto shadow-lg"><span>{error || live?.error}</span><button class="btn btn-ghost btn-sm" aria-label="Dismiss error" onclick={() => error = ''}>Dismiss</button></div>{/if}
  {#if notice}<div class="alert alert-success pointer-events-auto shadow-lg"><span>{notice}</span><button class="btn btn-ghost btn-sm" aria-label="Dismiss message" onclick={() => notice = ''}>Dismiss</button></div>{/if}
</div>

{#if bootstrap}
  <main class="mx-auto grid max-w-[1500px] gap-6 p-4 sm:p-6 lg:p-8">
    <section class="surface-card overflow-hidden">
      <div class="grid lg:grid-cols-[minmax(0,1.35fr)_minmax(300px,.65fr)]">
        <div class="p-5 sm:p-7">
          <p class="eyebrow">Session setup</p>
          <h2 class="mt-1 text-2xl font-semibold tracking-tight">{phase === 'setup' ? 'Ready for a new capture' : 'Capture session'}</h2>
          <p class="mt-2 max-w-2xl text-sm leading-relaxed text-base-content/65">Name this recording and attach a few simple facts that will help you identify or filter it later.</p>

          {#if phase === 'setup'}
            <div class="mt-6 grid gap-5">
              <label>
                <span class="field-label">Capture ID</span>
                <input class="input input-bordered w-full" bind:value={captureId} placeholder="e.g. pilot-session-01" />
                <span class="field-help">A short, unique name for this recording. It is stored inside the capture file.</span>
              </label>
              <label>
                <span class="field-label">Capture metadata <span class="font-normal text-base-content/50">(JSON)</span></span>
                <textarea class="textarea textarea-bordered min-h-28 w-full font-mono text-sm" rows="4" spellcheck="false" bind:value={attributesText} placeholder={'{\n  "operator": "Sam",\n  "condition": "rest"\n}'}></textarea>
                <span class="field-help">Optional searchable facts about the whole capture. Enter a JSON object using simple values only: text, numbers, true/false, or null. Example: <code>{'{"operator":"Sam","session":1}'}</code>. Lists and nested objects are not accepted.</span>
              </label>
              <label class="flex cursor-pointer items-start gap-3 rounded-xl border border-base-content/10 bg-base-200/60 p-4">
                <input class="checkbox checkbox-sm mt-0.5" type="checkbox" bind:checked={healthLogEnabled} />
                <span><strong class="block text-sm">Write a health log</strong><span class="field-help mt-0 block">Saves a separate diagnostic file with signal rates, missing data, and warnings. It does not change the capture itself.</span></span>
              </label>
              <button class="btn btn-primary btn-lg justify-self-start px-8" disabled={busy || !captureId.trim()} onclick={startCapture}>{busy ? 'Starting…' : 'Start capture'}</button>
            </div>
          {:else}
            <div class="mt-6 flex flex-wrap gap-3">
              {#if phase === 'recording'}
                <button class="btn btn-error" disabled={busy} onclick={stopCapture}>Stop and save capture</button>
              {:else}
                <button class="btn" disabled={busy} onclick={exitDashboard}>{busy ? 'Exiting…' : 'Exit dashboard'}</button>
              {/if}
            </div>
          {/if}
        </div>

        <aside class="border-t border-base-content/10 bg-base-200/50 p-5 sm:p-7 lg:border-l lg:border-t-0">
          <p class="eyebrow">Destination</p>
          <code class="mt-2 block rounded-lg bg-base-300/70 p-3 text-xs leading-relaxed">{bootstrap.output}</code>
          <details class="group mt-5 rounded-xl border border-base-content/10 bg-base-100/55">
            <summary class="flex cursor-pointer list-none items-center justify-between gap-3 p-4">
              <span><span class="block text-sm font-semibold">Device configuration</span><span class="mt-0.5 block text-xs text-base-content/55">{Object.keys(bootstrap.configuration).length} settings</span></span>
              <span class="text-lg transition-transform group-open:rotate-45" aria-hidden="true">+</span>
            </summary>
            <dl class="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-x-5 gap-y-4 border-t border-base-content/10 p-4">
              {#each Object.entries(bootstrap.configuration) as [key, value] (key)}
                <div class="min-w-0"><dt class="text-xs font-semibold uppercase tracking-wider text-base-content/50">{key.replaceAll('_', ' ')}</dt><dd class="mt-1 break-words text-sm font-medium">{String(value)}</dd></div>
              {/each}
            </dl>
          </details>
        </aside>
      </div>
    </section>

    <details class="surface-card group" open={phase === 'setup'}>
      <summary class="flex cursor-pointer list-none items-center justify-between gap-4 p-5 sm:px-7">
        <span><span class="block text-lg font-semibold">Signal health rules</span><span class="mt-1 block text-sm text-base-content/60">Choose when the dashboard should warn about stale, slow, fast, missing, or lost samples.</span></span>
        <span class="text-xl transition-transform group-open:rotate-45">+</span>
      </summary>
      <div class="border-t border-base-content/10 p-5 sm:p-7">
        <div class="grid grid-cols-[repeat(auto-fit,minmax(190px,1fr))] gap-4">
          <label><span class="field-label">Measurement window</span><input class="input input-bordered input-sm w-full" type="number" min="1" bind:value={thresholds.window_seconds} /><span class="field-help">Seconds used to calculate the observed sample rate.</span></label>
          <label><span class="field-label">Stale after</span><input class="input input-bordered input-sm w-full" type="number" min="0.1" step="0.1" bind:value={thresholds.stale_after_seconds} /><span class="field-help">Warn after this many seconds without samples.</span></label>
          <label><span class="field-label">Minimum rate</span><input class="input input-bordered input-sm w-full" type="number" min="0" step="0.01" bind:value={thresholds.minimum_rate_ratio} /><span class="field-help">Fraction of expected rate; 0.90 means 90%.</span></label>
          <label><span class="field-label">Maximum rate</span><input class="input input-bordered input-sm w-full" type="number" min="0" step="0.01" bind:value={thresholds.maximum_rate_ratio} /><span class="field-help">Fraction of expected rate; 1.10 means 110%.</span></label>
          <label><span class="field-label">Maximum missing</span><input class="input input-bordered input-sm w-full" type="number" min="0" max="1" step="0.001" bind:value={thresholds.maximum_missing_fraction} /><span class="field-help">Allowed missing fraction; 0.01 means 1%.</span></label>
          <label><span class="field-label">Maximum lost samples</span><input class="input input-bordered input-sm w-full" type="number" min="0" bind:value={thresholds.maximum_lost_samples} /><span class="field-help">Allowed device-reported lost samples before warning.</span></label>
        </div>
        <button class="btn btn-sm mt-5" disabled={busy} onclick={() => command('/api/thresholds', thresholds, 'Signal health rules applied')}>Apply health rules</button>
      </div>
    </details>

    {#if phase !== 'setup'}
      <section class="grid grid-cols-[repeat(auto-fit,minmax(230px,1fr))] gap-4">
        {#each live?.health?.streams ?? [] as health (health.stream_id)}
          <article class={['surface-card border-l-4 p-5', health.severity === 'warning' ? 'border-warning' : 'border-success']}>
            <div class="mb-4 flex items-center justify-between gap-3"><h3 class="font-semibold">{health.stream_id}</h3><span class={['badge badge-sm capitalize', health.severity === 'warning' ? 'badge-warning' : 'badge-success']}>{health.severity}</span></div>
            <dl class="metric-grid">
              <dt>Nominal</dt><dd>{health.nominal_rate_hz.toFixed(1)} Hz</dd>
              <dt>Observed</dt><dd>{health.observed_rate_hz?.toFixed(1) ?? 'warming'} Hz</dd>
              <dt>Source</dt><dd>{health.source_rate_hz?.toFixed(1) ?? '—'} Hz</dd>
              <dt>Missing</dt><dd>{(health.missing_fraction * 100).toFixed(3)}%</dd>
              <dt>Lost</dt><dd>{health.lost_samples}</dd>
            </dl>
            {#if health.warnings.length}<p class="mt-4 rounded-lg bg-warning/15 p-3 text-sm text-warning-content">{health.warnings.join(' · ')}</p>{/if}
          </article>
        {/each}
      </section>

      <section class="grid grid-cols-[repeat(auto-fit,minmax(min(100%,540px),1fr))] gap-4">
        {#each streams as stream (stream.stream_id)}
          <div class="surface-card overflow-hidden p-4">
            <Plot {stream} timestamps={traces[stream.stream_id]?.timestamps ?? []} samples={traces[stream.stream_id]?.samples ?? []} />
          </div>
        {/each}
      </section>
    {/if}

    <section class="surface-card p-5 sm:p-7">
      <div><p class="eyebrow">During capture</p><h2 class="mt-1 text-xl font-semibold">Markers and segments</h2><p class="mt-2 max-w-3xl text-sm leading-relaxed text-base-content/60">A marker records one moment; a segment records a duration with a start and stop. IDs are generated automatically so each occurrence stays unique.</p></div>
      <div class="my-4 grid grid-cols-[repeat(auto-fit,minmax(230px,1fr))] gap-3">
        {#each kinds as kind (`${kind.target}:${kind.kind}`)}
          <article class="rounded-xl border border-base-content/10 border-l-4 bg-base-200/60 p-4" style:border-left-color={kind.color ?? '#42d3ff'}>
            <div class="mb-3"><span class="badge badge-ghost badge-sm capitalize">{kind.target}</span><h3 class="mt-2 font-semibold">{kind.label ?? kind.kind}</h3><code class="text-xs">Next ID: {kind.id_prefix ?? kind.kind}{kind.separator}{String(kind.start).padStart(kind.padding, '0')}</code></div>
            <label><span class="field-label">Metadata for this occurrence <span class="font-normal text-base-content/50">(JSON)</span></span><textarea class="textarea textarea-bordered w-full font-mono text-sm" rows="2" spellcheck="false" placeholder={'{"quality":"good"}'} bind:value={kindAttributes[`${kind.target}:${kind.kind}`]}></textarea><span class="field-help">Optional simple key/value facts. Use <code>{'{}'}</code> when there is nothing to add.</span></label>
            <div class="mt-3 flex gap-2"><button class="btn btn-primary btn-sm flex-1" disabled={phase !== 'recording' || busy} onclick={() => emit(kind)}>{kind.target === 'marker' ? 'Add marker now' : 'Start segment now'}</button><button class="btn btn-ghost btn-sm" disabled={busy} onclick={() => command('/api/kinds/remove', { target: kind.target, kind: kind.kind }, 'Annotation kind removed')}>Remove</button></div>
          </article>
        {/each}
      </div>
      <details class="mt-5 rounded-xl border border-base-content/10 bg-base-200/30">
        <summary class="cursor-pointer p-4 font-medium">Create or update an annotation kind</summary>
        <div class="grid grid-cols-[110px_repeat(3,minmax(120px,1fr))_55px] gap-3 border-t border-base-content/10 p-4 max-md:grid-cols-2">
        <select class="select select-bordered" bind:value={newKind.target}><option value="marker">Marker</option><option value="segment">Segment</option></select>
        <input class="input input-bordered" placeholder="Kind, e.g. note" aria-label="Stable kind name" bind:value={newKind.kind} />
        <input class="input input-bordered" placeholder="Display label, e.g. Note" aria-label="Display label" bind:value={newKind.label} />
        <input class="input input-bordered" placeholder="ID prefix (defaults to kind)" bind:value={newKind.id_prefix} />
        <input class="input input-bordered p-1" type="color" aria-label="Annotation color" bind:value={newKind.color} />
        <label><span class="field-label">ID separator</span><input class="input input-bordered w-full" placeholder="_" bind:value={newKind.separator} /><span class="field-help">Character between the prefix and number.</span></label>
        <label><span class="field-label">Number width</span><input class="input input-bordered w-full" type="number" min="1" max="9" bind:value={newKind.padding} /><span class="field-help">2 creates IDs such as 01, 02.</span></label>
        <label><span class="field-label">First number</span><input class="input input-bordered w-full" type="number" min="0" bind:value={newKind.start} /><span class="field-help">The initial generated sequence number.</span></label>
        <label class="md:col-span-2"><span class="field-label">Default metadata <span class="font-normal text-base-content/50">(JSON)</span></span><input class="input input-bordered w-full font-mono" placeholder={'{"phase":"baseline"}'} bind:value={newKindDefaults} /><span class="field-help">Simple facts automatically added each time this kind is used. Enter <code>{'{}'}</code> for none.</span></label>
        <button class="btn md:col-span-2" disabled={!newKind.kind || busy} onclick={saveKind}>Add / update kind</button>
        </div>
      </details>
    </section>

    {#if live?.active_segments.length}
      <section class="surface-card p-5 sm:p-7">
        <h2 class="text-xl font-semibold">Active segments</h2><p class="mt-1 text-sm text-base-content/60">Segments close in reverse order; finish the most recently started one first.</p>
        <ol class="grid gap-2">
          {#each live.active_segments as segment, index (segment)}
            <li class="mt-3 flex items-center justify-between rounded-lg bg-base-200/60 p-3"><code>{segment}</code>{#if index === live.active_segments.length - 1}<button class="btn btn-sm" onclick={() => command('/api/segment/stop', { id: segment }, `${segment} closed`)}>Close segment</button>{/if}</li>
          {/each}
        </ol>
      </section>
    {/if}

    {#if live?.events.length}
      <section class="surface-card p-5 sm:p-7"><h2 class="text-xl font-semibold">Health events</h2><p class="mt-1 text-sm text-base-content/60">Changes in signal health detected from the rules above.</p><ul class="mt-3 max-h-64 overflow-auto">{#each live.events.slice(-30).reverse() as event (event.sequence)}<li class="border-b border-base-content/10 py-3 text-sm"><span class={['badge mr-2', event.active ? 'badge-warning' : 'badge-success']}>{event.active ? 'WARN' : 'OK'}</span>{event.stream_id ?? 'acquisition'} — {event.message}</li>{/each}</ul></section>
    {/if}
  </main>
{/if}

{#if dashboardClosed}
  <div class="fixed inset-0 z-50 grid place-items-center bg-base-300/75 p-4 backdrop-blur-sm" role="dialog" aria-modal="true" aria-labelledby="shutdown-title">
    <section class="surface-card w-full max-w-md p-6 text-center sm:p-8">
      <div class="mx-auto grid size-12 place-items-center rounded-full bg-success/15 text-2xl text-success" aria-hidden="true">✓</div>
      <h2 id="shutdown-title" class="mt-4 text-2xl font-semibold">Dashboard closed</h2>
      <p class="mt-2 text-sm leading-relaxed text-base-content/65">The local capture server has shut down. Your capture is saved and it is safe to close this tab.</p>
      <button class="btn btn-primary mt-6" onclick={closeTab}>Close this tab</button>
      {#if closeBlocked}<p class="mt-3 text-xs text-base-content/55">Your browser prevented automatic closing. You can close this tab normally.</p>{/if}
    </section>
  </div>
{/if}
