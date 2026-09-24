<script setup lang="ts">
import { computed } from "vue";
import type { ForecastPoint } from "../api";

const props = defineProps<{
  points: ForecastPoint[];
}>();

const width = 900;
const height = 330;
const padding = { top: 22, right: 24, bottom: 48, left: 58 };
const plotWidth = width - padding.left - padding.right;
const plotHeight = height - padding.top - padding.bottom;
const numberFormatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
const hourFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  hour12: false,
  timeZone: "UTC",
});

const ceiling = computed(() => {
  const maximum = Math.max(
    1,
    ...props.points.flatMap((point) => [point.prediction, point.actual_trip_count]),
  );
  const magnitude = 10 ** Math.floor(Math.log10(maximum));
  return Math.ceil((maximum * 1.1) / magnitude) * magnitude;
});

function xPosition(index: number): number {
  if (props.points.length < 2) return padding.left + plotWidth / 2;
  return padding.left + (index / (props.points.length - 1)) * plotWidth;
}

function yPosition(value: number): number {
  return padding.top + plotHeight - (value / ceiling.value) * plotHeight;
}

const predictionPolyline = computed(() =>
  props.points
    .map((point, index) => `${xPosition(index)},${yPosition(point.prediction)}`)
    .join(" "),
);

const actualPolyline = computed(() =>
  props.points
    .map((point, index) => `${xPosition(index)},${yPosition(point.actual_trip_count)}`)
    .join(" "),
);

const yTicks = computed(() =>
  Array.from({ length: 5 }, (_, index) => {
    const value = (ceiling.value * index) / 4;
    return {
      value,
      y: yPosition(value),
      label: numberFormatter.format(value),
    };
  }).reverse(),
);

const xTicks = computed(() => {
  if (props.points.length === 0) return [];
  const indexes = [
    0,
    Math.floor((props.points.length - 1) / 3),
    Math.floor(((props.points.length - 1) * 2) / 3),
    props.points.length - 1,
  ];
  return [...new Set(indexes)].map((index) => ({
    index,
    x: xPosition(index),
    label: hourFormatter.format(new Date(props.points[index].target_hour_utc)),
  }));
});

const chartDescription = computed(() => {
  if (props.points.length === 0) return "No forecast history is available.";
  const latest = props.points.at(-1)!;
  return [
    `${props.points.length}-hour forecast and actual pickup comparison.`,
    `Latest prediction ${numberFormatter.format(latest.prediction)} pickups,`,
    `actual ${numberFormatter.format(latest.actual_trip_count)} pickups,`,
    `absolute error ${numberFormatter.format(latest.absolute_error)}.`,
  ].join(" ");
});
</script>

<template>
  <div class="chart-shell" role="region" aria-label="Scrollable forecast chart" tabindex="0">
    <svg
      class="forecast-chart"
      :viewBox="`0 0 ${width} ${height}`"
      role="img"
      aria-labelledby="forecast-chart-title forecast-chart-description"
    >
      <title id="forecast-chart-title">Forecast versus actual pickups</title>
      <desc id="forecast-chart-description">{{ chartDescription }}</desc>

      <g v-for="tick in yTicks" :key="tick.value">
        <line
          :x1="padding.left"
          :x2="width - padding.right"
          :y1="tick.y"
          :y2="tick.y"
          class="chart-gridline"
        />
        <text :x="padding.left - 12" :y="tick.y + 4" class="chart-axis-label" text-anchor="end">
          {{ tick.label }}
        </text>
      </g>

      <line
        v-if="points.length"
        :x1="xPosition(points.length - 1)"
        :x2="xPosition(points.length - 1)"
        :y1="padding.top"
        :y2="padding.top + plotHeight"
        class="chart-current-line"
      />

      <polyline :points="actualPolyline" class="chart-line chart-line--actual" />
      <polyline :points="predictionPolyline" class="chart-line chart-line--prediction" />

      <g v-for="(point, index) in points" :key="`${point.target_hour_utc}-actual`">
        <circle
          :cx="xPosition(index)"
          :cy="yPosition(point.actual_trip_count)"
          r="3.4"
          class="chart-point chart-point--actual"
        >
          <title>
            {{ hourFormatter.format(new Date(point.target_hour_utc)) }} UTC — actual
            {{ numberFormatter.format(point.actual_trip_count) }}
          </title>
        </circle>
        <circle
          :cx="xPosition(index)"
          :cy="yPosition(point.prediction)"
          r="3.4"
          class="chart-point chart-point--prediction"
        >
          <title>
            {{ hourFormatter.format(new Date(point.target_hour_utc)) }} UTC — forecast
            {{ numberFormatter.format(point.prediction) }}
          </title>
        </circle>
      </g>

      <g v-for="tick in xTicks" :key="tick.index">
        <line
          :x1="tick.x"
          :x2="tick.x"
          :y1="padding.top + plotHeight"
          :y2="padding.top + plotHeight + 6"
          class="chart-axis-tick"
        />
        <text
          :x="tick.x"
          :y="height - 17"
          class="chart-axis-label chart-axis-label--x"
          text-anchor="middle"
        >
          {{ tick.label }}
        </text>
      </g>
    </svg>
  </div>
</template>
