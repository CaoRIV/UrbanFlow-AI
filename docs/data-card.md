# Data card — NYC TLC Yellow Taxi

## Phạm vi hiện tại

W1-T2 tải và xác minh **một tháng raw data**. Chưa aggregate, tạo label, chia train/validation/test hoặc đánh giá chất lượng bản ghi.

- Dataset: NYC TLC Yellow Taxi Trip Records
- Tháng: `2026-01`
- Cửa sổ ba tháng dự kiến: `2026-01` đến `2026-03`, chỉ tháng đầu đã tải trong W1-T2
- Ngày tải: `2026-09-17T09:21:32Z`
- Config tái lập: `configs/data_sources.json`
- Raw directory: `data/raw/` — bị Git ignore
- Machine-readable manifest: `data/raw/download_manifest.json` — bị Git ignore

Tháng `2026-01` được chọn làm tháng đầu của một cửa sổ ba tháng liên tiếp. Chất lượng và sự phù hợp của cửa sổ này sẽ được kiểm tra trong W1-T3 trước khi tải thêm hai tháng.

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

Lần chạy đầu tải vào file tạm, kiểm tra schema và số hàng, tính SHA-256 rồi atomic replace vào `data/raw/`. Manifest chỉ được ghi sau khi cả hai file hợp lệ.

Lần chạy lại xác minh kích thước và SHA-256 của file local. Kết quả W1-T2 cho cả hai file là `cached`; downloader không tải lại file hợp lệ.

## Chưa được kết luận trong W1-T2

Các nội dung sau thuộc W1-T3:

- null của pickup timestamp và pickup zone;
- pickup nằm ngoài tháng công bố;
- timezone thực tế của timestamp và quy tắc chuyển sang UTC;
- zone không nằm trong lookup, unknown hoặc N/A;
- khoảng thời gian bị thiếu;
- RAM và thời gian scan hai cột cần thiết;
- quyết định giữ hoặc loại bản ghi.

TLC cho biết trip records do các technology provider cung cấp và không đảm bảo tuyệt đối độ chính xác hoặc đầy đủ. Vì vậy số hàng raw không được diễn giải thành số pickup hợp lệ hoặc nhu cầu taxi.
