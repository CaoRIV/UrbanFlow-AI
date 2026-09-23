"""Build leakage-safe calendar, lag, and shifted rolling features."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
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
from urbanflow.evaluate_baseline import (
    BaselineConfig,
    _create_grid,
    _format_utc,
    load_baseline_config,
)

_REQUIRED_LAGS = (1, 24, 168)
_REQUIRED_ROLLING_WINDOWS = (24, 168)
_FEATURE_COLUMNS = (
    "zone_id",
    "utc_hour",
    "utc_day_of_week",
    "utc_month",
    "is_weekend",
    "lag_1h",
    "lag_24h",
    "lag_168h",
    "rolling_mean_24h",
    "rolling_mean_168h",
)
_FEATURE_SCHEMA = {
    "zone_id": pa.int32(),
    "target_hour_utc": pa.timestamp("us", tz="UTC"),
    "split": pa.string(),
    "target_trip_count": pa.int64(),
    "source_status": pa.string(),
    "utc_hour": pa.int8(),
    "utc_day_of_week": pa.int8(),
    "utc_month": pa.int8(),
    "is_weekend": pa.bool_(),
    "lag_1h": pa.int64(),
    "lag_24h": pa.int64(),
    "lag_168h": pa.int64(),
    "rolling_mean_24h": pa.float64(),
    "rolling_mean_168h": pa.float64(),
    "features_complete": pa.bool_(),
}


class FeatureError(ValueError):
    """Invalid feature configuration, input, or output contract."""


@dataclass(frozen=True)
class FeatureConfig:
    config_path: Path
    baseline_config_path: Path
    output_path: Path
    report_path: Path
    lag_hours: tuple[int, ...]
    rolling_windows_hours: tuple[int, ...]


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FeatureError(f"{key} must be a non-empty string")
    return value.strip()


def _required_integer_list(
    data: dict[str, Any], key: str, expected: tuple[int, ...]
) -> tuple[int, ...]:
    value = data.get(key)
    if (
        not isinstance(value, list)
        or any(not isinstance(item, int) or isinstance(item, bool) for item in value)
        or tuple(value) != expected
    ):
        raise FeatureError(f"{key} must be exactly {list(expected)}")
    return tuple(value)


def load_feature_config(path: Path) -> FeatureConfig:
    resolved = path.resolve()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FeatureError(f"cannot read feature config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise FeatureError("feature config root must be an object")

    base = resolved.parent
    baseline_config_path = (
        base / _required_string(data, "baseline_config")
    ).resolve()
    output_path = (base / _required_string(data, "output_path")).resolve()
    report_path = (base / _required_string(data, "report_path")).resolve()
    if output_path.suffix.lower() != ".parquet":
        raise FeatureError("output_path must end in .parquet")
    if report_path.suffix.lower() != ".json":
        raise FeatureError("report_path must end in .json")
    if report_path in {resolved, baseline_config_path}:
        raise FeatureError("report_path must not overwrite a config file")

    return FeatureConfig(
        config_path=resolved,
        baseline_config_path=baseline_config_path,
        output_path=output_path,
        report_path=report_path,
        lag_hours=_required_integer_list(data, "lag_hours", _REQUIRED_LAGS),
        rolling_windows_hours=_required_integer_list(
            data, "rolling_windows_hours", _REQUIRED_ROLLING_WINDOWS
        ),
    )


def _assign_splits(
    connection: duckdb.DuckDBPyConnection,
    baseline: BaselineConfig,
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
            for split in baseline.splits
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
    assigned = connection.execute("SELECT count(*) FROM assigned").fetchone()[0]
    grid = connection.execute("SELECT count(*) FROM grid").fetchone()[0]
    if assigned != grid:
        raise FeatureError("not every grid row belongs to exactly one configured split")


def _create_features(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("""
        CREATE TEMP TABLE feature_history AS
        SELECT zone_id, target_hour_utc, split,
               trip_count AS target_trip_count, source_status,
               CAST(extract(hour FROM target_hour_utc) AS TINYINT) AS utc_hour,
               CAST(extract(isodow FROM target_hour_utc) AS TINYINT)
                   AS utc_day_of_week,
               CAST(extract(month FROM target_hour_utc) AS TINYINT) AS utc_month,
               extract(isodow FROM target_hour_utc) IN (6, 7) AS is_weekend,
               lag(trip_count, 1) OVER ordered AS lag_1h,
               lag(trip_count, 24) OVER ordered AS lag_24h,
               lag(trip_count, 168) OVER ordered AS lag_168h,
               avg(trip_count) OVER prior_24 AS raw_rolling_mean_24h,
               count(trip_count) OVER prior_24 AS history_count_24h,
               avg(trip_count) OVER prior_168 AS raw_rolling_mean_168h,
               count(trip_count) OVER prior_168 AS history_count_168h
        FROM assigned
        WINDOW
            ordered AS (
                PARTITION BY zone_id ORDER BY target_hour_utc
            ),
            prior_24 AS (
                PARTITION BY zone_id ORDER BY target_hour_utc
                ROWS BETWEEN 24 PRECEDING AND 1 PRECEDING
            ),
            prior_168 AS (
                PARTITION BY zone_id ORDER BY target_hour_utc
                ROWS BETWEEN 168 PRECEDING AND 1 PRECEDING
            )
    """)
    connection.execute("""
        CREATE TEMP TABLE feature_values AS
        SELECT zone_id, target_hour_utc, split, target_trip_count,
               source_status, utc_hour, utc_day_of_week, utc_month, is_weekend,
               lag_1h, lag_24h, lag_168h,
               CASE WHEN history_count_24h = 24
                    THEN raw_rolling_mean_24h ELSE NULL END::DOUBLE
                    AS rolling_mean_24h,
               CASE WHEN history_count_168h = 168
                    THEN raw_rolling_mean_168h ELSE NULL END::DOUBLE
                    AS rolling_mean_168h
        FROM feature_history
    """)
    connection.execute("""
        CREATE TEMP TABLE features AS
        SELECT *,
               lag_1h IS NOT NULL
               AND lag_24h IS NOT NULL
               AND lag_168h IS NOT NULL
               AND rolling_mean_24h IS NOT NULL
               AND rolling_mean_168h IS NOT NULL
                   AS features_complete
        FROM feature_values
    """)
    bad_rows = connection.execute("""
        SELECT count(*) FROM features
        WHERE utc_hour NOT BETWEEN 0 AND 23
           OR utc_day_of_week NOT BETWEEN 1 AND 7
           OR utc_month NOT BETWEEN 1 AND 12
           OR lag_1h < 0 OR lag_24h < 0 OR lag_168h < 0
           OR rolling_mean_24h < 0 OR rolling_mean_168h < 0
           OR features_complete != (
               lag_1h IS NOT NULL
               AND lag_24h IS NOT NULL
               AND lag_168h IS NOT NULL
               AND rolling_mean_24h IS NOT NULL
               AND rolling_mean_168h IS NOT NULL
           )
    """).fetchone()[0]
    if bad_rows:
        raise FeatureError(f"feature contract failed for {bad_rows} rows")


def _split_report(
    connection: duckdb.DuckDBPyConnection,
    baseline: BaselineConfig,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for boundary in baseline.splits:
        row = connection.execute("""
            SELECT count(*) AS total_rows,
                   count(DISTINCT zone_id) AS zone_count,
                   count(DISTINCT target_hour_utc) AS hour_count,
                   count(*) FILTER (WHERE target_trip_count IS NOT NULL)
                       AS target_rows,
                   count(*) FILTER (WHERE target_trip_count IS NULL)
                       AS missing_target_rows,
                   count(*) FILTER (WHERE features_complete)
                       AS complete_feature_rows,
                   count(*) FILTER (
                       WHERE target_trip_count IS NOT NULL AND features_complete
                   ) AS training_eligible_rows,
                   count(*) FILTER (WHERE lag_1h IS NULL) AS missing_lag_1h,
                   count(*) FILTER (WHERE lag_24h IS NULL) AS missing_lag_24h,
                   count(*) FILTER (WHERE lag_168h IS NULL) AS missing_lag_168h,
                   count(*) FILTER (WHERE rolling_mean_24h IS NULL)
                       AS missing_rolling_mean_24h,
                   count(*) FILTER (WHERE rolling_mean_168h IS NULL)
                       AS missing_rolling_mean_168h
            FROM features WHERE split = ?
        """, [boundary.name]).fetchone()
        result[boundary.name] = {
            "start_utc": _format_utc(boundary.start_utc),
            "end_utc_exclusive": _format_utc(boundary.end_utc_exclusive),
            "total_rows": row[0],
            "zone_count": row[1],
            "hour_count": row[2],
            "target_rows": row[3],
            "missing_target_rows": row[4],
            "complete_feature_rows": row[5],
            "training_eligible_rows": row[6],
            "missing_by_feature": {
                "lag_1h": row[7],
                "lag_24h": row[8],
                "lag_168h": row[9],
                "rolling_mean_24h": row[10],
                "rolling_mean_168h": row[11],
            },
        }
    return result


def _write_features(
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
                SELECT * FROM features ORDER BY target_hour_utc, zone_id
            ) TO {_sql_literal(temporary.as_posix())}
              (FORMAT PARQUET, COMPRESSION ZSTD)
        """)
        schema = pq.read_schema(temporary)
        if schema.names != list(_FEATURE_SCHEMA) or any(
            schema.field(name).type != expected
            for name, expected in _FEATURE_SCHEMA.items()
        ):
            raise FeatureError(f"invalid feature output schema: {schema}")
        rows = pq.read_metadata(temporary).num_rows
        expected_rows = connection.execute("SELECT count(*) FROM features").fetchone()[0]
        unique_keys = connection.execute("""
            SELECT count(DISTINCT (zone_id, target_hour_utc)) FROM features
        """).fetchone()[0]
        if rows != expected_rows or rows != unique_keys:
            raise FeatureError("serialized features violate row-count or key invariants")
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


def _build(config: FeatureConfig) -> dict[str, Any]:
    baseline = load_baseline_config(config.baseline_config_path)
    grid_config = load_grid_config(baseline.grid_config_path)
    aggregate = load_aggregate_config(grid_config["aggregate_config"])
    source = load_source_config(aggregate.source_config_path)
    grid_paths = [
        grid_config["output_dir"] / f"hourly_grid_{item.month}.parquet"
        for item in source.months
    ]
    if config.output_path.parent == grid_config["output_dir"]:
        raise FeatureError("feature output must not be inside the hourly grid directory")

    with duckdb.connect() as connection:
        connection.execute(f"SET threads = {aggregate.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{aggregate.duckdb_memory_limit}'")
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute("SET enable_progress_bar = false")
        input_summary = _create_grid(connection, grid_paths, baseline)
        _assign_splits(connection, baseline)
        _create_features(connection)
        split_reports = _split_report(connection, baseline)
        output = _write_features(connection, config.output_path)

    return {
        "report_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "config": {
            "path": config.config_path.as_posix(),
            "sha256": _sha256(config.config_path),
            "baseline_config_path": config.baseline_config_path.as_posix(),
            "baseline_config_sha256": _sha256(config.baseline_config_path),
        },
        "feature_contract": {
            "feature_columns": list(_FEATURE_COLUMNS),
            "target_column": "target_trip_count",
            "calendar_timezone": "UTC",
            "utc_day_of_week_definition": "ISO 1=Monday through 7=Sunday",
            "lag_hours": list(config.lag_hours),
            "rolling_windows_hours": list(config.rolling_windows_hours),
            "rolling_policy": "prior rows only; full non-null window required",
            "missing_history_policy": "preserve as null; never impute zero",
            "seed": None,
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
            "duckdb_threads": aggregate.duckdb_threads,
            "duckdb_memory_limit": aggregate.duckdb_memory_limit,
        },
        "versions": {"duckdb": duckdb.__version__, "pyarrow": pa.__version__},
    }


def run_features(config_path: Path) -> dict[str, Any]:
    config = load_feature_config(config_path)
    started = time.perf_counter()
    rss_start, rss_peak, stop, sampler = _start_rss_sampler()
    try:
        try:
            report = _build(config)
        except (duckdb.Error, pa.ArrowException, OSError) as exc:
            raise FeatureError(f"cannot build features: {exc}") from exc
    finally:
        stop.set()
        sampler.join()
    report["resources"].update({
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "rss_start_bytes": rss_start,
        "rss_peak_bytes": max(rss_peak[0], psutil.Process().memory_info().rss),
    })
    try:
        _write_json(config.report_path, report)
    except OSError as exc:
        raise FeatureError(f"cannot write feature report {config.report_path}: {exc}") from exc
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/features.json")
    )
    args = parser.parse_args()
    try:
        report = run_features(args.config)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
