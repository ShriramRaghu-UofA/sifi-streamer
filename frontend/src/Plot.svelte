<script lang="ts">
  import uPlot from 'uplot';
  import 'uplot/dist/uPlot.min.css';

  type StreamInfo = {
    stream_id: string;
    label: string | null;
    channels: string[];
    channel_labels: (string | null)[];
    nominal_rate_hz: number;
  };

  let { stream, timestamps, samples } = $props<{
    stream: StreamInfo;
    timestamps: number[];
    samples: (number | null)[][];
  }>();
  let plot: uPlot | null = null;
  let width = $state(800);

  const colors = ['#42d3ff', '#ffb454', '#b6f36b', '#ef7dff', '#ff6b7a', '#8ca8ff', '#fff176', '#68e0c2'];
  let columns = $derived.by(() => {
    if (timestamps.length === 0) return [[], ...stream.channels.map(() => [])] as uPlot.AlignedData;
    const first = timestamps[0];
    const monotonic = timestamps.every(
      (value: number, index: number) => index === 0 || value > timestamps[index - 1]
    );
    const x = monotonic
      ? timestamps.map((value: number) => value - first)
      : timestamps.map((_value: number, index: number) => index / stream.nominal_rate_hz);
    return [
      x,
      ...stream.channels.map((_channel: string, channel: number) =>
        samples.map((row: (number | null)[]) => row[channel] ?? null)
      )
    ] as uPlot.AlignedData;
  });

  function attachPlot(node: HTMLDivElement) {
    const observer = new ResizeObserver(([entry]) => {
      width = Math.max(320, Math.floor(entry.contentRect.width));
      plot?.setSize({ width, height: 220 });
    });
    observer.observe(node);
    plot = new uPlot(
      {
        width,
        height: 220,
        title: stream.label ?? stream.stream_id,
        scales: { x: { time: false } },
        axes: [{ label: 'seconds' }, {}],
        series: [
          {},
          ...stream.channels.map((channel: string, index: number) => ({
            label: stream.channel_labels[index] ?? channel,
            stroke: colors[index % colors.length],
            width: 1,
            spanGaps: false
          }))
        ]
      },
      columns,
      node
    );
    return () => {
      observer.disconnect();
      plot?.destroy();
      plot = null;
    };
  }

  $effect(() => {
    plot?.setData(columns);
  });
</script>

<div class="plot" {@attach attachPlot}></div>
