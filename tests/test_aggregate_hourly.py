from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from urbanflow.aggregate_hourly import _dst_anomalies, run_aggregation


def _write_configs(root: Path, trip_url: str, zone_url: str) -> Path:
    source_config = root / "data_sources.json"
    source_config.write_text(
        json.dumps(
            {
                "months": [{"month": "2026-03", "url": trip_url}],
                "zone_lookup_url": zone_url,
                "raw_dir": "raw",
                "manifest_path": "raw/download_manifest.json",
                "timeout_seconds": 10,
            }
        ),
        encoding="utf-8",
    )
    aggregate_config = root / "aggregate.json"
    aggregate_config.write_text(
        json.dumps(
            {
                "source_config": "data_sources.json",
                "output_dir": "processed",
                "report_path": "artifacts/report.json",
                "timezone": "America/New_York",
                "duckdb_threads": 1,
                "duckdb_memory_limit": "256MB",
            }
        ),
        encoding="utf-8",
    )
    return aggregate_config


def test_dst_anomalies_identify_spring_and_fall_transitions() -> None:
    spring = _dst_anomalies("2026-03", "America/New_York")
    fall = _dst_anomalies("2026-11", "America/New_York")

    assert spring == (
        type(spring[0])(
            kind="nonexistent",
            start=datetime(2026, 3, 8, 2),
            end=datetime(2026, 3, 8, 3),
        ),
    )
    assert fall == (
        type(fall[0])(
            kind="ambiguous",
            start=datetime(2026, 11, 1, 1),
            end=datetime(2026, 11, 1, 2),
        ),
    )


def test_aggregation_filters_rows_and_preserves_trip_count(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    trip_path = raw_dir / "yellow_tripdata_2026-03.parquet"
    zone_path = raw_dir / "taxi_zone_lookup.csv"

    pickup_times = pa.array(
        [
            datetime(2026, 2, 28, 23, 59),
            datetime(2026, 3, 8, 1, 15),
            datetime(2026, 3, 8, 1, 45),
            datetime(2026, 3, 8, 2, 30),
            datetime(2026, 3, 8, 3, 10),
            datetime(2026, 3, 8, 4, 10),
            datetime(2026, 3, 8, 5, 10),
            None,
            datetime(2026, 3, 8, 6, 10),
        ],
        type=pa.timestamp("us"),
    )
    pickup_zones = pa.array(
        [1, 1, 1, 1, 1, 264, 999, 1, None], type=pa.int32()
    )
    pq.write_table(
        pa.table(
            {
                "tpep_pickup_datetime": pickup_times,
                "PULocationID": pickup_zones,
                "unused_column": pa.array(range(9), type=pa.int64()),
            }
        ),
        trip_path,
    )
    zone_path.write_text(
        "LocationID,Borough,Zone,service_zone\n"
        "1,EWR,Newark Airport,EWR\n"
        "264,Unknown,N/A,N/A\n",
        encoding="utf-8",
    )
    config_path = _write_configs(
        tmp_path, trip_path.as_uri(), zone_path.as_uri()
    )

    report = run_aggregation(config_path)
    month = report["months"][0]

    assert month["filters"] == {
        "null_timestamp": 1,
        "outside_month": 1,
        "dst_nonexistent": 1,
        "dst_ambiguous": 0,
        "null_zone": 1,
        "not_in_lookup": 1,
        "non_service_zone": 1,
        "retained": 3,
        "raw_rows": 9,
        "excluded_rows": 6,
    }
    assert month["output"]["output_rows"] == 2
    assert month["output"]["output_trip_count"] == 3
    assert month["output"]["duplicate_keys"] == 0
    assert report["summary"]["retained_rows"] == 3
    assert report["summary"]["output_trip_count"] == 3

    output = pq.read_table(month["output"]["path"])
    assert output.schema.field("target_hour_utc").type.tz is not None
    rows = output.to_pylist()
    assert [row["trip_count"] for row in rows] == [2, 1]
    assert [row["target_hour_utc"].hour for row in rows] == [6, 7]
