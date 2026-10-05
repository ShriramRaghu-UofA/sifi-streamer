<script lang="ts">
  import { untrack } from 'svelte';
  import uPlot from 'uplot';
  import 'uplot/dist/uPlot.min.css';
  import type { Stream } from './lib/types';
  let {
    stream,
    timestamps,
    samples,
    appearance,
  }: {
    stream: Stream;
    timestamps: number[];
    samples: (number | null)[][];
    appearance: string;
  } = $props();
  const colors = [
    '#10b981',
    '#3b82f6',
    '#e879f9',
    '#f59e0b',
    '#f43f5e',
    '#8b5cf6',
    '#06b6d4',
    '#84cc16',
  ];
  let ordered = $derived(
    timestamps.every((value, index) => index === 0 || value > timestamps[index - 1]),
  );
  let columns = $derived([
    ordered ? timestamps : timestamps.map((_, index) => index),
    ...stream.channels.map((_, index) => samples.map((row) => row[index] ?? null)),
  ] as uPlot.AlignedData);
  function attachPlot(node: HTMLDivElement) {
    const spec = untrack(() => stream);
    const stroke = () => getComputedStyle(node).color;
    const plot = new uPlot(
      {
        width: Math.max(1, node.clientWidth),
        height: 240,
        scales: { x: { time: false } },
        axes: [
          { stroke, grid: { show: false } },
          {
            stroke,
            grid: { stroke: () => getComputedStyle(node).getPropertyValue('--color-surface-400') },
          },
        ],
        series: [
          {},
          ...spec.channels.map((channel, index) => ({
            label: spec.channel_labels[index] ?? channel,
            stroke: colors[index % colors.length],
            width: 1.4,
            spanGaps: false,
          })),
        ],
      },
      untrack(() => columns),
      node,
    );
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width > 0)
        plot.setSize({ width: Math.floor(entry.contentRect.width), height: 240 });
    });
    observer.observe(node);
    $effect(() => {
      plot.setData(columns);
    });
    $effect(() => {
      appearance;
      plot.redraw(true, true);
    });
    return () => {
      observer.disconnect();
      plot.destroy();
    };
  }
</script>

<div
  class="min-w-0 overflow-hidden"
  role="img"
  aria-label={`${stream.label ?? stream.stream_id}: ${stream.channels.length} signal channels, ${timestamps.length} samples`}
>
  <div {@attach attachPlot}></div>
</div>
<p class="field-hint mt-2">
  {ordered
    ? 'Source time in seconds · latest 10 seconds'
    : 'Sample index · source clock is not monotonic'}. Missing values appear as gaps. Use the legend
  to toggle channels.
</p>
