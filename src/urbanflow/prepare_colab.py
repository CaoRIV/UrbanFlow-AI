"""Package the current source, tests, and training inputs for Google Colab."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from urbanflow.aggregate_hourly import _sha256
from urbanflow.build_features import load_feature_config
from urbanflow.train_model import load_model_config

_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)


class ColabBundleError(ValueError):
    """The Colab bundle cannot be built safely or completely."""


def _project_files(repo_root: Path) -> list[Path]:
    required = [repo_root / "pyproject.toml", repo_root / "README.md"]
    patterns = (
        "configs/*.json",
        "src/urbanflow/*.py",
        "tests/*.py",
        "notebooks/*.ipynb",
    )
    files = required + [
        path
        for pattern in patterns
        for path in sorted(repo_root.glob(pattern))
        if path.is_file()
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        raise ColabBundleError(
            "missing required project files: " + ", ".join(str(path) for path in missing)
        )
    if not any(path.name == "train_model.py" for path in files):
        raise ColabBundleError("training source is missing from the project")
    if not any(path.name == "test_train_model.py" for path in files):
        raise ColabBundleError("training tests are missing from the project")
    return files


def _safe_relative(path: Path, repo_root: Path) -> str:
    try:
        relative = path.resolve().relative_to(repo_root.resolve())
    except ValueError as exc:
        raise ColabBundleError(f"bundle input must stay inside the repository: {path}") from exc
    if any(part in {".git", ".venv", "venv", "data/raw"} for part in relative.parts):
        raise ColabBundleError(f"unsafe bundle input: {relative.as_posix()}")
    return relative.as_posix()


def _write_member(archive: zipfile.ZipFile, source: Path, archive_name: str) -> None:
    info = zipfile.ZipInfo(archive_name, _FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    with source.open("rb") as source_file, archive.open(info, "w") as destination:
        shutil.copyfileobj(source_file, destination, length=1024 * 1024)


def _write_bytes(archive: zipfile.ZipFile, archive_name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(archive_name, _FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, content)


def create_colab_bundle(config_path: Path, output_path: Path) -> dict[str, Any]:
    model_config = load_model_config(config_path)
    feature_config = load_feature_config(model_config.feature_config_path)
    repo_root = model_config.config_path.parent.parent.resolve()
    output = output_path.resolve()
    if output.suffix.lower() != ".zip":
        raise ColabBundleError("output path must end in .zip")
    if not feature_config.output_path.is_file():
        raise ColabBundleError(
            f"missing feature dataset: {feature_config.output_path}; run features first"
        )
    if not model_config.baseline_metrics_path.is_file():
        raise ColabBundleError(
            f"missing baseline metrics: {model_config.baseline_metrics_path}"
        )

    files = _project_files(repo_root)
    files.extend([feature_config.output_path, model_config.baseline_metrics_path])
    unique: dict[str, Path] = {}
    for path in files:
        archive_name = _safe_relative(path, repo_root)
        if archive_name in unique and unique[archive_name] != path:
            raise ColabBundleError(f"duplicate archive member: {archive_name}")
        unique[archive_name] = path

    manifest_files = [
        {
            "path": archive_name,
            "bytes": source.stat().st_size,
            "sha256": _sha256(source),
        }
        for archive_name, source in sorted(unique.items())
    ]
    manifest = {
        "bundle_version": 1,
        "entrypoint": "python -m urbanflow.train_model --config configs/model.json",
        "files": manifest_files,
    }
    manifest_bytes = json.dumps(
        manifest, indent=2, sort_keys=True, ensure_ascii=False
    ).encode("utf-8") + b"\n"

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".zip", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for archive_name, source in sorted(unique.items()):
                _write_member(archive, source, archive_name)
            _write_bytes(archive, "bundle-manifest.json", manifest_bytes)
        with zipfile.ZipFile(temporary) as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ColabBundleError(f"corrupt archive member: {bad_member}")
            names = archive.namelist()
            if len(names) != len(set(names)) or "bundle-manifest.json" not in names:
                raise ColabBundleError("bundle member contract failed")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "path": output.as_posix(),
        "bytes": output.stat().st_size,
        "sha256": _sha256(output),
        "file_count": len(manifest_files),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/model.json"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/colab/urbanflow-colab-input.zip"),
    )
    args = parser.parse_args()
    try:
        report = create_colab_bundle(args.config, args.output)
    except ValueError as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
