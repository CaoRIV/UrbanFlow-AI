"""Evaluate a leakage-safe seasonal-naive baseline on fixed UTC splits."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import psutil
import pyarrow as pa
import pyarrow.parquet as pq

from urbanflow.aggregate_hourly import (
    _sha256,
    _sql_literal,
    _start_rss_sampler,
    _write_json,
    load_aggregate_config,
)
from urbanflow.build_hourly_grid import _load_config as load_grid_config
from urbanflow.download_data import load_config as load_source_config

_SPLIT_NAMES = ("train", "validation", "test")
_GRID_SCHEMA = {
    "zone_id": pa.int32(),
    "target_hour_utc": pa.timestamp("us", tz="UTC"),
    "trip_count": pa.int64(),
    "source_month": pa.string(),
    "source_status": pa.string(),
}
_PREDICTION_SCHEMA = {
    "zone_id": pa.int32(),
    "target_hour_utc": pa.timestamp("us", tz="UTC"),
    "split": pa.string(),
    "actual_trip_count": pa.int64(),
    "source_status": pa.string(),
    "seasonal_lag_trip_count": pa.int64(),
    "prediction": pa.float64(),
    "prediction_source": pa.string(),
    "used_fallback": pa.bool_(),
    "absolute_error": pa.float64(),
}


class BaselineError(ValueError):
    """Invalid baseline configuration, input, or output contract."""


@dataclass(frozen=True)
class SplitBoundary:
    name: str
    start_utc: datetime
    end_utc_exclusive: datetime


@dataclass(frozen=True)
class BaselineConfig:
    config_path: Path
    grid_config_path: Path
    predictions_path: Path
    metrics_path: Path
    seasonal_lag_hours: int
    splits: tuple[SplitBoundary, ...]


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BaselineError(f"{key} must be a non-empty string")
    return value.strip()


def _parse_utc_hour(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise BaselineError(f"{field} must be a UTC ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise BaselineError(f"{field} must be a UTC ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise BaselineError(f"{field} must include a UTC offset")
    parsed = parsed.astimezone(UTC)
    if parsed.minute or parsed.second or parsed.microsecond:
        raise BaselineError(f"{field} must align to a whole UTC hour")
    return parsed


def load_baseline_config(path: Path) -> BaselineConfig:
    resolved = path.resolve()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineError(f"cannot read baseline config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BaselineError("baseline config root must be an object")

    lag_hours = data.get("seasonal_lag_hours")
    if not isinstance(lag_hours, int) or isinstance(lag_hours, bool) or lag_hours <= 0:
        raise BaselineError("seasonal_lag_hours must be a positive integer")

    split_data = data.get("splits")
    if not isinstance(split_data, dict) or set(split_data) != set(_SPLIT_NAMES):
        raise BaselineError("splits must contain exactly train, validation, and test")
    splits: list[SplitBoundary] = []
    for name in _SPLIT_NAMES:
        item = split_data[name]
        if not isinstance(item, dict):
            raise BaselineError(f"splits.{name} must be an object")
        start = _parse_utc_hour(item.get("start_utc"), f"splits.{name}.start_utc")
        end = _parse_utc_hour(
            item.get("end_utc_exclusive"),
            f"splits.{name}.end_utc_exclusive",
        )
        if start >= end:
            raise BaselineError(f"splits.{name} must have start before end")
        splits.append(SplitBoundary(name, start, end))
    for previous, current in zip(splits, splits[1:]):
        if previous.end_utc_exclusive != current.start_utc:
            raise BaselineError("train, validation, and test splits must be contiguous")

    base = resolved.parent
    grid_config_path = (base / _required_string(data, "grid_config")).resolve()
    predictions_path = (base / _required_string(data, "predictions_path")).resolve()
    metrics_path = (base / _required_string(data, "metrics_path")).resolve()
    if predictions_path.suffix.lower() != ".parquet":
        raise BaselineError("predictions_path must end in .parquet")
    if metrics_path.suffix.lower() != ".json":
        raise BaselineError("metrics_path must end in .json")
    if predictions_path == metrics_path:
        raise BaselineError("predictions_path and metrics_path must differ")

    return BaselineConfig(
        config_path=resolved,
        grid_config_path=grid_config_path,
        predictions_path=predictions_path,
        metrics_path=metrics_path,
        seasonal_lag_hours=lag_hours,
        splits=tuple(splits),
    )


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_schemas(paths: list[Path]) -> None:
    for path in paths:
        if not path.is_file():
            raise BaselineError(
                f"missing hourly grid: {path}; run urbanflow.build_hourly_grid first"
            )
        schema = pq.read_schema(path)
        if schema.names != list(_GRID_SCHEMA) or any(
            schema.field(name).type != expected
            for name, expected in _GRID_SCHEMA.items()
        ):
            raise BaselineError(f"invalid hourly grid schema for {path}: {schema}")


def _create_grid(
    connection: duckdb.DuckDBPyConnection,
    paths: list[Path],
    config: BaselineConfig,
) -> dict[str, int]:
    _validate_schemas(paths)
    path_list = ", ".join(_sql_literal(path.as_posix()) for path in paths)
    connection.execute(
        f"CREATE TEMP TABLE grid AS SELECT * FROM read_parquet([{path_list}])"
    )
    row = connection.execute("""
        SELECT count(*) AS row_count,
               count(DISTINCT (zone_id, target_hour_utc)) AS key_count,
               epoch(min(target_hour_utc)), epoch(max(target_hour_utc)),
               count(DISTINCT zone_id) AS zone_count,
               count(DISTINCT target_hour_utc) AS hour_count,
               count(*) FILTER (WHERE zone_id <= 0 OR trip_count < 0
                 OR date_trunc('hour', target_hour_utc) != target_hour_utc
                 OR source_status NOT IN ('available', 'source_missing', 'dst_ambiguous')
                 OR ((source_status = 'available') != (trip_count IS NOT NULL))) AS bad_rows
        FROM grid
    """).fetchone()
    if row is None or row[0] == 0:
        raise BaselineError("hourly grid is empty")

    first = config.splits[0].start_utc
    last_exclusive = config.splits[-1].end_utc_exclusive
    expected_hours = int((last_exclusive - first).total_seconds() // 3600)
    expected_rows = expected_hours * row[4]
    bad_hour_shapes = connection.execute("""
        SELECT count(*) FROM (
            SELECT target_hour_utc, count(*) AS rows,
                   count(DISTINCT zone_id) AS zones
            FROM grid
            GROUP BY target_hour_utc
            HAVING rows != ? OR zones != ?
        ) malformed
    """, [row[4], row[4]]).fetchone()[0]
    if (
        row[0] != row[1]
        or row[0] != expected_rows
        or row[2] != first.timestamp()
        or row[3] + 3600 != last_exclusive.timestamp()
        or row[5] != expected_hours
        or row[6]
        or bad_hour_shapes
    ):
        raise BaselineError(
            "hourly grid must be rectangular, unique, valid, and exactly cover the configured splits"
        )
    return {
        "row_count": row[0],
        "zone_count": row[4],
        "hour_count": row[5],
    }


def _create_predictions(
    connection: duckdb.DuckDBPyConnection,
    config: BaselineConfig,
) -> None:
    connection.execute("""
        CREATE TEMP TABLE split_bounds (
            split VARCHAR,
            start_utc TIMESTAMPTZ,
            end_utc_exclusive TIMESTAMPTZ
        )
    """)
    connection.executemany(
        "INSERT INTO split_bounds VALUES (?, ?, ?)",
        [
            (split.name, split.start_utc, split.end_utc_exclusive)
            for split in config.splits
        ],
    )
    connection.execute("""
        CREATE TEMP TABLE assigned AS
        SELECT g.*, b.split
        FROM grid g
        JOIN split_bounds b
          ON g.target_hour_utc >= b.start_utc
         AND g.target_hour_utc < b.end_utc_exclusive
    """)
    assigned_count = connection.execute("SELECT count(*) FROM assigned").fetchone()[0]
    grid_count = connection.execute("SELECT count(*) FROM grid").fetchone()[0]
    if assigned_count != grid_count:
        raise BaselineError("not every grid row belongs to exactly one configured split")

    lag_interval = f"INTERVAL '{config.seasonal_lag_hours} hours'"
    connection.execute(f"""
        CREATE TEMP TABLE enriched AS
        SELECT a.*,
               lag.trip_count AS seasonal_lag_trip_count,
               avg(a.trip_count) OVER (
                   PARTITION BY a.zone_id
                   ORDER BY a.target_hour_utc
                   ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
               ) AS prior_zone_mean
        FROM assigned a
        LEFT JOIN grid lag
          ON lag.zone_id = a.zone_id
         AND lag.target_hour_utc = a.target_hour_utc - {lag_interval}
    """)
    connection.execute("""
        CREATE TEMP TABLE train_zone_means AS
        SELECT zone_id, avg(trip_count) AS train_zone_mean
        FROM assigned
        WHERE split = 'train' AND trip_count IS NOT NULL
        GROUP BY zone_id
    """)
    connection.execute("""
        CREATE TEMP TABLE raw_predictions AS
        SELECT e.zone_id, e.target_hour_utc, e.split,
               e.trip_count AS actual_trip_count,
               e.source_status, e.seasonal_lag_trip_count,
               CASE
                   WHEN e.seasonal_lag_trip_count IS NOT NULL
                       THEN CAST(e.seasonal_lag_trip_count AS DOUBLE)
                   WHEN e.split = 'train' AND e.prior_zone_mean IS NOT NULL
                       THEN e.prior_zone_mean
                   WHEN e.split != 'train' AND t.train_zone_mean IS NOT NULL
                       THEN t.train_zone_mean
                   ELSE NULL
               END AS raw_prediction,
               CASE
                   WHEN e.seasonal_lag_trip_count IS NOT NULL THEN 'seasonal_lag'
                   WHEN e.split = 'train' AND e.prior_zone_mean IS NOT NULL
                       THEN 'prior_zone_mean'
                   WHEN e.split != 'train' AND t.train_zone_mean IS NOT NULL
                       THEN 'train_zone_mean'
                   ELSE 'unavailable'
               END AS prediction_source
        FROM enriched e
        LEFT JOIN train_zone_means t USING (zone_id)
    """)
    connection.execute("""
        CREATE TEMP TABLE predictions AS
        SELECT zone_id, target_hour_utc, split, actual_trip_count,
               source_status, seasonal_lag_trip_count,
               CASE WHEN raw_prediction IS NOT NULL
                    THEN greatest(raw_prediction, 0.0)
                    ELSE NULL END::DOUBLE AS prediction,
               prediction_source,
               prediction_source IN ('prior_zone_mean', 'train_zone_mean')
                   AS used_fallback,
               CASE
                   WHEN actual_trip_count IS NOT NULL AND raw_prediction IS NOT NULL
                       THEN abs(actual_trip_count - greatest(raw_prediction, 0.0))
                   ELSE NULL
               END::DOUBLE AS absolute_error
        FROM raw_predictions
    """)


def _number(value: Any) -> float | None:
    return None if value is None else round(float(value), 6)


def _split_metrics(
    connection: duckdb.DuckDBPyConnection,
    boundary: SplitBoundary,
) -> dict[str, Any]:
    row = connection.execute("""
        SELECT count(*) AS total_rows,
               count(DISTINCT zone_id) AS zone_count,
               count(DISTINCT target_hour_utc) AS hour_count,
               count(*) FILTER (WHERE actual_trip_count IS NOT NULL) AS target_rows,
               count(*) FILTER (WHERE actual_trip_count IS NULL) AS missing_target_rows,
               count(*) FILTER (WHERE prediction IS NOT NULL) AS prediction_rows,
               count(*) FILTER (WHERE prediction IS NULL) AS missing_prediction_rows,
               count(*) FILTER (WHERE actual_trip_count IS NOT NULL
                                  AND prediction IS NOT NULL) AS scored_rows,
               count(*) FILTER (WHERE used_fallback) AS fallback_rows,
               avg(absolute_error) AS mae,
               sum(absolute_error) AS absolute_error_sum,
               sum(actual_trip_count) FILTER (
                   WHERE actual_trip_count IS NOT NULL AND prediction IS NOT NULL
               ) AS actual_sum
        FROM predictions WHERE split = ?
    """, [boundary.name]).fetchone()
    source_counts = {
        source: count
        for source, count in connection.execute("""
            SELECT prediction_source, count(*)
            FROM predictions WHERE split = ?
            GROUP BY prediction_source ORDER BY prediction_source
        """, [boundary.name]).fetchall()
    }
    by_zone = [
        {
            "zone_id": zone_id,
            "scored_rows": scored_rows,
            "mae": _number(mae),
        }
        for zone_id, scored_rows, mae in connection.execute("""
            SELECT zone_id,
                   count(*) FILTER (WHERE absolute_error IS NOT NULL),
                   avg(absolute_error)
            FROM predictions WHERE split = ?
            GROUP BY zone_id ORDER BY zone_id
        """, [boundary.name]).fetchall()
    ]
    by_hour = [
        {
            "utc_hour": utc_hour,
            "scored_rows": scored_rows,
            "mae": _number(mae),
        }
        for utc_hour, scored_rows, mae in connection.execute("""
            SELECT CAST(extract(hour FROM target_hour_utc) AS INTEGER) AS utc_hour,
                   count(*) FILTER (WHERE absolute_error IS NOT NULL),
                   avg(absolute_error)
            FROM predictions WHERE split = ?
            GROUP BY utc_hour ORDER BY utc_hour
        """, [boundary.name]).fetchall()
    ]
    denominator = row[11]
    wape = None if denominator in (None, 0) else row[10] / denominator
    return {
        "start_utc": _format_utc(boundary.start_utc),
        "end_utc_exclusive": _format_utc(boundary.end_utc_exclusive),
        "total_rows": row[0],
        "zone_count": row[1],
        "hour_count": row[2],
        "target_rows": row[3],
        "missing_target_rows": row[4],
        "prediction_rows": row[5],
        "missing_prediction_rows": row[6],
        "scored_rows": row[7],
        "fallback_rows": row[8],
        "fallback_rate": _number(row[8] / row[5]) if row[5] else None,
        "prediction_source_counts": source_counts,
        "mae": _number(row[9]),
        "wape": _number(wape),
        "actual_sum": row[11],
        "absolute_error_sum": _number(row[10]),
        "mae_by_zone": by_zone,
        "mae_by_utc_hour": by_hour,
    }


def _write_predictions(
    connection: duckdb.DuckDBPyConnection,
    path: Path,
) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".parquet", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        connection.execute(f"""
            COPY (
                SELECT * FROM predictions ORDER BY target_hour_utc, zone_id
            ) TO {_sql_literal(temporary.as_posix())}
              (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        schema = pq.read_schema(temporary)
        if schema.names != list(_PREDICTION_SCHEMA) or any(
            schema.field(name).type != expected
            for name, expected in _PREDICTION_SCHEMA.items()
        ):
            raise BaselineError(f"invalid prediction output schema: {schema}")
        rows = pq.read_metadata(temporary).num_rows
        expected_rows = connection.execute(
            "SELECT count(*) FROM predictions"
        ).fetchone()[0]
        if rows != expected_rows:
            raise BaselineError("serialized prediction row count does not match")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": path.as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "rows": rows,
        "schema": [
            {"name": field.name, "type": str(field.type), "nullable": field.nullable}
            for field in schema
        ],
    }


def _evaluate(config: BaselineConfig) -> dict[str, Any]:
    grid_config = load_grid_config(config.grid_config_path)
    aggregate_config = load_aggregate_config(grid_config["aggregate_config"])
    source_config = load_source_config(aggregate_config.source_config_path)
    grid_paths = [
        grid_config["output_dir"] / f"hourly_grid_{source.month}.parquet"
        for source in source_config.months
    ]
    if config.predictions_path.parent == grid_config["output_dir"]:
        raise BaselineError("predictions_path must not be inside the hourly grid directory")

    with duckdb.connect() as connection:
        connection.execute(f"SET threads = {aggregate_config.duckdb_threads}")
        connection.execute(
            f"SET memory_limit = '{aggregate_config.duckdb_memory_limit}'"
        )
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute("SET enable_progress_bar = false")
        input_summary = _create_grid(connection, grid_paths, config)
        _create_predictions(connection, config)
        split_reports = {
            boundary.name: _split_metrics(connection, boundary)
            for boundary in config.splits
        }
        if any(report["scored_rows"] == 0 for report in split_reports.values()):
            raise BaselineError("every split must contain at least one scored prediction")
        output = _write_predictions(connection, config.predictions_path)

    return {
        "report_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "baseline": {
            "name": "seasonal_naive",
            "version": f"seasonal_naive_{config.seasonal_lag_hours}h_v1",
            "lag_hours": config.seasonal_lag_hours,
            "negative_prediction_policy": "clip_to_zero_before_evaluation",
            "fallback_policy": {
                "train": "zone mean from strictly earlier train targets",
                "validation_test": "zone mean fit on train targets only",
            },
            "seed": None,
        },
        "config": {
            "path": config.config_path.as_posix(),
            "sha256": _sha256(config.config_path),
            "grid_config_path": config.grid_config_path.as_posix(),
            "grid_config_sha256": _sha256(config.grid_config_path),
        },
        "input": {
            **input_summary,
            "files": [
                {"path": path.as_posix(), "sha256": _sha256(path)}
                for path in grid_paths
            ],
        },
        "output": output,
        "splits": split_reports,
        "resources": {
            "duckdb_threads": aggregate_config.duckdb_threads,
            "duckdb_memory_limit": aggregate_config.duckdb_memory_limit,
        },
        "versions": {"duckdb": duckdb.__version__, "pyarrow": pa.__version__},
    }


def run_baseline(config_path: Path) -> dict[str, Any]:
    config = load_baseline_config(config_path)
    started = time.perf_counter()
    rss_start, rss_peak, stop, sampler = _start_rss_sampler()
    try:
        try:
            report = _evaluate(config)
        except (duckdb.Error, pa.ArrowException, OSError) as exc:
            raise BaselineError(f"cannot evaluate baseline: {exc}") from exc
    finally:
        stop.set()
        sampler.join()
    report["resources"].update({
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "rss_start_bytes": rss_start,
        "rss_peak_bytes": max(rss_peak[0], psutil.Process().memory_info().rss),
    })
    try:
        _write_json(config.metrics_path, report)
    except OSError as exc:
        raise BaselineError(f"cannot write metrics report {config.metrics_path}: {exc}") from exc
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/baseline.json")
    )
    args = parser.parse_args()
    try:
        report = run_baseline(args.config)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
