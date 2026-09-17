import duckdb
import pyarrow as pa


def test_duckdb_aggregates_arrow_hourly_counts() -> None:
    hourly_counts = pa.table(
        {
            "zone_id": pa.array([1, 1, 2], type=pa.int16()),
            "trip_count": pa.array([2, 3, 4], type=pa.int32()),
        }
    )

    with duckdb.connect() as connection:
        connection.register("hourly_counts", hourly_counts)
        totals = connection.execute(
            """
            SELECT zone_id, sum(trip_count) AS trip_count
            FROM hourly_counts
            GROUP BY zone_id
            ORDER BY zone_id
            """
        ).fetchall()

    assert totals == [(1, 5), (2, 4)]
