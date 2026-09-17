from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from urbanflow.download_data import DataContractError, run_download


def _write_config(root: Path, trip_url: str, zone_url: str) -> Path:
    config_path = root / "download.json"
    config_path.write_text(
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
    return config_path


def _write_zone_lookup(path: Path) -> None:
    path.write_text(
        "LocationID,Borough,Zone,service_zone\n"
        "1,EWR,Newark Airport,EWR\n"
        "2,Queens,Jamaica Bay,Boro Zone\n",
        encoding="utf-8",
    )


def test_download_is_idempotent_and_records_file_contract(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    trip_source = source_dir / "yellow_tripdata_2026-01.parquet"
    zone_source = source_dir / "taxi_zone_lookup.csv"

    pq.write_table(
        pa.table(
            {
                "tpep_pickup_datetime": [datetime(2026, 1, 1, 0, 15)],
                "PULocationID": pa.array([1], type=pa.int32()),
            }
        ),
        trip_source,
    )
    _write_zone_lookup(zone_source)
    config_path = _write_config(
        tmp_path, trip_source.as_uri(), zone_source.as_uri()
    )

    first_result = run_download(config_path)
    downloaded_trip = tmp_path / "raw" / trip_source.name
    first_modified_at = downloaded_trip.stat().st_mtime_ns
    second_result = run_download(config_path)

    assert first_result["files"]["yellow_taxi"]["status"] == "downloaded"
    assert first_result["files"]["yellow_taxi"]["rows"] == 1
    assert first_result["files"]["taxi_zone_lookup"]["rows"] == 2
    assert second_result["files"]["yellow_taxi"]["status"] == "cached"
    assert second_result["files"]["taxi_zone_lookup"]["status"] == "cached"
    assert downloaded_trip.stat().st_mtime_ns == first_modified_at

    manifest = json.loads(
        (tmp_path / "raw" / "download_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["files"]["yellow_taxi"]["sha256"]
    assert {field["name"] for field in manifest["files"]["yellow_taxi"]["schema"]} >= {
        "tpep_pickup_datetime",
        "PULocationID",
    }


def test_download_rejects_parquet_without_required_columns(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    trip_source = source_dir / "yellow_tripdata_2026-01.parquet"
    zone_source = source_dir / "taxi_zone_lookup.csv"

    pq.write_table(pa.table({"wrong_column": [1]}), trip_source)
    _write_zone_lookup(zone_source)
    config_path = _write_config(
        tmp_path, trip_source.as_uri(), zone_source.as_uri()
    )

    with pytest.raises(DataContractError, match="PULocationID"):
        run_download(config_path)

    assert not (tmp_path / "raw" / trip_source.name).exists()
