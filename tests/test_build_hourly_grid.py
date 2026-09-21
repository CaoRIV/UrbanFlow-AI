from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from urbanflow.aggregate_hourly import run_aggregation


def fixture_config(root: Path, months: list[str], pickups: list[list[tuple]]) -> Path:
    raw = root / "raw"
    raw.mkdir()
    (raw / "taxi_zone_lookup.csv").write_text(
        "LocationID,Borough,Zone,service_zone\n"
        "1,EWR,Airport,EWR\n2,Queens,Park,Boro Zone\n"
        "264,Unknown,N/A,N/A\n265,N/A,Outside of NYC,N/A\n",
        encoding="utf-8",
    )
    for month, rows in zip(months, pickups):
        pq.write_table(pa.table({
            "tpep_pickup_datetime": pa.array([r[0] for r in rows], pa.timestamp("us")),
            "PULocationID": pa.array([r[1] for r in rows], pa.int32()),
        }), raw / f"yellow_tripdata_{month}.parquet")
    (root / "sources.json").write_text(json.dumps({
        "months": [{"month": m, "url": f"https://example.org/yellow_tripdata_{m}.parquet"} for m in months],
        "zone_lookup_url": "https://example.org/taxi_zone_lookup.csv",
        "raw_dir": "raw", "manifest_path": "raw/manifest.json",
    }), encoding="utf-8")
    (root / "aggregate.json").write_text(json.dumps({
        "source_config": "sources.json", "output_dir": "observed",
        "report_path": "aggregate-report.json", "timezone": "America/New_York",
        "duckdb_threads": 1, "duckdb_memory_limit": "256MB",
    }), encoding="utf-8")
    run_aggregation(root / "aggregate.json")
    path = root / "grid.json"
    path.write_text(json.dumps({
        "aggregate_config": "aggregate.json", "output_dir": "grid",
        "report_path": "grid-report.json",
    }), encoding="utf-8")
    return path


def run_grid(path):
    from urbanflow import build_hourly_grid
    return build_hourly_grid.run_grid(path)


def read_rows(report):
    return [r for month in report["months"] for r in pq.read_table(month["output"]["path"]).to_pylist()]


def test_grid_distinguishes_zero_missing_and_unused_lookup_zone(tmp_path):
    config = fixture_config(tmp_path, ["2026-01"], [[
        (datetime(2026, 1, 1, 0, 10), 1),
        (datetime(2026, 1, 1, 0, 20), 1),
        (datetime(2026, 1, 1, 1, 10), 264),
    ]])
    report = run_grid(config)
    rows = read_rows(report)
    by_key = {(r["zone_id"], r["target_hour_utc"]): r for r in rows}
    assert len(rows) == len(by_key) == 1488
    assert {r["zone_id"] for r in rows} == {1, 2}
    assert by_key[1, datetime(2026, 1, 1, 5, tzinfo=UTC)]["trip_count"] == 2
    assert by_key[2, datetime(2026, 1, 1, 5, tzinfo=UTC)]["trip_count"] == 0
    assert by_key[1, datetime(2026, 1, 1, 6, tzinfo=UTC)]["trip_count"] == 0
    missing = by_key[1, datetime(2026, 1, 1, 7, tzinfo=UTC)]
    assert missing["trip_count"] is None
    assert missing["source_status"] == "source_missing"
    assert report["summary"]["trip_count"] == 2
    assert report["summary"]["zero_rows"] == 3
    assert report["summary"]["missing_rows"] == 1484
    again = run_grid(config)
    assert again["months"][0]["output"]["sha256"] == report["months"][0]["output"]["sha256"]


@pytest.mark.parametrize("month,pickups,hours,missing_utc", [
    ("2026-03", [(datetime(2026, 3, 8, 1, 10), 1), (datetime(2026, 3, 8, 2, 10), 1), (datetime(2026, 3, 8, 3, 10), 1)], 743, []),
    ("2026-11", [(datetime(2026, 11, 1, 0, 10), 1), (datetime(2026, 11, 1, 1, 10), 1), (datetime(2026, 11, 1, 2, 10), 1)], 721, [datetime(2026, 11, 1, 5, tzinfo=UTC), datetime(2026, 11, 1, 6, tzinfo=UTC)]),
])
def test_dst_grid_has_continuous_utc_and_marks_both_fall_folds(tmp_path, month, pickups, hours, missing_utc):
    report = run_grid(fixture_config(tmp_path, [month], [pickups]))
    rows = read_rows(report)
    zone = [r for r in rows if r["zone_id"] == 1]
    assert len(zone) == hours
    assert all(b["target_hour_utc"] - a["target_hour_utc"] == timedelta(hours=1) for a, b in zip(zone, zone[1:]))
    assert [r["target_hour_utc"] for r in zone if r["source_status"] == "dst_ambiguous"] == missing_utc
    assert all(r["trip_count"] is None for r in zone if r["source_status"] == "dst_ambiguous")
    assert report["summary"]["trip_count"] == 2


def test_month_boundary_does_not_duplicate_or_drop_hour(tmp_path):
    report = run_grid(fixture_config(tmp_path, ["2026-02", "2026-03"], [
        [(datetime(2026, 2, 28, 23, 59), 1)], [(datetime(2026, 3, 1, 0, 0), 2)],
    ]))
    rows = read_rows(report)
    assert len(rows) == 2830
    assert len({(r["zone_id"], r["target_hour_utc"]) for r in rows}) == 2830
    observed = [r for r in rows if r["trip_count"] == 1]
    assert [r["target_hour_utc"] for r in observed] == [datetime(2026, 3, 1, 4, tzinfo=UTC), datetime(2026, 3, 1, 5, tzinfo=UTC)]


@pytest.mark.parametrize("damage", ["duplicate", "negative", "wrong_zone", "wrong_count", "wrong_hour", "missing_file", "duplicate_lookup"])
def test_rejects_invalid_or_stale_inputs_before_publishing(tmp_path, damage):
    config = fixture_config(tmp_path, ["2026-01"], [[(datetime(2026, 1, 1), 1)]])
    path = tmp_path / "observed/hourly_counts_observed_2026-01.parquet"
    table = pq.read_table(path)
    rows = table.to_pylist()
    if damage == "missing_file":
        path.unlink()
    elif damage == "duplicate_lookup":
        with (tmp_path / "raw/taxi_zone_lookup.csv").open("a") as handle:
            handle.write("1,EWR,Airport,EWR\n")
    else:
        if damage == "duplicate":
            rows *= 2
        elif damage == "negative":
            rows[0]["trip_count"] = -1
        elif damage == "wrong_zone":
            rows[0]["zone_id"] = 999
        elif damage == "wrong_count":
            rows[0]["trip_count"] = 2
        elif damage == "wrong_hour":
            rows[0]["target_hour_utc"] += timedelta(minutes=30)
        pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    with pytest.raises(ValueError):
        run_grid(config)
    assert not (tmp_path / "grid-report.json").exists()


def test_rejects_gap_between_configured_months(tmp_path):
    config = fixture_config(tmp_path, ["2026-01", "2026-03"], [
        [(datetime(2026, 1, 1), 1)], [(datetime(2026, 3, 1), 1)],
    ])
    with pytest.raises(ValueError, match="consecutive"):
        run_grid(config)
