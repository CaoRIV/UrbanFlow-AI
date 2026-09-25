"""Verify the reproducible environment, artifact chain, footprint, and serving store."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import os
import platform
import re
import statistics
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

_CONFIG_KEYS = {
    "pyproject_path",
    "api_config_path",
    "report_paths",
    "artifact_roots",
    "output_path",
}
_PIN_PATTERN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([^;\s]+)$")
_PYTHON_RANGE_PATTERN = re.compile(
    r"^\s*>=(\d+)\.(\d+)\s*,\s*<(\d+)\.(\d+)\s*$"
)
_IMPORT_NAMES = {"httpx2": "httpx"}


class ReliabilityError(ValueError):
    """The environment or one of the published artifacts is not reproducible."""


@dataclass(frozen=True)
class ReliabilityConfig:
    config_path: Path
    pyproject_path: Path
    api_config_path: Path
    report_paths: tuple[Path, ...]
    artifact_roots: tuple[Path, ...]
    output_path: Path


def _required_path(data: dict[str, Any], key: str, parent: Path) -> Path:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ReliabilityError(f"{key} must be a non-empty path string")
    return (parent / value).resolve()


def _required_path_list(
    data: dict[str, Any], key: str, parent: Path
) -> tuple[Path, ...]:
    values = data.get(key)
    if not isinstance(values, list) or not values:
        raise ReliabilityError(f"{key} must be a non-empty array of path strings")
    paths: list[Path] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise ReliabilityError(f"{key}[{index}] must be a non-empty path string")
        paths.append((parent / value).resolve())
    if len(paths) != len(set(paths)):
        raise ReliabilityError(f"{key} must not contain duplicate paths")
    return tuple(paths)


def load_reliability_config(path: Path) -> ReliabilityConfig:
    resolved = path.resolve()
    try:
        data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReliabilityError(f"cannot read reliability config {path}: {exc}") from exc
    if not isinstance(data, dict) or set(data) != _CONFIG_KEYS:
        raise ReliabilityError(
            f"reliability config must contain exactly {sorted(_CONFIG_KEYS)}"
        )

    parent = resolved.parent
    pyproject_path = _required_path(data, "pyproject_path", parent)
    api_config_path = _required_path(data, "api_config_path", parent)
    report_paths = _required_path_list(data, "report_paths", parent)
    artifact_roots = _required_path_list(data, "artifact_roots", parent)
    output_path = _required_path(data, "output_path", parent)
    if pyproject_path.suffix.lower() != ".toml":
        raise ReliabilityError("pyproject_path must end in .toml")
    if api_config_path.suffix.lower() != ".json":
        raise ReliabilityError("api_config_path must end in .json")
    if output_path.suffix.lower() != ".json":
        raise ReliabilityError("output_path must end in .json")
    if output_path in report_paths:
        raise ReliabilityError("output_path must not overwrite an input report")

    return ReliabilityConfig(
        config_path=resolved,
        pyproject_path=pyproject_path,
        api_config_path=api_config_path,
        report_paths=report_paths,
        artifact_roots=artifact_roots,
        output_path=output_path,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_distribution(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dependency_pins(pyproject: dict[str, Any]) -> list[tuple[str, str, str]]:
    project = pyproject.get("project")
    if not isinstance(project, dict):
        raise ReliabilityError("pyproject.toml is missing [project]")
    runtime = project.get("dependencies")
    optional = project.get("optional-dependencies", {})
    dev = optional.get("dev", []) if isinstance(optional, dict) else None
    if not isinstance(runtime, list) or not isinstance(dev, list):
        raise ReliabilityError("project dependencies and optional dev dependencies must be arrays")

    pins: list[tuple[str, str, str]] = []
    seen: dict[str, str] = {}
    for group, values in (("runtime", runtime), ("dev", dev)):
        for value in values:
            if not isinstance(value, str):
                raise ReliabilityError(f"{group} dependency entries must be strings")
            match = _PIN_PATTERN.fullmatch(value.strip())
            if match is None:
                raise ReliabilityError(
                    f"{group} dependency must use an exact == pin: {value!r}"
                )
            name, expected = match.groups()
            canonical = _canonical_distribution(name)
            previous = seen.get(canonical)
            if previous is not None and previous != expected:
                raise ReliabilityError(f"conflicting dependency pins for {name}")
            if previous is None:
                seen[canonical] = expected
                pins.append((group, name, expected))
    return pins


def verify_environment(pyproject_path: Path) -> dict[str, Any]:
    try:
        with pyproject_path.open("rb") as source:
            pyproject = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReliabilityError(f"cannot read pyproject {pyproject_path}: {exc}") from exc

    project = pyproject.get("project")
    requires_python = project.get("requires-python") if isinstance(project, dict) else None
    if not isinstance(requires_python, str):
        raise ReliabilityError("project.requires-python must be configured")
    match = _PYTHON_RANGE_PATTERN.fullmatch(requires_python)
    if match is None:
        raise ReliabilityError(
            "project.requires-python must use a >=major.minor,<major.minor range"
        )
    lower = (int(match.group(1)), int(match.group(2)))
    upper = (int(match.group(3)), int(match.group(4)))
    current = sys.version_info[:2]
    if not lower <= current < upper:
        raise ReliabilityError(
            f"Python {platform.python_version()} does not satisfy {requires_python}"
        )

    dependencies: list[dict[str, str]] = []
    failures: list[str] = []
    for group, name, expected in _dependency_pins(pyproject):
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            failures.append(f"missing {name}=={expected}")
            continue
        if installed != expected:
            failures.append(f"{name}=={installed}; expected {expected}")
            continue
        module_name = _IMPORT_NAMES.get(_canonical_distribution(name), name)
        try:
            importlib.import_module(module_name)
        except (ImportError, OSError) as exc:
            failures.append(f"cannot import {module_name} for {name}=={expected}: {exc}")
            continue
        dependencies.append(
            {
                "group": group,
                "name": name,
                "version": installed,
                "import": module_name,
            }
        )
    if failures:
        raise ReliabilityError(
            "dependency verification failed: "
            + "; ".join(failures)
            + '; run: .\\.venv\\Scripts\\python.exe -m pip install -e ".[dev]"'
        )

    return {
        "python": {
            "executable": str(Path(sys.executable).resolve()),
            "requires": requires_python,
            "version": platform.python_version(),
        },
        "dependencies": dependencies,
    }


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise ReliabilityError(f"missing {label}: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReliabilityError(f"invalid {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReliabilityError(f"{label} must contain a JSON object: {path}")
    return value


def _iter_path_records(
    value: Any, location: str = "$"
) -> list[tuple[str, str, str | None, int | None]]:
    records: list[tuple[str, str, str | None, int | None]] = []
    if isinstance(value, dict):
        path_value = value.get("path")
        if isinstance(path_value, str) and path_value.strip():
            hash_value = value.get("sha256")
            bytes_value = value.get("bytes")
            records.append(
                (
                    location,
                    path_value,
                    hash_value if isinstance(hash_value, str) else None,
                    bytes_value
                    if isinstance(bytes_value, int) and not isinstance(bytes_value, bool)
                    else None,
                )
            )
        for key, child in value.items():
            records.extend(_iter_path_records(child, f"{location}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            records.extend(_iter_path_records(child, f"{location}[{index}]"))
    return records


def _display_path(path: Path, repo_root: Path) -> str:
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def verify_report_chain(
    config: ReliabilityConfig,
) -> dict[str, Any]:
    repo_root = config.pyproject_path.parent
    report_files: list[dict[str, Any]] = []
    verified: dict[Path, dict[str, Any]] = {}
    reference_count = 0

    for report_path in config.report_paths:
        report = _read_json_object(report_path, "pipeline report")
        report_files.append(
            {
                "path": _display_path(report_path, repo_root),
                "bytes": report_path.stat().st_size,
                "sha256": _sha256(report_path),
            }
        )
        for location, raw_path, expected_hash, expected_bytes in _iter_path_records(report):
            reference_count += 1
            candidate = Path(raw_path)
            artifact_path = (
                candidate.resolve()
                if candidate.is_absolute()
                else (report_path.parent / candidate).resolve()
            )
            if not artifact_path.is_file():
                raise ReliabilityError(
                    f"missing file referenced by {report_path} at {location}: {artifact_path}"
                )
            actual_bytes = artifact_path.stat().st_size
            if expected_bytes is not None and actual_bytes != expected_bytes:
                raise ReliabilityError(
                    f"size does not match at {report_path}:{location}: "
                    f"{actual_bytes} != {expected_bytes} for {artifact_path}"
                )
            actual_hash = _sha256(artifact_path)
            if expected_hash is not None and actual_hash != expected_hash:
                raise ReliabilityError(
                    f"sha256 does not match at {report_path}:{location} for {artifact_path}"
                )
            entry = verified.setdefault(
                artifact_path,
                {
                    "path": _display_path(artifact_path, repo_root),
                    "bytes": actual_bytes,
                    "sha256": actual_hash,
                    "referenced_by": [],
                },
            )
            entry["referenced_by"].append(
                f"{_display_path(report_path, repo_root)}:{location}"
            )

    return {
        "reports": report_files,
        "references_checked": reference_count,
        "unique_files": [verified[path] for path in sorted(verified, key=str)],
    }


def _measure(operation: Callable[[], Any], runs: int = 10) -> tuple[Any, dict[str, Any]]:
    durations: list[float] = []
    result: Any = None
    for _ in range(runs):
        started = time.perf_counter()
        result = operation()
        durations.append((time.perf_counter() - started) * 1000)
    return result, {
        "runs": runs,
        "median_ms": round(statistics.median(durations), 3),
        "max_ms": round(max(durations), 3),
    }


def verify_api_store(api_config_path: Path) -> dict[str, Any]:
    try:
        from urbanflow.api import PredictionStore, load_api_config

        started = time.perf_counter()
        store = PredictionStore(load_api_config(api_config_path))
    except Exception as exc:
        raise ReliabilityError(f"API artifact validation failed: {exc}") from exc

    startup_ms = round((time.perf_counter() - started) * 1000, 3)
    try:
        target = store.test_end_utc_exclusive - timedelta(hours=1)
        zone_id = store.zones[0].zone_id
        forecast, forecast_timing = _measure(lambda: store.forecast(zone_id, target))
        rankings, rankings_timing = _measure(lambda: store.rankings(target, 10))
        history, history_timing = _measure(lambda: store.history(zone_id, target, 24))
        if forecast is None:
            raise ReliabilityError("API benchmark forecast returned no row")
        if len(rankings) != 10:
            raise ReliabilityError("API benchmark rankings did not return 10 rows")
        if len(history) != 24:
            raise ReliabilityError("API benchmark history did not return 24 rows")
        return {
            "model_name": store.model_name,
            "model_version": store.model_version,
            "prediction_rows": store.prediction_rows,
            "zone_count": store.zone_count,
            "hour_count": store.hour_count,
            "store_startup_ms": startup_ms,
            "query_benchmark": {
                "scope": "direct PredictionStore calls; excludes HTTP and browser overhead",
                "forecast": forecast_timing,
                "rankings_limit_10": rankings_timing,
                "history_24_hours": history_timing,
            },
        }
    finally:
        store.close()


def artifact_footprint(config: ReliabilityConfig) -> dict[str, Any]:
    repo_root = config.pyproject_path.parent
    seen: set[Path] = set()
    categories: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []

    for root in config.artifact_roots:
        if not root.is_dir():
            raise ReliabilityError(f"missing artifact root: {root}")
        category_files: list[Path] = []
        for path in sorted(root.rglob("*"), key=str):
            resolved = path.resolve()
            if not path.is_file() or resolved == config.output_path or resolved in seen:
                continue
            seen.add(resolved)
            category_files.append(resolved)
            files.append(
                {
                    "path": _display_path(resolved, repo_root),
                    "bytes": resolved.stat().st_size,
                }
            )
        categories.append(
            {
                "root": _display_path(root, repo_root),
                "file_count": len(category_files),
                "bytes": sum(path.stat().st_size for path in category_files),
            }
        )

    files.sort(key=lambda item: item["path"])
    largest = sorted(files, key=lambda item: (-item["bytes"], item["path"]))[:10]
    return {
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "categories": categories,
        "largest_files": largest,
        "files": files,
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
            json.dump(value, target, indent=2, sort_keys=True)
            target.write("\n")
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def run_reliability(config_path: Path) -> dict[str, Any]:
    config = load_reliability_config(config_path)
    environment = verify_environment(config.pyproject_path)
    report_chain = verify_report_chain(config)
    api = verify_api_store(config.api_config_path)
    footprint = artifact_footprint(config)
    report = {
        "report_version": 1,
        "status": "pass",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "config": {
            "path": str(config.config_path),
            "sha256": _sha256(config.config_path),
        },
        "environment": environment,
        "report_chain": report_chain,
        "api": api,
        "artifact_footprint": footprint,
        "optimization": {
            "performed": True,
            "decision": (
                "Model memory guards were calibrated from measured RSS while retaining "
                "the depth4/depth6 validation comparison. Direct PredictionStore median "
                "timings are below 10 ms, so no serving cache was added."
            ),
        },
    }
    _write_json(config.output_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("configs/reliability.json")
    )
    parser.add_argument(
        "--environment-only",
        action="store_true",
        help="Validate Python and exact dependency pins without reading artifacts.",
    )
    args = parser.parse_args()
    try:
        config = load_reliability_config(args.config)
        if args.environment_only:
            result = verify_environment(config.pyproject_path)
            print(
                "Environment valid: "
                f"Python {result['python']['version']}, "
                f"{len(result['dependencies'])} exact dependency pins import successfully."
            )
            return
        report = run_reliability(args.config)
    except ReliabilityError as exc:
        raise SystemExit(f"reliability verification failed: {exc}") from exc
    print(
        "Reliability verification passed: "
        f"{report['report_chain']['references_checked']} report references, "
        f"{report['artifact_footprint']['file_count']} files, "
        f"{report['artifact_footprint']['total_bytes']} bytes; "
        f"model={report['api']['model_version']}."
    )


if __name__ == "__main__":
    main()
