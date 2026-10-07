<script lang="ts">
  import { Switch, Accordion } from '@skeletonlabs/skeleton-svelte';
  import { FileCheck2, SlidersHorizontal, ChevronDown } from '@lucide/svelte';
  import MetadataEditor from './MetadataEditor.svelte';
  import type { Dashboard } from '../lib/dashboard.svelte.js';
  import { formatRate, title } from '../lib/types';
  import type { Stream } from '../lib/types';
  let { dashboard, metadataValid = $bindable(true) } = $props<{
    dashboard: Dashboard;
    metadataValid?: boolean;
  }>();
  let editable = $derived(dashboard.phase === 'setup');
  let provenance = $derived(dashboard.live ?? dashboard.bootstrap);
  let configuration = $derived(dashboard.bootstrap?.configuration ?? {});
  let sensors: { id: string; label: string; rate: number; enabled: boolean }[] = $derived.by(() => {
    const configured = [
      { id: 'emg', label: 'EMG', rateKey: 'emg_fs_hz' },
      { id: 'ecg', label: 'ECG', rateKey: 'ecg_fs_hz' },
      { id: 'eda', label: 'EDA', rateKey: 'eda_fs_hz' },
      { id: 'ppg', label: 'PPG', rateKey: 'ppg_effective_rate_hz' },
      { id: 'imu', label: 'IMU', rateKey: 'imu_fs_hz' },
      { id: 'temperature', label: 'Temperature', rateKey: 'temperature_fs_hz' },
    ].filter((sensor) => typeof configuration[sensor.rateKey] === 'number');
    if (configured.length) {
      return configured.map((sensor) => ({
        id: sensor.id,
        label: sensor.label,
        rate: configuration[sensor.rateKey] as number,
        enabled: configuration[`${sensor.id}_enabled`] !== false,
      }));
    }
    return dashboard.streams.map((stream: Stream) => ({
      id: stream.stream_id,
      label: stream.label ?? title(stream.stream_id),
      rate: stream.nominal_rate_hz,
      enabled: true,
    }));
  });
</script>

<div class="grid grid-cols-1 gap-6 lg:grid-cols-2">
  <section class="panel min-w-0 space-y-6">
    <div>
      <p class="eyebrow">01 / Session</p>
      <h2 class="h3 mt-2">{editable ? 'Prepare your capture' : 'Capture details'}</h2>
      <p class="muted mt-2 text-sm">
        Give this recording a name and the context you’ll want later.
      </p>
    </div>
    <label class="field"
      ><span>Capture ID</span><input
        class="input"
        bind:value={dashboard.captureId}
        disabled={!editable || dashboard.busy}
        placeholder="pilot-session-01"
      /><span class="field-hint">One unique occurrence, stored inside the capture.</span></label
    >
    <MetadataEditor
      bind:value={() => dashboard.attributes, (value) => (dashboard.attributes = value)}
      bind:valid={metadataValid}
      disabled={!editable || dashboard.busy}
      label="Session metadata"
    />
    <div class="border-t border-surface-200-800 pt-5">
      <Switch
        checked={dashboard.healthLogEnabled}
        onCheckedChange={({ checked }) => (dashboard.healthLogEnabled = checked)}
        disabled={!editable || dashboard.busy}
      >
        <Switch.Control><Switch.Thumb /></Switch.Control><Switch.HiddenInput />
        <Switch.Label>Save a separate health log</Switch.Label>
      </Switch>
      <p class="field-hint mt-2">
        Diagnostic rates and warnings in a .health.jsonl sidecar. Your capture remains
        authoritative.
      </p>
    </div>
    <div class="flex gap-3 rounded-container bg-surface-200-800 p-4">
      <FileCheck2 size={20} class="shrink-0 text-primary-700-300" />
      <div class="min-w-0">
        <p class="text-sm font-medium">Output file</p>
        <code class="text-xs muted break-all">{dashboard.bootstrap?.output}</code>
        <p class="field-hint mt-1">
          Exclusive creation. Existing recordings are never overwritten.
        </p>
      </div>
    </div>
  </section>
  <section class="panel min-w-0 space-y-5">
    <div>
      <p class="eyebrow">02 / Device</p>
      <h2 class="h3 mt-2">Acquisition configuration</h2>
      <p class="muted mt-2 text-sm">Set by the launcher. Review it before starting.</p>
    </div>
    <dl class="flex flex-wrap gap-x-6 gap-y-2 text-sm">
      {#each ['device', 'transport'] as key (key)}
        {#if configuration[key] != null}
          <div class="flex items-baseline gap-2">
            <dt class="muted">{title(key)}</dt>
            <dd class="font-medium">{configuration[key]}</dd>
          </div>
        {/if}
      {/each}
    </dl>
    <div class="rounded-container border border-surface-200-800 p-4 space-y-3">
      <h3 class="text-sm font-semibold">{editable ? 'Requested connection' : 'Reported device'}</h3>
      {#if Object.keys(provenance?.device_summary ?? {}).length}
        <dl class="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {#each Object.entries(provenance?.device_summary ?? {}) as [key, value] (key)}
            <div class="min-w-0">
              <dt class="field-hint">{key}</dt>
              <dd class="mt-1 text-sm font-medium break-all">{value}</dd>
            </div>
          {/each}
        </dl>
      {:else if editable}
        <p class="text-sm break-all">{configuration.device_handle ?? 'Automatic selection'}</p>
        <p class="field-hint">Device identity will appear after connection.</p>
      {:else}
        <p class="muted text-sm">
          {Object.keys(provenance?.device_info ?? {}).length
            ? 'Device report available in details.'
            : 'No device information reported.'}
        </p>
      {/if}
    </div>
    <div class="space-y-3">
      <h3 class="text-sm font-semibold">Sensor streams</h3>
      <ul class="grid grid-cols-2 gap-3 sm:grid-cols-3" aria-label="Sensor streams">
        {#each sensors as sensor (sensor.id)}
          <li
            class={[
              'min-w-0 rounded-container border p-4',
              sensor.enabled
                ? 'border-primary-200-800 bg-primary-50-950'
                : 'border-surface-200-800 bg-surface-200-800 muted',
            ]}
          >
            <div class="text-sm font-semibold">{sensor.label}</div>
            <p class={['mt-2 text-xl font-semibold tabular-nums', !sensor.enabled && 'opacity-60']}>
              {formatRate(sensor.rate)}
            </p>
            <p class="mt-2 flex items-center gap-2 text-xs">
              <span
                aria-hidden="true"
                class={[
                  'size-1.5 shrink-0 rounded-full',
                  sensor.enabled ? 'bg-primary-500' : 'bg-surface-400-600',
                ]}
              ></span>
              {sensor.enabled ? 'Enabled' : 'Disabled'}
            </p>
          </li>
        {/each}
      </ul>
      {#if sensors.some((sensor) => sensor.id === 'ppg')}
        <p class="field-hint">PPG shows the output rate after averaging.</p>
      {/if}
    </div>
    <Accordion collapsible>
      <Accordion.Item value="configuration"
        ><Accordion.ItemTrigger
          ><SlidersHorizontal size={17} /><span>All sensor settings</span><Accordion.ItemIndicator
            ><ChevronDown size={17} /></Accordion.ItemIndicator
          ></Accordion.ItemTrigger
        >
        <Accordion.ItemContent
          ><dl class="mt-3 divide-y divide-surface-200-800">
            {#each Object.entries(dashboard.bootstrap?.configuration ?? {}) as [key, value] (key)}<div
                class="flex justify-between gap-4 py-3 text-sm"
              >
                <dt class="muted min-w-0">{title(key)}</dt>
                <dd class="min-w-0 text-right break-all font-mono text-xs">
                  {value === null ? 'Not set' : String(value)}
                </dd>
              </div>{/each}
          </dl></Accordion.ItemContent
        >
      </Accordion.Item>
      <Accordion.Item value="device-info">
        <Accordion.ItemTrigger
          ><span>Device report</span><Accordion.ItemIndicator
            ><ChevronDown size={17} /></Accordion.ItemIndicator
          ></Accordion.ItemTrigger
        >
        <Accordion.ItemContent>
          <pre
            class="mt-3 whitespace-pre-wrap break-all rounded-container bg-surface-200-800 p-4 text-xs">{JSON.stringify(
              provenance?.device_info ?? {},
              null,
              2,
            )}</pre>
        </Accordion.ItemContent>
      </Accordion.Item>
      <Accordion.Item value="launch-configuration">
        <Accordion.ItemTrigger
          ><span>Recorded launch configuration</span><Accordion.ItemIndicator
            ><ChevronDown size={17} /></Accordion.ItemIndicator
          ></Accordion.ItemTrigger
        >
        <Accordion.ItemContent>
          {#if Object.keys(provenance?.launch_configuration ?? {}).length}
            <pre
              class="mt-3 whitespace-pre-wrap break-all rounded-container bg-surface-200-800 p-4 text-xs">{JSON.stringify(
                provenance?.launch_configuration,
                null,
                2,
              )}</pre>
          {:else}
            <p class="muted mt-3 text-sm">Recorded when capture startup begins.</p>
          {/if}
        </Accordion.ItemContent>
      </Accordion.Item>
    </Accordion>
    <div class="border-t border-surface-200-800 pt-5 text-sm space-y-2">
      <h3 class="font-semibold">What gets saved?</h3>
      <p class="muted leading-relaxed">
        Complete vendor packets, including events and status, plus your markers and segments. Plots
        show the declared signal streams; raw capture data retains the rest.
      </p>
    </div>
  </section>
</div>
