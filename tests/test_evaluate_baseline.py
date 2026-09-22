from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from urbanflow.evaluate_baseline import run_baseline

_GRID_SCHEMA = pa.schema([
    pa.field("zone_id", pa.int32()),
    pa.field("target_hour_utc", pa.timestamp("us", tz="UTC")),
    pa.field("trip_count", pa.int64()),
    pa.field("source_month", pa.string()),
    pa.field("source_status", pa.string()),
])


def _write_grid(path: Path, values: list[int | None]) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        {
            "zone_id": 1,
            "target_hour_utc": start + timedelta(hours=index),
            "trip_count": value,
            "source_month": "2026-01",
            "source_status": "available" if value is not None else "source_missing",
        }
        for index, value in enumerate(values)
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=_GRID_SCHEMA), path)


def _fixture_config(root: Path, values: list[int | None]) -> tuple[Path, Path]:
    (root / "sources.json").write_text(json.dumps({
        "months": [{
            "month": "2026-01",
            "url": "https://example.org/yellow_tripdata_2026-01.parquet",
        }],
        "zone_lookup_url": "https://example.org/taxi_zone_lookup.csv",
        "raw_dir": "raw",
        "manifest_path": "raw/manifest.json",
    }), encoding="utf-8")
    (root / "aggregate.json").write_text(json.dumps({
        "source_config": "sources.json",
        "output_dir": "observed",
        "report_path": "aggregate-report.json",
        "timezone": "America/New_York",
        "duckdb_threads": 1,
        "duckdb_memory_limit": "256MB",
    }), encoding="utf-8")
    (root / "grid.json").write_text(json.dumps({
        "aggregate_config": "aggregate.json",
        "output_dir": "grid",
        "report_path": "grid-report.json",
    }), encoding="utf-8")
    grid_path = root / "grid/hourly_grid_2026-01.parquet"
    _write_grid(grid_path, values)
    config_path = root / "baseline.json"
    config_path.write_text(json.dumps({
        "grid_config": "grid.json",
        "predictions_path": "artifacts/predictions.parquet",
        "metrics_path": "artifacts/metrics.json",
        "seasonal_lag_hours": 2,
        "splits": {
            "train": {
                "start_utc": "2026-01-01T00:00:00Z",
                "end_utc_exclusive": "2026-01-01T04:00:00Z",
            },
            "validation": {
                "start_utc": "2026-01-01T04:00:00Z",
                "end_utc_exclusive": "2026-01-01T06:00:00Z",
            },
            "test": {
                "start_utc": "2026-01-01T06:00:00Z",
                "end_utc_exclusive": "2026-01-01T08:00:00Z",
            },
        },
    }), encoding="utf-8")
    return config_path, grid_path


def _prediction_rows(report: dict) -> dict[datetime, dict]:
    rows = pq.read_table(report["output"]["path"]).to_pylist()
    return {row["target_hour_utc"]: row for row in rows}


def test_baseline_uses_past_lag_and_train_only_evaluation_fallback(tmp_path):
    config, grid = _fixture_config(tmp_path, [10, 20, None, 40, 50, 60, 70, 80])
    first_report = run_baseline(config)
    first = _prediction_rows(first_report)
    start = datetime(2026, 1, 1, tzinfo=UTC)

    assert first[start]["prediction"] is None
    assert first[start + timedelta(hours=1)]["prediction"] == 10
    assert first[start + timedelta(hours=1)]["prediction_source"] == "prior_zone_mean"
    assert first[start + timedelta(hours=4)]["seasonal_lag_trip_count"] is None
    assert first[start + timedelta(hours=4)]["prediction"] == pytest.approx(70 / 3)
    assert first[start + timedelta(hours=4)]["prediction_source"] == "train_zone_mean"
    assert first[start + timedelta(hours=6)]["prediction"] == 50
    assert first_report["splits"]["validation"]["fallback_rows"] == 1
    assert first_report["splits"]["validation"]["fallback_rate"] == 0.5
    assert first_report["splits"]["validation"]["missing_target_rows"] == 0

    _write_grid(grid, [10, 20, None, 40, 500, 60, 70, 800])
    second = _prediction_rows(run_baseline(config))
    assert second[start + timedelta(hours=4)]["prediction"] == pytest.approx(70 / 3)
    assert second[start + timedelta(hours=6)]["prediction"] == 500
    assert second[start + timedelta(hours=7)]["prediction"] == 60


def test_split_metrics_exclude_missing_targets_and_zero_wape_denominator(tmp_path):
    config, _ = _fixture_config(tmp_path, [0, 0, None, 0, 0, 0, 0, 0])
    report = run_baseline(config)

    assert report["splits"]["train"]["missing_target_rows"] == 1
    assert report["splits"]["train"]["scored_rows"] == 2
    assert report["splits"]["validation"]["mae"] == 0
    assert report["splits"]["validation"]["wape"] is None
    assert report["splits"]["test"]["mae"] == 0
    assert report["splits"]["test"]["wape"] is None
    assert len(report["splits"]["test"]["mae_by_utc_hour"]) == 2


def test_rejects_noncontiguous_splits_before_writing_outputs(tmp_path):
    config, _ = _fixture_config(tmp_path, [1] * 8)
    data = json.loads(config.read_text(encoding="utf-8"))
    data["splits"]["validation"]["start_utc"] = "2026-01-01T05:00:00Z"
    config.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="contiguous"):
        run_baseline(config)
    assert not (tmp_path / "artifacts/predictions.parquet").exists()
    assert not (tmp_path / "artifacts/metrics.json").exists()


def test_rejects_duplicate_grid_keys_before_writing_outputs(tmp_path):
    config, grid = _fixture_config(tmp_path, [1] * 8)
    table = pq.read_table(grid)
    duplicate = pa.concat_tables([table, table.slice(0, 1)])
    pq.write_table(duplicate, grid)

    with pytest.raises(ValueError, match="rectangular, unique"):
        run_baseline(config)
    assert not (tmp_path / "artifacts/predictions.parquet").exists()
    assert not (tmp_path / "artifacts/metrics.json").exists()
