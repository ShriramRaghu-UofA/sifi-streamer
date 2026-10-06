<script lang="ts">
  import { Activity, Radio, TriangleAlert } from '@lucide/svelte';
  import { SegmentedControl } from '@skeletonlabs/skeleton-svelte';
  import Plot from '../Plot.svelte';
  import type { Dashboard } from '../lib/dashboard.svelte.js';
  import { formatRate, title, type Stream, type StreamHealth } from '../lib/types';
  let { dashboard, appearance } = $props<{ dashboard: Dashboard; appearance: string }>();
  let layout = $state<string | null>('grid');
  let query = $state('');
  let visible: Stream[] = $derived(
    dashboard.streams.filter((item: Stream) =>
      `${item.label ?? ''} ${item.stream_id}`.toLowerCase().includes(query.toLowerCase()),
    ),
  );
</script>

<section class="space-y-5">
  <div class="flex flex-wrap items-end justify-between gap-4">
    <div>
      <p class="eyebrow">Live acquisition</p>
      <h2 class="h3 mt-2">Signal workspace</h2>
      <p class="muted text-sm mt-2">
        {dashboard.phase === 'recording'
          ? 'A rolling view of the latest samples. Recording continues across every view.'
          : 'The latest received samples. Plots remain available after capture stops.'}
      </p>
    </div>
    <div class="flex items-center gap-3">
      <input
        class="input max-w-48"
        placeholder="Find a stream…"
        aria-label="Find a stream"
        bind:value={query}
      />
      <SegmentedControl
        value={layout}
        onValueChange={({ value }) => (layout = value)}
        aria-label="Plot layout"
        ><SegmentedControl.Control
          ><SegmentedControl.Indicator />
          {#each ['grid', 'stack'] as option (option)}<SegmentedControl.Item value={option}
              ><SegmentedControl.ItemText>{title(option)}</SegmentedControl.ItemText
              ><SegmentedControl.ItemHiddenInput /></SegmentedControl.Item
            >{/each}
        </SegmentedControl.Control></SegmentedControl
      >
    </div>
  </div>
  {#if dashboard.overrunStreams.length}<div
      class="card preset-tonal-warning flex gap-3 p-4 text-sm"
      role="status"
    >
      <TriangleAlert size={18} class="shrink-0" />
      <p>
        Live view fell behind for {dashboard.overrunStreams.join(', ')}. The plot resumed at the
        newest samples. This does not indicate capture-log loss.
      </p>
    </div>{/if}
  {#if !dashboard.streams.length}<div class="panel py-16 flex flex-col items-center text-center">
      <div class="preset-tonal-primary rounded-full p-4"><Radio size={30} /></div>
      <h3 class="h4 mt-5">Your streams will appear here</h3>
      <p class="muted text-sm mt-2 max-w-md">
        Start a capture to connect the device and discover its channels. You can prepare annotation
        kinds and health rules first.
      </p>
    </div>
  {:else if !visible.length}<p class="panel muted">No streams match “{query}”.</p>
  {:else}<div class={['grid gap-5', layout === 'grid' && visible.length > 1 && 'xl:grid-cols-2']}>
      {#each visible as stream (stream.stream_id)}
        {@const health = dashboard.live?.health?.streams.find(
          (item: StreamHealth) => item.stream_id === stream.stream_id,
        )}
        <article class="panel min-w-0 space-y-4">
          <div class="flex items-start justify-between gap-3">
            <div>
              <h3 class="font-semibold flex items-center gap-2">
                <Activity size={17} class="text-primary-700-300" />{stream.label ??
                  title(stream.stream_id)}
              </h3>
              <p class="field-hint mt-1">
                {stream.channels.length} channels · configured {formatRate(stream.nominal_rate_hz)}
              </p>
            </div>
            <span
              class={[
                'badge',
                health?.severity === 'warning' || health?.severity === 'fatal'
                  ? 'preset-tonal-warning'
                  : 'preset-tonal-primary',
              ]}>{title(health?.severity ?? 'warming_up')}</span
            >
          </div>
          {#if dashboard.traces[stream.stream_id]?.timestamps.length}<Plot
              {stream}
              timestamps={dashboard.traces[stream.stream_id]?.timestamps ?? []}
              samples={dashboard.traces[stream.stream_id]?.samples ?? []}
              {appearance}
            />{:else}<div
              class="h-60 flex flex-col items-center justify-center rounded-container bg-surface-200-800 text-center p-4"
            >
              <Radio size={24} class="muted" />
              <p class="font-medium mt-3 text-sm">Waiting for samples</p>
              <p class="field-hint mt-1">This stream has not delivered data yet.</p>
            </div>{/if}
          <dl class="grid grid-cols-3 gap-3 border-t border-surface-200-800 pt-4 text-xs">
            <div>
              <dt class="muted">Packet-reported</dt>
              <dd class="mt-1 font-medium">{formatRate(health?.reported_rate_hz)}</dd>
            </div>
            <div>
              <dt class="muted">Received rate (window estimate)</dt>
              <dd class="mt-1 font-medium">{formatRate(health?.observed_rate_hz)}</dd>
              <dd class="field-hint">
                Sample rows / host elapsed time · rolling
                {dashboard.live?.thresholds.window_seconds ??
                  dashboard.bootstrap?.thresholds.window_seconds} s window (shorter during startup)
              </dd>
            </div>
            <div>
              <dt class="muted">Missing values</dt>
              <dd class="mt-1 font-medium">
                {health ? `${(health.missing_fraction * 100).toFixed(2)}%` : '—'}
              </dd>
            </div>
          </dl>
        </article>
      {/each}
    </div>{/if}
</section>
