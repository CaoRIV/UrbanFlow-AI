from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from urbanflow.inspect_data import run_inspection


def _write_configs(root: Path, trip_url: str, zone_url: str) -> Path:
    source_config = root / "data_sources.json"
    source_config.write_text(
        json.dumps(
            {
                "month": "2026-01",
                "yellow_taxi_url": trip_url,
                "zone_lookup_url": zone_url,
                "raw_dir": "raw",
                "manifest_path": "raw/download_manifest.json",
                "timeout_seconds": 10,
            }
        ),
        encoding="utf-8",
    )
    inspection_config = root / "eda.json"
    inspection_config.write_text(
        json.dumps(
            {
                "source_config": "data_sources.json",
                "output_path": "artifacts/quality.json",
                "timezone_assumption": "America/New_York",
                "duckdb_threads": 1,
                "duckdb_memory_limit": "256MB",
            }
        ),
        encoding="utf-8",
    )
    return inspection_config


def test_inspection_reports_quality_and_filter_decisions(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    trip_path = raw_dir / "yellow_tripdata_2026-01.parquet"
    zone_path = raw_dir / "taxi_zone_lookup.csv"

    pickup_times = pa.array(
        [
            datetime(2025, 12, 31, 23, 59),
            datetime(2026, 1, 1, 0, 15),
            datetime(2026, 1, 1, 1, 15),
            datetime(2026, 1, 1, 2, 15),
            datetime(2026, 1, 1, 3, 15),
            datetime(2026, 2, 1, 0, 1),
            None,
        ],
        type=pa.timestamp("us"),
    )
    pickup_zones = pa.array([1, 1, 264, 999, None, 1, 1], type=pa.int32())
    pq.write_table(
        pa.table(
            {
                "tpep_pickup_datetime": pickup_times,
                "PULocationID": pickup_zones,
                "unused_column": pa.array(range(7), type=pa.int64()),
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

    report = run_inspection(config_path)

    assert report["source"]["columns_scanned"] == [
        "tpep_pickup_datetime",
        "PULocationID",
    ]
    assert report["raw_quality"] == {
        "total_rows": 7,
        "pickup_null_rows": 1,
        "zone_null_rows": 1,
        "pickup_min": "2025-12-31T23:59:00",
        "pickup_max": "2026-02-01T00:01:00",
        "before_month_rows": 1,
        "after_month_rows": 1,
        "within_month_rows": 4,
        "distinct_raw_zone_ids": 3,
    }
    assert report["zones"]["within_month_null_zone_rows"] == 1
    assert report["zones"]["not_in_lookup_rows"] == 1
    assert report["zones"]["non_service_zone_rows"] == 1
    assert report["filter_decision"]["retained_rows"] == 1
    assert report["filter_decision"]["excluded_rows"] == 6
    assert report["hourly_coverage"]["expected_hours"] == 744
    assert report["hourly_coverage"]["observed_hours"] == 4
    assert report["resource_usage"]["rss_peak_bytes"] >= report["resource_usage"][
        "rss_start_bytes"
    ]
    assert (tmp_path / "artifacts" / "quality.json").is_file()
