# Data card — NYC TLC Yellow Taxi

## Phạm vi hiện tại

W2-T1 đã tải đủ ba tháng, áp dụng quy tắc chất lượng và aggregate pickup hợp lệ thành `zone_id × target_hour_utc`. Output hiện chỉ chứa các nhóm đã quan sát; chưa dựng full grid, chia train/validation/test hoặc train model.

- Dataset: NYC TLC Yellow Taxi Trip Records
- Tháng: `2026-01` đến `2026-03`
- Cửa sổ đánh giá dự kiến: hai tháng đầu cho train; tháng 3 sẽ được chia validation/test trong W2-T3
- Ngày tải: tháng 1 — `2026-09-17`; tháng 2–3 — `2026-09-19`
- Config tái lập: `configs/data_sources.json`
- Raw directory: `data/raw/` — bị Git ignore
- Machine-readable manifest: `data/raw/download_manifest.json` — bị Git ignore

Ba tháng `2026-01` đến `2026-03` đã được xác minh và aggregate từng tháng. Tháng 3 bao gồm DST spring-forward của `America/New_York`; pipeline nhận diện giờ local không tồn tại `2026-03-08T02:00:00–03:00:00` và xác nhận raw data không chứa record trong khoảng đó.

## Nguồn và provenance

Trang nguồn chính thức: [NYC TLC Trip Record Data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page).

| File | URL | Kích thước | Số hàng | SHA-256 |
| --- | --- | ---: | ---: | --- |
| `yellow_tripdata_2026-01.parquet` | `https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2026-01.parquet` | 64,165,080 bytes | 3,724,889 | `8b3933fe6f0d7b6d8826613c0dd724edc680ff7c49e2bd4c7635c05102728637` |
| `yellow_tripdata_2026-02.parquet` | `https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2026-02.parquet` | 58,683,353 bytes | 3,399,866 | `5352ca800f29c40a221ed5b0c6392c451fde7fcd236a73651bd5aea74713a26c` |
| `yellow_tripdata_2026-03.parquet` | `https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2026-03.parquet` | 67,891,249 bytes | 3,952,451 | `d7af794a7ac06cbbb5afb4d1df9e86d84f2e878584de708ff2ffca4bd7d234d9` |
| `taxi_zone_lookup.csv` | `https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv` | 12,331 bytes | 265 | `1a99e105092230f8620f301edcca7f80d3080642ff404d28ed957d3fa222c8ed` |

Số hàng của từng file Yellow Taxi lấy từ Parquet metadata. Số hàng lookup lấy bằng PyArrow CSV reader. Đây là số hàng raw, chưa phải số pickup hợp lệ.

## Schema Yellow Taxi thực tế

| Cột | Arrow type | Nullable |
| --- | --- | --- |
| `VendorID` | `int32` | có |
| `tpep_pickup_datetime` | `timestamp[us]` | có |
| `tpep_dropoff_datetime` | `timestamp[us]` | có |
| `passenger_count` | `int64` | có |
| `trip_distance` | `double` | có |
| `RatecodeID` | `int64` | có |
| `store_and_fwd_flag` | `large_string` | có |
| `PULocationID` | `int32` | có |
| `DOLocationID` | `int32` | có |
| `payment_type` | `int64` | có |
| `fare_amount` | `double` | có |
| `extra` | `double` | có |
| `mta_tax` | `double` | có |
| `tip_amount` | `double` | có |
| `tolls_amount` | `double` | có |
| `improvement_surcharge` | `double` | có |
| `total_amount` | `double` | có |
| `congestion_surcharge` | `double` | có |
| `Airport_fee` | `double` | có |
| `cbd_congestion_fee` | `double` | có |

Hai cột cần cho V1 có mặt:

- `tpep_pickup_datetime`: `timestamp[us]`, không có timezone trong schema;
- `PULocationID`: `int32`.

Pipeline sau này chỉ scan các cột cần thiết thay vì nạp toàn bộ schema trên vào RAM.

## Schema taxi zone lookup thực tế

| Cột | Arrow type | Nullable |
| --- | --- | --- |
| `LocationID` | `int64` | có |
| `Borough` | `string` | có |
| `Zone` | `string` | có |
| `service_zone` | `string` | có |

## Tái lập

Từ repository root với môi trường W1-T1 đã cài:

```powershell
python -m urbanflow.download_data --config configs/data_sources.json
```

Chạy kiểm tra chất lượng W1-T3:

```powershell
python -m urbanflow.inspect_data --config configs/eda.json
```

Báo cáo JSON được ghi vào `artifacts/eda/2026-01-quality.json` và bị Git ignore.

Aggregate ba tháng theo pickup zone và UTC hour:

```powershell
python -m urbanflow.aggregate_hourly --config configs/aggregate.json
```

Processed Parquet nằm trong `data/processed/hourly_counts_observed/`; report nằm tại `artifacts/etl/hourly-aggregate-report.json`. Cả hai đều bị Git ignore.

Lần chạy đầu tải vào file tạm, kiểm tra schema và số hàng, tính SHA-256 rồi atomic replace vào `data/raw/`. Manifest chỉ được ghi sau khi cả hai file hợp lệ.

Lần chạy lại xác minh kích thước và SHA-256 của file local. Kết quả W1-T2 cho cả hai file là `cached`; downloader không tải lại file hợp lệ.

## Kết quả chất lượng W1-T3

Chạy ngày `2026-09-19` với DuckDB chỉ scan `tpep_pickup_datetime` và `PULocationID`.

### Timestamp và phạm vi tháng

| Chỉ số | Giá trị |
| --- | ---: |
| Tổng raw rows | 3,724,889 |
| Pickup timestamp null | 0 |
| Pickup zone null | 0 |
| Timestamp nhỏ nhất | `2025-12-31T23:57:29` |
| Timestamp lớn nhất | `2026-02-01T00:45:01` |
| Rows trước `2026-01-01T00:00:00` | 6 |
| Rows từ `2026-02-01T00:00:00` trở đi | 1 |
| Rows nằm đúng tháng | 3,724,882 |
| Pickup zone ID phân biệt trong raw | 262 |

### Đối chiếu taxi zone lookup

| Nhóm | Rows |
| --- | ---: |
| Zone khớp lookup trong tháng | 3,724,882 |
| Zone không có trong lookup | 0 |
| Zone 264 — Unknown / N/A | 4,391 |
| Zone 265 — N/A / Outside of NYC | 1,539 |
| Rows giữ lại sau quy tắc lọc | 3,718,952 |
| Zone hợp lệ giữ lại | 260 |

Hai zone 264 và 265 có trong lookup nhưng không đại diện một pickup taxi zone phục vụ cụ thể, nên được ghi riêng và loại khỏi V1.

### Độ phủ thời gian

| Chỉ số | Giá trị |
| --- | ---: |
| Giờ kỳ vọng trong tháng | 744 |
| Giờ có ít nhất một raw pickup | 744 |
| Giờ thiếu hoàn toàn | 0 |
| Raw rows/giờ nhỏ nhất | 178 |
| Raw rows/giờ lớn nhất | 11,739 |
| Raw rows/giờ trung bình | 5,006.562 |
| Ngày quan sát được | 31 |
| Raw rows/ngày nhỏ nhất | 44,858 |
| Raw rows/ngày lớn nhất | 153,101 |

Không có giờ thiếu hoàn toàn ở cấp nguồn trong tháng này. Kết luận này chưa biến các cặp `zone × hour` không có pickup thành zero; full grid thuộc W2-T2.

### Timezone

Parquet nguồn lưu `timestamp[us]` không kèm timezone. W2-T1 diễn giải timestamp là local wall time `America/New_York`, nhận diện giờ local mơ hồ/không tồn tại trước khi chuyển UTC, rồi mới floor theo UTC hour. Tháng 3 có một khoảng không tồn tại `2026-03-08T02:00:00–03:00:00`; raw data có 0 rows trong khoảng này.

### Tài nguyên lần chạy này

| Chỉ số | Giá trị |
| --- | ---: |
| Thời gian | 6.033 giây |
| RSS đầu tiến trình | 49,483,776 bytes |
| RSS đỉnh | 80,273,408 bytes |
| RSS tăng tại đỉnh | 30,789,632 bytes |
| DuckDB threads | 2 |
| DuckDB memory limit | 1 GB |

Các số tài nguyên là quan sát của lần chạy này trên máy phát triển, không phải cam kết cho máy khác.

### Quy tắc lọc đã áp dụng trong W2-T1

1. Loại và đếm pickup timestamp null.
2. Chỉ giữ timestamp nằm trong tháng local tương ứng trước khi chuyển UTC.
3. Loại và đếm pickup zone null.
4. Chỉ giữ zone có trong lookup.
5. Loại và báo riêng lookup zone 264/265 thuộc Unknown, N/A hoặc Outside of NYC.
6. Loại và đếm timestamp nằm trong local hour mơ hồ hoặc không tồn tại do DST.

TLC cho biết trip records do các technology provider cung cấp và không đảm bảo tuyệt đối độ chính xác hoặc đầy đủ. Vì vậy số hàng raw không được diễn giải thành nhu cầu taxi.

## Kết quả aggregate W2-T1

### Lọc và số trip giữ lại

| Tháng | Raw rows | Ngoài tháng | Zone 264/265 | DST bị loại | Rows giữ lại |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-01 | 3,724,889 | 7 | 5,930 | 0 | 3,718,952 |
| 2026-02 | 3,399,866 | 16 | 5,021 | 0 | 3,394,829 |
| 2026-03 | 3,952,451 | 19 | 6,249 | 0 | 3,946,183 |
| **Tổng** | **11,077,206** | **42** | **17,200** | **0** | **11,059,964** |

Không tháng nào có pickup timestamp null, pickup zone null hoặc zone ngoài lookup. Các lý do lọc trong bảng là rời nhau theo thứ tự quyết định của pipeline.

### Output observed hourly counts

| Tháng | Rows output | Tổng `trip_count` | Zone | UTC đầu | UTC cuối | File bytes |
| --- | ---: | ---: | ---: | --- | --- | ---: |
| 2026-01 | 122,342 | 3,718,952 | 260 | `2026-01-01 05:00+00` | `2026-02-01 04:00+00` | 169,654 |
| 2026-02 | 111,154 | 3,394,829 | 257 | `2026-02-01 05:00+00` | `2026-03-01 04:00+00` | 156,055 |
| 2026-03 | 122,108 | 3,946,183 | 259 | `2026-03-01 05:00+00` | `2026-04-01 03:00+00` | 171,390 |
| **Tổng** | **355,604** | **11,059,964** | — | — | — | **497,099** |

Mỗi file có schema `zone_id: int32`, `target_hour_utc: timestamp[us, tz=UTC]`, `trip_count: int64`, `source_month: string`. Khóa `(zone_id, target_hour_utc)` duy nhất trong từng file; tổng `trip_count` bằng đúng số trip giữ lại.

Output này chưa có các cặp `zone × hour` bằng zero. W2-T2 sẽ dựng full grid và tách zero thật khỏi source-missing interval.
