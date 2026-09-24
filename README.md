# UrbanFlow AI — bộ tài liệu khởi động

Mục tiêu: ứng dụng dự báo **số lượt đón khách Yellow Taxi trong giờ kế tiếp theo taxi zone tại NYC**, có API và dashboard để trình bày trong CV. Pipeline historical backtest, model card, FastAPI và dashboard Vue đã triển khai; đây vẫn là demo đánh giá lịch sử, không phải hệ thống real time.

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

Đánh giá seasonal-naive baseline trên các split UTC cố định:

```powershell
python -m urbanflow.evaluate_baseline --config configs/baseline.json
```

Config khóa train tại `[2026-01-01T05:00:00Z, 2026-03-01T05:00:00Z)`,
validation tại `[2026-03-01T05:00:00Z, 2026-03-16T04:00:00Z)` và test tại
`[2026-03-16T04:00:00Z, 2026-04-01T04:00:00Z)`. Dự báo chính là count cùng
zone ở `target_hour_utc - 168h`. Khi lag thiếu, train chỉ dùng mean của chính
zone từ các target sớm hơn; validation/test dùng zone mean fit trên train.

Predictions nằm tại `artifacts/baseline/seasonal-naive-predictions.parquet`;
`artifacts/baseline/metrics.json` lưu MAE/WAPE tổng thể, MAE theo zone và UTC
hour, fallback rate, split boundaries và hash input/output. Target `NULL` không
được chấm điểm; WAPE là `NULL` khi tổng actual bằng zero. Trên Q1/2026,
validation đạt MAE `6.397990`, WAPE `0.308649`; test đạt MAE `4.675301`,
WAPE `0.237388`. Đây là historical backtest, không phải dự báo real time.

Tạo feature leakage-safe trên đúng full grid và split đã khóa:

```powershell
python -m urbanflow.build_features --config configs/features.json
```

Output `data/processed/features/hourly_features.parquet` giữ target và metadata
riêng khỏi các feature: calendar UTC, lag 1/24/168 giờ và rolling mean 24/168 giờ.
Mỗi rolling window kết thúc tại `target_hour_utc - 1h` và chỉ có giá trị khi đủ
toàn bộ lịch sử không NULL; lịch sử thiếu được giữ NULL, không tự đổi thành zero.
`artifacts/features/feature-report.json` lưu schema, hash, số hàng usable theo
split và tài nguyên chạy.

Train model CPU bằng `configs/model.json`. Lệnh có memory guard và sẽ dừng trước
khi train nếu RAM khả dụng dưới 2 GB:

```powershell
python -m urbanflow.train_model --config configs/model.json
```

Với máy 8 GB không đủ RAM khi Orca đang mở, tạo bundle Colab rồi chạy notebook
`notebooks/train_model_colab.ipynb` trên CPU runtime:

```powershell
python -m urbanflow.prepare_colab --config configs/model.json
```

Upload `artifacts/colab/urbanflow-colab-input.zip` trong cell đầu tiên. Notebook
xác minh checksum, tạo Python 3.11 environment, chạy model tests và full suite trước
khi train, sau đó tải về `urbanflow-model-artifacts.zip`.

Kết quả W3-T2 trên cùng split Q1/2026 chọn `depth6` bằng validation MAE:
`4.226789` (WAPE `0.203907`), tốt hơn candidate `depth4` có MAE
`4.367897`. Sau khi khóa cấu hình và fit lại trên train + validation, test đạt
MAE `3.766444`, WAPE `0.191241`; seasonal-naive baseline tương ứng là MAE
`4.675301`, WAPE `0.237388` (cải thiện tương đối `19.4395%`). Model version
`xgboost_bed2c66982d2` dùng seed 42, 2 threads và 305 boost rounds. Artifacts
nằm trong `artifacts/model/`; đây vẫn là historical backtest, không phải forecast
production hoặc bằng chứng chất lượng ngoài Q1/2026.

Phân tích lỗi W3-T3 kiểm tra schema, checksum và khóa/actual giữa model với
baseline trước khi tính lại metric trực tiếp từ predictions:

```powershell
python -m urbanflow.analyze_model --config configs/model_analysis.json
```

Báo cáo machine-readable được ghi vào `artifacts/model/error-analysis.json`;
[model card](docs/model-card.md) ghi protocol chọn candidate, test comparison,
residual, zone/hour yếu, provenance và giới hạn. Trên 100.992 test rows, model
tốt hơn baseline tại 214/263 zones và kém hơn tại 49 zones. Model overpredict
62.948 rows, underpredict 33.784 rows; 4.382 raw predictions âm được clip về 0.
Zone regression lớn nhất là Battery Park City (model MAE `6.492402`, baseline
`6.195312`). Vì model vẫn giảm test MAE từ `4.675301` xuống `3.766444`, artifact
phục vụ được khóa ở `xgboost_bed2c66982d2`. Test chỉ quyết định artifact phục vụ,
không được dùng để đổi feature, candidate hoặc boost rounds.

Khởi động API W4-T1 từ repository root sau khi đã tạo error-analysis report:

```powershell
python -m uvicorn urbanflow.api:app --host 127.0.0.1 --port 8000
```

OpenAPI UI nằm tại `http://127.0.0.1:8000/docs`. Các endpoint:

- `GET /health`: model/version, test window và số prediction rows đã load.
- `GET /zones`: 263 zones thực sự có prediction, sắp theo `zone_id`.
- `GET /forecast?cutoff_utc=2026-03-16T04:00:00Z&zone_id=161`.
- `GET /rankings?cutoff_utc=2026-03-16T04:00:00Z&limit=10`: top zones theo prediction.
- `GET /history?zone_id=161&end_utc=2026-03-17T03:00:00Z&hours=24`: chuỗi prediction/actual và MAE của cửa sổ.

`cutoff_utc` là biên exclusive của dữ liệu đã quan sát và đồng thời là đầu giờ
đích, nên response trên có `target_hour_utc = 2026-03-16T04:00:00Z`. API chỉ
chấp nhận giờ tròn UTC trong test window, trả `404` cho zone không được phục vụ,
và luôn gắn `source = historical_backtest`. Ví dụ response:

```json
{
  "source": "historical_backtest",
  "cutoff_utc": "2026-03-16T04:00:00Z",
  "target_hour_utc": "2026-03-16T04:00:00Z",
  "zone_id": 161,
  "borough": "Manhattan",
  "zone_name": "Midtown Center",
  "service_zone": "Yellow Zone",
  "prediction": 25.38193702697754,
  "actual_trip_count": 18,
  "absolute_error": 7.381937026977539,
  "model_name": "xgboost_hist_cpu",
  "model_version": "xgboost_bed2c66982d2"
}
```

Khi startup, API đối chiếu serving decision, SHA-256 của predictions/zone lookup,
schema, test window, full-grid dimensions và khóa duy nhất trước khi nhận request.
API đọc prediction đã khóa bằng DuckDB; không load XGBoost hoặc train lại model.

### Chạy dashboard Vue W4-T2

Giữ API ở terminal thứ nhất bằng đúng Python trong virtual environment:

```powershell
./.venv/Scripts/python.exe -m uvicorn urbanflow.api:app --host 127.0.0.1 --port 8000
```

Chạy Vite ở terminal thứ hai; dev server proxy `/api` sang FastAPI tại port 8000:

```powershell
cd web
npm install
npm run dev
```

Mở `http://127.0.0.1:5173`. Dashboard khởi tạo tại giờ cuối của test window,
cho phép chọn cutoff UTC và zone, hiển thị top 10 zones, chart 24 giờ forecast/actual,
prediction, actual, absolute error và rolling MAE. Banner `Historical backtest` luôn
hiển thị để tránh diễn giải thành forecast live. Kiểm tra TypeScript và production build:

```powershell
cd web
npm run build
```

Ảnh demo desktop: [`docs/screenshots/w4-t2-dashboard.png`](docs/screenshots/w4-t2-dashboard.png).

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
