# Data card — NYC TLC Yellow Taxi

## Phạm vi hiện tại

W3-T1 đã tải và làm sạch đủ ba tháng, dựng full grid 263 zone × UTC hour, khóa split,
ghi seasonal-naive baseline và tạo feature lag/rolling leakage-safe. Chưa train model W3.

- Dataset: NYC TLC Yellow Taxi Trip Records
- Tháng: `2026-01` đến `2026-03`
- Cửa sổ đánh giá: hai tháng đầu train; tháng 3 chia validation/test theo ranh giới UTC trong `configs/baseline.json`
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

Output observed này chưa có các cặp `zone × hour` bằng zero; full grid và source-missing contract được ghi ở phần W2-T2 bên dưới.

## Full hourly grid W2-T2 — 2026-09-21

Lệnh: `python -m urbanflow.build_hourly_grid --config configs/grid.json`.
Tập zone gồm 263 service zones trong lookup (loại 264/265), kể cả zone không có
chuyến trong ba tháng. Khoảng UTC là `[2026-01-01T05:00:00Z, 2026-04-01T04:00:00Z)`.

| Tháng | Giờ UTC | Grid rows | Observed rows | Zero rows | Missing rows | Tổng trips |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 2026-01 | 744 | 195,672 | 122,342 | 73,330 | 0 | 3,718,952 |
| 2026-02 | 672 | 176,736 | 111,154 | 65,582 | 0 | 3,394,829 |
| 2026-03 | 743 | 195,409 | 122,108 | 73,301 | 0 | 3,946,183 |
| **Tổng** | **2,159** | **567,817** | **355,604** | **212,213** | **0** | **11,059,964** |

Đây là số liệu từ lần chạy dữ liệu thật, không phải fixture test. Không có khóa
trùng và tổng trips không đổi so với W2-T1. Tháng 3 có 743 giờ UTC do spring DST;
không chèn thêm một giờ zero cho local gap.

Schema: `zone_id: int32`, `target_hour_utc: timestamp[us, tz=UTC]`,
`trip_count: int64` (nullable), `source_month: string`, `source_status: string`.
Giờ không có pickup nguồn giữ `NULL/source_missing`; fall DST không phân biệt
được fold giữ `NULL/dst_ambiguous` ở cả hai UTC hours. Giờ có pickup nguồn nhưng
zone không có chuyến hợp lệ nhận zero. Độ phủ nguồn dùng mọi pickup có timestamp
trong tháng và không mơ hồ, bao gồm zone bị loại. Không thể suy ra nguồn đầy đủ
từng phần chỉ từ việc có pickup; `missing_rows = 0` không loại trừ mất dữ liệu
một phần. Metadata chất lượng này không được dùng làm feature giờ đích.

Pipeline quét raw chỉ hai cột, đối chiếu từng count với observed output trước khi
dựng grid, chạy từng tháng với DuckDB 2 threads / memory limit 1 GB. Lần chạy đầu
ghi nhận 4.332–6.947 giây/tháng, RSS đỉnh lớn nhất 128,954,368 bytes. Các số này
phụ thuộc máy và lần chạy. Báo cáo JSON và Parquet đều nằm ngoài Git.

Chạy lại cùng config cho SHA-256 giống nhau ở cả ba Parquet. Truy vấn độc lập
trên toàn bộ output xác nhận 567,817 khóa duy nhất, 2,159 giờ liên tục cách nhau
đúng 1 giờ UTC và tổng trips không đổi. Bộ test có 18 trường hợp pass, gồm DST
spring/fall, zero/missing, zone không có chuyến, biên tháng và input không hợp lệ.

## Seasonal-naive baseline W2-T3 — 2026-09-22

Lệnh: `python -m urbanflow.evaluate_baseline --config configs/baseline.json`.
Baseline dự báo count cùng zone tại `target_hour_utc - 168h`; split theo UTC,
không shuffle. Ba khoảng half-open đã khóa trong config:

- train: `[2026-01-01T05:00:00Z, 2026-03-01T05:00:00Z)`;
- validation: `[2026-03-01T05:00:00Z, 2026-03-16T04:00:00Z)`;
- test: `[2026-03-16T04:00:00Z, 2026-04-01T04:00:00Z)`.

| Split | Giờ UTC | Rows | Scored | Fallback rows | MAE | WAPE |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Train | 1,416 | 372,408 | 372,145 | 43,921 | 6.230696 | 0.326374 |
| Validation | 359 | 94,417 | 94,417 | 0 | 6.397990 | 0.308649 |
| Test | 384 | 100,992 | 100,992 | 0 | 4.675301 | 0.237388 |

Mỗi split có 263 zone. Actual Q1/2026 không có source-missing rows. Target train
đầu tiên của mỗi zone không có lịch sử nên 263 rows không được chấm; 167 giờ train
tiếp theo dùng mean cùng zone từ các target sớm hơn. Validation/test có đủ lag 168h,
không dùng fallback. Nếu dữ liệu khác có lag thiếu, validation/test chỉ dùng zone
mean fit trên train; actual target và actual validation/test không cập nhật fallback.

`artifacts/baseline/metrics.json` chứa MAE theo zone và UTC hour ngoài bảng tổng
hợp. Predictions Parquet có 567,817 khóa duy nhất, kích thước 1,998,569 bytes và
SHA-256 `1b11400cad16f56e3819c487799bd8511fdd2b80b4ff4f0942a75c6f745c0128`;
chạy lại giữ nguyên hash. Một lần chạy ghi nhận 1.283 giây, RSS đỉnh 450,646,016
bytes với DuckDB 2 threads / memory limit 1 GB. Artifact nằm ngoài Git.

Test baseline kiểm tra leakage bằng cách sửa actual target/tương lai: prediction
của chính target không đổi, còn prediction ở target sau đúng lag thay đổi. WAPE
được để `NULL` khi tổng actual bằng zero; label `NULL` không bị đổi thành zero.
Kết quả test baseline đã được ghi nhận một lần; các quyết định W3 chỉ dùng validation
và phải so sánh model trên đúng split/config này.

## Feature dataset W3-T1 — 2026-09-23

Lệnh: `python -m urbanflow.build_features --config configs/features.json`. Output
`data/processed/features/hourly_features.parquet` có 567,817 rows, giữ nguyên 263
zone × 2,159 target hours và ba split của baseline. Khóa
`(zone_id, target_hour_utc)` duy nhất.

Feature model gồm `zone_id`; calendar UTC (hour, ISO day-of-week, month, weekend);
lag 1/24/168h; rolling mean 24/168h. Target, split và `source_status` được giữ để
train/audit nhưng không thuộc danh sách feature. Mỗi lag/rolling chỉ đọc rows trước
target trong cùng zone. Rolling cần đủ toàn bộ window không NULL; lịch sử thiếu giữ
NULL, không được coi là zero. `features_complete` đánh dấu hàng có đủ cả năm
history feature.

| Split | Rows | Target non-NULL | Feature complete | Training eligible |
| --- | ---: | ---: | ---: | ---: |
| Train | 372,408 | 372,408 | 328,224 | 328,224 |
| Validation | 94,417 | 94,417 | 94,417 | 94,417 |
| Test | 100,992 | 100,992 | 100,992 | 100,992 |

Train thiếu 263 lag-1h, 6,312 lag/rolling-24h và 44,184 lag/rolling-168h do warm-up;
validation/test có đủ lịch sử. Đây là thống kê output feature, không phải metric model;
W3-T1 không thay MAE/WAPE baseline.

Parquet có kích thước 3,936,369 bytes và SHA-256
`b089d90c160c71d2f71c3bc53da1528cd5edcef195598f334c79eaf32f10fe99`; chạy lại
giữ nguyên hash. Một lần chạy ghi nhận 1.903 giây, RSS đỉnh 525,185,024 bytes với
DuckDB 2 threads / memory limit 1 GB. Report tại
`artifacts/features/feature-report.json`; cả report và feature Parquet nằm ngoài Git.
