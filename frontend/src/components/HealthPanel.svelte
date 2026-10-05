<script lang="ts">
  import { Accordion } from '@skeletonlabs/skeleton-svelte';
  import { SlidersHorizontal, ChevronDown, CircleCheck, TriangleAlert, Save } from '@lucide/svelte';
  import type { Dashboard } from '../lib/dashboard.svelte.js';
  import type { Thresholds } from '../lib/types';
  import { formatRate, title, thresholdsValid } from '../lib/types';
  let { dashboard } = $props<{ dashboard: Dashboard }>();
  const rules: { key: keyof Thresholds; label: string; hint: string; step: string; min: number }[] =
    [
      {
        key: 'window_seconds',
        label: 'Evaluation window (s)',
        hint: 'Duration used to estimate live rates.',
        step: '0.1',
        min: 0.1,
      },
      {
        key: 'stale_after_seconds',
        label: 'Stale after (s)',
        hint: 'Warn if no packet arrives in this time.',
        step: '0.1',
        min: 0.1,
      },
      {
        key: 'minimum_rate_ratio',
        label: 'Minimum rate ratio',
        hint: '0.9 means 90% of the nominal rate.',
        step: '0.01',
        min: 0,
      },
      {
        key: 'maximum_rate_ratio',
        label: 'Maximum rate ratio',
        hint: '1.1 means 110% of the nominal rate.',
        step: '0.01',
        min: 0,
      },
      {
        key: 'maximum_missing_fraction',
        label: 'Maximum missing fraction',
        hint: '0.01 allows 1% missing values.',
        step: '0.01',
        min: 0,
      },
      {
        key: 'maximum_lost_samples',
        label: 'Maximum lost samples',
        hint: 'Vendor-reported sample loss per window.',
        step: '1',
        min: 0,
      },
    ];
  let health = $derived(dashboard.live?.health);
  let rulesValid = $derived(thresholdsValid(dashboard.thresholds));
  function setRule(key: keyof Thresholds, text: string) {
    const value = text === '' ? null : Number(text);
    if (key === 'window_seconds') dashboard.thresholds.window_seconds = value ?? 0;
    else dashboard.thresholds[key] = value;
  }
  function save(event: SubmitEvent) {
    event.preventDefault();
    if (rulesValid)
      void dashboard.command('/api/thresholds', dashboard.thresholds, 'Health rules saved');
  }
</script>

<section class="space-y-6">
  <div>
    <p class="eyebrow">Quality at a glance</p>
    <h2 class="h3 mt-2">Signal health</h2>
    <p class="muted mt-2 text-sm">
      Nominal is configured. Reported is measured by the bridge. Observed is measured by the
      acquisition worker.
    </p>
  </div>
  <section class="panel space-y-4">
    <h3 class="font-semibold">Stream diagnostics</h3>
    {#if !health}<p class="muted text-sm">Diagnostics appear once capture starts.</p>{:else}
      <div class="overflow-x-auto">
        <table class="table whitespace-nowrap">
          <thead
            ><tr
              ><th>Stream</th><th>Health</th><th>Nominal</th><th>Reported</th><th>Observed</th><th
                >Source</th
              ><th>Missing</th><th>Lost</th><th>Packets</th></tr
            ></thead
          ><tbody
            >{#each health.streams as stream (stream.stream_id)}<tr
                ><td class="font-medium">{title(stream.stream_id)}</td><td
                  ><span
                    class={[
                      'badge',
                      stream.severity === 'healthy'
                        ? 'preset-tonal-success'
                        : 'preset-tonal-warning',
                    ]}>{title(stream.severity)}</span
                  ></td
                ><td>{formatRate(stream.nominal_rate_hz)}</td><td
                  >{formatRate(stream.reported_rate_hz)}</td
                ><td>{formatRate(stream.observed_rate_hz)}</td><td
                  >{formatRate(stream.source_rate_hz)}</td
                ><td>{(stream.missing_fraction * 100).toFixed(2)}%</td><td>{stream.lost_samples}</td
                ><td>{stream.packet_count}</td></tr
              >{/each}</tbody
          >
        </table>
      </div>
      <Accordion multiple collapsible
        >{#each health.streams as stream (stream.stream_id)}<Accordion.Item value={stream.stream_id}
            ><Accordion.ItemTrigger
              ><span>{title(stream.stream_id)} / counters & warnings</span><Accordion.ItemIndicator
                ><ChevronDown size={16} /></Accordion.ItemIndicator
              ></Accordion.ItemTrigger
            ><Accordion.ItemContent
              ><div class="py-3 space-y-3">
                <div class="flex flex-wrap gap-2">
                  {#each stream.warnings as warning (warning)}<span
                      class="badge preset-tonal-warning">{title(warning)}</span
                    >{:else}<span class="badge preset-tonal-success">No active warnings</span
                    >{/each}
                </div>
                <dl class="grid gap-3 text-sm sm:grid-cols-3">
                  <div>
                    <dt class="muted">Samples</dt>
                    <dd>{stream.sample_count}</dd>
                  </div>
                  <div>
                    <dt class="muted">Last packet age</dt>
                    <dd>
                      {stream.last_packet_age_seconds == null
                        ? '—'
                        : `${stream.last_packet_age_seconds.toFixed(2)} s`}
                    </dd>
                  </div>
                  <div>
                    <dt class="muted">Non-OK packets</dt>
                    <dd>{stream.non_ok_packets}</dd>
                  </div>
                  <div>
                    <dt class="muted">Malformed / misaligned</dt>
                    <dd>{stream.malformed_packets} / {stream.misaligned_packets}</dd>
                  </div>
                  <div>
                    <dt class="muted">Timestamp errors</dt>
                    <dd>{stream.timestamp_errors}</dd>
                  </div>
                  <div>
                    <dt class="muted">Missing by channel</dt>
                    <dd>{stream.missing_by_channel.join(', ') || '—'}</dd>
                  </div>
                </dl>
              </div></Accordion.ItemContent
            ></Accordion.Item
          >{/each}</Accordion
      >
    {/if}
  </section>
  <div class="grid gap-6 lg:grid-cols-2">
    <section class="panel">
      <h3 class="font-semibold flex items-center gap-2">
        <SlidersHorizontal size={18} />Health rules
      </h3>
      <p class="field-hint mt-2">
        Change during capture. Leave an optional rule blank to disable it.
      </p>
      <form class="mt-5 space-y-5" onsubmit={save}>
        <div class="grid gap-4 sm:grid-cols-2">
          {#each rules as rule (rule.key)}<label class="field"
              ><span>{rule.label}</span><input
                class="input"
                type="number"
                min={rule.min}
                step={rule.step}
                value={dashboard.thresholds[rule.key] ?? ''}
                required={rule.key === 'window_seconds'}
                oninput={(event) => setRule(rule.key, event.currentTarget.value)}
              /><span class="field-hint">{rule.hint}</span></label
            >{/each}
        </div>
        {#if !rulesValid}<p class="text-error-700-300 text-sm" role="alert">
            Use positive durations, nonnegative ratios, and a whole-number lost-sample count.
          </p>{/if}<button
          class="btn preset-filled-primary-500"
          type="submit"
          disabled={!rulesValid || dashboard.busy || dashboard.closed}
          ><Save size={16} />Save health rules</button
        >
      </form>
    </section>
    <section class="panel">
      <h3 class="font-semibold">Health transitions</h3>
      <p class="field-hint mt-2">Latest warnings and recoveries, newest first.</p>
      <ul class="mt-5 max-h-[34rem] overflow-y-auto divide-y divide-surface-200-800">
        {#each [...(dashboard.live?.events ?? [])]
          .slice(-50)
          .reverse() as event (event.sequence)}<li class="flex gap-3 py-4">
            {#if event.active}<TriangleAlert
                size={17}
                class="mt-0.5 shrink-0 text-warning-700-300"
              />{:else}<CircleCheck size={17} class="mt-0.5 shrink-0 text-success-700-300" />{/if}
            <div>
              <p class="text-sm">{event.message}</p>
              <p class="field-hint mt-1">
                {event.stream_id ?? 'Acquisition'} · {event.active ? 'Warning' : 'Recovered'} · event
                {event.sequence}
              </p>
            </div>
          </li>{:else}<li class="muted text-sm py-5">No health transitions yet.</li>{/each}
      </ul>
    </section>
  </div>
</section>
