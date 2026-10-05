<script lang="ts">
  import { Switch, Accordion } from '@skeletonlabs/skeleton-svelte';
  import { FileCheck2, SlidersHorizontal, ChevronDown } from '@lucide/svelte';
  import MetadataEditor from './MetadataEditor.svelte';
  import type { Dashboard } from '../lib/dashboard.svelte.js';
  import { title } from '../lib/types';
  let { dashboard, metadataValid = $bindable(true) } = $props<{
    dashboard: Dashboard;
    metadataValid?: boolean;
  }>();
  let editable = $derived(dashboard.phase === 'setup');
</script>

<div class="grid gap-6 lg:grid-cols-2">
  <section class="panel space-y-6">
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
  <section class="panel space-y-5">
    <div>
      <p class="eyebrow">02 / Device</p>
      <h2 class="h3 mt-2">Acquisition configuration</h2>
      <p class="muted mt-2 text-sm">Set by the launcher. Review it before starting.</p>
    </div>
    <dl class="grid grid-cols-2 gap-4">
      {#each ['device', 'transport', 'emg_fs_hz', 'imu_fs_hz'] as key (key)}
        {#if dashboard.bootstrap?.configuration[key] != null}<div
            class="rounded-container bg-surface-200-800 p-4"
          >
            <dt class="field-hint">
              {key === 'emg_fs_hz'
                ? 'EMG sample rate'
                : key === 'imu_fs_hz'
                  ? 'IMU sample rate'
                  : title(key)}
            </dt>
            <dd class="mt-1 font-semibold">
              {dashboard.bootstrap.configuration[key]}{key.endsWith('_hz') ? ' Hz' : ''}
            </dd>
          </div>{/if}
      {/each}
    </dl>
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
