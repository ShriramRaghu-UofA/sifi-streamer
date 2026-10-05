<script lang="ts">
  import { Accordion, Dialog, Portal } from '@skeletonlabs/skeleton-svelte';
  import { Plus, Flag, Layers, Pencil, Trash2, ChevronDown, X, Square } from '@lucide/svelte';
  import MetadataEditor from './MetadataEditor.svelte';
  import type { Dashboard } from '../lib/dashboard.svelte.js';
  import type { Kind, Attributes } from '../lib/types';
  let { dashboard } = $props<{ dashboard: Dashboard }>();
  let editorOpen = $state(false);
  let editing = $state(false);
  let draft = $state<Kind>(emptyKind());
  let defaultsValid = $state(true);
  let occurrenceAttributes = $state<Record<string, Attributes>>({});
  let occurrenceValid = $state<Record<string, boolean>>({});
  function emptyKind(): Kind {
    return {
      target: 'marker',
      kind: '',
      label: '',
      color: '#10b981',
      id_prefix: '',
      separator: '_',
      padding: 2,
      start: 1,
      default_attributes: {},
    };
  }
  function key(kind: Kind) {
    return `${kind.target}:${kind.kind}`;
  }
  function edit(kind?: Kind) {
    editing = !!kind;
    draft = kind ? { ...kind, default_attributes: { ...kind.default_attributes } } : emptyKind();
    defaultsValid = true;
    editorOpen = true;
  }
  async function save(event: SubmitEvent) {
    event.preventDefault();
    if (!defaultsValid) return;
    const saved = await dashboard.command(
      '/api/kinds/set',
      {
        ...draft,
        label: draft.label || null,
        id_prefix: draft.id_prefix || null,
      },
      'Annotation kind saved',
    );
    if (saved) editorOpen = false;
  }
  function emit(kind: Kind) {
    if (occurrenceValid[key(kind)] === false) return;
    return dashboard.command(
      kind.target === 'marker' ? '/api/marker' : '/api/segment/start',
      {
        kind: kind.kind,
        attributes: occurrenceAttributes[key(kind)] ?? {},
      },
      kind.target === 'marker' ? 'Marker recorded' : 'Segment started',
    );
  }
</script>

<section class="space-y-6">
  <div class="flex flex-wrap justify-between items-end gap-4">
    <div>
      <p class="eyebrow">Context, as it happens</p>
      <h2 class="h3 mt-2">Annotations</h2>
      <p class="muted mt-2 text-sm">
        Markers capture a moment. Segments capture a duration. Every occurrence gets a unique ID.
      </p>
    </div>
    <button class="btn preset-filled-primary-500" disabled={dashboard.busy} onclick={() => edit()}
      ><Plus size={17} /> New kind</button
    >
  </div>
  {#if dashboard.segments.length}
    <section class="panel border-primary-500/40 space-y-4">
      <div class="flex items-center gap-2">
        <Layers size={18} class="text-primary-700-300" />
        <h3 class="font-semibold">Open segments</h3>
        <span class="badge preset-tonal-primary">{dashboard.segments.length}</span>
      </div>
      <p class="field-hint">
        Close the most recently started segment first. Stopping capture closes all open segments.
      </p>
      <ol class="space-y-2">
        {#each [...dashboard.segments].reverse() as id, index (id)}<li
            class="flex items-center justify-between gap-3 rounded-container bg-surface-200-800 px-4 py-3"
          >
            <code class="text-sm">{id}</code><button
              class="btn btn-sm preset-tonal"
              disabled={index !== 0 || dashboard.busy || dashboard.phase !== 'recording'}
              onclick={() => dashboard.command('/api/segment/stop', { id }, 'Segment closed')}
              ><Square size={13} />{index === 0 ? 'Close segment' : 'Nested below'}</button
            >
          </li>{/each}
      </ol>
    </section>
  {/if}
  {#if !dashboard.kinds.length}<div class="panel flex flex-col items-center py-14 text-center">
      <Flag size={32} class="text-primary-700-300" />
      <h3 class="h4 mt-4">Give your moments a name</h3>
      <p class="muted mt-2 max-w-md text-sm">
        Create a kind such as “Note” or “Rest”, or load your reusable kinds with the launcher’s
        --kinds-file option.
      </p>
      <button class="btn preset-tonal-primary mt-5" onclick={() => edit()}
        >Create your first kind</button
      >
    </div>
  {:else}<div class="grid gap-5 md:grid-cols-2 xl:grid-cols-3">
      {#each dashboard.kinds as kind (key(kind))}
        <article
          class="panel space-y-4 border-t-4"
          style:border-top-color={kind.color ?? 'var(--color-primary-500)'}
        >
          <div class="flex items-start justify-between gap-3">
            <div>
              <span class="badge preset-tonal"
                >{kind.target === 'marker' ? 'Point / marker' : 'Duration / segment'}</span
              >
              <h3 class="h4 mt-3">{kind.label ?? kind.kind}</h3>
              <p class="field-hint mt-1">Kind: {kind.kind}</p>
            </div>
            <div class="flex gap-1">
              <button
                class="btn-icon btn-sm preset-tonal"
                aria-label={`Edit ${kind.label ?? kind.kind}`}
                disabled={dashboard.busy}
                onclick={() => edit(kind)}><Pencil size={15} /></button
              ><button
                class="btn-icon btn-sm preset-tonal"
                aria-label={`Remove ${kind.label ?? kind.kind}`}
                disabled={dashboard.busy}
                onclick={() =>
                  dashboard.command(
                    '/api/kinds/remove',
                    { target: kind.target, kind: kind.kind },
                    'Annotation kind removed',
                  )}><Trash2 size={15} /></button
              >
            </div>
          </div>
          <button
            class="btn preset-filled-primary-500 w-full"
            disabled={dashboard.phase !== 'recording' ||
              dashboard.busy ||
              occurrenceValid[key(kind)] === false}
            onclick={() => emit(kind)}
            >{#if kind.target === 'marker'}<Flag size={16} /> Add marker{:else}<Layers size={16} /> Start
              segment{/if}</button
          >
          <Accordion collapsible
            ><Accordion.Item value="metadata"
              ><Accordion.ItemTrigger
                ><span>Occurrence metadata</span><Accordion.ItemIndicator
                  ><ChevronDown size={15} /></Accordion.ItemIndicator
                ></Accordion.ItemTrigger
              ><Accordion.ItemContent
                ><div class="pt-3 space-y-3">
                  {#if Object.keys(kind.default_attributes).length}<p class="field-hint">
                      Defaults: {JSON.stringify(kind.default_attributes)}. Fields below override
                      these for this occurrence.
                    </p>{/if}
                  <MetadataEditor
                    bind:value={
                      () => occurrenceAttributes[key(kind)] ?? {},
                      (value) => (occurrenceAttributes[key(kind)] = value)
                    }
                    bind:valid={
                      () => occurrenceValid[key(kind)] ?? true,
                      (value) => (occurrenceValid[key(kind)] = value)
                    }
                    disabled={dashboard.busy}
                    label="Extra fields"
                  />
                </div></Accordion.ItemContent
              ></Accordion.Item
            ></Accordion
          >
        </article>
      {/each}
    </div>{/if}
</section>
<Dialog
  open={editorOpen}
  onOpenChange={({ open }) => (editorOpen = open)}
  closeOnInteractOutside={!dashboard.busy}
  closeOnEscape={!dashboard.busy}
>
  <Portal
    ><Dialog.Backdrop class="dialog-backdrop" /><Dialog.Positioner class="dialog-position"
      ><Dialog.Content class="dialog-panel">
        <header class="flex justify-between items-center gap-3">
          <Dialog.Title class="h3"
            >{editing ? 'Edit annotation kind' : 'New annotation kind'}</Dialog.Title
          ><Dialog.CloseTrigger
            class="btn-icon preset-tonal"
            aria-label="Close kind editor"
            disabled={dashboard.busy}><X size={18} /></Dialog.CloseTrigger
          >
        </header>
        <Dialog.Description class="muted text-sm mt-2"
          >Choose a stable category and how occurrence IDs are generated.</Dialog.Description
        >
        <form class="mt-6 space-y-5" onsubmit={save}>
          <div class="grid grid-cols-2 gap-4">
            <label class="field"
              ><span>Record type</span><select
                class="select"
                bind:value={draft.target}
                disabled={editing}
                ><option value="marker">Marker / moment</option><option value="segment"
                  >Segment / duration</option
                ></select
              ></label
            ><label class="field"
              ><span>Kind name</span><input
                class="input"
                bind:value={draft.kind}
                required
                disabled={editing}
                placeholder="rest"
              /></label
            >
          </div>
          <div class="grid grid-cols-[1fr_5rem] gap-4">
            <label class="field"
              ><span>Display label</span><input
                class="input"
                bind:value={draft.label}
                placeholder="Rest period"
              /></label
            ><label class="field"
              ><span>Color</span><input
                class="input h-10 p-1"
                type="color"
                bind:value={draft.color}
              /></label
            >
          </div>
          <div class="grid grid-cols-2 gap-4">
            <label class="field"
              ><span>ID prefix</span><input
                class="input"
                bind:value={draft.id_prefix}
                placeholder="Defaults to kind"
              /></label
            ><label class="field"
              ><span>Separator</span><input class="input" bind:value={draft.separator} /></label
            ><label class="field"
              ><span>Number width</span><input
                class="input"
                type="number"
                min="1"
                max="9"
                bind:value={draft.padding}
                required
              /></label
            ><label class="field"
              ><span>Starting number</span><input
                class="input"
                type="number"
                min="0"
                step="1"
                bind:value={draft.start}
                required
              /></label
            >
          </div>
          <p class="field-hint">
            Example ID: <code
              >{draft.id_prefix || draft.kind || 'kind'}{draft.separator}{String(
                draft.start ?? 1,
              ).padStart(draft.padding ?? 2, '0')}</code
            >. Existing IDs stay reserved when a kind is edited.
          </p>
          {#if editorOpen}<MetadataEditor
              bind:value={draft.default_attributes}
              bind:valid={defaultsValid}
              label="Default metadata"
              disabled={dashboard.busy}
            />{/if}
          {#if dashboard.error}<p class="text-sm text-error-700-300" role="alert">
              {dashboard.error}
            </p>{/if}
          <footer class="flex justify-end gap-3 pt-2">
            <Dialog.CloseTrigger class="btn preset-tonal" disabled={dashboard.busy}
              >Cancel</Dialog.CloseTrigger
            ><button
              class="btn preset-filled-primary-500"
              type="submit"
              disabled={!draft.kind.trim() || !defaultsValid || dashboard.busy}
              >{dashboard.busy ? 'Saving…' : 'Save kind'}</button
            >
          </footer>
        </form>
      </Dialog.Content></Dialog.Positioner
    ></Portal
  >
</Dialog>
