"""Serve locked historical-backtest predictions through FastAPI."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

_SOURCE = "historical_backtest"
_CONFIG_KEYS = {
    "analysis_report_path",
    "predictions_path",
    "zone_lookup_path",
    "duckdb_threads",
    "duckdb_memory_limit",
}
_MEMORY_LIMIT = re.compile(r"^[1-9][0-9]*(?:MB|GB)$")
_REQUIRED_PREDICTION_FIELDS = {
    "zone_id": pa.int32(),
    "target_hour_utc": pa.timestamp("us", tz="UTC"),
    "split": pa.string(),
    "actual_trip_count": pa.int64(),
    "prediction": pa.float64(),
    "absolute_error": pa.float64(),
}


class ApiArtifactError(ValueError):
    """Invalid API configuration or serving artifact."""


@dataclass(frozen=True)
class ApiConfig:
    config_path: Path
    analysis_report_path: Path
    predictions_path: Path
    zone_lookup_path: Path
    duckdb_threads: int
    duckdb_memory_limit: str


class ZoneResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    zone_id: int = Field(ge=1)
    borough: str
    zone_name: str
    service_zone: str


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"]
    service: Literal["urbanflow-api"]
    source: Literal["historical_backtest"]
    model_name: str
    model_version: str
    test_start_utc: datetime
    test_end_utc_exclusive: datetime
    prediction_rows: int = Field(ge=1)
    zone_count: int = Field(ge=1)


class ZonesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["historical_backtest"]
    count: int = Field(ge=1)
    zones: list[ZoneResponse]


class ForecastResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["historical_backtest"]
    cutoff_utc: datetime
    target_hour_utc: datetime
    zone_id: int = Field(ge=1)
    borough: str
    zone_name: str
    service_zone: str
    prediction: float = Field(ge=0)
    actual_trip_count: int = Field(ge=0)
    absolute_error: float = Field(ge=0)
    model_name: str
    model_version: str

class RankingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["historical_backtest"]
    cutoff_utc: datetime
    target_hour_utc: datetime
    model_name: str
    model_version: str
    count: int = Field(ge=1)
    forecasts: list[ForecastResponse]


class HistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["historical_backtest"]
    zone_id: int = Field(ge=1)
    borough: str
    zone_name: str
    service_zone: str
    model_name: str
    model_version: str
    requested_hours: int = Field(ge=1, le=168)
    count: int = Field(ge=1)
    start_utc: datetime
    end_utc: datetime
    mae: float = Field(ge=0)
    points: list[ForecastResponse]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _required_path(data: dict[str, Any], key: str, parent: Path) -> Path:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ApiArtifactError(f"{key} must be a non-empty path string")
    return (parent / value).resolve()


def _required_integer(
    data: dict[str, Any], key: str, minimum: int, maximum: int
) -> int:
    value = data.get(key)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ApiArtifactError(f"{key} must be an integer from {minimum} to {maximum}")
    return value


def load_api_config(path: Path) -> ApiConfig:
    resolved = path.resolve()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApiArtifactError(f"cannot read API config {path}: {exc}") from exc
    if not isinstance(data, dict) or set(data) != _CONFIG_KEYS:
        raise ApiArtifactError(f"API config must contain exactly {sorted(_CONFIG_KEYS)}")

    memory_limit = data.get("duckdb_memory_limit")
    if not isinstance(memory_limit, str) or not _MEMORY_LIMIT.fullmatch(memory_limit):
        raise ApiArtifactError("duckdb_memory_limit must look like 256MB or 1GB")

    parent = resolved.parent
    return ApiConfig(
        config_path=resolved,
        analysis_report_path=_required_path(
            data, "analysis_report_path", parent
        ),
        predictions_path=_required_path(data, "predictions_path", parent),
        zone_lookup_path=_required_path(data, "zone_lookup_path", parent),
        duckdb_threads=_required_integer(data, "duckdb_threads", 1, 4),
        duckdb_memory_limit=memory_limit,
    )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ApiArtifactError(f"missing {label}: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApiArtifactError(f"invalid {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ApiArtifactError(f"{label} must contain a JSON object")
    return value


def _parse_report_timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ApiArtifactError(f"{label} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiArtifactError(f"{label} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApiArtifactError(f"{label} must include a UTC offset")
    if parsed.utcoffset().total_seconds() != 0:
        raise ApiArtifactError(f"{label} must use UTC")
    parsed = parsed.astimezone(UTC)
    if parsed.minute or parsed.second or parsed.microsecond:
        raise ApiArtifactError(f"{label} must be a whole UTC hour")
    return parsed


def _validate_prediction_schema(path: Path) -> None:
    if not path.is_file():
        raise ApiArtifactError(f"missing predictions: {path}")
    schema = pq.read_schema(path)
    for name, expected_type in _REQUIRED_PREDICTION_FIELDS.items():
        if name not in schema.names or schema.field(name).type != expected_type:
            raise ApiArtifactError(
                f"predictions field {name!r} must have type {expected_type}"
            )


def _serving_metadata(
    report: dict[str, Any], config: ApiConfig
) -> tuple[str, str, str, datetime, datetime, int, int, int]:
    try:
        decision = report["serving_decision"]
        model = report["model"]
        baseline = report["baseline"]
        artifacts = report["artifacts"]
        evaluation = report["test_evaluation"]
        test_window = report["test_window"]
        model_version = decision["model_version"]
        model_name = decision["algorithm"]
    except (KeyError, TypeError) as exc:
        raise ApiArtifactError(f"analysis report is missing required field: {exc}") from exc

    if model_version == model.get("version"):
        expected_hash = artifacts.get("model_predictions_sha256")
        expected_name = model.get("name")
    elif model_version == baseline.get("version"):
        expected_hash = artifacts.get("baseline_predictions_sha256")
        expected_name = baseline.get("name")
    else:
        raise ApiArtifactError("serving decision does not identify a reported model")
    if model_name != expected_name:
        raise ApiArtifactError("serving algorithm and model version do not match")
    if not isinstance(expected_hash, str) or _sha256(config.predictions_path) != expected_hash:
        raise ApiArtifactError("predictions do not match the serving decision hash")
    zone_hash = artifacts.get("zone_lookup_sha256")
    if not isinstance(zone_hash, str) or _sha256(config.zone_lookup_path) != zone_hash:
        raise ApiArtifactError("zone lookup does not match the analysis report hash")

    try:
        row_count = int(evaluation["rows"])
        zone_count = int(evaluation["zone_count"])
        hour_count = int(evaluation["hour_count"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiArtifactError("analysis report has invalid test dimensions") from exc
    if min(row_count, zone_count, hour_count) < 1:
        raise ApiArtifactError("analysis report test dimensions must be positive")
    if row_count != zone_count * hour_count:
        raise ApiArtifactError("analysis report test predictions are not a full grid")

    start = _parse_report_timestamp(test_window.get("start_utc"), "test start")
    end = _parse_report_timestamp(
        test_window.get("end_utc_exclusive"), "test end"
    )
    if end <= start:
        raise ApiArtifactError("test end must be later than test start")
    if int((end - start).total_seconds() // 3600) != hour_count:
        raise ApiArtifactError("test window does not match reported hour count")
    return (
        model_name,
        model_version,
        expected_hash,
        start,
        end,
        row_count,
        zone_count,
        hour_count,
    )


class PredictionStore:
    """Validated, in-memory DuckDB view of the locked test predictions."""

    def __init__(self, config: ApiConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._connection = duckdb.connect()
        self._closed = False
        try:
            self._initialize()
        except Exception:
            self._connection.close()
            self._closed = True
            raise

    def _initialize(self) -> None:
        report = _load_json(self._config.analysis_report_path, "analysis report")
        _validate_prediction_schema(self._config.predictions_path)
        if not self._config.zone_lookup_path.is_file():
            raise ApiArtifactError(
                f"missing zone lookup: {self._config.zone_lookup_path}"
            )
        (
            self.model_name,
            self.model_version,
            self.predictions_sha256,
            self.test_start_utc,
            self.test_end_utc_exclusive,
            self.prediction_rows,
            self.zone_count,
            self.hour_count,
        ) = _serving_metadata(report, self._config)

        self._connection.execute(f"SET threads = {self._config.duckdb_threads}")
        self._connection.execute(
            f"SET memory_limit = {_sql_literal(self._config.duckdb_memory_limit)}"
        )
        self._connection.execute("SET preserve_insertion_order = false")
        predictions_path = _sql_literal(self._config.predictions_path.as_posix())
        zone_path = _sql_literal(self._config.zone_lookup_path.as_posix())
        self._connection.execute(f"""
            CREATE TEMP TABLE forecasts AS
            SELECT zone_id, target_hour_utc, actual_trip_count,
                   prediction, absolute_error
            FROM read_parquet({predictions_path})
            WHERE split = 'test'
        """)
        self._connection.execute(f"""
            CREATE TEMP TABLE zone_lookup AS
            SELECT CAST(LocationID AS INTEGER) AS zone_id,
                   CAST(Borough AS VARCHAR) AS borough,
                   CAST(Zone AS VARCHAR) AS zone_name,
                   CAST(service_zone AS VARCHAR) AS service_zone
            FROM read_csv({zone_path}, header = true, all_varchar = true)
        """)
        self._validate_loaded_data()
        self._connection.execute("""
            CREATE UNIQUE INDEX forecast_key
            ON forecasts (zone_id, target_hour_utc)
        """)
        self._connection.execute("""
            CREATE TEMP TABLE served_zones AS
            SELECT DISTINCT z.zone_id, z.borough, z.zone_name, z.service_zone
            FROM zone_lookup z
            INNER JOIN forecasts f USING (zone_id)
        """)
        zone_rows = self._connection.execute("""
            SELECT zone_id, borough, zone_name, service_zone
            FROM served_zones
            ORDER BY zone_id
        """).fetchall()
        self.zones = tuple(
            ZoneResponse(
                zone_id=int(row[0]),
                borough=str(row[1]),
                zone_name=str(row[2]),
                service_zone=str(row[3]),
            )
            for row in zone_rows
        )
        self.zone_ids = frozenset(zone.zone_id for zone in self.zones)

    def _validate_loaded_data(self) -> None:
        summary = self._connection.execute("""
            SELECT count(*), count(DISTINCT (zone_id, target_hour_utc)),
                   count(DISTINCT zone_id), count(DISTINCT target_hour_utc),
                   count(*) FILTER (WHERE zone_id IS NULL
                       OR target_hour_utc IS NULL
                       OR actual_trip_count IS NULL
                       OR prediction IS NULL
                       OR absolute_error IS NULL),
                   count(*) FILTER (WHERE actual_trip_count < 0 OR prediction < 0
                       OR absolute_error < 0),
                   max(abs(absolute_error - abs(actual_trip_count - prediction))),
                   epoch(min(target_hour_utc)), epoch(max(target_hour_utc))
            FROM forecasts
        """).fetchone()
        if summary[0] != summary[1]:
            raise ApiArtifactError("test predictions contain duplicate keys")
        if summary[4] != 0 or summary[5] != 0:
            raise ApiArtifactError("test predictions contain NULL or negative values")
        if summary[6] is None or summary[6] > 0.000000000001:
            raise ApiArtifactError("test prediction absolute errors are inconsistent")
        expected_last = int(self.test_end_utc_exclusive.timestamp()) - 3600
        dimensions = tuple(int(value) for value in summary[:4])
        if dimensions != (
            self.prediction_rows,
            self.prediction_rows,
            self.zone_count,
            self.hour_count,
        ):
            raise ApiArtifactError("prediction dimensions do not match analysis report")
        if int(summary[7]) != int(self.test_start_utc.timestamp()):
            raise ApiArtifactError("first prediction hour does not match test window")
        if int(summary[8]) != expected_last:
            raise ApiArtifactError("last prediction hour does not match test window")

        lookup_summary = self._connection.execute("""
            SELECT count(*) - count(DISTINCT zone_id),
                   count(*) FILTER (WHERE zone_id IS NULL OR borough IS NULL
                       OR zone_name IS NULL OR service_zone IS NULL)
            FROM zone_lookup
        """).fetchone()
        if lookup_summary != (0, 0):
            raise ApiArtifactError("zone lookup contains duplicate or NULL values")
        missing_zones = self._connection.execute("""
            SELECT count(*)
            FROM (SELECT DISTINCT zone_id FROM forecasts) f
            LEFT JOIN zone_lookup z USING (zone_id)
            WHERE z.zone_id IS NULL
        """).fetchone()[0]
        if missing_zones != 0:
            raise ApiArtifactError("zone lookup does not cover every served zone")

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def _forecast_response(self, row: tuple[Any, ...]) -> ForecastResponse:
        target = datetime.fromtimestamp(row[1], UTC)
        return ForecastResponse(
            source=_SOURCE,
            cutoff_utc=target,
            target_hour_utc=target,
            zone_id=int(row[0]),
            borough=str(row[5]),
            zone_name=str(row[6]),
            service_zone=str(row[7]),
            prediction=float(row[2]),
            actual_trip_count=int(row[3]),
            absolute_error=float(row[4]),
            model_name=self.model_name,
            model_version=self.model_version,
        )

    def forecast(self, zone_id: int, target_hour_utc: datetime) -> ForecastResponse | None:
        with self._lock:
            row = self._connection.execute("""
                SELECT f.zone_id, epoch(f.target_hour_utc), f.prediction,
                       f.actual_trip_count, f.absolute_error,
                       z.borough, z.zone_name, z.service_zone
                FROM forecasts f
                INNER JOIN served_zones z USING (zone_id)
                WHERE f.zone_id = ?
                  AND f.target_hour_utc = to_timestamp(?)
            """, [zone_id, target_hour_utc.timestamp()]).fetchone()
        if row is None:
            return None
        return self._forecast_response(row)

    def rankings(
        self,
        target_hour_utc: datetime,
        limit: int,
    ) -> tuple[ForecastResponse, ...]:
        with self._lock:
            rows = self._connection.execute("""
                SELECT f.zone_id, epoch(f.target_hour_utc), f.prediction,
                       f.actual_trip_count, f.absolute_error,
                       z.borough, z.zone_name, z.service_zone
                FROM forecasts f
                INNER JOIN served_zones z USING (zone_id)
                WHERE f.target_hour_utc = to_timestamp(?)
                ORDER BY f.prediction DESC, f.zone_id
                LIMIT ?
            """, [target_hour_utc.timestamp(), limit]).fetchall()
        return tuple(self._forecast_response(row) for row in rows)

    def history(
        self,
        zone_id: int,
        end_utc: datetime,
        hours: int,
    ) -> tuple[ForecastResponse, ...]:
        start_utc = end_utc - timedelta(hours=hours - 1)
        with self._lock:
            rows = self._connection.execute("""
                SELECT f.zone_id, epoch(f.target_hour_utc), f.prediction,
                       f.actual_trip_count, f.absolute_error,
                       z.borough, z.zone_name, z.service_zone
                FROM forecasts f
                INNER JOIN served_zones z USING (zone_id)
                WHERE f.zone_id = ?
                  AND f.target_hour_utc BETWEEN to_timestamp(?) AND to_timestamp(?)
                ORDER BY f.target_hour_utc
            """, [zone_id, start_utc.timestamp(), end_utc.timestamp()]).fetchall()
        return tuple(self._forecast_response(row) for row in rows)


def _get_store(request: Request) -> PredictionStore:
    store = getattr(request.app.state, "prediction_store", None)
    if not isinstance(store, PredictionStore):
        raise HTTPException(status_code=503, detail="prediction store is not ready")
    return store


def _validated_cutoff(value: datetime, store: PredictionStore) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(
            status_code=422,
            detail="cutoff_utc must include UTC offset Z or +00:00",
        )
    if value.utcoffset().total_seconds() != 0:
        raise HTTPException(
            status_code=422,
            detail="cutoff_utc must use UTC offset Z or +00:00",
        )
    cutoff = value.astimezone(UTC)
    if cutoff.minute or cutoff.second or cutoff.microsecond:
        raise HTTPException(
            status_code=422,
            detail="cutoff_utc must be a whole UTC hour",
        )
    if not store.test_start_utc <= cutoff < store.test_end_utc_exclusive:
        raise HTTPException(
            status_code=422,
            detail=(
                "cutoff_utc must be inside the locked test window "
                f"[{store.test_start_utc.isoformat()}, "
                f"{store.test_end_utc_exclusive.isoformat()})"
            ),
        )
    return cutoff


def create_app(config_path: Path = Path("configs/api.json")) -> FastAPI:
    """Create an API whose lifespan validates and loads serving artifacts."""

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        store = PredictionStore(load_api_config(config_path))
        application.state.prediction_store = store
        try:
            yield
        finally:
            store.close()
            application.state.prediction_store = None

    application = FastAPI(
        title="UrbanFlow AI",
        version="0.1.0",
        description=(
            "Historical backtest API for next-hour NYC Yellow Taxi pickup forecasts. "
            "cutoff_utc is the exclusive end of observed data and the start of the "
            "forecast target hour."
        ),
        lifespan=lifespan,
    )

    @application.get("/health", response_model=HealthResponse)
    def health(store: Annotated[PredictionStore, Depends(_get_store)]) -> HealthResponse:
        return HealthResponse(
            status="ok",
            service="urbanflow-api",
            source=_SOURCE,
            model_name=store.model_name,
            model_version=store.model_version,
            test_start_utc=store.test_start_utc,
            test_end_utc_exclusive=store.test_end_utc_exclusive,
            prediction_rows=store.prediction_rows,
            zone_count=store.zone_count,
        )

    @application.get("/zones", response_model=ZonesResponse)
    def zones(store: Annotated[PredictionStore, Depends(_get_store)]) -> ZonesResponse:
        return ZonesResponse(
            source=_SOURCE,
            count=len(store.zones),
            zones=list(store.zones),
        )

    @application.get("/forecast", response_model=ForecastResponse)
    def forecast(
        cutoff_utc: Annotated[
            datetime,
            Query(
                description=(
                    "UTC hour that ends available history and starts the forecast "
                    "target interval"
                )
            ),
        ],
        zone_id: Annotated[int, Query(ge=1)],
        store: Annotated[PredictionStore, Depends(_get_store)],
    ) -> ForecastResponse:
        cutoff = _validated_cutoff(cutoff_utc, store)
        if zone_id not in store.zone_ids:
            raise HTTPException(
                status_code=404,
                detail=f"zone_id {zone_id} is not available in the test predictions",
            )
        result = store.forecast(zone_id, cutoff)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"forecast is unavailable for zone_id {zone_id} at "
                    f"{cutoff.isoformat()}"
                ),
            )
        return result

    @application.get("/rankings", response_model=RankingsResponse)
    def rankings(
        cutoff_utc: Annotated[
            datetime,
            Query(description="Forecast target hour in UTC"),
        ],
        store: Annotated[PredictionStore, Depends(_get_store)],
        limit: Annotated[int, Query(ge=1, le=25)] = 10,
    ) -> RankingsResponse:
        cutoff = _validated_cutoff(cutoff_utc, store)
        forecasts = store.rankings(cutoff, limit)
        if not forecasts:
            raise HTTPException(
                status_code=404,
                detail=f"rankings are unavailable at {cutoff.isoformat()}",
            )
        return RankingsResponse(
            source=_SOURCE,
            cutoff_utc=cutoff,
            target_hour_utc=cutoff,
            model_name=store.model_name,
            model_version=store.model_version,
            count=len(forecasts),
            forecasts=list(forecasts),
        )

    @application.get("/history", response_model=HistoryResponse)
    def history(
        zone_id: Annotated[int, Query(ge=1)],
        end_utc: Annotated[
            datetime,
            Query(description="Last included forecast hour in UTC"),
        ],
        store: Annotated[PredictionStore, Depends(_get_store)],
        hours: Annotated[int, Query(ge=1, le=168)] = 24,
    ) -> HistoryResponse:
        end = _validated_cutoff(end_utc, store)
        if zone_id not in store.zone_ids:
            raise HTTPException(
                status_code=404,
                detail=f"zone_id {zone_id} is not available in the test predictions",
            )
        points = store.history(zone_id, end, hours)
        if not points:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"history is unavailable for zone_id {zone_id} at "
                    f"{end.isoformat()}"
                ),
            )
        latest = points[-1]
        return HistoryResponse(
            source=_SOURCE,
            zone_id=latest.zone_id,
            borough=latest.borough,
            zone_name=latest.zone_name,
            service_zone=latest.service_zone,
            model_name=store.model_name,
            model_version=store.model_version,
            requested_hours=hours,
            count=len(points),
            start_utc=points[0].target_hour_utc,
            end_utc=points[-1].target_hour_utc,
            mae=sum(point.absolute_error for point in points) / len(points),
            points=list(points),
        )

    return application


app = create_app(Path(os.environ.get("URBANFLOW_API_CONFIG", "configs/api.json")))
