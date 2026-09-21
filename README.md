# UrbanFlow AI — bộ tài liệu khởi động

Mục tiêu: ứng dụng dự báo **số lượt đón khách Yellow Taxi trong giờ kế tiếp theo taxi zone tại NYC**, có API và dashboard để trình bày trong CV. Dự án hiện ở giai đoạn khởi tạo môi trường và pipeline; chưa phải ứng dụng đã triển khai.

## Thiết lập môi trường phát triển

Yêu cầu Python 3.11. Trên PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pytest
```

Smoke test tạo một bảng Arrow nhỏ và xác minh DuckDB aggregate đúng kết quả. Bước này không tải hoặc tạo dữ liệu TLC.

## Tải dữ liệu thô

Cấu hình W2-T1 chọn ba tháng Yellow Taxi liên tiếp cùng taxi zone lookup. Từ repository root:

```powershell
python -m urbanflow.download_data --config configs/data_sources.json
```

Raw files và manifest được lưu trong `data/raw/` và không được commit. Downloader xác minh schema, số hàng, kích thước và SHA-256; lần chạy lại dùng file cache nếu checksum còn đúng. Kết quả nguồn và schema thực tế nằm trong [data card](docs/data-card.md).

Kiểm tra chất lượng tháng đã tải mà không nạp toàn bộ cột vào RAM:

```powershell
python -m urbanflow.inspect_data --config configs/eda.json
```

Kết quả machine-readable nằm trong `artifacts/eda/`; bảng số liệu và quyết định lọc được lưu trong [data card](docs/data-card.md).

Aggregate từng tháng thành observed hourly counts theo UTC:

```powershell
python -m urbanflow.aggregate_hourly --config configs/aggregate.json
```

Pipeline chỉ scan pickup timestamp và pickup zone, áp dụng lọc đã ghi trong data card, xử lý DST trước khi đổi UTC, rồi ghi Parquet theo tháng vào `data/processed/hourly_counts_observed/`. Đây chưa phải full grid; zero và source-missing được xử lý trong W2-T2.

Dựng full hourly grid sau bước aggregate:

```powershell
python -m urbanflow.build_hourly_grid --config configs/grid.json
```

Output theo tháng nằm trong `data/processed/hourly_grid/`, báo cáo nằm tại
`artifacts/etl/hourly-grid-report.json`. Config grid tham chiếu config aggregate để
dùng cùng nguồn, timezone và giới hạn tài nguyên. Tập zone lấy từ lookup hợp lệ,
không phụ thuộc zone có chuyến trong train/test. Khoảng thời gian lấy toàn bộ các
tháng liên tiếp trong source config, từ đầu tháng local đầu tiên đến đầu tháng
kế tiếp tháng cuối (exclusive), rồi dựng lưới theo UTC.

Schema giữ `zone_id`, `target_hour_utc`, `trip_count`, `source_month` và thêm
`source_status`: `available`, `source_missing`, hoặc `dst_ambiguous`.
`trip_count` bằng `0` chỉ khi giờ có pickup nguồn nhưng zone không có chuyến hợp lệ;
giờ thiếu nguồn hoặc mơ hồ do DST giữ `NULL`. Giờ nguồn có pickup chỉ ở zone bị
loại vẫn được coi là có độ phủ. Đây là giả định độ phủ theo giờ, không phát hiện
được mất dữ liệu một phần. Không dùng `source_status` của giờ đích làm feature.

Pipeline kiểm tra observed counts khớp với raw/lookup trước khi ghi từng file.
Thiếu file, schema sai, counts cũ hoặc tháng không liên tiếp sẽ báo lỗi. Mỗi file
tháng được ghi atomic; nếu tháng sau thất bại, file tháng trước có thể đã cập nhật.
Chỉ dùng toàn bộ output sau khi lệnh kết thúc thành công và báo cáo được ghi mới.

Kiểm tra pipeline bằng dữ liệu toy (không tải TLC):

```powershell
python -m pytest
```

## Đọc theo thứ tự

1. [PROJECT_SPEC.md](docs/PROJECT_SPEC.md): mục tiêu, phạm vi, định nghĩa dự báo và tiêu chí hoàn thành.
2. [ROADMAP_6_WEEKS.md](docs/ROADMAP_6_WEEKS.md): đầu việc từng tuần, đầu ra và cổng kiểm tra; có lịch rút xuống 4 tuần.
3. [DATA_AND_EVALUATION.md](docs/DATA_AND_EVALUATION.md): nguồn dữ liệu, pipeline, chống rò rỉ dữ liệu và chỉ số.
4. [AGENTS.md](AGENTS.md): hướng dẫn đặt vào gốc repo để Codex/agent đọc.
5. [AI_WORKFLOW.md](docs/AI_WORKFLOW.md): cách dùng Codex, OMP và Orca theo từng phiên làm việc, mẫu prompt và bàn giao.
6. [TASK_BOARD.md](docs/TASK_BOARD.md): danh sách task có thể giao ngay cho agent và mẫu báo cáo.

## Chọn cấu hình ban đầu

- Máy mục tiêu: Core i5, RAM 8GB, SSD 512GB; chạy CPU và chỉ một tác vụ nặng mỗi lần.
- V1: 3 tháng Yellow Taxi liên tiếp đã phát hành, một lần lấy dữ liệu theo tháng rồi tổng hợp thành `zone × hour`; dùng DuckDB hoặc Polars lazy để tránh nạp toàn bộ raw vào RAM.
- Mô hình: seasonal naive (cùng zone, cùng giờ tuần trước) → histogram gradient boosting hoặc XGBoost CPU nhỏ. Không cam kết model mới sẽ thắng baseline.
- API: FastAPI; UI: Vue nếu đã quen, bảng và biểu đồ trước; dữ liệu phục vụ có thể đọc từ Parquet/SQLite. Không cần Docker, PostgreSQL/PostGIS, MLflow để hoàn thành V1.
- Nếu tháng thứ ba chưa có dữ liệu hoặc chất lượng kém, chọn ba tháng liên tiếp khác và ghi lại lựa chọn trong data card.

## Nguồn tham khảo chính

- [NYC TLC Trip Record Data](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page): bản Parquet, taxi zone lookup, hướng dẫn nguồn và các lưu ý chất lượng.
- [NYC TLC Yellow Taxi Data Dictionary](https://www.nyc.gov/assets/tlc/downloads/pdf/data_dictionary_trip_records_yellow.pdf): trường pickup time và `PULocationID`.
- [NYC Open Data Taxi Zones](https://data.cityofnewyork.us/Transportation/NYC-Taxi-Zones/8meu-9t5y): hình học zone cho phần bản đồ tùy chọn.
- [Open-Meteo Historical Weather](https://open-meteo.com/en/docs/historical-weather-api) và [Historical Forecast](https://open-meteo.com/en/docs/historical-forecast-api): chỉ dùng nếu mở rộng weather và phải phân biệt quan trắc lịch sử với dự báo biết trước thời điểm dự đoán.
- [Orca Docs](https://www.onorca.dev/): IDE chạy các agent CLI trong terminal/worktree.

Các lệnh cài đặt cụ thể cho Codex/OMP/Orca thay đổi theo phiên bản; trong bộ tài liệu chỉ mô tả cách phối hợp độc lập phiên bản. Kiểm tra tài liệu chính thức của phiên bản đang cài trước khi cấu hình.
