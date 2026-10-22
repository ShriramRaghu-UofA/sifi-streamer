<script lang="ts">
  import { Palette, Sun, Moon, Monitor, ChevronDown } from '@lucide/svelte';
  import { Popover, Portal, SegmentedControl } from '@skeletonlabs/skeleton-svelte';
  import { themes, title, type Theme, type ColorMode } from '../lib/types';
  let { theme, mode, onchange } = $props<{
    theme: Theme;
    mode: ColorMode;
    onchange: (theme: Theme, mode: ColorMode) => void;
  }>();
</script>

<Popover positioning={{ placement: 'bottom-end' }}>
  <Popover.Trigger class="btn preset-tonal" aria-label="Appearance"
    ><Palette size={17} /><span class="hidden sm:inline">Appearance</span><ChevronDown
      size={14}
    /></Popover.Trigger
  >
  <Portal
    ><Popover.Positioner class="z-40"
      ><Popover.Content
        class="card border border-surface-200-800 bg-surface-100-900 p-5 shadow-xl space-y-4 w-72"
      >
        <Popover.Title class="font-semibold">Make it yours</Popover.Title>
        <label class="field"
          ><span>Theme</span><select
            class="select"
            value={theme}
            onchange={(event) => onchange(event.currentTarget.value as Theme, mode)}
            >{#each themes as item (item)}<option value={item}>{title(item)}</option>{/each}</select
          ></label
        >
        <SegmentedControl
          value={mode}
          onValueChange={({ value }) => {
            if (value) onchange(theme, value as ColorMode);
          }}
        >
          <SegmentedControl.Label class="text-sm mb-2">Color mode</SegmentedControl.Label>
          <SegmentedControl.Control
            ><SegmentedControl.Indicator />
            <SegmentedControl.Item value="light"
              ><SegmentedControl.ItemText
                ><Sun size={16} /><span class="sr-only">Light</span></SegmentedControl.ItemText
              ><SegmentedControl.ItemHiddenInput /></SegmentedControl.Item
            >
            <SegmentedControl.Item value="dark"
              ><SegmentedControl.ItemText
                ><Moon size={16} /><span class="sr-only">Dark</span></SegmentedControl.ItemText
              ><SegmentedControl.ItemHiddenInput /></SegmentedControl.Item
            >
            <SegmentedControl.Item value="system"
              ><SegmentedControl.ItemText
                ><Monitor size={16} /><span class="sr-only">System</span></SegmentedControl.ItemText
              ><SegmentedControl.ItemHiddenInput /></SegmentedControl.Item
            >
          </SegmentedControl.Control>
        </SegmentedControl>
        <p class="field-hint">Skeleton themes. Saved on this browser.</p>
      </Popover.Content></Popover.Positioner
    ></Portal
  >
</Popover>
