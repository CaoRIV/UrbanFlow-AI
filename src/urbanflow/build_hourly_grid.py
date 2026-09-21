"""Complete observed counts with UTC hours and a fixed lookup zone universe."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import psutil
import pyarrow as pa
import pyarrow.parquet as pq

from urbanflow.aggregate_hourly import (
    _SPECIAL_ZONE_SQL,
    _dst_anomalies,
    _labeled_sql,
    _month_bounds,
    _sha256,
    _sql_literal,
    _start_rss_sampler,
    _write_json,
    load_aggregate_config,
)
from urbanflow.download_data import load_config


class GridError(ValueError):
    """Invalid configuration, missing source or inconsistent observed counts."""


def _load_config(path: Path) -> dict[str, Path]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GridError(f"cannot read grid config {path}: {exc}") from exc
    keys = ("aggregate_config", "output_dir", "report_path")
    if not isinstance(data, dict) or any(
        not isinstance(data.get(k), str) or not data[k].strip() for k in keys
    ):
        raise GridError(f"grid config requires non-empty strings: {', '.join(keys)}")
    return {k: (path.resolve().parent / data[k]).resolve() for k in keys}


def _load_zones(connection, path: Path) -> list[int]:
    if not path.is_file():
        raise GridError(f"missing zone lookup: {path}")
    connection.execute(
        f"CREATE TEMP TABLE lookup AS SELECT * FROM read_csv_auto({_sql_literal(path.as_posix())})"
    )
    invalid = connection.execute("""
        SELECT count(*) - count(DISTINCT LocationID),
               count(*) FILTER (WHERE LocationID IS NULL OR LocationID <= 0
                                OR LocationID != try_cast(LocationID AS INTEGER))
        FROM lookup
    """).fetchone()
    if any(invalid):
        raise GridError("lookup must contain unique, positive integer LocationID values")
    connection.execute(f"""
        CREATE TEMP TABLE zones AS
        SELECT CAST(LocationID AS INTEGER) AS zone_id
        FROM lookup z WHERE NOT {_SPECIAL_ZONE_SQL}
    """)
    zones = [row[0] for row in connection.execute("SELECT zone_id FROM zones ORDER BY zone_id").fetchall()]
    if not zones:
        raise GridError("lookup contains no service zones")
    return zones


def _load_observed(connection, path: Path) -> None:
    if not path.is_file():
        raise GridError(f"missing observed counts: {path}; run urbanflow.aggregate_hourly first")
    schema = pq.read_schema(path)
    expected = {"zone_id": pa.int32(), "target_hour_utc": pa.timestamp("us", tz="UTC"),
                "trip_count": pa.int64(), "source_month": pa.string()}
    if schema.names != list(expected) or any(schema.field(k).type != v for k, v in expected.items()):
        raise GridError(f"invalid observed schema: {schema}")
    connection.execute(f"CREATE TEMP TABLE observed AS SELECT * FROM read_parquet({_sql_literal(path.as_posix())})")


def _build_month(config, aggregate, source, month: str) -> dict:
    started = time.perf_counter()
    rss_start, rss_peak, stop, sampler = _start_rss_sampler()
    try:
        report = _build_month_inner(config, aggregate, source, month)
    finally:
        stop.set()
        sampler.join()
    report["resource_usage"] = {
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "rss_start_bytes": rss_start,
        "rss_peak_bytes": max(rss_peak[0], psutil.Process().memory_info().rss),
    }
    return report


def _build_month_inner(config, aggregate, source, month: str) -> dict:
    raw = source.raw_dir / f"yellow_tripdata_{month}.parquet"
    lookup = source.raw_dir / "taxi_zone_lookup.csv"
    observed = aggregate.output_dir / f"hourly_counts_observed_{month}.parquet"
    if not raw.is_file():
        raise GridError(f"missing raw input: {raw}; cannot infer source coverage")
    local_start, local_end = _month_bounds(month)
    timezone = ZoneInfo(aggregate.timezone)
    start = local_start.replace(tzinfo=timezone).astimezone(UTC)
    end = local_end.replace(tzinfo=timezone).astimezone(UTC)
    hours = int((end - start).total_seconds() / 3600)
    anomalies = _dst_anomalies(month, aggregate.timezone)
    ambiguous = sorted({
        a.start.replace(tzinfo=timezone, fold=fold).astimezone(UTC)
        for a in anomalies if a.kind == "ambiguous" for fold in (0, 1)
    })
    destination = config["output_dir"] / f"hourly_grid_{month}.parquet"
    with duckdb.connect() as connection:
        connection.execute(f"SET threads = {aggregate.duckdb_threads}")
        connection.execute(f"SET memory_limit = '{aggregate.duckdb_memory_limit}'")
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute("SET enable_progress_bar = false")
        zones = _load_zones(connection, lookup)
        _load_observed(connection, observed)
        labeled = _labeled_sql(raw, lookup, local_start, local_end, anomalies)
        # Materialize only grouped counts, never raw trips. Coverage includes
        # non-service/unknown zones, since these still demonstrate source activity.
        connection.execute(f"""
            CREATE TEMP TABLE source_counts AS
            SELECT CAST(PULocationID AS INTEGER) AS zone_id,
                   date_trunc('hour', timezone({_sql_literal(aggregate.timezone)},
                              tpep_pickup_datetime)) AS target_hour_utc,
                   disposition, CAST(count(*) AS BIGINT) AS trip_count
            FROM ({labeled}) labeled
            WHERE disposition IN ('retained', 'null_zone', 'not_in_lookup', 'non_service_zone')
            GROUP BY zone_id, target_hour_utc, disposition
        """)
        connection.execute(f"""
            CREATE TEMP TABLE expected AS
            SELECT zone_id, target_hour_utc, trip_count, {_sql_literal(month)} AS source_month
            FROM source_counts WHERE disposition = 'retained'
        """)
        mismatch = connection.execute("""
            SELECT count(*) FROM (
                (SELECT * FROM observed EXCEPT ALL SELECT * FROM expected)
                UNION ALL
                (SELECT * FROM expected EXCEPT ALL SELECT * FROM observed)
            ) differences
        """).fetchone()[0]
        if mismatch:
            raise GridError(f"observed counts disagree with raw/lookup for {month}: {mismatch} rows; rerun aggregation")
        connection.execute("CREATE TEMP TABLE ambiguous (target_hour_utc TIMESTAMPTZ)")
        if ambiguous:
            connection.executemany("INSERT INTO ambiguous VALUES (?)", [(h,) for h in ambiguous])
        connection.execute("""
            CREATE TEMP TABLE coverage AS
            SELECT h.target_hour_utc,
                   CASE WHEN a.target_hour_utc IS NOT NULL THEN 'dst_ambiguous'
                        WHEN s.target_hour_utc IS NULL THEN 'source_missing'
                        ELSE 'available' END AS source_status
            FROM range(?::TIMESTAMPTZ, ?::TIMESTAMPTZ, INTERVAL '1 hour') h(target_hour_utc)
            LEFT JOIN (SELECT DISTINCT target_hour_utc FROM source_counts) s USING (target_hour_utc)
            LEFT JOIN ambiguous a USING (target_hour_utc)
        """, [start, end])
        connection.execute(f"""
            CREATE TEMP TABLE grid AS
            SELECT z.zone_id, c.target_hour_utc,
                   CASE WHEN c.source_status = 'available' THEN coalesce(o.trip_count, 0)
                        ELSE NULL END::BIGINT AS trip_count,
                   {_sql_literal(month)} AS source_month, c.source_status
            FROM zones z CROSS JOIN coverage c
            LEFT JOIN observed o USING (zone_id, target_hour_utc)
        """)
        row = connection.execute("""
            SELECT count(*), coalesce(sum(trip_count), 0),
                   count(*) FILTER (WHERE trip_count = 0),
                   count(*) FILTER (WHERE trip_count IS NULL),
                   count(*) FILTER (WHERE trip_count > 0),
                   count(DISTINCT (zone_id, target_hour_utc)),
                   count(*) FILTER (WHERE trip_count < 0 OR
                     ((source_status = 'available') != (trip_count IS NOT NULL)))
            FROM grid
        """).fetchone()
        expected_trips = connection.execute("SELECT coalesce(sum(trip_count), 0) FROM observed").fetchone()[0]
        if row[0] != hours * len(zones) or row[0] != row[5] or row[1] != expected_trips or row[6]:
            raise GridError(f"grid contract failed for {month}")
        missing_hours = [
            {"target_hour_utc": h, "reason": status}
            for h, status in connection.execute(
                "SELECT strftime(target_hour_utc, '%Y-%m-%dT%H:%M:%SZ'), source_status "
                "FROM coverage WHERE source_status != 'available' ORDER BY target_hour_utc"
            ).fetchall()
        ]
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".parquet", dir=destination.parent)
        os.close(fd)
        temporary = Path(name)
        try:
            connection.execute(f"""
                COPY (SELECT * FROM grid ORDER BY target_hour_utc, zone_id)
                TO {_sql_literal(temporary.as_posix())} (FORMAT PARQUET, COMPRESSION ZSTD)
            """)
            output_schema = pq.read_schema(temporary)
            if pq.read_metadata(temporary).num_rows != row[0]:
                raise GridError("serialized grid row count does not match")
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "month": month, "zone_ids": zones, "zone_count": len(zones), "hours": hours,
        "start_utc": start.isoformat(), "end_utc_exclusive": end.isoformat(),
        "output_rows": row[0], "trip_count": row[1], "zero_rows": row[2],
        "missing_rows": row[3], "observed_rows": row[4], "duplicate_keys": 0,
        "missing_hours": missing_hours,
        "inputs": [{"path": p.as_posix(), "sha256": _sha256(p)} for p in (raw, lookup, observed)],
        "output": {"path": destination.as_posix(), "sha256": _sha256(destination),
                   "bytes": destination.stat().st_size,
                   "schema": [{"name": f.name, "type": str(f.type)} for f in output_schema]},
    }


def run_grid(config_path: Path) -> dict:
    config = _load_config(config_path)
    aggregate = load_aggregate_config(config["aggregate_config"])
    if aggregate.timezone != "America/New_York":
        raise GridError("V1 grid supports America/New_York source timestamps only")
    source = load_config(aggregate.source_config_path)
    months = [s.month for s in source.months]
    for previous, current in zip(months, months[1:]):
        if _month_bounds(previous)[1] != _month_bounds(current)[0]:
            raise GridError("grid requires consecutive configured months")
    if config["output_dir"] == aggregate.output_dir or config["output_dir"] == source.raw_dir:
        raise GridError("grid output directory must differ from input directories")
    reports = []
    for month in months:
        try:
            item = _build_month(config, aggregate, source, month)
        except (duckdb.Error, pa.ArrowException, OSError) as exc:
            raise GridError(f"cannot build grid for {month}: {exc}") from exc
        reports.append(item)
    report = {
        "report_version": 1, "generated_at_utc": datetime.now(UTC).isoformat(),
        "config_path": config_path.resolve().as_posix(),
        "config_sha256": _sha256(config_path),
        "aggregate_config_sha256": _sha256(config["aggregate_config"]),
        "source_config_sha256": _sha256(aggregate.source_config_path),
        "timezone": aggregate.timezone, "duckdb_threads": aggregate.duckdb_threads,
        "duckdb_memory_limit": aggregate.duckdb_memory_limit,
        "versions": {"duckdb": duckdb.__version__, "pyarrow": pa.__version__},
        "coverage_policy": "available if any in-month, unambiguous raw pickup exists; partial loss cannot be detected",
        "zone_policy": "all service zones from lookup, independent of observed activity",
        "months": reports,
        "summary": {key: sum(r[key] for r in reports) for key in
                    ("hours", "output_rows", "trip_count", "zero_rows", "missing_rows", "observed_rows")},
    }
    report["summary"]["zone_count"] = reports[0]["zone_count"]
    _write_json(config["report_path"], report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/grid.json"))
    args = parser.parse_args()
    try:
        report = run_grid(args.config)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
