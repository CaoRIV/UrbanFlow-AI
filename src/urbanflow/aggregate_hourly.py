from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import duckdb
import psutil
import pyarrow as pa
import pyarrow.parquet as pq

from urbanflow.download_data import DownloadConfig, MonthlyTripSource, load_config

_MEMORY_LIMIT_PATTERN = re.compile(r"^[1-9]\d*(MB|GB)$")
_SPECIAL_ZONE_SQL = """
(
    lower(trim(coalesce(z.Borough, ''))) IN ('unknown', 'n/a')
    OR lower(trim(coalesce(z.Zone, ''))) IN ('unknown', 'n/a', 'outside of nyc')
    OR lower(trim(coalesce(z.service_zone, ''))) IN ('unknown', 'n/a')
)
"""


class AggregateConfigError(ValueError):
    """Raised when the hourly aggregation config is invalid."""


class AggregateInputError(ValueError):
    """Raised when a required raw input or manifest entry is missing."""


class AggregateContractError(ValueError):
    """Raised when an aggregate output violates its data contract."""


@dataclass(frozen=True)
class AggregateConfig:
    source_config_path: Path
    output_dir: Path
    report_path: Path
    timezone: str
    duckdb_threads: int
    duckdb_memory_limit: str


@dataclass(frozen=True)
class LocalTimeAnomaly:
    kind: str
    start: datetime
    end: datetime


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AggregateConfigError(f"{key} must be a non-empty string")
    return value.strip()


def load_aggregate_config(config_path: Path) -> AggregateConfig:
    resolved_path = config_path.resolve()
    try:
        data = json.loads(resolved_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AggregateConfigError(f"config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise AggregateConfigError(
            f"invalid JSON config: {config_path}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise AggregateConfigError("config root must be a JSON object")

    threads = data.get("duckdb_threads")
    if not isinstance(threads, int) or not 1 <= threads <= 4:
        raise AggregateConfigError("duckdb_threads must be an integer from 1 to 4")

    memory_limit = _required_string(data, "duckdb_memory_limit").upper()
    if not _MEMORY_LIMIT_PATTERN.fullmatch(memory_limit):
        raise AggregateConfigError(
            "duckdb_memory_limit must be a positive number followed by MB or GB"
        )

    timezone = _required_string(data, "timezone")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise AggregateConfigError(f"unknown timezone: {timezone}") from exc

    base_dir = resolved_path.parent
    return AggregateConfig(
        source_config_path=(
            base_dir / _required_string(data, "source_config")
        ).resolve(),
        output_dir=(base_dir / _required_string(data, "output_dir")).resolve(),
        report_path=(base_dir / _required_string(data, "report_path")).resolve(),
        timezone=timezone,
        duckdb_threads=threads,
        duckdb_memory_limit=memory_limit,
    )


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(month, "%Y-%m")
    if start.month == 12:
        return start, datetime(start.year + 1, 1, 1)
    return start, datetime(start.year, start.month + 1, 1)


def _classify_local_time(value: datetime, timezone: ZoneInfo) -> str | None:
    aware_fold_zero = value.replace(tzinfo=timezone, fold=0)
    aware_fold_one = value.replace(tzinfo=timezone, fold=1)
    roundtrip_zero = (
        aware_fold_zero.astimezone(UTC)
        .astimezone(timezone)
        .replace(tzinfo=None)
    )
    roundtrip_one = (
        aware_fold_one.astimezone(UTC)
        .astimezone(timezone)
        .replace(tzinfo=None)
    )
    valid_zero = roundtrip_zero == value
    valid_one = roundtrip_one == value
    if not valid_zero and not valid_one:
        return "nonexistent"
    if (
        valid_zero
        and valid_one
        and aware_fold_zero.utcoffset() != aware_fold_one.utcoffset()
    ):
        return "ambiguous"
    return None


def _dst_anomalies(month: str, timezone_name: str) -> tuple[LocalTimeAnomaly, ...]:
    timezone = ZoneInfo(timezone_name)
    start, end = _month_bounds(month)
    anomalies: list[LocalTimeAnomaly] = []
    current = start
    while current < end:
        kind = _classify_local_time(current + timedelta(minutes=30), timezone)
        if kind is not None:
            anomalies.append(
                LocalTimeAnomaly(kind=kind, start=current, end=current + timedelta(hours=1))
            )
        current += timedelta(hours=1)
    return tuple(anomalies)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_timestamp(value: datetime) -> str:
    return f"TIMESTAMP {_sql_literal(value.strftime('%Y-%m-%d %H:%M:%S'))}"


def _anomaly_predicate(
    anomalies: tuple[LocalTimeAnomaly, ...], kind: str
) -> str:
    ranges = [
        f"(t.tpep_pickup_datetime >= {_sql_timestamp(anomaly.start)} "
        f"AND t.tpep_pickup_datetime < {_sql_timestamp(anomaly.end)})"
        for anomaly in anomalies
        if anomaly.kind == kind
    ]
    return "(" + " OR ".join(ranges) + ")" if ranges else "FALSE"


def _labeled_sql(
    trip_path: Path,
    zone_path: Path,
    start: datetime,
    end: datetime,
    anomalies: tuple[LocalTimeAnomaly, ...],
) -> str:
    nonexistent = _anomaly_predicate(anomalies, "nonexistent")
    ambiguous = _anomaly_predicate(anomalies, "ambiguous")
    return f"""
        WITH trips AS (
            SELECT tpep_pickup_datetime, PULocationID
            FROM read_parquet({_sql_literal(trip_path.as_posix())})
        ),
        zones AS (
            SELECT LocationID, Borough, Zone, service_zone
            FROM read_csv_auto({_sql_literal(zone_path.as_posix())})
        )
        SELECT
            t.tpep_pickup_datetime,
            t.PULocationID,
            CASE
                WHEN t.tpep_pickup_datetime IS NULL THEN 'null_timestamp'
                WHEN t.tpep_pickup_datetime < {_sql_timestamp(start)}
                  OR t.tpep_pickup_datetime >= {_sql_timestamp(end)}
                    THEN 'outside_month'
                WHEN {nonexistent} THEN 'dst_nonexistent'
                WHEN {ambiguous} THEN 'dst_ambiguous'
                WHEN t.PULocationID IS NULL THEN 'null_zone'
                WHEN z.LocationID IS NULL THEN 'not_in_lookup'
                WHEN {_SPECIAL_ZONE_SQL} THEN 'non_service_zone'
                ELSE 'retained'
            END AS disposition
        FROM trips t
        LEFT JOIN zones z ON t.PULocationID = z.LocationID
    """


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        while chunk := file_handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _start_rss_sampler() -> tuple[int, list[int], threading.Event, threading.Thread]:
    process = psutil.Process()
    baseline = process.memory_info().rss
    peak = [baseline]
    stop = threading.Event()

    def sample() -> None:
        while not stop.wait(0.01):
            peak[0] = max(peak[0], process.memory_info().rss)

    thread = threading.Thread(target=sample, name="aggregate-rss-sampler", daemon=True)
    thread.start()
    return baseline, peak, stop, thread


def _validate_inputs(
    source_config: DownloadConfig, source: MonthlyTripSource
) -> tuple[Path, Path]:
    trip_path = source_config.raw_dir / f"yellow_tripdata_{source.month}.parquet"
    zone_path = source_config.raw_dir / "taxi_zone_lookup.csv"
    missing = [str(path) for path in (trip_path, zone_path) if not path.is_file()]
    if missing:
        raise AggregateInputError(
            "missing raw input; run urbanflow.download_data first: "
            + ", ".join(missing)
        )
    return trip_path, zone_path


def _filter_counts(
    connection: duckdb.DuckDBPyConnection, labeled_sql: str
) -> dict[str, int]:
    rows = connection.execute(
        f"""
        SELECT disposition, count(*) AS rows
        FROM ({labeled_sql}) labeled
        GROUP BY disposition
        """
    ).fetchall()
    counts = {
        "null_timestamp": 0,
        "outside_month": 0,
        "dst_nonexistent": 0,
        "dst_ambiguous": 0,
        "null_zone": 0,
        "not_in_lookup": 0,
        "non_service_zone": 0,
        "retained": 0,
    }
    counts.update({row[0]: row[1] for row in rows})
    counts["raw_rows"] = sum(row[1] for row in rows)
    counts["excluded_rows"] = counts["raw_rows"] - counts["retained"]
    return counts


def _write_month_output(
    connection: duckdb.DuckDBPyConnection,
    labeled_sql: str,
    destination: Path,
    month: str,
    timezone_name: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".parquet", dir=destination.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    temporary_path.unlink()

    aggregate_sql = f"""
        SELECT
            CAST(PULocationID AS INTEGER) AS zone_id,
            date_trunc(
                'hour',
                timezone({_sql_literal(timezone_name)}, tpep_pickup_datetime)
            ) AS target_hour_utc,
            CAST(count(*) AS BIGINT) AS trip_count,
            {_sql_literal(month)} AS source_month
        FROM ({labeled_sql}) labeled
        WHERE disposition = 'retained'
        GROUP BY zone_id, target_hour_utc
        ORDER BY target_hour_utc, zone_id
    """
    try:
        connection.execute(
            f"COPY ({aggregate_sql}) TO {_sql_literal(temporary_path.as_posix())} "
            "(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def _validate_month_output(
    connection: duckdb.DuckDBPyConnection,
    path: Path,
    expected_trip_count: int,
) -> dict[str, Any]:
    schema = pq.read_schema(path)
    expected_columns = ["zone_id", "target_hour_utc", "trip_count", "source_month"]
    if schema.names != expected_columns:
        raise AggregateContractError(
            f"unexpected aggregate schema: {schema.names}; expected {expected_columns}"
        )
    target_type = schema.field("target_hour_utc").type
    if not pa.types.is_timestamp(target_type) or target_type.tz is None:
        raise AggregateContractError(
            f"target_hour_utc must be timezone-aware, got {target_type}"
        )

    row = connection.execute(
        f"""
        SELECT
            count(*) AS output_rows,
            sum(trip_count) AS output_trip_count,
            count(DISTINCT zone_id) AS zone_count,
            CAST(min(target_hour_utc) AS VARCHAR) AS min_target_hour_utc,
            CAST(max(target_hour_utc) AS VARCHAR) AS max_target_hour_utc
        FROM read_parquet({_sql_literal(path.as_posix())})
        """
    ).fetchone()
    assert row is not None
    duplicate_keys = connection.execute(
        f"""
        SELECT count(*)
        FROM (
            SELECT zone_id, target_hour_utc, count(*) AS rows
            FROM read_parquet({_sql_literal(path.as_posix())})
            GROUP BY zone_id, target_hour_utc
            HAVING count(*) > 1
        ) duplicates
        """
    ).fetchone()[0]
    if duplicate_keys:
        raise AggregateContractError(f"aggregate contains {duplicate_keys} duplicate keys")
    if row[1] != expected_trip_count:
        raise AggregateContractError(
            f"trip_count sum {row[1]} does not match retained rows {expected_trip_count}"
        )

    return {
        "path": path.as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "output_rows": row[0],
        "output_trip_count": row[1],
        "zone_count": row[2],
        "min_target_hour_utc": row[3],
        "max_target_hour_utc": row[4],
        "duplicate_keys": duplicate_keys,
        "schema": [
            {"name": field.name, "type": str(field.type), "nullable": field.nullable}
            for field in schema
        ],
    }


def _aggregate_month(
    config: AggregateConfig,
    source_config: DownloadConfig,
    source: MonthlyTripSource,
) -> dict[str, Any]:
    trip_path, zone_path = _validate_inputs(source_config, source)
    start, end = _month_bounds(source.month)
    anomalies = _dst_anomalies(source.month, config.timezone)
    destination = config.output_dir / f"hourly_counts_observed_{source.month}.parquet"

    rss_start, rss_peak, stop_sampling, sampling_thread = _start_rss_sampler()
    started_at = time.perf_counter()
    try:
        with duckdb.connect() as connection:
            connection.execute(f"SET threads = {config.duckdb_threads}")
            connection.execute("SET enable_progress_bar = false")
            connection.execute("SET TimeZone = 'UTC'")
            connection.execute(
                f"SET memory_limit = '{config.duckdb_memory_limit}'"
            )
            labeled_sql = _labeled_sql(
                trip_path, zone_path, start, end, anomalies
            )
            filter_counts = _filter_counts(connection, labeled_sql)
            _write_month_output(
                connection,
                labeled_sql,
                destination,
                source.month,
                config.timezone,
            )
            output = _validate_month_output(
                connection, destination, filter_counts["retained"]
            )
    finally:
        stop_sampling.set()
        sampling_thread.join()
        rss_peak[0] = max(rss_peak[0], psutil.Process().memory_info().rss)

    return {
        "month": source.month,
        "source": {
            "path": trip_path.as_posix(),
            "bytes": trip_path.stat().st_size,
            "sha256": _sha256(trip_path),
        },
        "filters": filter_counts,
        "dst_anomalies": [
            {
                "kind": anomaly.kind,
                "start_local": anomaly.start.isoformat(sep="T"),
                "end_local": anomaly.end.isoformat(sep="T"),
            }
            for anomaly in anomalies
        ],
        "output": output,
        "resource_usage": {
            "elapsed_seconds": round(time.perf_counter() - started_at, 3),
            "rss_start_bytes": rss_start,
            "rss_peak_bytes": rss_peak[0],
            "rss_peak_increase_bytes": max(0, rss_peak[0] - rss_start),
        },
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as temporary_file:
        json.dump(value, temporary_file, indent=2, sort_keys=True)
        temporary_file.write("\n")
        temporary_path = Path(temporary_file.name)
    os.replace(temporary_path, path)


def run_aggregation(config_path: Path) -> dict[str, Any]:
    config = load_aggregate_config(config_path)
    source_config = load_config(config.source_config_path)
    monthly_reports = [
        _aggregate_month(config, source_config, source)
        for source in source_config.months
    ]
    report = {
        "report_version": 1,
        "generated_at_utc": datetime.now(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
        "timezone": config.timezone,
        "columns_scanned": ["tpep_pickup_datetime", "PULocationID"],
        "months": monthly_reports,
        "summary": {
            "raw_rows": sum(item["filters"]["raw_rows"] for item in monthly_reports),
            "retained_rows": sum(
                item["filters"]["retained"] for item in monthly_reports
            ),
            "excluded_rows": sum(
                item["filters"]["excluded_rows"] for item in monthly_reports
            ),
            "output_rows": sum(
                item["output"]["output_rows"] for item in monthly_reports
            ),
            "output_trip_count": sum(
                item["output"]["output_trip_count"] for item in monthly_reports
            ),
        },
    }
    if report["summary"]["retained_rows"] != report["summary"]["output_trip_count"]:
        raise AggregateContractError("cross-month trip-count invariant failed")
    _write_json(config.report_path, report)
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Filter and aggregate NYC TLC pickups by zone and UTC hour."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/aggregate.json"),
        help="JSON config path (default: configs/aggregate.json)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    try:
        report = run_aggregation(args.config)
    except (AggregateConfigError, AggregateInputError, AggregateContractError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
