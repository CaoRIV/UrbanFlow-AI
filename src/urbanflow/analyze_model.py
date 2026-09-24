"""Analyze locked test predictions and generate the W3-T3 model card."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from urbanflow.evaluate_baseline import (
    _PREDICTION_SCHEMA as _BASELINE_PREDICTION_SCHEMA,
)
from urbanflow.train_model import _PREDICTION_SCHEMA as _MODEL_PREDICTION_SCHEMA

_CONFIG_KEYS = {
    "model_metrics_path",
    "model_predictions_path",
    "baseline_metrics_path",
    "baseline_predictions_path",
    "zone_lookup_path",
    "report_path",
    "model_card_path",
    "top_n",
    "duckdb_threads",
    "duckdb_memory_limit",
}
_MEMORY_LIMIT = re.compile(r"^[1-9][0-9]*(?:MB|GB)$")


class AnalysisError(ValueError):
    """Invalid analysis configuration, artifact, or evaluation result."""


@dataclass(frozen=True)
class AnalysisConfig:
    config_path: Path
    model_metrics_path: Path
    model_predictions_path: Path
    baseline_metrics_path: Path
    baseline_predictions_path: Path
    zone_lookup_path: Path
    report_path: Path
    model_card_path: Path
    top_n: int
    duckdb_threads: int
    duckdb_memory_limit: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _required_path(data: dict[str, Any], key: str, parent: Path) -> Path:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AnalysisError(f"{key} must be a non-empty path string")
    return (parent / value).resolve()


def _required_integer(
    data: dict[str, Any], key: str, minimum: int, maximum: int
) -> int:
    value = data.get(key)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise AnalysisError(f"{key} must be an integer from {minimum} to {maximum}")
    return value


def load_analysis_config(path: Path) -> AnalysisConfig:
    resolved = path.resolve()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"cannot read analysis config {path}: {exc}") from exc
    if not isinstance(data, dict) or set(data) != _CONFIG_KEYS:
        raise AnalysisError(
            f"analysis config must contain exactly {sorted(_CONFIG_KEYS)}"
        )

    parent = resolved.parent
    paths = {
        key: _required_path(data, key, parent)
        for key in (
            "model_metrics_path",
            "model_predictions_path",
            "baseline_metrics_path",
            "baseline_predictions_path",
            "zone_lookup_path",
            "report_path",
            "model_card_path",
        )
    }
    input_paths = {
        paths["model_metrics_path"],
        paths["model_predictions_path"],
        paths["baseline_metrics_path"],
        paths["baseline_predictions_path"],
        paths["zone_lookup_path"],
        resolved,
    }
    if paths["report_path"] in input_paths or paths["model_card_path"] in input_paths:
        raise AnalysisError("analysis outputs must not overwrite an input or config")
    if paths["report_path"] == paths["model_card_path"]:
        raise AnalysisError("report_path and model_card_path must be different")

    memory_limit = data.get("duckdb_memory_limit")
    if not isinstance(memory_limit, str) or not _MEMORY_LIMIT.fullmatch(memory_limit):
        raise AnalysisError("duckdb_memory_limit must look like 512MB or 1GB")

    return AnalysisConfig(
        config_path=resolved,
        model_metrics_path=paths["model_metrics_path"],
        model_predictions_path=paths["model_predictions_path"],
        baseline_metrics_path=paths["baseline_metrics_path"],
        baseline_predictions_path=paths["baseline_predictions_path"],
        zone_lookup_path=paths["zone_lookup_path"],
        report_path=paths["report_path"],
        model_card_path=paths["model_card_path"],
        top_n=_required_integer(data, "top_n", 1, 25),
        duckdb_threads=_required_integer(data, "duckdb_threads", 1, 4),
        duckdb_memory_limit=memory_limit,
    )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise AnalysisError(f"missing {label}: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"invalid {label} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AnalysisError(f"{label} must contain a JSON object: {path}")
    return data


def _validate_schema(
    path: Path,
    expected: pa.Schema | dict[str, pa.DataType],
    label: str,
) -> None:
    if not path.is_file():
        raise AnalysisError(f"missing {label}: {path}")
    schema = pq.read_schema(path)
    expected_fields = (
        list(expected)
        if isinstance(expected, pa.Schema)
        else [pa.field(name, data_type) for name, data_type in expected.items()]
    )
    if schema.names != [field.name for field in expected_fields] or any(
        schema.field(field.name).type != field.type for field in expected_fields
    ):
        raise AnalysisError(f"invalid {label} schema: {schema}")


def _number(value: Any) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


def _ratio(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return _number(numerator / denominator)


def _assert_close(actual: Any, expected: Any, label: str) -> None:
    if actual is None or expected is None:
        if actual is not expected:
            raise AnalysisError(f"{label} nullability does not match")
        return
    if abs(float(actual) - float(expected)) > 0.000001:
        raise AnalysisError(f"{label} does not match: {actual} != {expected}")


def _validate_reports(
    config: AnalysisConfig,
    model_metrics: dict[str, Any],
    baseline_metrics: dict[str, Any],
) -> None:
    try:
        model_output = model_metrics["output"]["predictions"]
        baseline_output = baseline_metrics["output"]
        model_baseline_hash = model_metrics["config"]["baseline_metrics_sha256"]
        selected_name = model_metrics["model"]["selected_candidate"]
        candidates = model_metrics["selection"]["candidates"]
    except (KeyError, TypeError) as exc:
        raise AnalysisError(f"metrics report is missing required field: {exc}") from exc

    checks = (
        (
            config.model_predictions_path,
            model_output.get("sha256"),
            model_output.get("bytes"),
            "model predictions",
        ),
        (
            config.baseline_predictions_path,
            baseline_output.get("sha256"),
            baseline_output.get("bytes"),
            "baseline predictions",
        ),
    )
    for path, expected_hash, expected_bytes, label in checks:
        if not path.is_file():
            raise AnalysisError(f"missing {label}: {path}")
        if _sha256(path) != expected_hash or path.stat().st_size != expected_bytes:
            raise AnalysisError(f"{label} hash or size does not match its metrics report")
    if _sha256(config.baseline_metrics_path) != model_baseline_hash:
        raise AnalysisError("model was not evaluated against the supplied baseline metrics")
    if (
        not isinstance(candidates, list)
        or not candidates
        or not all(isinstance(candidate, dict) for candidate in candidates)
    ):
        raise AnalysisError("model metrics contain invalid validation candidates")
    if any("test" in candidate for candidate in candidates):
        raise AnalysisError("candidate selection report must not contain test metrics")
    selected = [
        candidate for candidate in candidates if candidate.get("name") == selected_name
    ]
    if len(selected) != 1:
        raise AnalysisError("selected candidate is missing or duplicated")
    if selected[0].get("boost_rounds") != model_metrics["model"].get("boost_rounds"):
        raise AnalysisError("selected candidate rounds do not match the final model")


def _create_comparison(
    connection: duckdb.DuckDBPyConnection,
    config: AnalysisConfig,
    model_metrics: dict[str, Any],
    baseline_metrics: dict[str, Any],
) -> dict[str, Any]:
    model_path = _sql_literal(config.model_predictions_path.as_posix())
    baseline_path = _sql_literal(config.baseline_predictions_path.as_posix())
    zone_path = _sql_literal(config.zone_lookup_path.as_posix())

    connection.execute(f"""
        CREATE TEMP TABLE model_predictions AS
        SELECT zone_id, target_hour_utc, split, actual_trip_count,
               prediction_raw, prediction, absolute_error, candidate_name, fit_scope
        FROM read_parquet({model_path})
    """)
    connection.execute(f"""
        CREATE TEMP TABLE baseline_predictions AS
        SELECT zone_id, target_hour_utc, split, actual_trip_count,
               prediction, absolute_error
        FROM read_parquet({baseline_path})
        WHERE split = 'test'
    """)
    connection.execute(f"""
        CREATE TEMP TABLE zones AS
        SELECT CAST(LocationID AS INTEGER) AS zone_id,
               CAST(Borough AS VARCHAR) AS borough,
               CAST(Zone AS VARCHAR) AS zone_name,
               CAST(service_zone AS VARCHAR) AS service_zone
        FROM read_csv({zone_path}, header = true, all_varchar = true)
    """)

    model_summary = connection.execute("""
        SELECT count(*), count(DISTINCT (zone_id, target_hour_utc)),
               count(*) FILTER (WHERE zone_id IS NULL OR target_hour_utc IS NULL
                   OR split IS NULL OR actual_trip_count IS NULL
                   OR prediction_raw IS NULL OR prediction IS NULL
                   OR absolute_error IS NULL OR candidate_name IS NULL
                   OR fit_scope IS NULL),
               list_sort(list(DISTINCT split)),
               list_sort(list(DISTINCT candidate_name)),
               list_sort(list(DISTINCT fit_scope))
        FROM model_predictions
    """).fetchone()
    if model_summary[0] != model_summary[1] or model_summary[2] != 0:
        raise AnalysisError("model predictions contain duplicate keys or NULL values")
    if model_summary[3] != ["test", "validation"]:
        raise AnalysisError("model predictions must contain validation and test splits")
    if model_summary[4] != [model_metrics["model"]["selected_candidate"]]:
        raise AnalysisError("model predictions do not use the selected candidate")
    if model_summary[5] != ["train", "train_validation"]:
        raise AnalysisError("model predictions contain an invalid fit scope")

    baseline_summary = connection.execute("""
        SELECT count(*), count(DISTINCT (zone_id, target_hour_utc)),
               count(*) FILTER (WHERE zone_id IS NULL OR target_hour_utc IS NULL
                   OR actual_trip_count IS NULL OR prediction IS NULL
                   OR absolute_error IS NULL)
        FROM baseline_predictions
    """).fetchone()
    if baseline_summary[0] != baseline_summary[1] or baseline_summary[2] != 0:
        raise AnalysisError("baseline test predictions contain duplicate keys or NULL values")

    connection.execute("""
        CREATE TEMP TABLE comparison AS
        SELECT m.zone_id, m.target_hour_utc, m.actual_trip_count,
               m.prediction_raw AS model_prediction_raw,
               m.prediction AS model_prediction,
               b.prediction AS baseline_prediction,
               abs(m.actual_trip_count - m.prediction) AS model_absolute_error,
               abs(b.actual_trip_count - b.prediction) AS baseline_absolute_error,
               m.prediction - m.actual_trip_count AS model_signed_error,
               b.prediction - b.actual_trip_count AS baseline_signed_error,
               CAST(extract(hour FROM m.target_hour_utc) AS INTEGER) AS utc_hour
        FROM model_predictions m
        INNER JOIN baseline_predictions b USING (zone_id, target_hour_utc)
        WHERE m.split = 'test'
          AND m.fit_scope = 'train_validation'
          AND m.actual_trip_count = b.actual_trip_count
    """)
    joined = connection.execute("SELECT count(*) FROM comparison").fetchone()[0]
    model_test_rows = connection.execute(
        "SELECT count(*) FROM model_predictions WHERE split = 'test'"
    ).fetchone()[0]
    expected_model_rows = model_metrics["input"]["splits"]["test"]["eligible_rows"]
    expected_baseline_rows = baseline_metrics["splits"]["test"]["scored_rows"]
    if not (
        joined
        == model_test_rows
        == baseline_summary[0]
        == expected_model_rows
        == expected_baseline_rows
    ):
        raise AnalysisError("model and baseline test keys or actual values do not align")

    zone_validation = connection.execute("""
        SELECT count(DISTINCT c.zone_id),
               count(DISTINCT z.zone_id),
               count(*) FILTER (WHERE z.zone_id IS NULL),
               (SELECT count(*) - count(DISTINCT zone_id) FROM zones)
        FROM comparison c
        LEFT JOIN zones z USING (zone_id)
    """).fetchone()
    if zone_validation[0] != zone_validation[1] or zone_validation[2] != 0:
        raise AnalysisError("zone lookup does not cover every evaluated zone")
    if zone_validation[3] != 0:
        raise AnalysisError("zone lookup contains duplicate LocationID values")

    key_summary = connection.execute("""
        SELECT count(*) AS rows, count(DISTINCT zone_id) AS zones,
               count(DISTINCT target_hour_utc) AS hours,
               epoch(min(target_hour_utc)), epoch(max(target_hour_utc)),
               min(model_prediction),
               max(abs(model_absolute_error - abs(actual_trip_count - model_prediction))),
               max(abs(baseline_absolute_error - abs(actual_trip_count - baseline_prediction)))
        FROM comparison
    """).fetchone()
    if key_summary[5] < 0 or key_summary[6] > 0.000000000001 or key_summary[7] > 0.000000000001:
        raise AnalysisError("prediction clipping or stored absolute errors are invalid")
    return {
        "rows": int(key_summary[0]),
        "zone_count": int(key_summary[1]),
        "hour_count": int(key_summary[2]),
        "first_target_hour_utc": datetime.fromtimestamp(key_summary[3], UTC).isoformat(),
        "last_target_hour_utc": datetime.fromtimestamp(key_summary[4], UTC).isoformat(),
    }


def _overall_metrics(connection: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    row = connection.execute("""
        SELECT count(*), sum(actual_trip_count),
               avg(model_absolute_error), sum(model_absolute_error),
               avg(baseline_absolute_error), sum(baseline_absolute_error),
               avg(model_signed_error), avg(baseline_signed_error),
               count(*) FILTER (WHERE model_signed_error < 0),
               count(*) FILTER (WHERE model_signed_error > 0),
               count(*) FILTER (WHERE model_signed_error = 0),
               count(*) FILTER (WHERE baseline_signed_error < 0),
               count(*) FILTER (WHERE baseline_signed_error > 0),
               count(*) FILTER (WHERE baseline_signed_error = 0),
               count(*) FILTER (WHERE model_prediction_raw < 0),
               quantile_cont(model_absolute_error, 0.5),
               quantile_cont(model_absolute_error, 0.9),
               quantile_cont(model_absolute_error, 0.95),
               quantile_cont(model_absolute_error, 0.99),
               quantile_cont(baseline_absolute_error, 0.5),
               quantile_cont(baseline_absolute_error, 0.9),
               quantile_cont(baseline_absolute_error, 0.95),
               quantile_cont(baseline_absolute_error, 0.99)
        FROM comparison
    """).fetchone()
    count = int(row[0])
    actual_sum = float(row[1])

    def metrics(
        mae: Any,
        absolute_sum: Any,
        signed_mean: Any,
        under: Any,
        over: Any,
        exact: Any,
        quantiles: tuple[Any, Any, Any, Any],
    ) -> dict[str, Any]:
        return {
            "scored_rows": count,
            "actual_sum": _number(actual_sum),
            "mae": _number(mae),
            "wape": _ratio(float(absolute_sum), actual_sum),
            "absolute_error_sum": _number(absolute_sum),
            "mean_signed_error_prediction_minus_actual": _number(signed_mean),
            "underprediction_rows": int(under),
            "underprediction_rate": _ratio(int(under), count),
            "overprediction_rows": int(over),
            "overprediction_rate": _ratio(int(over), count),
            "exact_rows": int(exact),
            "absolute_error_quantiles": {
                "p50": _number(quantiles[0]),
                "p90": _number(quantiles[1]),
                "p95": _number(quantiles[2]),
                "p99": _number(quantiles[3]),
            },
        }

    model = metrics(row[2], row[3], row[6], row[8], row[9], row[10], row[15:19])
    model["negative_raw_prediction_rows"] = int(row[14])
    model["negative_raw_prediction_rate"] = _ratio(int(row[14]), count)
    baseline = metrics(row[4], row[5], row[7], row[11], row[12], row[13], row[19:23])
    return {"model": model, "baseline": baseline}


def _group_rows(
    rows: list[tuple[Any, ...]],
    *,
    zone: bool,
) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        index = 0
        item: dict[str, Any]
        if zone:
            item = {
                "zone_id": int(row[0]),
                "borough": row[1],
                "zone_name": row[2],
                "service_zone": row[3],
            }
            index = 4
        else:
            item = {"utc_hour": int(row[0])}
            index = 1
        count = int(row[index])
        actual_sum = float(row[index + 1])
        model_mae_raw = float(row[index + 2])
        baseline_mae_raw = float(row[index + 4])
        model_mae = _number(model_mae_raw)
        baseline_mae = _number(baseline_mae_raw)
        item.update({
            "scored_rows": count,
            "actual_sum": _number(actual_sum),
            "model": {
                "mae": model_mae,
                "wape": _ratio(float(row[index + 3]), actual_sum),
                "mean_signed_error_prediction_minus_actual": _number(row[index + 6]),
                "underprediction_rows": int(row[index + 8]),
                "overprediction_rows": int(row[index + 9]),
            },
            "baseline": {
                "mae": baseline_mae,
                "wape": _ratio(float(row[index + 5]), actual_sum),
                "mean_signed_error_prediction_minus_actual": _number(row[index + 7]),
                "underprediction_rows": int(row[index + 10]),
                "overprediction_rows": int(row[index + 11]),
            },
            "mae_improvement_baseline_minus_model": round(
                baseline_mae_raw - model_mae_raw, 12
            ),
        })
        result.append(item)
    return result


def _subgroups(connection: duckdb.DuckDBPyConnection) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    zone_rows = connection.execute("""
        SELECT c.zone_id, z.borough, z.zone_name, z.service_zone,
               count(*), sum(c.actual_trip_count),
               avg(c.model_absolute_error), sum(c.model_absolute_error),
               avg(c.baseline_absolute_error), sum(c.baseline_absolute_error),
               avg(c.model_signed_error), avg(c.baseline_signed_error),
               count(*) FILTER (WHERE c.model_signed_error < 0),
               count(*) FILTER (WHERE c.model_signed_error > 0),
               count(*) FILTER (WHERE c.baseline_signed_error < 0),
               count(*) FILTER (WHERE c.baseline_signed_error > 0)
        FROM comparison c
        INNER JOIN zones z USING (zone_id)
        GROUP BY c.zone_id, z.borough, z.zone_name, z.service_zone
        ORDER BY c.zone_id
    """).fetchall()
    hour_rows = connection.execute("""
        SELECT utc_hour, count(*), sum(actual_trip_count),
               avg(model_absolute_error), sum(model_absolute_error),
               avg(baseline_absolute_error), sum(baseline_absolute_error),
               avg(model_signed_error), avg(baseline_signed_error),
               count(*) FILTER (WHERE model_signed_error < 0),
               count(*) FILTER (WHERE model_signed_error > 0),
               count(*) FILTER (WHERE baseline_signed_error < 0),
               count(*) FILTER (WHERE baseline_signed_error > 0)
        FROM comparison
        GROUP BY utc_hour
        ORDER BY utc_hour
    """).fetchall()
    return _group_rows(zone_rows, zone=True), _group_rows(hour_rows, zone=False)


def _comparison_summary(
    overall: dict[str, Any],
    by_zone: list[dict[str, Any]],
    by_hour: list[dict[str, Any]],
    top_n: int,
) -> dict[str, Any]:
    model_mae = overall["model"]["mae"]
    baseline_mae = overall["baseline"]["mae"]
    model_wape = overall["model"]["wape"]
    baseline_wape = overall["baseline"]["wape"]
    mae_improvement = baseline_mae - model_mae
    wape_improvement = baseline_wape - model_wape

    model_wins = [item for item in by_zone if item["mae_improvement_baseline_minus_model"] > 0]
    ties = [item for item in by_zone if item["mae_improvement_baseline_minus_model"] == 0]
    regressions = [item for item in by_zone if item["mae_improvement_baseline_minus_model"] < 0]
    winner = "model" if (model_mae, model_wape) < (baseline_mae, baseline_wape) else "baseline"
    return {
        "overall_improvement": {
            "mae_absolute": _number(mae_improvement),
            "mae_relative": _ratio(mae_improvement, baseline_mae),
            "wape_absolute": _number(wape_improvement),
            "wape_relative": _ratio(wape_improvement, baseline_wape),
        },
        "zone_comparison": {
            "model_better_zones": len(model_wins),
            "tied_zones": len(ties),
            "model_worse_zones": len(regressions),
        },
        "worst_model_zones": sorted(
            by_zone, key=lambda item: (-item["model"]["mae"], item["zone_id"])
        )[:top_n],
        "largest_model_improvements": sorted(
            model_wins,
            key=lambda item: (-item["mae_improvement_baseline_minus_model"], item["zone_id"]),
        )[:top_n],
        "model_regression_zones": sorted(
            regressions,
            key=lambda item: (item["mae_improvement_baseline_minus_model"], item["zone_id"]),
        )[:top_n],
        "worst_model_utc_hours": sorted(
            by_hour, key=lambda item: (-item["model"]["mae"], item["utc_hour"])
        )[:top_n],
        "serving_winner": winner,
    }


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return lines


def _model_card(report: dict[str, Any], report_sha256: str) -> str:
    model = report["model"]
    baseline = report["baseline"]
    overall = report["test_evaluation"]["overall"]
    comparison = report["test_evaluation"]["comparison"]
    decision = report["serving_decision"]
    candidates = report["selection"]["candidates"]
    worst_zones = comparison["worst_model_zones"]
    regressions = comparison["model_regression_zones"]
    worst_hours = comparison["worst_model_utc_hours"]
    relative_mae = comparison["overall_improvement"]["mae_relative"]
    relative_wape = comparison["overall_improvement"]["wape_relative"]
    if relative_mae >= 0 and relative_wape >= 0:
        performance_line = (
            f"XGBoost cải thiện MAE tương đối **{relative_mae * 100:.4f}%** và "
            f"WAPE tương đối **{relative_wape * 100:.4f}%** so với baseline."
        )
    else:
        performance_line = (
            f"XGBoost kém baseline: MAE thay đổi **{relative_mae * 100:.4f}%** và "
            f"WAPE thay đổi **{relative_wape * 100:.4f}%** theo quy ước "
            "(baseline - model) / baseline."
        )

    lines = [
        "# Model card — UrbanFlow AI",
        "",
        "## Trạng thái và quyết định phục vụ",
        "",
        f"- **Model được chọn:** `{decision['model_version']}` (`{decision['algorithm']}`).",
        f"- **Quyết định:** {decision['reason']}",
        "- **Bối cảnh:** historical backtest, không phải dự báo production hoặc real time.",
        "",
        "## Mục đích",
        "",
        "Dự báo số lượt đón Yellow Taxi được ghi nhận cho từng taxi zone trong giờ kế tiếp.",
        "Đầu ra hỗ trợ demo hồi cứu trên test set; không đo nhu cầu chưa được phục vụ và",
        "không được diễn giải như quan hệ nhân quả.",
        "",
        "## Dữ liệu và hợp đồng đánh giá",
        "",
        f"- Test UTC: `{report['test_window']['start_utc']}` đến `{report['test_window']['end_utc_exclusive']}` (half-open).",
        f"- Test rows: {report['test_evaluation']['rows']:,}; zones: {report['test_evaluation']['zone_count']}; hours: {report['test_evaluation']['hour_count']}.",
        "- Feature chỉ dùng calendar UTC, zone và lag/rolling kết thúc trước target hour.",
        "- Candidate được chọn bằng validation MAE; test chỉ dùng sau khi khóa candidate và rounds.",
        "- Prediction âm được clip về 0 trước khi chấm điểm; raw prediction vẫn được lưu để audit.",
        "",
        "## Chọn cấu hình trên validation",
        "",
        *_markdown_table(
            ["Candidate", "Rounds", "MAE", "WAPE"],
            [
                [
                    f"`{item['name']}`",
                    item["boost_rounds"],
                    f"{item['validation']['mae']:.6f}",
                    f"{item['validation']['wape']:.6f}",
                ]
                for item in candidates
            ],
        ),
        "",
        "## Kết quả test đã khóa",
        "",
        *_markdown_table(
            ["Model", "MAE", "WAPE", "Mean signed error"],
            [
                [
                    f"XGBoost `{model['version']}`",
                    f"{overall['model']['mae']:.6f}",
                    f"{overall['model']['wape']:.6f}",
                    f"{overall['model']['mean_signed_error_prediction_minus_actual']:.6f}",
                ],
                [
                    f"Seasonal naive `{baseline['version']}`",
                    f"{overall['baseline']['mae']:.6f}",
                    f"{overall['baseline']['wape']:.6f}",
                    f"{overall['baseline']['mean_signed_error_prediction_minus_actual']:.6f}",
                ],
            ],
        ),
        "",
        performance_line,
        "",
        "## Phân tích residual",
        "",
        f"- XGBoost underpredict {overall['model']['underprediction_rows']:,} rows "
        f"({overall['model']['underprediction_rate'] * 100:.2f}%), overpredict "
        f"{overall['model']['overprediction_rows']:,} rows ({overall['model']['overprediction_rate'] * 100:.2f}%).",
        f"- Raw prediction âm: {overall['model']['negative_raw_prediction_rows']:,} rows "
        f"({overall['model']['negative_raw_prediction_rate'] * 100:.2f}%).",
        f"- Absolute-error p50/p90/p95/p99: "
        f"{overall['model']['absolute_error_quantiles']['p50']:.3f} / "
        f"{overall['model']['absolute_error_quantiles']['p90']:.3f} / "
        f"{overall['model']['absolute_error_quantiles']['p95']:.3f} / "
        f"{overall['model']['absolute_error_quantiles']['p99']:.3f}.",
        f"- Theo MAE, model tốt hơn baseline tại {comparison['zone_comparison']['model_better_zones']} zones, "
        f"hòa tại {comparison['zone_comparison']['tied_zones']} và kém hơn tại "
        f"{comparison['zone_comparison']['model_worse_zones']} zones.",
        "",
        "## Zone có MAE model cao nhất",
        "",
        *_markdown_table(
            ["Zone", "Borough", "Model MAE", "Baseline MAE", "Δ MAE baseline-model"],
            [
                [
                    f"{item['zone_id']} — {item['zone_name']}",
                    item["borough"],
                    f"{item['model']['mae']:.6f}",
                    f"{item['baseline']['mae']:.6f}",
                    f"{item['mae_improvement_baseline_minus_model']:.6f}",
                ]
                for item in worst_zones
            ],
        ),
        "",
        "## Zone model kém baseline nhiều nhất",
        "",
    ]
    if regressions:
        lines.extend(_markdown_table(
            ["Zone", "Borough", "Model MAE", "Baseline MAE", "Δ MAE baseline-model"],
            [
                [
                    f"{item['zone_id']} — {item['zone_name']}",
                    item["borough"],
                    f"{item['model']['mae']:.6f}",
                    f"{item['baseline']['mae']:.6f}",
                    f"{item['mae_improvement_baseline_minus_model']:.6f}",
                ]
                for item in regressions
            ],
        ))
    else:
        lines.append("Không có zone nào có MAE model cao hơn baseline trên test set này.")
    lines.extend([
        "",
        "## UTC hours có MAE model cao nhất",
        "",
        *_markdown_table(
            ["UTC hour", "Model MAE", "Baseline MAE", "Δ MAE baseline-model"],
            [
                [
                    item["utc_hour"],
                    f"{item['model']['mae']:.6f}",
                    f"{item['baseline']['mae']:.6f}",
                    f"{item['mae_improvement_baseline_minus_model']:.6f}",
                ]
                for item in worst_hours
            ],
        ),
        "",
        "## Tái lập và provenance",
        "",
        "```powershell",
        "python -m urbanflow.analyze_model --config configs/model_analysis.json",
        "```",
        "",
        f"- Model SHA-256: `{report['artifacts']['model_sha256']}`.",
        f"- Model predictions SHA-256: `{report['artifacts']['model_predictions_sha256']}`.",
        f"- Baseline predictions SHA-256: `{report['artifacts']['baseline_predictions_sha256']}`.",
        f"- Error-analysis report SHA-256: `{report_sha256}`.",
        f"- Train resources: {model['resources']['threads']} threads, "
        f"{model['resources']['elapsed_seconds']:.3f}s, RSS peak {model['resources']['rss_peak_bytes']:,} bytes.",
        "",
        "## Giới hạn",
        "",
        "- Chỉ đánh giá Q1/2026; chưa đo drift, mùa khác, holiday dài hạn hoặc thay đổi vận hành.",
        "- Target là pickup được ghi nhận, không phải toàn bộ nhu cầu đi lại.",
        "- Không có weather/event/traffic và không có luồng trip near-real-time trong V1.",
        "- MAE tổng thể có thể che subgroup yếu; các zone/hour ở trên cần được hiển thị trung thực trong demo.",
        "- Test đã được xem để quyết định model phục vụ, nhưng không được dùng để đổi feature, candidate hoặc rounds.",
        "- API/UI phải gắn nhãn historical backtest và trả đúng model version.",
        "",
    ])
    return "\n".join(lines)


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    _write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def run_analysis(config_path: Path) -> dict[str, Any]:
    config = load_analysis_config(config_path)
    model_metrics = _load_json(config.model_metrics_path, "model metrics")
    baseline_metrics = _load_json(config.baseline_metrics_path, "baseline metrics")
    _validate_schema(
        config.model_predictions_path, _MODEL_PREDICTION_SCHEMA, "model predictions"
    )
    _validate_schema(
        config.baseline_predictions_path,
        _BASELINE_PREDICTION_SCHEMA,
        "baseline predictions",
    )
    if not config.zone_lookup_path.is_file():
        raise AnalysisError(f"missing zone lookup: {config.zone_lookup_path}")
    _validate_reports(config, model_metrics, baseline_metrics)

    connection = duckdb.connect()
    try:
        connection.execute(f"SET threads = {config.duckdb_threads}")
        connection.execute(
            f"SET memory_limit = {_sql_literal(config.duckdb_memory_limit)}"
        )
        connection.execute("SET preserve_insertion_order = false")
        test_summary = _create_comparison(
            connection, config, model_metrics, baseline_metrics
        )
        overall = _overall_metrics(connection)
        by_zone, by_hour = _subgroups(connection)
    finally:
        connection.close()

    for name, persisted in (
        ("model", model_metrics["test"]),
        ("baseline", baseline_metrics["splits"]["test"]),
    ):
        _assert_close(overall[name]["mae"], persisted["mae"], f"{name} test MAE")
        _assert_close(overall[name]["wape"], persisted["wape"], f"{name} test WAPE")

    comparison = _comparison_summary(overall, by_zone, by_hour, config.top_n)
    model_wins = comparison["serving_winner"] == "model"
    model = model_metrics["model"]
    baseline = baseline_metrics["baseline"]
    candidate_summaries = [
        {
            "name": candidate["name"],
            "parameters": candidate["parameters"],
            "best_iteration": candidate["best_iteration"],
            "boost_rounds": candidate["boost_rounds"],
            "elapsed_seconds": candidate["elapsed_seconds"],
            "validation": {
                key: candidate["validation"][key]
                for key in ("mae", "wape", "actual_sum", "absolute_error_sum")
            },
        }
        for candidate in model_metrics["selection"]["candidates"]
    ]
    decision = {
        "algorithm": model["name"] if model_wins else baseline["name"],
        "model_version": model["version"] if model_wins else baseline["version"],
        "criterion": "locked test MAE; WAPE breaks an exact MAE tie",
        "reason": (
            f"Phục vụ {model['version']} vì locked-test MAE {overall['model']['mae']:.6f} "
            f"thấp hơn seasonal-naive MAE {overall['baseline']['mae']:.6f}; test không "
            "được dùng để đổi feature, candidate parameters hoặc boost rounds."
            if model_wins
            else f"Phục vụ {baseline['version']} vì model này không kém hơn trên "
            "locked-test MAE/WAPE."
        ),
    }
    report = {
        "report_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "config": {
            "path": config.config_path.as_posix(),
            "sha256": _sha256(config.config_path),
            "top_n": config.top_n,
            "duckdb_threads": config.duckdb_threads,
            "duckdb_memory_limit": config.duckdb_memory_limit,
        },
        "test_window": {
            "start_utc": baseline_metrics["splits"]["test"]["start_utc"],
            "end_utc_exclusive": baseline_metrics["splits"]["test"][
                "end_utc_exclusive"
            ],
        },
        "selection": {
            "criterion": model_metrics["selection"]["metric"],
            "tie_break": model_metrics["selection"]["tie_break"],
            "candidates": candidate_summaries,
            "selected_candidate": model["selected_candidate"],
            "boost_rounds": model["boost_rounds"],
            "candidate_reports_contain_test_metrics": False,
        },
        "model": {
            **model,
            "resources": model_metrics["resources"],
            "versions": model_metrics["versions"],
        },
        "baseline": baseline,
        "artifacts": {
            "model_metrics_path": config.model_metrics_path.as_posix(),
            "model_metrics_sha256": _sha256(config.model_metrics_path),
            "model_predictions_path": config.model_predictions_path.as_posix(),
            "model_predictions_sha256": _sha256(config.model_predictions_path),
            "baseline_metrics_path": config.baseline_metrics_path.as_posix(),
            "baseline_metrics_sha256": _sha256(config.baseline_metrics_path),
            "baseline_predictions_path": config.baseline_predictions_path.as_posix(),
            "baseline_predictions_sha256": _sha256(config.baseline_predictions_path),
            "zone_lookup_path": config.zone_lookup_path.as_posix(),
            "zone_lookup_sha256": _sha256(config.zone_lookup_path),
            "model_sha256": model_metrics["output"]["model"]["sha256"],
        },
        "test_evaluation": {
            **test_summary,
            "overall": overall,
            "comparison": comparison,
            "by_zone": by_zone,
            "by_utc_hour": by_hour,
        },
        "serving_decision": decision,
        "limitations": [
            "Historical backtest on Q1/2026 only; performance outside this period is unknown.",
            "Target is recorded Yellow Taxi pickups, not latent or unmet travel demand.",
            "V1 excludes weather, events, traffic, and near-real-time trip ingestion.",
            "Test informed the final serving choice but did not change features or tuning.",
        ],
    }
    _write_json(config.report_path, report)
    report_hash = _sha256(config.report_path)
    _write_text(config.model_card_path, _model_card(report, report_hash))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/model_analysis.json")
    )
    args = parser.parse_args()
    try:
        report = run_analysis(args.config)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(json.dumps({
        "report_path": load_analysis_config(args.config).report_path.as_posix(),
        "model_card_path": load_analysis_config(args.config).model_card_path.as_posix(),
        "serving_decision": report["serving_decision"],
        "test_overall": report["test_evaluation"]["overall"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
