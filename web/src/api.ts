export type BacktestSource = "historical_backtest";

export interface HealthResponse {
  status: "ok";
  service: "urbanflow-api";
  source: BacktestSource;
  model_name: string;
  model_version: string;
  test_start_utc: string;
  test_end_utc_exclusive: string;
  prediction_rows: number;
  zone_count: number;
}

export interface Zone {
  zone_id: number;
  borough: string;
  zone_name: string;
  service_zone: string;
}

export interface ZonesResponse {
  source: BacktestSource;
  count: number;
  zones: Zone[];
}

export interface ForecastPoint extends Zone {
  source: BacktestSource;
  cutoff_utc: string;
  target_hour_utc: string;
  prediction: number;
  actual_trip_count: number;
  absolute_error: number;
  model_name: string;
  model_version: string;
}

export interface RankingsResponse {
  source: BacktestSource;
  cutoff_utc: string;
  target_hour_utc: string;
  model_name: string;
  model_version: string;
  count: number;
  forecasts: ForecastPoint[];
}

export interface HistoryResponse extends Zone {
  source: BacktestSource;
  model_name: string;
  model_version: string;
  requested_hours: number;
  count: number;
  start_utc: string;
  end_utc: string;
  mae: number;
  points: ForecastPoint[];
}

const API_BASE = (import.meta.env.VITE_API_BASE_URL ?? "/api").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { Accept: "application/json" },
    signal,
  });

  if (!response.ok) {
    let message = `Request failed with status ${response.status}`;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) message = payload.detail;
    } catch {
      // Preserve the status-based message when an upstream response is not JSON.
    }
    throw new ApiError(message, response.status);
  }

  return (await response.json()) as T;
}

function query(values: Record<string, string | number>): string {
  return new URLSearchParams(
    Object.entries(values).map(([key, value]) => [key, String(value)]),
  ).toString();
}

export function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return request<HealthResponse>("/health", signal);
}

export function getZones(signal?: AbortSignal): Promise<ZonesResponse> {
  return request<ZonesResponse>("/zones", signal);
}

export function getRankings(
  cutoffUtc: string,
  limit = 10,
  signal?: AbortSignal,
): Promise<RankingsResponse> {
  return request<RankingsResponse>(
    `/rankings?${query({ cutoff_utc: cutoffUtc, limit })}`,
    signal,
  );
}

export function getHistory(
  zoneId: number,
  endUtc: string,
  hours = 24,
  signal?: AbortSignal,
): Promise<HistoryResponse> {
  return request<HistoryResponse>(
    `/history?${query({ zone_id: zoneId, end_utc: endUtc, hours })}`,
    signal,
  );
}
