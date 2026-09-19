# Data card — NYC TLC Yellow Taxi

## Phạm vi hiện tại

W1-T2 đã tải raw data; W1-T3 đã kiểm tra chất lượng tháng đầu bằng DuckDB. Chưa aggregate `zone × hour`, tạo label, chia train/validation/test hoặc train model.

- Dataset: NYC TLC Yellow Taxi Trip Records
- Tháng: `2026-01`
- Cửa sổ ba tháng dự kiến: `2026-01` đến `2026-03`, chỉ tháng đầu đã tải trong W1-T2
- Ngày tải: `2026-09-17T09:21:32Z`
- Config tái lập: `configs/data_sources.json`
- Raw directory: `data/raw/` — bị Git ignore
- Machine-readable manifest: `data/raw/download_manifest.json` — bị Git ignore

Tháng `2026-01` được chọn làm tháng đầu của cửa sổ `2026-01` đến `2026-03`. W1-T3 xác nhận tháng đầu đủ 744 giờ và phù hợp để tiếp tục; tháng 3 được giữ trong kế hoạch để kiểm tra chuyển đổi DST trước ETL.

## Nguồn và provenance

Trang nguồn chính thức: [NYC TLC Trip Record Data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page).

| File | URL | Kích thước | Số hàng | SHA-256 |
| --- | --- | ---: | ---: | --- |
| `yellow_tripdata_2026-01.parquet` | `https://d37ci6vzurychx.cloudfront.net/trip-data/yellow_tripdata_2026-01.parquet` | 64,165,080 bytes | 3,724,889 | `8b3933fe6f0d7b6d8826613c0dd724edc680ff7c49e2bd4c7635c05102728637` |
| `taxi_zone_lookup.csv` | `https://d37ci6vzurychx.cloudfront.net/misc/taxi_zone_lookup.csv` | 12,331 bytes | 265 | `1a99e105092230f8620f301edcca7f80d3080642ff404d28ed957d3fa222c8ed` |

Số hàng Yellow Taxi lấy từ Parquet metadata. Số hàng lookup lấy bằng PyArrow CSV reader. Đây là số hàng raw, chưa phải số pickup hợp lệ.

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

Parquet lưu `timestamp[us]` không kèm timezone. V1 diễn giải timestamp nguồn là local wall time `America/New_York`, dựa trên ngữ nghĩa tháng công bố của TLC, rồi mới chuyển UTC trong ETL. Tháng 1 không đi qua chuyển đổi DST; quy tắc giờ thiếu/trùng phải được xác minh lại khi tải tháng 3 trước khi aggregate.

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

### Quy tắc lọc cho W2-T1

1. Loại và đếm pickup timestamp null.
2. Chỉ giữ timestamp trong `[2026-01-01T00:00:00, 2026-02-01T00:00:00)` theo local wall time nguồn trước khi chuyển UTC.
3. Loại và đếm pickup zone null.
4. Chỉ giữ zone có trong lookup.
5. Loại và báo riêng lookup zone 264/265 thuộc Unknown, N/A hoặc Outside of NYC.
6. Với dữ liệu hiện tại, giữ 3,718,952 rows và loại 5,937 rows; đây vẫn là trip-level input, chưa phải label.

TLC cho biết trip records do các technology provider cung cấp và không đảm bảo tuyệt đối độ chính xác hoặc đầy đủ. Vì vậy số hàng raw không được diễn giải thành nhu cầu taxi.
