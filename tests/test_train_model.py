from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest

from urbanflow.build_features import _FEATURE_SCHEMA
from urbanflow.train_model import (
    ModelResourceError,
    load_model_config,
    run_model,
)


def _write_features(path: Path, test_target_offset: int = 0) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    split_ranges = (("train", 0, 180), ("validation", 180, 228), ("test", 228, 276))
    for split, first, end in split_ranges:
        for hour_index in range(first, end):
            for zone_id in (1, 2, 3):
                base = zone_id * 3 + hour_index % 24 + (hour_index // 24) % 3
                target = base + (test_target_offset if split == "test" else 0)
                rows.append({
                    "zone_id": zone_id,
                    "target_hour_utc": start + timedelta(hours=hour_index),
                    "split": split,
                    "target_trip_count": target,
                    "source_status": "available",
                    "utc_hour": hour_index % 24,
                    "utc_day_of_week": (hour_index // 24) % 7 + 1,
                    "utc_month": 1,
                    "is_weekend": (hour_index // 24) % 7 + 1 in (6, 7),
                    "lag_1h": max(base - 1, 0),
                    "lag_24h": max(base - 2, 0),
                    "lag_168h": max(base - 3, 0),
                    "rolling_mean_24h": float(max(base - 1, 0)),
                    "rolling_mean_168h": float(max(base - 2, 0)),
                    "features_complete": True,
                })
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = pa.schema([
        pa.field(name, data_type) for name, data_type in _FEATURE_SCHEMA.items()
    ])
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    feature_path = tmp_path / "features.parquet"
    _write_features(feature_path)
    (tmp_path / "baseline.json").write_text("{}", encoding="utf-8")
    (tmp_path / "features.json").write_text(json.dumps({
        "baseline_config": "baseline.json",
        "output_path": "features.parquet",
        "report_path": "feature-report.json",
        "lag_hours": [1, 24, 168],
        "rolling_windows_hours": [24, 168],
    }), encoding="utf-8")
    (tmp_path / "baseline-metrics.json").write_text(json.dumps({
        "splits": {
            "validation": {"mae": 20.0, "wape": 1.0},
            "test": {"mae": 20.0, "wape": 1.0},
        }
    }), encoding="utf-8")
    config_path = tmp_path / "model.json"
    config_path.write_text(json.dumps({
        "feature_config": "features.json",
        "baseline_metrics_path": "baseline-metrics.json",
        "model_path": "artifacts/model.json",
        "manifest_path": "artifacts/manifest.json",
        "predictions_path": "artifacts/predictions.parquet",
        "metrics_path": "artifacts/metrics.json",
        "seed": 7,
        "threads": 1,
        "minimum_available_memory_mb": 16,
        "maximum_process_rss_mb": 2048,
        "max_boost_rounds": 40,
        "early_stopping_rounds": 5,
        "candidates": [{
            "name": "tiny",
            "max_depth": 2,
            "learning_rate": 0.2,
            "min_child_weight": 1.0,
            "subsample": 1.0,
            "colsample_bytree": 1.0,
            "reg_lambda": 1.0,
        }],
    }), encoding="utf-8")
    return config_path, feature_path


def _split_predictions(path: str, split: str) -> list[float]:
    table = pq.read_table(path)
    filtered = table.filter(pc.equal(table["split"], split))
    return filtered["prediction"].to_pylist()


def test_model_selection_and_fit_are_isolated_from_test_targets(tmp_path):
    config_path, feature_path = _fixture(tmp_path)
    first = run_model(config_path)
    first_validation_predictions = _split_predictions(
        first["output"]["predictions"]["path"], "validation"
    )
    first_test_predictions = _split_predictions(
        first["output"]["predictions"]["path"], "test"
    )

    _write_features(feature_path, test_target_offset=100)
    second = run_model(config_path)

    assert second["model"]["selected_candidate"] == first["model"]["selected_candidate"]
    assert second["model"]["boost_rounds"] == first["model"]["boost_rounds"]
    assert second["output"]["model"]["sha256"] == first["output"]["model"]["sha256"]
    assert _split_predictions(
        second["output"]["predictions"]["path"], "validation"
    ) == pytest.approx(first_validation_predictions)
    assert _split_predictions(
        second["output"]["predictions"]["path"], "test"
    ) == pytest.approx(first_test_predictions)
    assert second["validation"]["mae"] == first["validation"]["mae"]
    assert second["test"]["mae"] != first["test"]["mae"]

    predictions = pq.read_table(second["output"]["predictions"]["path"])
    keys = list(zip(
        predictions["zone_id"].to_pylist(),
        predictions["target_hour_utc"].to_pylist(),
    ))
    assert len(keys) == len(set(keys)) == 288


def test_preflight_stops_before_writing_when_system_memory_is_below_floor(tmp_path):
    config_path, _ = _fixture(tmp_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["minimum_available_memory_mb"] = 1_048_576
    config_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ModelResourceError, match="stopped at preflight"):
        run_model(config_path)
    assert not (tmp_path / "artifacts/model.json").exists()
    assert not (tmp_path / "artifacts/metrics.json").exists()


def test_model_config_rejects_more_than_four_threads(tmp_path):
    config_path, _ = _fixture(tmp_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    data["threads"] = 5
    config_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="threads must be an integer from 1 to 4"):
        load_model_config(config_path)
