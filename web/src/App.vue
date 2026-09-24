<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  Clock3,
  Database,
  Gauge,
  MapPin,
  RefreshCw,
  Route,
  ShieldCheck,
} from "@lucide/vue";
import ForecastChart from "./components/ForecastChart.vue";
import {
  ApiError,
  getHealth,
  getHistory,
  getRankings,
  getZones,
  type ForecastPoint,
  type HealthResponse,
  type HistoryResponse,
  type RankingsResponse,
  type Zone,
} from "./api";

const health = ref<HealthResponse | null>(null);
const zones = ref<Zone[]>([]);
const rankings = ref<RankingsResponse | null>(null);
const history = ref<HistoryResponse | null>(null);
const selectedZoneId = ref<number | null>(null);
const cutoffInput = ref("");
const appliedCutoff = ref("");
const bootLoading = ref(true);
const dataLoading = ref(false);
const errorMessage = ref("");
let activeController: AbortController | null = null;

const numberFormatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 1 });
const integerFormatter = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
const compactFormatter = new Intl.NumberFormat("en-US", {
  notation: "compact",
  maximumFractionDigits: 1,
});
const dateTimeFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: "UTC",
});
const dateFormatter = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
  timeZone: "UTC",
});

function toInputValue(isoTimestamp: string): string {
  return new Date(isoTimestamp).toISOString().slice(0, 16);
}

function toUtcIso(inputValue: string): string {
  return new Date(`${inputValue}:00.000Z`).toISOString();
}

function readableError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "The dashboard could not load the requested backtest snapshot.";
}

const cutoffMin = computed(() =>
  health.value ? toInputValue(health.value.test_start_utc) : "",
);
const cutoffMax = computed(() => {
  if (!health.value) return "";
  const lastHour = new Date(health.value.test_end_utc_exclusive).getTime() - 60 * 60 * 1000;
  return toInputValue(new Date(lastHour).toISOString());
});
const selectedZone = computed(() =>
  zones.value.find((zone) => zone.zone_id === selectedZoneId.value),
);
const currentPoint = computed<ForecastPoint | null>(() => history.value?.points.at(-1) ?? null);
const currentRank = computed(() => {
  const index = rankings.value?.forecasts.findIndex(
    (forecast) => forecast.zone_id === selectedZoneId.value,
  );
  return index !== undefined && index >= 0 ? index + 1 : null;
});
const rankingMaximum = computed(() => rankings.value?.forecasts[0]?.prediction ?? 1);
const testRange = computed(() => {
  if (!health.value) return "Locked test window";
  return `${dateFormatter.format(new Date(health.value.test_start_utc))} – ${dateFormatter.format(
    new Date(health.value.test_end_utc_exclusive),
  )}`;
});
const appliedCutoffLabel = computed(() =>
  appliedCutoff.value
    ? `${dateTimeFormatter.format(new Date(appliedCutoff.value))} UTC`
    : "No cutoff selected",
);
const serviceStatus = computed(() => {
  if (bootLoading.value) return "Connecting";
  return health.value && !errorMessage.value ? "API online" : "Attention";
});

function rankingBarWidth(prediction: number): string {
  return `${Math.max(3, (prediction / rankingMaximum.value) * 100)}%`;
}

function beginRequest(): AbortController {
  activeController?.abort();
  activeController = new AbortController();
  return activeController;
}

async function initializeDashboard(): Promise<void> {
  const controller = beginRequest();
  bootLoading.value = true;
  errorMessage.value = "";

  try {
    const [healthPayload, zonesPayload] = await Promise.all([
      getHealth(controller.signal),
      getZones(controller.signal),
    ]);
    if (controller.signal.aborted) return;

    health.value = healthPayload;
    zones.value = zonesPayload.zones;
    cutoffInput.value = toInputValue(
      new Date(
        new Date(healthPayload.test_end_utc_exclusive).getTime() - 60 * 60 * 1000,
      ).toISOString(),
    );
    appliedCutoff.value = toUtcIso(cutoffInput.value);

    const rankingPayload = await getRankings(appliedCutoff.value, 10, controller.signal);
    if (controller.signal.aborted) return;
    rankings.value = rankingPayload;
    selectedZoneId.value = rankingPayload.forecasts[0]?.zone_id ?? zonesPayload.zones[0]?.zone_id ?? null;

    if (selectedZoneId.value !== null) {
      history.value = await getHistory(
        selectedZoneId.value,
        appliedCutoff.value,
        24,
        controller.signal,
      );
    }
  } catch (error) {
    if (!controller.signal.aborted) errorMessage.value = readableError(error);
  } finally {
    if (activeController === controller) {
      bootLoading.value = false;
      dataLoading.value = false;
    }
  }
}

async function loadSnapshot(): Promise<void> {
  if (!cutoffInput.value) return;
  const controller = beginRequest();
  dataLoading.value = true;
  errorMessage.value = "";
  appliedCutoff.value = toUtcIso(cutoffInput.value);

  try {
    const rankingRequest = getRankings(appliedCutoff.value, 10, controller.signal);
    const historyRequest =
      selectedZoneId.value === null
        ? null
        : getHistory(selectedZoneId.value, appliedCutoff.value, 24, controller.signal);
    const [rankingPayload, historyPayload] = await Promise.all([
      rankingRequest,
      historyRequest,
    ]);
    if (controller.signal.aborted) return;

    rankings.value = rankingPayload;
    if (historyPayload) {
      history.value = historyPayload;
    } else {
      selectedZoneId.value = rankingPayload.forecasts[0]?.zone_id ?? null;
      if (selectedZoneId.value !== null) {
        history.value = await getHistory(
          selectedZoneId.value,
          appliedCutoff.value,
          24,
          controller.signal,
        );
      }
    }
  } catch (error) {
    if (!controller.signal.aborted) errorMessage.value = readableError(error);
  } finally {
    if (activeController === controller) dataLoading.value = false;
  }
}

async function loadSelectedZone(): Promise<void> {
  if (selectedZoneId.value === null || !appliedCutoff.value) return;
  const controller = beginRequest();
  dataLoading.value = true;
  errorMessage.value = "";

  try {
    history.value = await getHistory(
      selectedZoneId.value,
      appliedCutoff.value,
      24,
      controller.signal,
    );
  } catch (error) {
    if (!controller.signal.aborted) errorMessage.value = readableError(error);
  } finally {
    if (activeController === controller) dataLoading.value = false;
  }
}

async function selectRankedZone(zoneId: number): Promise<void> {
  if (zoneId === selectedZoneId.value) return;
  selectedZoneId.value = zoneId;
  await loadSelectedZone();
}

async function retryRequest(): Promise<void> {
  if (!health.value) {
    await initializeDashboard();
    return;
  }
  await loadSnapshot();
}

onMounted(initializeDashboard);
onBeforeUnmount(() => activeController?.abort());
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <a class="brand" href="#dashboard" aria-label="UrbanFlow AI dashboard home">
        <span class="brand-mark" aria-hidden="true"><Route :size="21" :stroke-width="2.2" /></span>
        <span class="brand-copy">
          <strong>URBANFLOW</strong>
          <span>MODEL MONITOR</span>
        </span>
      </a>
      <div class="topbar-meta">
        <span class="environment-label">HISTORICAL TEST ENVIRONMENT</span>
        <span class="service-pill" :class="{ 'service-pill--warning': errorMessage }">
          <span class="status-dot" aria-hidden="true"></span>
          {{ serviceStatus }}
        </span>
      </div>
    </header>

    <main id="dashboard" class="dashboard">
      <section class="hero" aria-labelledby="dashboard-title">
        <div class="hero-copy">
          <p class="eyebrow"><Activity :size="16" aria-hidden="true" /> NYC YELLOW TAXI / HOURLY DEMAND</p>
          <h1 id="dashboard-title">Next-hour pickup backtest</h1>
          <p class="hero-description">
            Inspect zone-level model behavior at a locked historical cutoff. Predictions and observed
            outcomes share the same UTC target hour.
          </p>
        </div>

        <div class="hero-stat" role="group" aria-label="Dataset coverage">
          <span class="hero-stat-label">Test coverage</span>
          <strong>{{ testRange }}</strong>
          <span>{{ health ? `${health.zone_count} zones · ${integerFormatter.format(health.prediction_rows)} predictions` : "Loading manifest…" }}</span>
        </div>
      </section>

      <aside class="backtest-banner" aria-label="Historical backtest disclosure">
        <AlertTriangle :size="20" aria-hidden="true" />
        <div>
          <strong>Historical backtest — not a live operational forecast</strong>
          <span>Actual outcomes are visible only for retrospective evaluation inside the locked test window.</span>
        </div>
        <span class="banner-tag">EVALUATION ONLY</span>
      </aside>

      <section class="control-panel" aria-labelledby="controls-title">
        <div class="control-heading">
          <div>
            <p class="section-kicker">Snapshot controls</p>
            <h2 id="controls-title">Choose a decision point</h2>
          </div>
          <p>The cutoff closes available history and opens the one-hour target interval.</p>
        </div>

        <form class="filter-form" @submit.prevent="loadSnapshot">
          <label class="field">
            <span class="field-label"><Clock3 :size="15" aria-hidden="true" /> Target hour / cutoff</span>
            <input
              v-model="cutoffInput"
              type="datetime-local"
              name="cutoff"
              :min="cutoffMin"
              :max="cutoffMax"
              step="3600"
              required
              :disabled="bootLoading"
            />
            <small>UTC · whole-hour boundaries</small>
          </label>

          <label class="field">
            <span class="field-label"><MapPin :size="15" aria-hidden="true" /> Focus zone</span>
            <select
              v-model.number="selectedZoneId"
              name="zone"
              :disabled="bootLoading || zones.length === 0"
              @change="loadSelectedZone"
            >
              <option v-for="zone in zones" :key="zone.zone_id" :value="zone.zone_id">
                {{ zone.zone_name }} · {{ zone.borough }}
              </option>
            </select>
            <small>{{ selectedZone ? `TLC zone ${selectedZone.zone_id} · ${selectedZone.service_zone}` : "Loading served zones" }}</small>
          </label>

          <button class="primary-button" type="submit" :disabled="bootLoading || dataLoading">
            <RefreshCw :size="17" :class="{ spin: dataLoading }" aria-hidden="true" />
            {{ dataLoading ? "Updating" : "Apply snapshot" }}
          </button>
        </form>
      </section>

      <div v-if="errorMessage" class="error-panel" role="alert">
        <AlertTriangle :size="21" aria-hidden="true" />
        <div>
          <strong>Dashboard data unavailable</strong>
          <span>{{ errorMessage }}</span>
        </div>
        <button type="button" class="secondary-button" @click="retryRequest">Retry</button>
      </div>

      <section v-if="bootLoading" class="loading-layout" aria-label="Loading dashboard data" aria-live="polite">
        <span class="sr-only">Loading dashboard data</span>
        <div v-for="index in 4" :key="`metric-${index}`" class="skeleton skeleton--metric"></div>
        <div class="skeleton skeleton--chart"></div>
        <div class="skeleton skeleton--table"></div>
      </section>

      <template v-else-if="health && rankings && history && currentPoint">
        <div class="snapshot-bar" aria-live="polite">
          <span><Clock3 :size="15" aria-hidden="true" /> Snapshot: <strong>{{ appliedCutoffLabel }}</strong></span>
          <span><Database :size="15" aria-hidden="true" /> {{ health.model_version }}</span>
          <span v-if="dataLoading" class="updating-label"><RefreshCw :size="14" class="spin" aria-hidden="true" /> Refreshing data</span>
        </div>

        <section class="metric-grid" aria-label="Selected zone performance metrics" :aria-busy="dataLoading">
          <article class="metric-card metric-card--primary">
            <div class="metric-icon"><ArrowUpRight :size="19" aria-hidden="true" /></div>
            <div>
              <span class="metric-label">Predicted pickups</span>
              <strong>{{ numberFormatter.format(currentPoint.prediction) }}</strong>
              <small>{{ selectedZone?.zone_name }}</small>
            </div>
          </article>
          <article class="metric-card">
            <div class="metric-icon"><Activity :size="19" aria-hidden="true" /></div>
            <div>
              <span class="metric-label">Observed pickups</span>
              <strong>{{ integerFormatter.format(currentPoint.actual_trip_count) }}</strong>
              <small>Historical outcome</small>
            </div>
          </article>
          <article class="metric-card">
            <div class="metric-icon metric-icon--amber"><Gauge :size="19" aria-hidden="true" /></div>
            <div>
              <span class="metric-label">Absolute error</span>
              <strong>{{ numberFormatter.format(currentPoint.absolute_error) }}</strong>
              <small>At selected target hour</small>
            </div>
          </article>
          <article class="metric-card">
            <div class="metric-icon"><ShieldCheck :size="19" aria-hidden="true" /></div>
            <div>
              <span class="metric-label">Rolling MAE</span>
              <strong>{{ numberFormatter.format(history.mae) }}</strong>
              <small>{{ history.count }} available hour{{ history.count === 1 ? "" : "s" }}</small>
            </div>
          </article>
        </section>

        <section class="workspace" :aria-busy="dataLoading">
          <article class="panel chart-panel">
            <header class="panel-heading">
              <div>
                <p class="section-kicker">Zone {{ history.zone_id }} · {{ history.borough }}</p>
                <h2>{{ history.zone_name }}</h2>
              </div>
              <div class="chart-legend" role="group" aria-label="Chart legend">
                <span><i class="legend-line legend-line--actual"></i> Actual</span>
                <span><i class="legend-line legend-line--forecast"></i> Forecast</span>
              </div>
            </header>
            <div class="panel-context">
              <span>Pickup count</span>
              <span>{{ history.count }}-hour view ending {{ appliedCutoffLabel }}</span>
            </div>
            <ForecastChart :points="history.points" />
            <footer class="chart-footer">
              <span>Solid blue = observed</span>
              <span>Dashed amber = model prediction</span>
              <span class="current-marker"><i></i> Selected hour</span>
            </footer>
          </article>

          <article class="panel ranking-panel">
            <header class="panel-heading ranking-heading">
              <div>
                <p class="section-kicker">Demand concentration</p>
                <h2>Top forecast zones</h2>
              </div>
              <span class="rank-chip">TOP {{ rankings.count }}</span>
            </header>
            <p class="panel-subtitle">Ranked by predicted pickups at the selected target hour.</p>

            <div class="table-scroll">
              <table>
                <caption class="sr-only">Top zones ranked by forecast pickup count</caption>
                <thead>
                  <tr>
                    <th scope="col">Rank</th>
                    <th scope="col">Zone</th>
                    <th scope="col">Forecast</th>
                    <th scope="col">Actual</th>
                    <th scope="col">Error</th>
                  </tr>
                </thead>
                <tbody>
                  <tr
                    v-for="(forecast, index) in rankings.forecasts"
                    :key="forecast.zone_id"
                    :class="{ 'ranking-row--selected': forecast.zone_id === selectedZoneId }"
                  >
                    <td><span class="rank-number">{{ String(index + 1).padStart(2, "0") }}</span></td>
                    <td>
                      <button
                        type="button"
                        class="zone-button"
                        :aria-current="forecast.zone_id === selectedZoneId ? 'true' : undefined"
                        :aria-label="`Inspect ${forecast.zone_name}, rank ${index + 1}`"
                        @click="selectRankedZone(forecast.zone_id)"
                      >
                        <strong>{{ forecast.zone_name }}</strong>
                        <span>{{ forecast.borough }}</span>
                      </button>
                    </td>
                    <td class="forecast-cell">
                      <strong>{{ numberFormatter.format(forecast.prediction) }}</strong>
                      <span class="rank-bar" aria-hidden="true">
                        <i :style="{ width: rankingBarWidth(forecast.prediction) }"></i>
                      </span>
                    </td>
                    <td>{{ integerFormatter.format(forecast.actual_trip_count) }}</td>
                    <td>{{ numberFormatter.format(forecast.absolute_error) }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <footer class="ranking-footer">
              <span v-if="currentRank">Selected zone ranks #{{ currentRank }}</span>
              <span v-else>Selected zone is outside the top {{ rankings.count }}</span>
              <span>{{ compactFormatter.format(rankings.forecasts.reduce((sum, item) => sum + item.prediction, 0)) }} predicted top-zone pickups</span>
            </footer>
          </article>
        </section>

        <section class="provenance" aria-labelledby="provenance-title">
          <div class="provenance-icon"><Database :size="20" aria-hidden="true" /></div>
          <div>
            <p class="section-kicker">Reproducibility record</p>
            <h2 id="provenance-title">Locked evaluation artifacts</h2>
          </div>
          <dl>
            <div><dt>Model</dt><dd>{{ health.model_name }}</dd></div>
            <div><dt>Version</dt><dd>{{ health.model_version }}</dd></div>
            <div><dt>Source</dt><dd>Historical backtest</dd></div>
            <div><dt>Timezone</dt><dd>UTC</dd></div>
          </dl>
        </section>
      </template>
    </main>

    <footer class="site-footer">
      <span>UrbanFlow AI · Historical evaluation console</span>
      <span>Prediction target: zone × next hour</span>
    </footer>
  </div>
</template>
