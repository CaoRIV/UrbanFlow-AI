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
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

import pyarrow as pa
import pyarrow.csv as pa_csv
import pyarrow.parquet as pq

_CHUNK_SIZE = 1024 * 1024
_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_USER_AGENT = "UrbanFlow-AI/0.1"


class ConfigError(ValueError):
    """Raised when the download configuration is invalid."""


class DataContractError(ValueError):
    """Raised when a downloaded file violates its expected schema."""


class DownloadError(RuntimeError):
    """Raised when a source cannot be downloaded safely."""


@dataclass(frozen=True)
class DownloadConfig:
    month: str
    yellow_taxi_url: str
    zone_lookup_url: str
    raw_dir: Path
    manifest_path: Path
    timeout_seconds: int


@dataclass(frozen=True)
class ResourceSpec:
    key: str
    url: str
    destination: Path
    format: str
    required_columns: frozenset[str]


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value.strip()


def load_config(config_path: Path) -> DownloadConfig:
    resolved_path = config_path.resolve()
    try:
        data = json.loads(resolved_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON config: {config_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("config root must be a JSON object")

    month = _required_string(data, "month")
    if not _MONTH_PATTERN.fullmatch(month):
        raise ConfigError("month must use YYYY-MM with a valid month number")

    timeout_seconds = data.get("timeout_seconds", 60)
    if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
        raise ConfigError("timeout_seconds must be a positive integer")

    yellow_taxi_url = _required_string(data, "yellow_taxi_url")
    expected_trip_filename = f"yellow_tripdata_{month}.parquet"
    trip_filename = Path(unquote(urlparse(yellow_taxi_url).path)).name
    if trip_filename != expected_trip_filename:
        raise ConfigError(
            "yellow_taxi_url filename must match configured month: "
            f"expected {expected_trip_filename}, got {trip_filename or '<empty>'}"
        )

    zone_lookup_url = _required_string(data, "zone_lookup_url")
    zone_filename = Path(unquote(urlparse(zone_lookup_url).path)).name
    if zone_filename != "taxi_zone_lookup.csv":
        raise ConfigError(
            "zone_lookup_url must point to a file named taxi_zone_lookup.csv"
        )

    base_dir = resolved_path.parent
    raw_dir = (base_dir / _required_string(data, "raw_dir")).resolve()
    manifest_path = (
        base_dir / _required_string(data, "manifest_path")
    ).resolve()

    return DownloadConfig(
        month=month,
        yellow_taxi_url=yellow_taxi_url,
        zone_lookup_url=zone_lookup_url,
        raw_dir=raw_dir,
        manifest_path=manifest_path,
        timeout_seconds=timeout_seconds,
    )


def _resource_specs(config: DownloadConfig) -> tuple[ResourceSpec, ResourceSpec]:
    return (
        ResourceSpec(
            key="yellow_taxi",
            url=config.yellow_taxi_url,
            destination=config.raw_dir
            / f"yellow_tripdata_{config.month}.parquet",
            format="parquet",
            required_columns=frozenset({"tpep_pickup_datetime", "PULocationID"}),
        ),
        ResourceSpec(
            key="taxi_zone_lookup",
            url=config.zone_lookup_url,
            destination=config.raw_dir / "taxi_zone_lookup.csv",
            format="csv",
            required_columns=frozenset(
                {"LocationID", "Borough", "Zone", "service_zone"}
            ),
        ),
    )


def _schema_metadata(schema: pa.Schema) -> list[dict[str, Any]]:
    return [
        {
            "name": field.name,
            "type": str(field.type),
            "nullable": field.nullable,
        }
        for field in schema
    ]


def _inspect_file(spec: ResourceSpec, path: Path) -> dict[str, Any]:
    try:
        if spec.format == "parquet":
            parquet_file = pq.ParquetFile(path)
            try:
                schema = parquet_file.schema_arrow
                rows = parquet_file.metadata.num_rows
            finally:
                parquet_file.close()
        elif spec.format == "csv":
            table = pa_csv.read_csv(path)
            schema = table.schema
            rows = table.num_rows
        else:
            raise DataContractError(f"unsupported format: {spec.format}")
    except (OSError, pa.ArrowException) as exc:
        raise DataContractError(f"cannot read {spec.key} as {spec.format}: {exc}") from exc

    missing_columns = sorted(spec.required_columns.difference(schema.names))
    if missing_columns:
        raise DataContractError(
            f"{spec.key} is missing required columns: {', '.join(missing_columns)}"
        )
    if rows <= 0:
        raise DataContractError(f"{spec.key} contains no rows")

    return {"rows": rows, "schema": _schema_metadata(schema)}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        while chunk := file_handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def _cached_entry(
    spec: ResourceSpec, manifest: dict[str, Any] | None
) -> dict[str, Any] | None:
    if manifest is None or manifest.get("manifest_version") != 1:
        return None

    files = manifest.get("files")
    if not isinstance(files, dict):
        return None
    entry = files.get(spec.key)
    if not isinstance(entry, dict) or entry.get("url") != spec.url:
        return None
    if not spec.destination.is_file():
        return None

    expected_bytes = entry.get("bytes")
    expected_sha256 = entry.get("sha256")
    if not isinstance(expected_bytes, int) or not isinstance(expected_sha256, str):
        return None
    if spec.destination.stat().st_size != expected_bytes:
        return None
    if _sha256(spec.destination) != expected_sha256:
        return None
    return entry


def _download_resource(
    spec: ResourceSpec, timeout_seconds: int
) -> dict[str, Any]:
    spec.destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{spec.destination.name}.",
            suffix=".part",
            dir=spec.destination.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            request = Request(spec.url, headers={"User-Agent": _USER_AGENT})
            with urlopen(request, timeout=timeout_seconds) as response:
                while chunk := response.read(_CHUNK_SIZE):
                    temporary_file.write(chunk)

        inspection = _inspect_file(spec, temporary_path)
        file_size = temporary_path.stat().st_size
        checksum = _sha256(temporary_path)
        os.replace(temporary_path, spec.destination)
        temporary_path = None
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise DownloadError(f"failed to download {spec.url}: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return {
        "url": spec.url,
        "path": spec.destination.as_posix(),
        "downloaded_at_utc": _utc_now(),
        "bytes": file_size,
        "sha256": checksum,
        **inspection,
    }


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as temporary_file:
        json.dump(manifest, temporary_file, indent=2, sort_keys=True)
        temporary_file.write("\n")
        temporary_path = Path(temporary_file.name)
    os.replace(temporary_path, path)


def run_download(config_path: Path) -> dict[str, Any]:
    config = load_config(config_path)
    previous_manifest = _load_manifest(config.manifest_path)
    manifest_files: dict[str, dict[str, Any]] = {}
    statuses: dict[str, str] = {}
    changed = False

    for spec in _resource_specs(config):
        entry = _cached_entry(spec, previous_manifest)
        if entry is None:
            entry = _download_resource(spec, config.timeout_seconds)
            statuses[spec.key] = "downloaded"
            changed = True
        else:
            statuses[spec.key] = "cached"
        manifest_files[spec.key] = entry

    manifest = {
        "manifest_version": 1,
        "month": config.month,
        "recorded_at_utc": _utc_now(),
        "files": manifest_files,
    }
    if changed or previous_manifest is None:
        _write_manifest(config.manifest_path, manifest)

    return {
        "month": config.month,
        "manifest_path": config.manifest_path.as_posix(),
        "files": {
            key: {"status": statuses[key], **entry}
            for key, entry in manifest_files.items()
        },
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and validate one month of NYC TLC Yellow Taxi data."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("configs/data_sources.json"),
        help="JSON config path (default: configs/data_sources.json)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    try:
        result = run_download(args.config)
    except (ConfigError, DataContractError, DownloadError) as exc:
        raise SystemExit(f"error: {exc}") from exc
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
