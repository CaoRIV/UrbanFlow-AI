from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from urbanflow.api import ApiArtifactError, create_app

_PREDICTION_SCHEMA = pa.schema([
    pa.field("zone_id", pa.int32()),
    pa.field("target_hour_utc", pa.timestamp("us", tz="UTC")),
    pa.field("split", pa.string()),
    pa.field("actual_trip_count", pa.int64()),
    pa.field("prediction", pa.float64()),
    pa.field("absolute_error", pa.float64()),
])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(root: Path, *, duplicate_key: bool = False) -> Path:
    first_hour = datetime(2026, 3, 16, 4, tzinfo=UTC)
    second_hour = datetime(2026, 3, 16, 5, tzinfo=UTC)
    predictions = [
        {
            "zone_id": 1,
            "target_hour_utc": first_hour,
            "split": "test",
            "actual_trip_count": 4,
            "prediction": 5.0,
            "absolute_error": 1.0,
        },
        {
            "zone_id": 1,
            "target_hour_utc": second_hour,
            "split": "test",
            "actual_trip_count": 8,
            "prediction": 6.0,
            "absolute_error": 2.0,
        },
        {
            "zone_id": 2,
            "target_hour_utc": first_hour,
            "split": "test",
            "actual_trip_count": 0,
            "prediction": 0.0,
            "absolute_error": 0.0,
        },
        {
            "zone_id": 2,
            "target_hour_utc": second_hour,
            "split": "test",
            "actual_trip_count": 1,
            "prediction": 1.5,
            "absolute_error": 0.5,
        },
    ]
    if duplicate_key:
        predictions.append(dict(predictions[0]))

    predictions_path = root / "predictions.parquet"
    pq.write_table(
        pa.Table.from_pylist(predictions, schema=_PREDICTION_SCHEMA),
        predictions_path,
    )
    zone_lookup_path = root / "zones.csv"
    zone_lookup_path.write_text(
        "LocationID,Borough,Zone,service_zone\n"
        "1,Manhattan,Alpha,Yellow Zone\n"
        "2,Queens,Beta,Boro Zone\n"
        "3,Bronx,Not Served,Boro Zone\n",
        encoding="utf-8",
    )
    report = {
        "serving_decision": {
            "algorithm": "xgboost_hist_cpu",
            "model_version": "xgboost_fixture",
        },
        "model": {
            "name": "xgboost_hist_cpu",
            "version": "xgboost_fixture",
        },
        "baseline": {
            "name": "seasonal_naive",
            "version": "seasonal_naive_168h_v1",
        },
        "artifacts": {
            "model_predictions_sha256": _sha256(predictions_path),
            "baseline_predictions_sha256": "b" * 64,
            "zone_lookup_sha256": _sha256(zone_lookup_path),
        },
        "test_window": {
            "start_utc": "2026-03-16T04:00:00Z",
            "end_utc_exclusive": "2026-03-16T06:00:00Z",
        },
        "test_evaluation": {
            "rows": 4,
            "zone_count": 2,
            "hour_count": 2,
        },
    }
    report_path = root / "error-analysis.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    config = {
        "analysis_report_path": report_path.name,
        "predictions_path": predictions_path.name,
        "zone_lookup_path": zone_lookup_path.name,
        "duckdb_threads": 1,
        "duckdb_memory_limit": "64MB",
    }
    config_path = root / "api.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def test_api_contract_serves_locked_backtest_prediction(tmp_path: Path) -> None:
    app = create_app(_fixture(tmp_path))

    with TestClient(app) as client:
        health = client.get("/health")
        zones = client.get("/zones")
        forecast = client.get(
            "/forecast",
            params={"cutoff_utc": "2026-03-16T04:00:00Z", "zone_id": 1},
        )

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "service": "urbanflow-api",
        "source": "historical_backtest",
        "model_name": "xgboost_hist_cpu",
        "model_version": "xgboost_fixture",
        "test_start_utc": "2026-03-16T04:00:00Z",
        "test_end_utc_exclusive": "2026-03-16T06:00:00Z",
        "prediction_rows": 4,
        "zone_count": 2,
    }
    assert zones.status_code == 200
    assert zones.json() == {
        "source": "historical_backtest",
        "count": 2,
        "zones": [
            {
                "zone_id": 1,
                "borough": "Manhattan",
                "zone_name": "Alpha",
                "service_zone": "Yellow Zone",
            },
            {
                "zone_id": 2,
                "borough": "Queens",
                "zone_name": "Beta",
                "service_zone": "Boro Zone",
            },
        ],
    }
    assert forecast.status_code == 200
    assert forecast.json() == {
        "source": "historical_backtest",
        "cutoff_utc": "2026-03-16T04:00:00Z",
        "target_hour_utc": "2026-03-16T04:00:00Z",
        "zone_id": 1,
        "borough": "Manhattan",
        "zone_name": "Alpha",
        "service_zone": "Yellow Zone",
        "prediction": 5.0,
        "actual_trip_count": 4,
        "absolute_error": 1.0,
        "model_name": "xgboost_hist_cpu",
        "model_version": "xgboost_fixture",
    }


def test_forecast_rejects_invalid_time_and_zone_inputs(tmp_path: Path) -> None:
    app = create_app(_fixture(tmp_path))

    with TestClient(app) as client:
        cases = [
            (
                {"cutoff_utc": "2026-03-16T04:00:00", "zone_id": 1},
                422,
                "must include UTC offset",
            ),
            (
                {"cutoff_utc": "2026-03-16T05:00:00+01:00", "zone_id": 1},
                422,
                "must use UTC offset",
            ),
            (
                {"cutoff_utc": "2026-03-16T04:30:00Z", "zone_id": 1},
                422,
                "whole UTC hour",
            ),
            (
                {"cutoff_utc": "2026-03-16T03:00:00Z", "zone_id": 1},
                422,
                "inside the locked test window",
            ),
            (
                {"cutoff_utc": "2026-03-16T06:00:00Z", "zone_id": 1},
                422,
                "inside the locked test window",
            ),
            (
                {"cutoff_utc": "2026-03-16T04:00:00Z", "zone_id": 3},
                404,
                "not available in the test predictions",
            ),
        ]
        for params, expected_status, expected_detail in cases:
            response = client.get("/forecast", params=params)
            assert response.status_code == expected_status
            assert expected_detail in response.json()["detail"]

        assert client.get("/forecast", params={"zone_id": 1}).status_code == 422
        assert client.get(
            "/forecast",
            params={"cutoff_utc": "not-a-timestamp", "zone_id": 1},
        ).status_code == 422
        assert client.get(
            "/forecast",
            params={"cutoff_utc": "2026-03-16T04:00:00Z", "zone_id": 0},
        ).status_code == 422


def test_api_rejects_duplicate_prediction_keys_at_startup(tmp_path: Path) -> None:
    app = create_app(_fixture(tmp_path, duplicate_key=True))

    with pytest.raises(ApiArtifactError, match="duplicate keys"):
        with TestClient(app):
            pass
