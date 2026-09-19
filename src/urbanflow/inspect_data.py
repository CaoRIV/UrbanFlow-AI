from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb
import psutil

from urbanflow.download_data import DownloadConfig, load_config

_MEMORY_LIMIT_PATTERN = re.compile(r"^[1-9]\d*(MB|GB)$")
_SELECTED_TRIP_COLUMNS = ("tpep_pickup_datetime", "PULocationID")
_SPECIAL_ZONE_SQL = """
(
    lower(trim(coalesce(z.Borough, ''))) IN ('unknown', 'n/a')
    OR lower(trim(coalesce(z.Zone, ''))) IN ('unknown', 'n/a', 'outside of nyc')
    OR lower(trim(coalesce(z.service_zone, ''))) IN ('unknown', 'n/a')
)
"""


class InspectionConfigError(ValueError):
    """Raised when the EDA configuration is invalid."""


class InspectionInputError(ValueError):
    """Raised when a required raw input is unavailable."""


@dataclass(frozen=True)
class InspectionConfig:
    source_config_path: Path
    output_path: Path
    timezone_assumption: str
    duckdb_threads: int
    duckdb_memory_limit: str


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InspectionConfigError(f"{key} must be a non-empty string")
    return value.strip()


def load_inspection_config(config_path: Path) -> InspectionConfig:
    resolved_path = config_path.resolve()
    try:
        data = json.loads(resolved_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InspectionConfigError(f"config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise InspectionConfigError(
            f"invalid JSON config: {config_path}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise InspectionConfigError("config root must be a JSON object")

    threads = data.get("duckdb_threads")
    if not isinstance(threads, int) or not 1 <= threads <= 4:
        raise InspectionConfigError("duckdb_threads must be an integer from 1 to 4")

    memory_limit = _required_string(data, "duckdb_memory_limit").upper()
    if not _MEMORY_LIMIT_PATTERN.fullmatch(memory_limit):
        raise InspectionConfigError(
            "duckdb_memory_limit must be a positive number followed by MB or GB"
        )

    timezone_assumption = _required_string(data, "timezone_assumption")
    try:
        ZoneInfo(timezone_assumption)
    except ZoneInfoNotFoundError as exc:
        raise InspectionConfigError(
            f"unknown timezone_assumption: {timezone_assumption}"
        ) from exc

    base_dir = resolved_path.parent
    return InspectionConfig(
        source_config_path=(
            base_dir / _required_string(data, "source_config")
        ).resolve(),
        output_path=(base_dir / _required_string(data, "output_path")).resolve(),
        timezone_assumption=timezone_assumption,
        duckdb_threads=threads,
        duckdb_memory_limit=memory_limit,
    )


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(month, "%Y-%m")
    if start.month == 12:
        end = datetime(start.year + 1, 1, 1)
    else:
        end = datetime(start.year, start.month + 1, 1)
    return start, end


def _validate_inputs(source_config: DownloadConfig) -> tuple[Path, Path]:
    trip_path = source_config.raw_dir / f"yellow_tripdata_{source_config.month}.parquet"
    zone_path = source_config.raw_dir / "taxi_zone_lookup.csv"
    missing = [str(path) for path in (trip_path, zone_path) if not path.is_file()]
    if missing:
        raise InspectionInputError(
            "missing raw input; run urbanflow.download_data first: "
            + ", ".join(missing)
        )
    return trip_path, zone_path


def _start_rss_sampler() -> tuple[int, list[int], threading.Event, threading.Thread]:
    process = psutil.Process()
    baseline = process.memory_info().rss
    peak = [baseline]
    stop = threading.Event()

    def sample() -> None:
        while not stop.wait(0.01):
            peak[0] = max(peak[0], process.memory_info().rss)

    thread = threading.Thread(target=sample, name="rss-sampler", daemon=True)
    thread.start()
    return baseline, peak, stop, thread


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat(sep="T") if value is not None else None


def _query_summary(
    connection: duckdb.DuckDBPyConnection,
    trip_path: Path,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT
            count(*) AS total_rows,
            count(*) FILTER (WHERE tpep_pickup_datetime IS NULL) AS pickup_null_rows,
            count(*) FILTER (WHERE PULocationID IS NULL) AS zone_null_rows,
            min(tpep_pickup_datetime) AS pickup_min,
            max(tpep_pickup_datetime) AS pickup_max,
            count(*) FILTER (WHERE tpep_pickup_datetime < ?) AS before_month_rows,
            count(*) FILTER (WHERE tpep_pickup_datetime >= ?) AS after_month_rows,
            count(*) FILTER (
                WHERE tpep_pickup_datetime >= ? AND tpep_pickup_datetime < ?
            ) AS within_month_rows,
            count(DISTINCT PULocationID) AS distinct_raw_zone_ids
        FROM read_parquet(?)
        """,
        [start, end, start, end, str(trip_path)],
    ).fetchone()
    assert row is not None
    return {
        "total_rows": row[0],
        "pickup_null_rows": row[1],
        "zone_null_rows": row[2],
        "pickup_min": _isoformat(row[3]),
        "pickup_max": _isoformat(row[4]),
        "before_month_rows": row[5],
        "after_month_rows": row[6],
        "within_month_rows": row[7],
        "distinct_raw_zone_ids": row[8],
    }


def _query_zones(
    connection: duckdb.DuckDBPyConnection,
    trip_path: Path,
    zone_path: Path,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    summary = connection.execute(
        f"""
        WITH trips AS (
            SELECT PULocationID
            FROM read_parquet(?)
            WHERE tpep_pickup_datetime >= ? AND tpep_pickup_datetime < ?
        ),
        zones AS (
            SELECT LocationID, Borough, Zone, service_zone
            FROM read_csv_auto(?)
        )
        SELECT
            count(*) FILTER (WHERE t.PULocationID IS NULL) AS null_zone_rows,
            count(*) FILTER (
                WHERE t.PULocationID IS NOT NULL AND z.LocationID IS NULL
            ) AS not_in_lookup_rows,
            count(*) FILTER (WHERE z.LocationID IS NOT NULL) AS lookup_matched_rows,
            count(*) FILTER (
                WHERE z.LocationID IS NOT NULL AND {_SPECIAL_ZONE_SQL}
            ) AS non_service_zone_rows,
            count(*) FILTER (
                WHERE t.PULocationID IS NOT NULL
                  AND z.LocationID IS NOT NULL
                  AND NOT {_SPECIAL_ZONE_SQL}
            ) AS retained_rows,
            count(DISTINCT t.PULocationID) FILTER (
                WHERE z.LocationID IS NOT NULL AND NOT {_SPECIAL_ZONE_SQL}
            ) AS retained_zone_count
        FROM trips t
        LEFT JOIN zones z ON t.PULocationID = z.LocationID
        """,
        [str(trip_path), start, end, str(zone_path)],
    ).fetchone()
    assert summary is not None

    invalid_rows = connection.execute(
        f"""
        WITH trips AS (
            SELECT PULocationID
            FROM read_parquet(?)
            WHERE tpep_pickup_datetime >= ? AND tpep_pickup_datetime < ?
        ),
        zones AS (
            SELECT LocationID, Borough, Zone, service_zone
            FROM read_csv_auto(?)
        )
        SELECT
            t.PULocationID,
            z.Borough,
            z.Zone,
            CASE
                WHEN t.PULocationID IS NULL THEN 'null_zone'
                WHEN z.LocationID IS NULL THEN 'not_in_lookup'
                ELSE 'non_service_lookup_zone'
            END AS reason,
            count(*) AS rows
        FROM trips t
        LEFT JOIN zones z ON t.PULocationID = z.LocationID
        WHERE t.PULocationID IS NULL
           OR z.LocationID IS NULL
           OR {_SPECIAL_ZONE_SQL}
        GROUP BY t.PULocationID, z.Borough, z.Zone, reason
        ORDER BY rows DESC, t.PULocationID
        """,
        [str(trip_path), start, end, str(zone_path)],
    ).fetchall()

    return {
        "within_month_null_zone_rows": summary[0],
        "not_in_lookup_rows": summary[1],
        "lookup_matched_rows": summary[2],
        "non_service_zone_rows": summary[3],
        "retained_rows": summary[4],
        "retained_zone_count": summary[5],
        "invalid_zone_breakdown": [
            {
                "zone_id": row[0],
                "borough": row[1],
                "zone": row[2],
                "reason": row[3],
                "rows": row[4],
            }
            for row in invalid_rows
        ],
    }


def _query_time_coverage(
    connection: duckdb.DuckDBPyConnection,
    trip_path: Path,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    hourly_rows = connection.execute(
        """
        SELECT date_trunc('hour', tpep_pickup_datetime) AS pickup_hour, count(*) AS rows
        FROM read_parquet(?)
        WHERE tpep_pickup_datetime >= ? AND tpep_pickup_datetime < ?
        GROUP BY pickup_hour
        ORDER BY pickup_hour
        """,
        [str(trip_path), start, end],
    ).fetchall()
    daily_rows = connection.execute(
        """
        SELECT CAST(tpep_pickup_datetime AS DATE) AS pickup_date, count(*) AS rows
        FROM read_parquet(?)
        WHERE tpep_pickup_datetime >= ? AND tpep_pickup_datetime < ?
        GROUP BY pickup_date
        ORDER BY pickup_date
        """,
        [str(trip_path), start, end],
    ).fetchall()

    observed = {row[0]: row[1] for row in hourly_rows}
    expected_hours: list[datetime] = []
    current = start
    while current < end:
        expected_hours.append(current)
        current += timedelta(hours=1)
    missing_hours = [hour for hour in expected_hours if hour not in observed]
    hourly_counts = list(observed.values())
    daily_counts = [row[1] for row in daily_rows]

    return {
        "expected_hours": len(expected_hours),
        "observed_hours": len(observed),
        "missing_hours": [_isoformat(hour) for hour in missing_hours],
        "min_rows_per_observed_hour": min(hourly_counts) if hourly_counts else None,
        "max_rows_per_observed_hour": max(hourly_counts) if hourly_counts else None,
        "mean_rows_per_observed_hour": (
            round(sum(hourly_counts) / len(hourly_counts), 3)
            if hourly_counts
            else None
        ),
        "observed_days": len(daily_rows),
        "min_rows_per_observed_day": min(daily_counts) if daily_counts else None,
        "max_rows_per_observed_day": max(daily_counts) if daily_counts else None,
    }


def _write_json(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as temporary_file:
        json.dump(report, temporary_file, indent=2, sort_keys=True)
        temporary_file.write("\n")
        temporary_path = Path(temporary_file.name)
    os.replace(temporary_path, path)


def run_inspection(config_path: Path) -> dict[str, Any]:
    config = load_inspection_config(config_path)
    source_config = load_config(config.source_config_path)
    trip_path, zone_path = _validate_inputs(source_config)
    start, end = _month_bounds(source_config.month)

    rss_start, rss_peak, stop_sampling, sampling_thread = _start_rss_sampler()
    started_at = time.perf_counter()
    try:
        with duckdb.connect() as connection:
            connection.execute(f"SET threads = {config.duckdb_threads}")
            connection.execute("SET enable_progress_bar = false")
            connection.execute(
                f"SET memory_limit = '{config.duckdb_memory_limit}'"
            )
            summary = _query_summary(connection, trip_path, start, end)
            zones = _query_zones(connection, trip_path, zone_path, start, end)
            coverage = _query_time_coverage(connection, trip_path, start, end)
    finally:
        stop_sampling.set()
        sampling_thread.join()
        rss_peak[0] = max(rss_peak[0], psutil.Process().memory_info().rss)
    elapsed_seconds = time.perf_counter() - started_at

    retained_rows = zones["retained_rows"]
    report = {
        "report_version": 1,
        "generated_at_utc": datetime.now(ZoneInfo("UTC"))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "source": {
            "month": source_config.month,
            "trip_path": trip_path.as_posix(),
            "zone_lookup_path": zone_path.as_posix(),
            "columns_scanned": list(_SELECTED_TRIP_COLUMNS),
        },
        "raw_quality": summary,
        "zones": zones,
        "hourly_coverage": coverage,
        "timezone": {
            "raw_arrow_timezone": None,
            "assumption": config.timezone_assumption,
            "status": "assumed_from_TLC_local_month_semantics",
            "dst_validation": "pending_until_a_DST_transition_month_is_loaded",
        },
        "filter_decision": {
            "drop_null_pickup_datetime": True,
            "drop_outside_configured_local_month": True,
            "drop_null_pickup_zone": True,
            "drop_zone_not_in_lookup": True,
            "drop_non_service_lookup_zone": True,
            "retained_rows": retained_rows,
            "excluded_rows": summary["total_rows"] - retained_rows,
        },
        "resource_usage": {
            "elapsed_seconds": round(elapsed_seconds, 3),
            "rss_start_bytes": rss_start,
            "rss_peak_bytes": rss_peak[0],
            "rss_peak_increase_bytes": max(0, rss_peak[0] - rss_start),
            "duckdb_threads": config.duckdb_threads,
            "duckdb_memory_limit": config.duckdb_memory_limit,
        },
    }
    _write_json(config.output_path, report)
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inspect one raw NYC TLC Yellow Taxi month without loading all columns."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/eda.json"),
        help="JSON config path (default: configs/eda.json)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    try:
        report = run_inspection(args.config)
    except (InspectionConfigError, InspectionInputError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
