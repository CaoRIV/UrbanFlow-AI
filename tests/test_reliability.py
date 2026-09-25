from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from urbanflow.reliability import (
    ReliabilityError,
    load_reliability_config,
    verify_environment,
    verify_report_chain,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config(tmp_path: Path, report_path: Path) -> Path:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\n"
        'requires-python = ">=3.11,<3.12"\n'
        "dependencies = []\n"
        "[project.optional-dependencies]\n"
        "dev = []\n",
        encoding="utf-8",
    )
    api_config = tmp_path / "api.json"
    api_config.write_text("{}", encoding="utf-8")
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir(exist_ok=True)
    config_path = tmp_path / "reliability.json"
    config_path.write_text(
        json.dumps(
            {
                "pyproject_path": "pyproject.toml",
                "api_config_path": "api.json",
                "report_paths": [report_path.name],
                "artifact_roots": ["artifacts"],
                "output_path": "artifacts/result.json",
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_report_chain_rejects_tampered_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "artifacts" / "predictions.parquet"
    artifact.parent.mkdir()
    artifact.write_bytes(b"locked prediction artifact")
    report_path = tmp_path / "metrics.json"
    report_path.write_text(
        json.dumps(
            {
                "output": {
                    "path": str(artifact),
                    "bytes": artifact.stat().st_size,
                    "sha256": _sha256(artifact),
                }
            }
        ),
        encoding="utf-8",
    )
    config = load_reliability_config(_config(tmp_path, report_path))

    result = verify_report_chain(config)
    assert result["references_checked"] == 1
    assert result["unique_files"][0]["path"] == "artifacts/predictions.parquet"

    artifact.write_bytes(b"tampered prediction artifact")
    with pytest.raises(ReliabilityError, match="size does not match"):
        verify_report_chain(config)


def test_environment_reports_missing_exact_dependency(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\n"
        f'requires-python = ">={sys.version_info.major}.{sys.version_info.minor},'
        f'<{sys.version_info.major}.{sys.version_info.minor + 1}"\n'
        'dependencies = ["urbanflow-missing-dependency==1.0.0"]\n'
        "[project.optional-dependencies]\n"
        "dev = []\n",
        encoding="utf-8",
    )

    with pytest.raises(ReliabilityError, match="missing urbanflow-missing-dependency"):
        verify_environment(pyproject)
