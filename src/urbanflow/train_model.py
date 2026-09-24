"""Train and evaluate a memory-bounded CPU model on fixed time splits."""
from __future__ import annotations

import argparse
import gc
import json
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psutil
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from urbanflow.aggregate_hourly import _sha256, _write_json
from urbanflow.build_features import (
    _FEATURE_COLUMNS,
    _FEATURE_SCHEMA,
    load_feature_config,
)

_SPLIT_NAMES = ("train", "validation", "test")
_NUMERIC_FEATURES = tuple(name for name in _FEATURE_COLUMNS if name != "zone_id")
_CANDIDATE_KEYS = {
    "name",
    "max_depth",
    "learning_rate",
    "min_child_weight",
    "subsample",
    "colsample_bytree",
    "reg_lambda",
}
_PREDICTION_SCHEMA = pa.schema([
    pa.field("zone_id", pa.int32()),
    pa.field("target_hour_utc", pa.timestamp("us", tz="UTC")),
    pa.field("split", pa.string()),
    pa.field("actual_trip_count", pa.int64()),
    pa.field("prediction_raw", pa.float64()),
    pa.field("prediction", pa.float64()),
    pa.field("absolute_error", pa.float64()),
    pa.field("candidate_name", pa.string()),
    pa.field("fit_scope", pa.string()),
])


class ModelError(ValueError):
    """Invalid model configuration, input, output, or training result."""


class ModelResourceError(ModelError):
    """Training stopped before system or process memory exceeds its budget."""


@dataclass(frozen=True)
class Candidate:
    name: str
    parameters: dict[str, int | float]


@dataclass(frozen=True)
class ModelConfig:
    config_path: Path
    feature_config_path: Path
    baseline_metrics_path: Path
    model_path: Path
    manifest_path: Path
    predictions_path: Path
    metrics_path: Path
    seed: int
    threads: int
    minimum_available_memory_bytes: int
    maximum_process_rss_bytes: int
    max_boost_rounds: int
    early_stopping_rounds: int
    candidates: tuple[Candidate, ...]


@dataclass
class SplitData:
    name: str
    zone_id: Any
    target_hour_us: Any
    target: Any
    matrix: Any
    total_rows: int
    eligible_rows: int


@dataclass(frozen=True)
class PreparedData:
    splits: dict[str, SplitData]
    zone_ids: tuple[int, ...]
    feature_names: tuple[str, ...]
    input_rows: int


class MemoryBudget:
    """Fail closed when system availability or this process exceeds configured limits."""

    def __init__(self, minimum_available_bytes: int, maximum_rss_bytes: int) -> None:
        self.minimum_available_bytes = minimum_available_bytes
        self.maximum_rss_bytes = maximum_rss_bytes
        self.process = psutil.Process()
        self.peak_rss_bytes = 0
        self.minimum_available_seen_bytes = psutil.virtual_memory().available

    def check(self, stage: str) -> None:
        available = psutil.virtual_memory().available
        rss = self.process.memory_info().rss
        self.peak_rss_bytes = max(self.peak_rss_bytes, rss)
        self.minimum_available_seen_bytes = min(
            self.minimum_available_seen_bytes, available
        )
        if available < self.minimum_available_bytes:
            raise ModelResourceError(
                f"stopped at {stage}: system available memory {available} bytes is "
                f"below the configured minimum {self.minimum_available_bytes} bytes"
            )
        if rss > self.maximum_rss_bytes:
            raise ModelResourceError(
                f"stopped at {stage}: process RSS {rss} bytes exceeds the configured "
                f"maximum {self.maximum_rss_bytes} bytes"
            )



def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ModelError(f"{key} must be a non-empty string")
    return value.strip()


def _required_integer(
    data: dict[str, Any], key: str, minimum: int, maximum: int
) -> int:
    value = data.get(key)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        raise ModelError(f"{key} must be an integer from {minimum} to {maximum}")
    return value


def _candidate_number(
    item: dict[str, Any],
    key: str,
    minimum: float,
    maximum: float,
    *,
    include_zero: bool = False,
) -> float:
    value = item.get(key)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ModelError(f"candidate {key} must be numeric")
    number = float(value)
    lower_valid = number >= minimum if include_zero else number > minimum
    if not lower_valid or number > maximum:
        operator = "at least" if include_zero else "greater than"
        raise ModelError(
            f"candidate {key} must be {operator} {minimum} and at most {maximum}"
        )
    return number


def _parse_candidate(item: Any, index: int) -> Candidate:
    if not isinstance(item, dict) or set(item) != _CANDIDATE_KEYS:
        raise ModelError(
            f"candidates[{index}] must contain exactly {sorted(_CANDIDATE_KEYS)}"
        )
    name = _required_string(item, "name")
    max_depth = item.get("max_depth")
    if (
        not isinstance(max_depth, int)
        or isinstance(max_depth, bool)
        or not 2 <= max_depth <= 12
    ):
        raise ModelError("candidate max_depth must be an integer from 2 to 12")
    return Candidate(
        name=name,
        parameters={
            "max_depth": max_depth,
            "eta": _candidate_number(item, "learning_rate", 0.0, 1.0),
            "min_child_weight": _candidate_number(
                item, "min_child_weight", 0.0, 1_000_000.0, include_zero=True
            ),
            "subsample": _candidate_number(item, "subsample", 0.0, 1.0),
            "colsample_bytree": _candidate_number(
                item, "colsample_bytree", 0.0, 1.0
            ),
            "reg_lambda": _candidate_number(
                item, "reg_lambda", 0.0, 1_000_000.0, include_zero=True
            ),
        },
    )


def load_model_config(path: Path) -> ModelConfig:
    resolved = path.resolve()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelError(f"cannot read model config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ModelError("model config root must be an object")

    base = resolved.parent
    path_keys = {
        "feature_config": None,
        "baseline_metrics_path": ".json",
        "model_path": ".json",
        "manifest_path": ".json",
        "predictions_path": ".parquet",
        "metrics_path": ".json",
    }
    paths: dict[str, Path] = {}
    for key, suffix in path_keys.items():
        value = (base / _required_string(data, key)).resolve()
        if suffix is not None and value.suffix.lower() != suffix:
            raise ModelError(f"{key} must end in {suffix}")
        paths[key] = value
    output_paths = {
        paths["model_path"],
        paths["manifest_path"],
        paths["predictions_path"],
        paths["metrics_path"],
    }
    if len(output_paths) != 4:
        raise ModelError("model, manifest, predictions, and metrics paths must differ")
    if resolved in output_paths or paths["feature_config"] in output_paths:
        raise ModelError("output paths must not overwrite configuration files")

    seed = _required_integer(data, "seed", 0, 2**31 - 1)
    threads = _required_integer(data, "threads", 1, 4)
    minimum_memory_mb = _required_integer(
        data, "minimum_available_memory_mb", 16, 1_048_576
    )
    maximum_rss_mb = _required_integer(
        data, "maximum_process_rss_mb", 64, 1_048_576
    )
    max_rounds = _required_integer(data, "max_boost_rounds", 2, 2_000)
    early_stopping = _required_integer(
        data, "early_stopping_rounds", 1, max_rounds - 1
    )

    raw_candidates = data.get("candidates")
    if not isinstance(raw_candidates, list) or not 1 <= len(raw_candidates) <= 3:
        raise ModelError("candidates must contain from one to three configurations")
    candidates = tuple(
        _parse_candidate(item, index) for index, item in enumerate(raw_candidates)
    )
    names = [candidate.name for candidate in candidates]
    if len(names) != len(set(names)):
        raise ModelError("candidate names must be unique")

    return ModelConfig(
        config_path=resolved,
        feature_config_path=paths["feature_config"],
        baseline_metrics_path=paths["baseline_metrics_path"],
        model_path=paths["model_path"],
        manifest_path=paths["manifest_path"],
        predictions_path=paths["predictions_path"],
        metrics_path=paths["metrics_path"],
        seed=seed,
        threads=threads,
        minimum_available_memory_bytes=minimum_memory_mb * 1024 * 1024,
        maximum_process_rss_bytes=maximum_rss_mb * 1024 * 1024,
        max_boost_rounds=max_rounds,
        early_stopping_rounds=early_stopping,
        candidates=candidates,
    )


def _validate_feature_schema(path: Path) -> None:
    if not path.is_file():
        raise ModelError(
            f"missing feature dataset: {path}; run urbanflow.build_features first"
        )
    schema = pq.read_schema(path)
    if schema.names != list(_FEATURE_SCHEMA) or any(
        schema.field(name).type != expected
        for name, expected in _FEATURE_SCHEMA.items()
    ):
        raise ModelError(f"invalid feature schema: {schema}")


def _numpy_column(table: pa.Table, name: str, np: Any) -> Any:
    values = table[name].combine_chunks()
    return np.asarray(values.to_numpy(zero_copy_only=False))


def _build_split(
    table: pa.Table,
    name: str,
    zone_ids: Any,
    zone_universe: Any,
    timestamps_us: Any,
    split_mask: Any,
    complete: Any,
    target_valid: Any,
    targets: Any,
    numeric_columns: dict[str, Any],
    np: Any,
    sparse: Any,
) -> SplitData:
    eligible = split_mask & complete & target_valid
    total_rows = int(np.count_nonzero(split_mask))
    indices = np.flatnonzero(eligible)
    if total_rows == 0 or indices.size == 0:
        raise ModelError(f"split {name} has no eligible rows")

    split_zones = zone_ids[indices].astype(np.int32, copy=False)
    zone_positions = np.searchsorted(zone_universe, split_zones)
    if np.any(zone_positions >= zone_universe.size) or np.any(
        zone_universe[zone_positions] != split_zones
    ):
        raise ModelError(f"split {name} contains a zone outside the fixed universe")

    numeric = np.column_stack(
        [numeric_columns[column][indices] for column in _NUMERIC_FEATURES]
    ).astype(np.float32, copy=False)
    if not np.isfinite(numeric).all():
        raise ModelError(f"split {name} contains non-finite eligible features")
    rows = np.arange(indices.size, dtype=np.int32)
    one_hot = sparse.csr_matrix(
        (
            np.ones(indices.size, dtype=np.float32),
            (rows, zone_positions.astype(np.int32, copy=False)),
        ),
        shape=(indices.size, zone_universe.size),
    )
    matrix = sparse.hstack(
        [one_hot, sparse.csr_matrix(numeric)], format="csr", dtype=np.float32
    )
    return SplitData(
        name=name,
        zone_id=split_zones,
        target_hour_us=timestamps_us[indices].astype(np.int64, copy=False),
        target=targets[indices].astype(np.float32, copy=False),
        matrix=matrix,
        total_rows=total_rows,
        eligible_rows=int(indices.size),
    )


def _prepare_data(path: Path, budget: MemoryBudget, np: Any, sparse: Any) -> PreparedData:
    _validate_feature_schema(path)
    budget.check("before feature load")
    columns = [
        "zone_id",
        "target_hour_utc",
        "split",
        "target_trip_count",
        *_NUMERIC_FEATURES,
        "features_complete",
    ]
    table = pq.read_table(path, columns=columns, use_threads=False)
    budget.check("after feature load")
    if table.num_rows == 0:
        raise ModelError("feature dataset is empty")

    split_values = set(pc.unique(table["split"]).to_pylist())
    if split_values != set(_SPLIT_NAMES):
        raise ModelError("feature dataset must contain exactly train, validation, and test")

    zone_ids = np.asarray(
        table["zone_id"].combine_chunks().to_numpy(zero_copy_only=False),
        dtype=np.int32,
    )
    timestamp_array = table["target_hour_utc"].combine_chunks().cast(pa.int64())
    timestamps_us = np.asarray(
        timestamp_array.to_numpy(zero_copy_only=False), dtype=np.int64
    )
    if np.any(timestamps_us[1:] < timestamps_us[:-1]) or np.any(
        (timestamps_us[1:] == timestamps_us[:-1])
        & (zone_ids[1:] <= zone_ids[:-1])
    ):
        raise ModelError(
            "feature rows must be uniquely ordered by target_hour_utc then zone_id"
        )

    zone_universe = np.unique(zone_ids)
    if zone_universe.size == 0 or np.any(zone_universe <= 0):
        raise ModelError("feature dataset contains no valid zones")

    target_column = table["target_trip_count"].combine_chunks()
    target_valid = np.asarray(
        target_column.is_valid().to_numpy(zero_copy_only=False), dtype=bool
    )
    targets = np.asarray(
        pc.fill_null(target_column, 0).to_numpy(zero_copy_only=False),
        dtype=np.float32,
    )
    complete = np.asarray(
        pc.fill_null(table["features_complete"].combine_chunks(), False).to_numpy(
            zero_copy_only=False
        ),
        dtype=bool,
    )
    numeric_columns = {
        name: _numpy_column(table, name, np) for name in _NUMERIC_FEATURES
    }
    splits: dict[str, SplitData] = {}
    for name in _SPLIT_NAMES:
        mask = np.asarray(
            pc.equal(table["split"], name).to_numpy(zero_copy_only=False), dtype=bool
        )
        splits[name] = _build_split(
            table,
            name,
            zone_ids,
            zone_universe,
            timestamps_us,
            mask,
            complete,
            target_valid,
            targets,
            numeric_columns,
            np,
            sparse,
        )
        budget.check(f"after preparing {name} split")

    feature_names = tuple(
        [f"zone_{int(zone_id)}" for zone_id in zone_universe]
        + list(_NUMERIC_FEATURES)
    )
    return PreparedData(
        splits=splits,
        zone_ids=tuple(int(value) for value in zone_universe),
        feature_names=feature_names,
        input_rows=table.num_rows,
    )


def _metric_summary(
    actual: Any,
    prediction_raw: Any,
    zone_id: Any,
    target_hour_us: Any,
    np: Any,
) -> tuple[dict[str, Any], Any, Any]:
    actual_64 = np.asarray(actual, dtype=np.float64)
    raw_64 = np.asarray(prediction_raw, dtype=np.float64)
    if actual_64.shape != raw_64.shape or actual_64.size == 0:
        raise ModelError("metrics require equally sized, non-empty actual and prediction")
    if not np.isfinite(actual_64).all() or not np.isfinite(raw_64).all():
        raise ModelError("metrics require finite actual and prediction values")
    prediction = np.maximum(raw_64, 0.0)
    absolute_error = np.abs(actual_64 - prediction)
    denominator = float(actual_64.sum(dtype=np.float64))
    by_zone = []
    for zone in np.unique(zone_id):
        mask = zone_id == zone
        by_zone.append({
            "zone_id": int(zone),
            "scored_rows": int(np.count_nonzero(mask)),
            "mae": round(float(absolute_error[mask].mean()), 6),
        })
    utc_hour = (np.asarray(target_hour_us, dtype=np.int64) // 3_600_000_000) % 24
    by_hour = []
    for hour in range(24):
        mask = utc_hour == hour
        if np.any(mask):
            by_hour.append({
                "utc_hour": hour,
                "scored_rows": int(np.count_nonzero(mask)),
                "mae": round(float(absolute_error[mask].mean()), 6),
            })
    metrics = {
        "scored_rows": int(actual_64.size),
        "mae": round(float(absolute_error.mean()), 6),
        "wape": None
        if denominator == 0.0
        else round(float(absolute_error.sum(dtype=np.float64) / denominator), 6),
        "actual_sum": round(denominator, 6),
        "absolute_error_sum": round(
            float(absolute_error.sum(dtype=np.float64)), 6
        ),
        "mae_by_zone": by_zone,
        "mae_by_utc_hour": by_hour,
    }
    return metrics, prediction, absolute_error


def _memory_callback(xgb: Any, budget: MemoryBudget, label: str) -> Any:
    class MemoryGuardCallback(xgb.callback.TrainingCallback):
        def after_iteration(
            self, model: Any, epoch: int, evals_log: dict[str, Any]
        ) -> bool:
            budget.check(f"{label} iteration {epoch + 1}")
            return False

    return MemoryGuardCallback()


def _base_parameters(config: ModelConfig) -> dict[str, Any]:
    return {
        "objective": "reg:squarederror",
        "eval_metric": "mae",
        "tree_method": "hist",
        "max_bin": 128,
        "nthread": config.threads,
        "seed": config.seed,
        "verbosity": 0,
    }


def _train_candidates(
    config: ModelConfig,
    prepared: PreparedData,
    budget: MemoryBudget,
    np: Any,
    xgb: Any,
) -> tuple[Candidate, int, Any, list[dict[str, Any]]]:
    train = prepared.splits["train"]
    validation = prepared.splits["validation"]
    budget.check("before candidate matrices")
    dtrain = xgb.DMatrix(
        train.matrix,
        label=train.target,
        feature_names=list(prepared.feature_names),
        nthread=config.threads,
    )
    dvalidation = xgb.DMatrix(
        validation.matrix,
        label=validation.target,
        feature_names=list(prepared.feature_names),
        nthread=config.threads,
    )
    budget.check("after candidate matrices")

    selected_candidate: Candidate | None = None
    selected_rounds = 0
    selected_booster: Any = None
    selected_prediction: Any = None
    selected_key: tuple[float, float, str] | None = None
    reports: list[dict[str, Any]] = []
    for candidate in config.candidates:
        budget.check(f"before candidate {candidate.name}")
        started = time.perf_counter()
        parameters = {**_base_parameters(config), **candidate.parameters}
        booster = xgb.train(
            parameters,
            dtrain,
            num_boost_round=config.max_boost_rounds,
            evals=[(dvalidation, "validation")],
            early_stopping_rounds=config.early_stopping_rounds,
            verbose_eval=False,
            callbacks=[_memory_callback(xgb, budget, candidate.name)],
        )
        best_iteration = int(booster.best_iteration)
        rounds = best_iteration + 1
        prediction_raw = booster.predict(
            dvalidation, iteration_range=(0, rounds)
        )
        metrics, _, _ = _metric_summary(
            validation.target,
            prediction_raw,
            validation.zone_id,
            validation.target_hour_us,
            np,
        )
        report = {
            "name": candidate.name,
            "parameters": candidate.parameters,
            "best_iteration": best_iteration,
            "boost_rounds": rounds,
            "validation": metrics,
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
        reports.append(report)
        wape_key = float("inf") if metrics["wape"] is None else metrics["wape"]
        key = (metrics["mae"], wape_key, candidate.name)
        if selected_key is None or key < selected_key:
            selected_candidate = candidate
            selected_rounds = rounds
            selected_booster = booster
            selected_prediction = prediction_raw
            selected_key = key
        else:
            del booster
        budget.check(f"after candidate {candidate.name}")

    del dtrain, dvalidation
    gc.collect()
    if selected_candidate is None or selected_prediction is None:
        raise ModelError("no candidate completed training")
    del selected_booster
    gc.collect()
    return (
        selected_candidate,
        selected_rounds,
        selected_prediction,
        reports,
    )


def _fit_final_model(
    config: ModelConfig,
    candidate: Candidate,
    rounds: int,
    prepared: PreparedData,
    budget: MemoryBudget,
    np: Any,
    sparse: Any,
    xgb: Any,
) -> tuple[Any, Any]:
    train = prepared.splits["train"]
    validation = prepared.splits["validation"]
    test = prepared.splits["test"]
    budget.check("before final matrix assembly")
    combined_matrix = sparse.vstack(
        [train.matrix, validation.matrix], format="csr", dtype=np.float32
    )
    combined_target = np.concatenate([train.target, validation.target])
    dtrain_validation = xgb.DMatrix(
        combined_matrix,
        label=combined_target,
        feature_names=list(prepared.feature_names),
        nthread=config.threads,
    )
    dtest = xgb.DMatrix(
        test.matrix,
        label=test.target,
        feature_names=list(prepared.feature_names),
        nthread=config.threads,
    )
    train.matrix = None
    validation.matrix = None
    combined_matrix = None
    combined_target = None
    gc.collect()
    budget.check("after final matrices")
    booster = xgb.train(
        {**_base_parameters(config), **candidate.parameters},
        dtrain_validation,
        num_boost_round=rounds,
        verbose_eval=False,
        callbacks=[_memory_callback(xgb, budget, "final model")],
    )
    prediction_raw = booster.predict(dtest)
    del dtrain_validation, dtest
    gc.collect()
    budget.check("after final prediction")
    return booster, prediction_raw


def _save_model(booster: Any, path: Path) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.", suffix=path.suffix, dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        booster.save_model(temporary)
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise ModelError("XGBoost produced an empty model artifact")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "path": path.as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _prediction_table(
    split: SplitData,
    prediction_raw: Any,
    candidate_name: str,
    fit_scope: str,
    np: Any,
) -> tuple[pa.Table, dict[str, Any]]:
    metrics, prediction, absolute_error = _metric_summary(
        split.target,
        prediction_raw,
        split.zone_id,
        split.target_hour_us,
        np,
    )
    count = split.eligible_rows
    table = pa.Table.from_arrays(
        [
            pa.array(split.zone_id, type=pa.int32()),
            pa.array(split.target_hour_us, type=pa.timestamp("us", tz="UTC")),
            pa.array([split.name] * count, type=pa.string()),
            pa.array(split.target.astype(np.int64), type=pa.int64()),
            pa.array(np.asarray(prediction_raw, dtype=np.float64), type=pa.float64()),
            pa.array(prediction, type=pa.float64()),
            pa.array(absolute_error, type=pa.float64()),
            pa.array([candidate_name] * count, type=pa.string()),
            pa.array([fit_scope] * count, type=pa.string()),
        ],
        schema=_PREDICTION_SCHEMA,
    )
    return table, metrics


def _write_predictions(
    validation: SplitData,
    validation_raw: Any,
    test: SplitData,
    test_raw: Any,
    candidate_name: str,
    path: Path,
    np: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    validation_table, validation_metrics = _prediction_table(
        validation, validation_raw, candidate_name, "train", np
    )
    test_table, test_metrics = _prediction_table(
        test, test_raw, candidate_name, "train_validation", np
    )
    table = pa.concat_tables([validation_table, test_table])
    if table.schema != _PREDICTION_SCHEMA:
        raise ModelError(f"invalid prediction output schema: {table.schema}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".parquet", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        pq.write_table(table, temporary, compression="zstd")
        if pq.read_schema(temporary) != _PREDICTION_SCHEMA:
            raise ModelError("serialized prediction schema does not match")
        if pq.read_metadata(temporary).num_rows != table.num_rows:
            raise ModelError("serialized prediction row count does not match")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    output = {
        "path": path.as_posix(),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        "rows": table.num_rows,
        "schema": [
            {"name": field.name, "type": str(field.type), "nullable": field.nullable}
            for field in table.schema
        ],
    }
    return output, validation_metrics, test_metrics


def _load_baseline_metrics(path: Path) -> dict[str, dict[str, float]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        splits = data["splits"]
        result = {
            name: {
                metric: float(splits[name][metric])
                for metric in ("mae", "wape")
            }
            for name in ("validation", "test")
        }
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ModelError(f"invalid baseline metrics {path}: {exc}") from exc
    return result


def _comparison(
    model: dict[str, Any], baseline: dict[str, float]
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for metric in ("mae", "wape"):
        baseline_value = baseline[metric]
        model_value = model[metric]
        improvement = baseline_value - model_value
        result[metric] = {
            "baseline": round(baseline_value, 6),
            "model": round(model_value, 6),
            "absolute_improvement": round(improvement, 6),
            "relative_improvement": None
            if baseline_value == 0
            else round(improvement / baseline_value, 6),
        }
    return result


def _train(config: ModelConfig, budget: MemoryBudget) -> dict[str, Any]:
    budget.check("preflight")
    try:
        import numpy as np
        import scipy
        import scipy.sparse as sparse
        import xgboost as xgb
    except ImportError as exc:
        raise ModelError(
            "model dependencies are missing; install the project environment first"
        ) from exc
    budget.check("after model dependency imports")

    feature_config = load_feature_config(config.feature_config_path)
    feature_path = feature_config.output_path
    prepared = _prepare_data(feature_path, budget, np, sparse)
    baseline = _load_baseline_metrics(config.baseline_metrics_path)
    (
        selected,
        selected_rounds,
        validation_raw,
        candidate_reports,
    ) = _train_candidates(config, prepared, budget, np, xgb)
    budget.check("after candidate selection")
    final_booster, test_raw = _fit_final_model(
        config,
        selected,
        selected_rounds,
        prepared,
        budget,
        np,
        sparse,
        xgb,
    )
    model_output = _save_model(final_booster, config.model_path)
    predictions_output, validation_metrics, test_metrics = _write_predictions(
        prepared.splits["validation"],
        validation_raw,
        prepared.splits["test"],
        test_raw,
        selected.name,
        config.predictions_path,
        np,
    )
    budget.check("after artifact serialization")

    version = f"xgboost_{model_output['sha256'][:12]}"
    manifest = {
        "manifest_version": 1,
        "model": {
            "name": "xgboost_hist_cpu",
            "version": version,
            "selected_candidate": selected.name,
            "parameters": selected.parameters,
            "boost_rounds": selected_rounds,
            "fit_scope": "train_validation",
            "negative_prediction_policy": "clip_to_zero_before_evaluation",
            "seed": config.seed,
        },
        "feature_contract": {
            "zone_encoding": "fixed sparse one-hot",
            "zone_ids": list(prepared.zone_ids),
            "numeric_features": list(_NUMERIC_FEATURES),
            "matrix_feature_names": list(prepared.feature_names),
        },
        "input": {
            "feature_path": feature_path.as_posix(),
            "feature_sha256": _sha256(feature_path),
            "model_config_path": config.config_path.as_posix(),
            "model_config_sha256": _sha256(config.config_path),
        },
        "model_artifact": model_output,
    }
    _write_json(config.manifest_path, manifest)
    manifest_output = {
        "path": config.manifest_path.as_posix(),
        "bytes": config.manifest_path.stat().st_size,
        "sha256": _sha256(config.manifest_path),
    }

    return {
        "report_version": 1,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "config": {
            "path": config.config_path.as_posix(),
            "sha256": _sha256(config.config_path),
            "feature_config_path": config.feature_config_path.as_posix(),
            "feature_config_sha256": _sha256(config.feature_config_path),
            "baseline_metrics_path": config.baseline_metrics_path.as_posix(),
            "baseline_metrics_sha256": _sha256(config.baseline_metrics_path),
        },
        "model": manifest["model"],
        "selection": {
            "metric": "validation_mae",
            "tie_break": "validation_wape_then_candidate_name",
            "candidates": candidate_reports,
        },
        "input": {
            "feature_path": feature_path.as_posix(),
            "feature_sha256": _sha256(feature_path),
            "rows": prepared.input_rows,
            "zone_count": len(prepared.zone_ids),
            "splits": {
                name: {
                    "total_rows": split.total_rows,
                    "eligible_rows": split.eligible_rows,
                    "excluded_rows": split.total_rows - split.eligible_rows,
                }
                for name, split in prepared.splits.items()
            },
        },
        "validation": {
            **validation_metrics,
            "fit_scope": "train",
            "baseline_comparison": _comparison(
                validation_metrics, baseline["validation"]
            ),
        },
        "test": {
            **test_metrics,
            "fit_scope": "train_validation",
            "baseline_comparison": _comparison(test_metrics, baseline["test"]),
        },
        "output": {
            "model": model_output,
            "manifest": manifest_output,
            "predictions": predictions_output,
        },
        "resources": {
            "threads": config.threads,
            "minimum_available_memory_bytes": config.minimum_available_memory_bytes,
            "maximum_process_rss_bytes": config.maximum_process_rss_bytes,
        },
        "versions": {
            "numpy": np.__version__,
            "scipy": scipy.__version__,
            "pyarrow": pa.__version__,
            "xgboost": xgb.__version__,
        },
    }


def run_model(config_path: Path) -> dict[str, Any]:
    config = load_model_config(config_path)
    budget = MemoryBudget(
        config.minimum_available_memory_bytes, config.maximum_process_rss_bytes
    )
    started = time.perf_counter()
    rss_start = psutil.Process().memory_info().rss
    report = _train(config, budget)
    budget.check("final report")
    report["resources"].update({
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "rss_start_bytes": rss_start,
        "rss_peak_bytes": budget.peak_rss_bytes,
        "minimum_system_available_bytes": budget.minimum_available_seen_bytes,
    })
    try:
        _write_json(config.metrics_path, report)
    except OSError as exc:
        raise ModelError(f"cannot write model metrics {config.metrics_path}: {exc}") from exc
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/model.json"))
    args = parser.parse_args()
    try:
        report = run_model(args.config)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
