from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from urbanflow.analyze_model import AnalysisError, run_analysis
from urbanflow.evaluate_baseline import (
    _PREDICTION_SCHEMA as _BASELINE_PREDICTION_SCHEMA,
)
from urbanflow.train_model import _PREDICTION_SCHEMA as _MODEL_PREDICTION_SCHEMA


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _metric(rows: list[dict[str, object]], prediction_key: str) -> dict[str, float | int]:
    test_rows = [row for row in rows if row["split"] == "test"]
    absolute_error_sum = sum(
        abs(float(row[prediction_key]) - int(row["actual_trip_count"]))
        for row in test_rows
    )
    actual_sum = sum(int(row["actual_trip_count"]) for row in test_rows)
    return {
        "scored_rows": len(test_rows),
        "actual_sum": float(actual_sum),
        "absolute_error_sum": absolute_error_sum,
        "mae": absolute_error_sum / len(test_rows),
        "wape": absolute_error_sum / actual_sum,
    }


def _write_fixture(
    root: Path,
    *,
    model_predictions: list[float] | None = None,
    mismatched_baseline_actual: bool = False,
) -> Path:
    test_hours = (
        datetime(2026, 3, 16, 4, tzinfo=UTC),
        datetime(2026, 3, 16, 5, tzinfo=UTC),
    )
    actuals = (10, 10, 1, 0)
    model_values = model_predictions or [11.0, 10.0, 2.0, 0.0]
    raw_values = [11.0, 10.0, 2.0, -0.5]
    baseline_values = [12.0, 12.0, 1.0, 0.0]
    keys = (
        (1, test_hours[0]),
        (1, test_hours[1]),
        (2, test_hours[0]),
        (2, test_hours[1]),
    )

    model_rows: list[dict[str, object]] = [
        {
            "zone_id": 1,
            "target_hour_utc": datetime(2026, 3, 15, 4, tzinfo=UTC),
            "split": "validation",
            "actual_trip_count": 5,
            "prediction_raw": 5.0,
            "prediction": 5.0,
            "absolute_error": 0.0,
            "candidate_name": "depth6",
            "fit_scope": "train",
        },
        {
            "zone_id": 2,
            "target_hour_utc": datetime(2026, 3, 15, 4, tzinfo=UTC),
            "split": "validation",
            "actual_trip_count": 0,
            "prediction_raw": -0.2,
            "prediction": 0.0,
            "absolute_error": 0.0,
            "candidate_name": "depth6",
            "fit_scope": "train",
        },
    ]
    for (zone_id, target_hour), actual, raw, prediction in zip(
        keys, actuals, raw_values, model_values, strict=True
    ):
        model_rows.append({
            "zone_id": zone_id,
            "target_hour_utc": target_hour,
            "split": "test",
            "actual_trip_count": actual,
            "prediction_raw": raw if prediction == max(raw, 0.0) else prediction,
            "prediction": prediction,
            "absolute_error": abs(actual - prediction),
            "candidate_name": "depth6",
            "fit_scope": "train_validation",
        })

    baseline_rows: list[dict[str, object]] = []
    for index, ((zone_id, target_hour), actual, prediction) in enumerate(
        zip(keys, actuals, baseline_values, strict=True)
    ):
        if mismatched_baseline_actual and index == 0:
            actual += 1
        baseline_rows.append({
            "zone_id": zone_id,
            "target_hour_utc": target_hour,
            "split": "test",
            "actual_trip_count": actual,
            "source_status": "ok",
            "seasonal_lag_trip_count": int(prediction),
            "prediction": prediction,
            "prediction_source": "seasonal_lag_168h",
            "used_fallback": False,
            "absolute_error": abs(actual - prediction),
        })

    model_path = root / "model.parquet"
    baseline_path = root / "baseline.parquet"
    pq.write_table(
        pa.Table.from_pylist(model_rows, schema=_MODEL_PREDICTION_SCHEMA), model_path
    )
    baseline_schema = pa.schema([
        pa.field(name, data_type)
        for name, data_type in _BASELINE_PREDICTION_SCHEMA.items()
    ])
    pq.write_table(pa.Table.from_pylist(baseline_rows, schema=baseline_schema), baseline_path)

    baseline_metric = _metric(baseline_rows, "prediction")
    baseline_metrics = {
        "baseline": {
            "name": "seasonal_naive",
            "version": "seasonal_naive_168h_v1",
            "lag_hours": 168,
            "seed": None,
            "negative_prediction_policy": "clip_to_zero_before_evaluation",
            "fallback_policy": {"validation_test": "train only"},
        },
        "output": {
            "sha256": _sha256(baseline_path),
            "bytes": baseline_path.stat().st_size,
        },
        "splits": {
            "test": {
                **baseline_metric,
                "start_utc": "2026-03-16T04:00:00Z",
                "end_utc_exclusive": "2026-03-16T06:00:00Z",
            }
        },
    }
    baseline_metrics_path = root / "baseline-metrics.json"
    baseline_metrics_path.write_text(
        json.dumps(baseline_metrics), encoding="utf-8"
    )

    model_metric = _metric(model_rows, "prediction")
    model_metrics = {
        "config": {"baseline_metrics_sha256": _sha256(baseline_metrics_path)},
        "input": {"splits": {"test": {"eligible_rows": len(actuals)}}},
        "model": {
            "name": "xgboost_hist_cpu",
            "version": "xgboost_fixture",
            "selected_candidate": "depth6",
            "boost_rounds": 3,
            "fit_scope": "train_validation",
            "parameters": {"max_depth": 6},
            "seed": 42,
            "negative_prediction_policy": "clip_to_zero_before_evaluation",
        },
        "selection": {
            "metric": "validation_mae_min",
            "tie_break": "validation_wape_then_candidate_order",
            "candidates": [{
                "name": "depth6",
                "parameters": {"max_depth": 6},
                "best_iteration": 2,
                "boost_rounds": 3,
                "elapsed_seconds": 0.1,
                "validation": {
                    "mae": 0.0,
                    "wape": 0.0,
                    "actual_sum": 5.0,
                    "absolute_error_sum": 0.0,
                },
            }],
        },
        "test": model_metric,
        "output": {
            "model": {"sha256": "a" * 64},
            "predictions": {
                "sha256": _sha256(model_path),
                "bytes": model_path.stat().st_size,
            },
        },
        "resources": {
            "threads": 1,
            "elapsed_seconds": 0.1,
            "rss_peak_bytes": 1_000,
        },
        "versions": {
            "numpy": "test",
            "pyarrow": pa.__version__,
            "scipy": "test",
            "xgboost": "test",
        },
    }
    model_metrics_path = root / "model-metrics.json"
    model_metrics_path.write_text(json.dumps(model_metrics), encoding="utf-8")

    zone_lookup_path = root / "zones.csv"
    zone_lookup_path.write_text(
        "LocationID,Borough,Zone,service_zone\n"
        "1,Manhattan,Alpha,Yellow Zone\n"
        "2,Queens,Beta,Boro Zone\n",
        encoding="utf-8",
    )
    config = {
        "model_metrics_path": model_metrics_path.name,
        "model_predictions_path": model_path.name,
        "baseline_metrics_path": baseline_metrics_path.name,
        "baseline_predictions_path": baseline_path.name,
        "zone_lookup_path": zone_lookup_path.name,
        "report_path": "analysis.json",
        "model_card_path": "model-card.md",
        "top_n": 2,
        "duckdb_threads": 1,
        "duckdb_memory_limit": "64MB",
    }
    config_path = root / "analysis-config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def test_analysis_selects_model_and_exposes_weak_subgroup(tmp_path: Path) -> None:
    config_path = _write_fixture(tmp_path)

    report = run_analysis(config_path)

    assert report["serving_decision"]["model_version"] == "xgboost_fixture"
    assert report["test_evaluation"]["overall"]["model"]["mae"] == 0.5
    assert report["test_evaluation"]["overall"]["baseline"]["mae"] == 1.0
    assert report["test_evaluation"]["overall"]["model"][
        "negative_raw_prediction_rows"
    ] == 1
    regressions = report["test_evaluation"]["comparison"]["model_regression_zones"]
    assert [(item["zone_id"], item["zone_name"]) for item in regressions] == [
        (2, "Beta")
    ]
    assert report["test_evaluation"]["comparison"]["zone_comparison"] == {
        "model_better_zones": 1,
        "tied_zones": 0,
        "model_worse_zones": 1,
    }
    model_card = (tmp_path / "model-card.md").read_text(encoding="utf-8")
    assert "historical backtest" in model_card
    assert "2 — Beta" in model_card
    assert "XGBoost cải thiện MAE" in model_card


def test_analysis_serves_baseline_when_model_is_worse(tmp_path: Path) -> None:
    config_path = _write_fixture(
        tmp_path, model_predictions=[14.0, 14.0, 2.0, 0.0]
    )

    report = run_analysis(config_path)

    assert report["serving_decision"]["model_version"] == "seasonal_naive_168h_v1"
    assert report["test_evaluation"]["overall"]["model"]["mae"] == 2.25
    assert report["test_evaluation"]["comparison"]["serving_winner"] == "baseline"
    model_card = (tmp_path / "model-card.md").read_text(encoding="utf-8")
    assert "XGBoost kém baseline" in model_card


def test_analysis_rejects_misaligned_actuals_before_writing_outputs(
    tmp_path: Path,
) -> None:
    config_path = _write_fixture(tmp_path, mismatched_baseline_actual=True)

    with pytest.raises(AnalysisError, match="keys or actual values do not align"):
        run_analysis(config_path)

    assert not (tmp_path / "analysis.json").exists()
    assert not (tmp_path / "model-card.md").exists()
