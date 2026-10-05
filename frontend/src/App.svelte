<script lang="ts">
  import { onMount } from 'svelte';
  import { AppBar, Tabs, Dialog, Portal, Toast, Progress } from '@skeletonlabs/skeleton-svelte';
  import {
    Activity,
    Radio,
    Flag,
    HeartPulse,
    Settings2,
    Circle,
    Square,
    Play,
    LogOut,
    X,
    TriangleAlert,
    CheckCheck,
    FileCheck2,
  } from '@lucide/svelte';
  import { Dashboard } from './lib/dashboard.svelte.js';
  import { themes, title, thresholdsValid, type Theme, type ColorMode } from './lib/types';
  import Appearance from './components/Appearance.svelte';
  import SessionPanel from './components/SessionPanel.svelte';
  import SignalsPanel from './components/SignalsPanel.svelte';
  import AnnotationsPanel from './components/AnnotationsPanel.svelte';
  import HealthPanel from './components/HealthPanel.svelte';

  const dashboard = new Dashboard(location.hash.slice(1));
  let tab = $state('session');
  let metadataValid = $state(true);
  let stopOpen = $state(false);
  let theme = $state<Theme>('cerberus');
  let mode = $state<ColorMode>('dark');
  let systemDark = $state(false);
  let preferenceError = $state('');
  let resolvedDark = $derived(mode === 'dark' || (mode === 'system' && systemDark));
  let appearance = $derived(`${theme}:${resolvedDark}`);
  let health = $derived(dashboard.live?.health);
  let samples = $derived(health?.streams.reduce((sum, item) => sum + item.sample_count, 0) ?? 0);
  let warnings = $derived(
    health?.streams.reduce((sum, item) => sum + item.warnings.length, 0) ?? 0,
  );
  let phase = $derived(dashboard.phase);
  let failure = $derived(dashboard.error || dashboard.live?.error || dashboard.bootstrap?.error);

  function applyAppearance(nextTheme: Theme, nextMode: ColorMode, persist = true) {
    theme = nextTheme;
    mode = nextMode;
    const dark = nextMode === 'dark' || (nextMode === 'system' && systemDark);
    document.documentElement.dataset.theme = nextTheme;
    document.documentElement.classList.toggle('dark', dark);
    document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
    if (persist) {
      try {
        localStorage.setItem('sifi-appearance', JSON.stringify({ theme, mode }));
        preferenceError = '';
      } catch {
        preferenceError = 'Appearance changed for this session. Browser storage is unavailable.';
      }
    }
  }
  async function start() {
    if (metadataValid && thresholdsValid(dashboard.thresholds) && (await dashboard.start()))
      tab = 'signals';
  }
  async function stop() {
    if (await dashboard.command('/api/capture/stop', {}, 'Capture stopped and flushed'))
      stopOpen = false;
  }
  onMount(() => {
    const query = matchMedia('(prefers-color-scheme: dark)');
    systemDark = query.matches;
    try {
      const stored = localStorage.getItem('sifi-appearance');
      if (stored) {
        const saved = JSON.parse(stored);
        if (themes.includes(saved.theme) && ['system', 'light', 'dark'].includes(saved.mode)) {
          theme = saved.theme;
          mode = saved.mode;
        }
      }
    } catch {
      preferenceError =
        'Saved appearance could not be read. You can still change it for this session.';
    }
    applyAppearance(theme, mode, false);
    const onSystemChange = (event: MediaQueryListEvent) => {
      systemDark = event.matches;
      applyAppearance(theme, mode, false);
    };
    query.addEventListener('change', onSystemChange);
    void dashboard.initialize();
    dashboard.startPolling();
    return () => {
      query.removeEventListener('change', onSystemChange);
      dashboard.dispose();
    };
  });
</script>

<svelte:head
  ><title>Capture Studio · SiFi</title><meta
    name="description"
    content="Local SiFi capture, signal monitoring, and annotations."
  /></svelte:head
>
<a
  href="#workspace"
  class="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50 btn preset-filled-primary-500"
  >Skip to workspace</a
>
<AppBar class="sticky top-0 z-30 border-b border-surface-200-800 bg-surface-100-900 p-0">
  <AppBar.Toolbar
    class="mx-auto w-full max-w-[1440px] grid-cols-[auto_1fr_auto] px-4 sm:px-8 py-4 gap-3"
  >
    <AppBar.Lead
      ><div class="preset-filled-primary-500 rounded-container p-2.5">
        <Activity size={23} />
      </div></AppBar.Lead
    >
    <AppBar.Headline
      ><h1 class="font-semibold text-lg tracking-tight">Capture Studio</h1>
      <p class="eyebrow text-[10px] mt-0.5">SiFi / Local acquisition</p></AppBar.Headline
    >
    <AppBar.Trail><Appearance {theme} {mode} onchange={applyAppearance} /></AppBar.Trail>
  </AppBar.Toolbar>
</AppBar>

<main id="workspace" class="mx-auto max-w-[1440px] p-4 sm:p-8 space-y-6">
  {#if preferenceError}<div
      class="card preset-tonal-warning p-3 text-sm flex justify-between gap-3"
      role="status"
    >
      <p>{preferenceError}</p>
      <button
        class="btn-icon btn-sm preset-tonal"
        aria-label="Dismiss appearance notice"
        onclick={() => (preferenceError = '')}><X size={15} /></button
      >
    </div>{/if}
  {#if failure}<div class="card preset-tonal-error p-4 flex items-start gap-3" role="alert">
      <TriangleAlert size={20} class="shrink-0 mt-0.5" />
      <div class="min-w-0">
        <h2 class="font-semibold">Action needs attention</h2>
        <p class="text-sm mt-1 break-words">{failure}</p>
      </div>
    </div>{/if}
  {#if dashboard.connectionError}<div class="card preset-tonal-warning p-4 flex gap-3" role="alert">
      <TriangleAlert size={20} class="shrink-0" />
      <div>
        <p class="font-semibold">Live connection interrupted</p>
        <p class="text-sm mt-1">
          {dashboard.connectionError}. Displayed data may be stale. Recording status is unconfirmed
          until the server responds.
        </p>
      </div>
    </div>{/if}

  {#if dashboard.closed}<section class="panel py-16 flex flex-col items-center text-center">
      <div class="rounded-full preset-tonal-success p-4"><CheckCheck size={34} /></div>
      <h2 class="h2 mt-6">All done.</h2>
      <p class="muted mt-3 max-w-md">
        The local server acknowledged shutdown. It is safe to close this tab.
      </p>
      <code class="mt-6 text-sm break-all">{dashboard.bootstrap?.output}</code>
    </section>
  {:else if !dashboard.bootstrap}
    <section class="panel py-16 text-center space-y-4">
      <Radio size={32} class="mx-auto text-primary-700-300" />
      <h2 class="h3">
        {dashboard.error ? 'Unable to open your session' : 'Connecting to your local session'}
      </h2>
      <p class="muted text-sm">
        {dashboard.error
          ? 'Open the complete URL printed by sifi-capture-web, including its session token.'
          : 'Loading capture settings and available controls…'}
      </p>
      {#if dashboard.error}<button
          class="btn preset-tonal mx-auto"
          onclick={() => dashboard.initialize()}>Try again</button
        >{:else}<Progress value={null} class="max-w-xs mx-auto"
          ><Progress.Label class="sr-only">Loading session</Progress.Label><Progress.Track
            ><Progress.Range /></Progress.Track
          ></Progress
        >{/if}
    </section>
  {:else}
    <section class="panel flex flex-wrap justify-between items-center gap-5">
      <div class="min-w-0">
        <p class="eyebrow">{phase === 'setup' ? 'Ready when you are' : 'Current session'}</p>
        <h2 class="h2 mt-2 break-all">{dashboard.captureId}</h2>
        <div class="flex flex-wrap items-center gap-3 mt-3">
          <span
            class={[
              'badge',
              phase === 'recording'
                ? 'preset-tonal-success'
                : phase === 'failed'
                  ? 'preset-tonal-error'
                  : 'preset-tonal',
            ]}><Circle size={9} fill="currentColor" />{title(phase)}</span
          ><span class="text-xs muted"
            >{dashboard.bootstrap.configuration.transport ?? 'synthetic'} · {dashboard.healthLogEnabled
              ? 'Health log on'
              : 'Health log off'}</span
          >
        </div>
      </div>
      <div class="flex flex-wrap gap-3">
        {#if phase === 'setup'}<button
            class="btn preset-filled-primary-500 px-6"
            disabled={dashboard.busy ||
              !metadataValid ||
              !thresholdsValid(dashboard.thresholds) ||
              !dashboard.captureId.trim()}
            onclick={start}
            ><Play size={17} />{dashboard.busy ? 'Starting…' : 'Start capture'}</button
          >
        {:else if phase === 'recording'}<button
            class="btn preset-filled-error-500"
            disabled={dashboard.busy}
            onclick={() => (stopOpen = true)}><Square size={16} />Stop & save</button
          >
        {:else}<span class="text-sm muted self-center"
            >{phase === 'stopped'
              ? 'Recording flushed to disk.'
              : 'Review the session status above.'}</span
          >{/if}
        {#if phase !== 'recording' && phase !== 'starting' && phase !== 'stopping'}<button
            class="btn preset-tonal"
            disabled={dashboard.busy}
            onclick={() => dashboard.command('/api/server/stop')}
            ><LogOut size={17} />Exit dashboard</button
          >{/if}
      </div>
    </section>
    {#if dashboard.streams.length}<section
        class="grid grid-cols-2 gap-4 lg:grid-cols-4"
        aria-label="Acquisition summary"
      >
        <div class="panel">
          <p class="field-hint">Signal streams</p>
          <p class="h3 mt-2">{dashboard.streams.length}</p>
        </div>
        <div class="panel">
          <p class="field-hint">Acquired samples</p>
          <p class="h3 mt-2 tabular-nums">{samples.toLocaleString()}</p>
        </div>
        <div class="panel">
          <p class="field-hint">Signal health</p>
          <p
            class={[
              'text-lg font-semibold mt-2',
              warnings ? 'text-warning-700-300' : 'text-success-700-300',
            ]}
          >
            {title(health?.severity ?? 'warming_up')}
          </p>
        </div>
        <div class="panel">
          <p class="field-hint">Open segments</p>
          <p class="h3 mt-2">
            {dashboard.segments.length}<span class="text-xs muted font-normal ml-3"
              >{warnings} health warnings</span
            >
          </p>
        </div>
      </section>{/if}
    <Tabs value={tab} onValueChange={({ value }) => (tab = value)}>
      <Tabs.List class="mb-6 overflow-x-auto"
        ><Tabs.Trigger value="signals"><Radio size={17} /><span>Signals</span></Tabs.Trigger
        ><Tabs.Trigger value="annotations"><Flag size={17} /><span>Annotations</span></Tabs.Trigger
        ><Tabs.Trigger value="health"
          ><HeartPulse size={17} /><span>Health</span>{#if warnings}<span
              class="badge preset-tonal-warning">{warnings}</span
            >{/if}</Tabs.Trigger
        ><Tabs.Trigger value="session"><Settings2 size={17} /><span>Session</span></Tabs.Trigger
        ><Tabs.Indicator /></Tabs.List
      >
      <Tabs.Content value="signals"><SignalsPanel {dashboard} {appearance} /></Tabs.Content>
      <Tabs.Content value="annotations"><AnnotationsPanel {dashboard} /></Tabs.Content>
      <Tabs.Content value="health"><HealthPanel {dashboard} /></Tabs.Content>
      <Tabs.Content value="session"><SessionPanel {dashboard} bind:metadataValid /></Tabs.Content>
    </Tabs>
    <footer
      class="text-xs muted flex flex-wrap items-center justify-between gap-3 border-t border-surface-200-800 pt-5"
    >
      <span class="flex items-center gap-2"
        ><FileCheck2 size={14} />Authoritative capture · JSONL / Zstandard</span
      ><span>Local session · data stays on this machine</span>
    </footer>
  {/if}
</main>

<Dialog
  open={stopOpen}
  onOpenChange={({ open }) => (stopOpen = open)}
  closeOnInteractOutside={!dashboard.busy}
  closeOnEscape={!dashboard.busy}
>
  <Portal
    ><Dialog.Backdrop class="dialog-backdrop" /><Dialog.Positioner class="dialog-position"
      ><Dialog.Content class="dialog-panel max-w-md">
        <Dialog.Title class="h3">Stop and save this capture?</Dialog.Title><Dialog.Description
          class="muted mt-3 text-sm leading-relaxed"
          >Acquisition will stop, open segments will close, and buffered records will be flushed to
          disk. This session cannot be restarted.</Dialog.Description
        >
        {#if failure}<p role="alert" class="text-sm text-error-700-300 mt-4">{failure}</p>{/if}
        <footer class="flex justify-end gap-3 mt-6">
          <Dialog.CloseTrigger class="btn preset-tonal" disabled={dashboard.busy}
            >Keep recording</Dialog.CloseTrigger
          ><button class="btn preset-filled-error-500" disabled={dashboard.busy} onclick={stop}
            >{dashboard.busy ? 'Saving…' : 'Stop and save'}</button
          >
        </footer>
      </Dialog.Content></Dialog.Positioner
    ></Portal
  >
</Dialog>
<Portal
  ><Toast.Group toaster={dashboard.toaster}>
    {#snippet children(toast)}<Toast {toast}
        ><Toast.Message
          ><Toast.Title>{toast.title}</Toast.Title><Toast.Description
            >{toast.description}</Toast.Description
          ></Toast.Message
        ><Toast.CloseTrigger aria-label="Dismiss notification"><X size={16} /></Toast.CloseTrigger
        ></Toast
      >{/snippet}
  </Toast.Group></Portal
>
