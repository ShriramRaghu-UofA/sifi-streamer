<script lang="ts">
  import { untrack } from 'svelte';
  import { Plus, Trash2 } from '@lucide/svelte';
  import type { Attributes, Scalar } from '../lib/types';
  type Row = {
    id: number;
    key: string;
    type: 'string' | 'number' | 'boolean' | 'null';
    text: string;
  };
  let {
    value = $bindable<Attributes>({}),
    valid = $bindable(true),
    disabled = false,
    label = 'Metadata',
  } = $props<{
    value?: Attributes;
    valid?: boolean;
    disabled?: boolean;
    label?: string;
  }>();
  let sequence = 0;
  let rows = $state<Row[]>(
    Object.entries(untrack(() => value)).map(([key, item]) => ({
      id: sequence++,
      key,
      type: item === null ? 'null' : (typeof item as Row['type']),
      text: item === null ? '' : String(item),
    })),
  );
  let problem = $state('');
  function commit() {
    const result: Attributes = {};
    problem = '';
    for (const row of rows) {
      if (!row.key.trim()) {
        problem = 'Each field needs a name.';
        break;
      }
      if (Object.hasOwn(result, row.key)) {
        problem = `Field “${row.key}” appears twice.`;
        break;
      }
      let scalar: Scalar = row.text;
      if (row.type === 'number') {
        scalar = Number(row.text);
        if (!row.text.trim() || !Number.isFinite(scalar)) {
          problem = `“${row.key}” needs a finite number.`;
          break;
        }
      } else if (row.type === 'boolean') scalar = row.text === 'true';
      else if (row.type === 'null') scalar = null;
      Object.defineProperty(result, row.key, {
        value: scalar,
        enumerable: true,
        configurable: true,
      });
    }
    valid = !problem;
    if (valid) value = result;
  }
</script>

<fieldset class="space-y-3" {disabled}>
  <legend class="mb-2 text-sm font-medium">{label}</legend>
  {#if !rows.length}<p class="field-hint">
      No extra fields. Add searchable facts such as operator or condition.
    </p>{/if}
  {#each rows as row (row.id)}
    <div
      class="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] items-end gap-2 sm:grid-cols-[minmax(0,1fr)_7rem_minmax(0,1fr)_auto]"
    >
      <label class="field"
        ><span class="sr-only">Field name</span><input
          class="input"
          placeholder="Field name"
          bind:value={row.key}
          oninput={commit}
        /></label
      >
      <label class="field"
        ><span class="sr-only">Value type</span><select
          class="select"
          bind:value={row.type}
          onchange={() => {
            if (row.type === 'boolean') row.text = 'true';
            commit();
          }}
          ><option value="string">Text</option><option value="number">Number</option><option
            value="boolean">Boolean</option
          ><option value="null">Null</option></select
        ></label
      >
      <label class="field col-span-2 sm:col-span-1"
        ><span class="sr-only">Field value</span>
        {#if row.type === 'boolean'}<select class="select" bind:value={row.text} onchange={commit}
            ><option value="true">True</option><option value="false">False</option></select
          >
        {:else}<input
            class="input"
            placeholder={row.type === 'null' ? 'No value' : 'Value'}
            disabled={row.type === 'null' || disabled}
            bind:value={row.text}
            oninput={commit}
            inputmode={row.type === 'number' ? 'decimal' : 'text'}
          />{/if}
      </label>
      <button
        type="button"
        class="btn-icon preset-tonal row-start-1 col-start-3 sm:col-start-4"
        aria-label={`Remove ${row.key || 'field'}`}
        onclick={() => {
          rows = rows.filter((item) => item.id !== row.id);
          commit();
        }}><Trash2 size={16} /></button
      >
    </div>
  {/each}
  {#if problem}<p class="text-sm text-error-700-300" role="alert">{problem}</p>{/if}
  <button
    type="button"
    class="btn btn-sm preset-tonal"
    onclick={() => {
      rows.push({ id: sequence++, key: '', type: 'string', text: '' });
      commit();
    }}><Plus size={15} /> Add field</button
  >
  <p class="field-hint">
    Values can be text, numbers, booleans, or null. Lists and nested objects are not supported.
  </p>
</fieldset>
