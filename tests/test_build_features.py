from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from urbanflow.build_features import run_features

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


def _fixture_config(
    root: Path, values: list[int | None]
) -> tuple[Path, Path]:
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
    (root / "baseline.json").write_text(json.dumps({
        "grid_config": "grid.json",
        "predictions_path": "artifacts/predictions.parquet",
        "metrics_path": "artifacts/metrics.json",
        "seasonal_lag_hours": 168,
        "splits": {
            "train": {
                "start_utc": "2026-01-01T00:00:00Z",
                "end_utc_exclusive": "2026-01-06T00:00:00Z",
            },
            "validation": {
                "start_utc": "2026-01-06T00:00:00Z",
                "end_utc_exclusive": "2026-01-07T16:00:00Z",
            },
            "test": {
                "start_utc": "2026-01-07T16:00:00Z",
                "end_utc_exclusive": "2026-01-09T08:00:00Z",
            },
        },
    }), encoding="utf-8")
    config_path = root / "features.json"
    config_path.write_text(json.dumps({
        "baseline_config": "baseline.json",
        "output_path": "processed/features.parquet",
        "report_path": "artifacts/feature-report.json",
        "lag_hours": [1, 24, 168],
        "rolling_windows_hours": [24, 168],
    }), encoding="utf-8")
    return config_path, grid_path


def _feature_rows(report: dict) -> dict[datetime, dict]:
    rows = pq.read_table(report["output"]["path"]).to_pylist()
    return {row["target_hour_utc"]: row for row in rows}


def _model_features(row: dict) -> tuple:
    names = (
        "utc_hour",
        "utc_day_of_week",
        "utc_month",
        "is_weekend",
        "lag_1h",
        "lag_24h",
        "lag_168h",
        "rolling_mean_24h",
        "rolling_mean_168h",
        "features_complete",
    )
    return tuple(row[name] for name in names)


def test_features_use_only_counts_strictly_before_target(tmp_path):
    config, grid = _fixture_config(tmp_path, list(range(200)))
    first_report = run_features(config)
    first = _feature_rows(first_report)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    target = start + timedelta(hours=170)

    row = first[target]
    assert row["split"] == "test"
    assert row["utc_hour"] == 2
    assert row["utc_day_of_week"] == 4
    assert row["utc_month"] == 1
    assert row["is_weekend"] is False
    assert row["lag_1h"] == 169
    assert row["lag_24h"] == 146
    assert row["lag_168h"] == 2
    assert row["rolling_mean_24h"] == pytest.approx(157.5)
    assert row["rolling_mean_168h"] == pytest.approx(85.5)
    assert row["features_complete"] is True

    changed = list(range(200))
    changed[170] = 999
    changed[190] = 777
    _write_grid(grid, changed)
    second = _feature_rows(run_features(config))

    assert _model_features(second[target]) == _model_features(row)
    assert second[target]["target_trip_count"] == 999
    assert second[target + timedelta(hours=1)]["lag_1h"] == 999
    assert _model_features(second[start + timedelta(hours=169)]) == _model_features(
        first[start + timedelta(hours=169)]
    )


def test_missing_history_stays_null_in_lags_and_full_windows(tmp_path):
    values: list[int | None] = list(range(200))
    values[169] = None
    config, _ = _fixture_config(tmp_path, values)
    report = run_features(config)
    rows = _feature_rows(report)
    target = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=170)
    row = rows[target]

    assert row["lag_1h"] is None
    assert row["lag_24h"] == 146
    assert row["lag_168h"] == 2
    assert row["rolling_mean_24h"] is None
    assert row["rolling_mean_168h"] is None
    assert row["features_complete"] is False
    assert report["splits"]["test"]["missing_by_feature"]["lag_1h"] == 1
    assert report["splits"]["test"]["complete_feature_rows"] == 2
    assert report["splits"]["test"]["training_eligible_rows"] == 1


def test_rejects_duplicate_grid_keys_before_publishing(tmp_path):
    config, grid = _fixture_config(tmp_path, list(range(200)))
    table = pq.read_table(grid)
    pq.write_table(pa.concat_tables([table, table.slice(0, 1)]), grid)

    with pytest.raises(ValueError, match="rectangular, unique"):
        run_features(config)
    assert not (tmp_path / "processed/features.parquet").exists()
    assert not (tmp_path / "artifacts/feature-report.json").exists()
