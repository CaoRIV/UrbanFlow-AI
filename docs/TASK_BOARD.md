# Task board — UrbanFlow AI

Quy ước: `[ ]` chưa làm, `[x]` xong sau khi đã kiểm tra. Giao một mã task mỗi lần. Kết quả thực nằm trong nhật ký bên dưới.

## Tuần 1

- [x] **W1-T1** — Khởi tạo repo, môi trường Python, `.gitignore`, README chạy thử. Xong khi tạo venv, import dependency, test smoke chạy; raw/artifact bị ignore.
- [x] **W1-T2** — Viết `docs/data-card.md` và script tải đúng **một** tháng Yellow Taxi + zone lookup từ TLC, lưu URL/size/hash hoặc dấu vết tương đương. Xong khi tải lại được và ghi số hàng/schema.
- [x] **W1-T3** — EDA: null pickup/zone, range ngày, bản ghi ngoài tháng, các zone lạ và thống kê RAM/thời gian. Xong khi có bảng chất lượng và quyết định lọc.

## Tuần 2

- [x] **W2-T1** — Tải thêm tháng thứ hai, ba; scan chỉ cột cần, aggregate theo UTC hour và zone. Xong khi pipeline tái chạy được và không đọc mọi cột vào RAM.
- [x] **W2-T2** — Dựng full hourly grid, phân biệt zero thật với khoảng nguồn thiếu và xử lý DST; test toy. Xong khi khóa chính `zone_id,target_hour_utc` duy nhất.
- [x] **W2-T3** — Split time và seasonal naive; ghi MAE/WAPE, số hàng/số zone mỗi split. Xong khi có baseline lưu file và config split.

## Tuần 3

- [x] **W3-T1** — Tạo calendar + lag 1/24/168h, rolling shift theo zone; test không rò rỉ tương lai.
- [x] **W3-T2** — Train model CPU nhỏ, dùng validation để chọn một cấu hình; khóa test, lưu model/config/metrics.
- [x] **W3-T3** — Model card: bảng baseline/model test, lỗi theo zone/giờ, giới hạn; chọn model phục vụ một cách trung thực.

## Tuần 4

- [ ] **W4-T1** — FastAPI `GET /health`, `GET /zones`, `GET /forecast?cutoff_utc=...&zone_id=...`; response có `target_hour_utc`, `prediction`, `model_version`, nguồn backtest. Test input lỗi.
- [ ] **W4-T2** — Vue trang demo: chọn cutoff test, top zones, line chart prediction/actual, MAE và chú thích historical backtest.
- [ ] **W4-T3** — README end-to-end, script chạy API/UI, screenshot và walkthrough demo.

## Tuần 5–6 (nếu có)

- [ ] **W5-T1** — Chạy lại pipeline từ đầu và kiểm tra dependency, error path, kích thước artifact; tối ưu chỗ chậm có đo đạc.
- [ ] **W5-T2** — Viết hướng dẫn tái lập và CV bullets có số liệu **thực** từ test.
- [ ] **W6-T1** — Chọn đúng một extension: weather forecast có timestamp khả dụng, map zone, residual anomaly, hoặc CI; viết tiêu chí trước khi làm.
- [ ] **W6-T2** — So sánh extension cùng split/baseline; cập nhật README, model card, demo.

## Mẫu ghi sau mỗi task

```text
Mã: W?-T?
Ngày, nhánh/commit:
Thay đổi:
Lệnh đã chạy / kết quả:
Số liệu thật (nếu có):
Vấn đề còn lại / quyết định:
```

## Nhật ký thực hiện

### W1-T1 — 2026-09-17

- Trạng thái: hoàn thành trên nhánh `develop`, commit `5540d82`.
- Thay đổi: cấu hình Python 3.11, dependency cố định, package `urbanflow`, quy tắc ignore, smoke test DuckDB/Arrow và hướng dẫn cài đặt.
- Đã chạy: `.venv/Scripts/python.exe -m pytest` — 1 test passed; import `duckdb`, `pyarrow`, `urbanflow` thành công.
- Phiên bản đã kiểm tra: DuckDB 1.5.5, PyArrow 23.0.1, pytest 9.1.1.
- Kiểm tra ignore: `.venv/`, `data/raw/`, `data/processed/` và `artifacts/` đều được Git bỏ qua.
- Vấn đề còn lại: chưa có; W1-T2 chưa bắt đầu.

### W1-T2 — 2026-09-17

- Trạng thái: hoàn thành trên nhánh `develop`, commit `889357d`.
- Thay đổi: config nguồn tháng `2026-01`, downloader atomic/idempotent, kiểm tra schema, manifest checksum, data card và hướng dẫn chạy.
- Đã chạy: `python -m urbanflow.download_data --config configs/data_sources.json`; lần hai trả `cached` cho cả hai file; `python -m pytest` — 3 test passed.
- Yellow Taxi: 64,165,080 bytes; 3,724,889 raw rows; SHA-256 `8b3933fe6f0d7b6d8826613c0dd724edc680ff7c49e2bd4c7635c05102728637`.
- Zone lookup: 12,331 bytes; 265 rows; SHA-256 `1a99e105092230f8620f301edcca7f80d3080642ff404d28ed957d3fa222c8ed`.
- Kiểm tra ignore: Parquet, lookup và manifest dưới `data/raw/` đều được Git bỏ qua.
- Vấn đề còn lại: timezone, null, out-of-month và zone lạ chưa đánh giá; chuyển sang W1-T3.

### W1-T3 — 2026-09-19

- Trạng thái: hoàn thành trên nhánh `develop`, commit `cb46ea2`.
- Thay đổi: config EDA, DuckDB scan đúng hai cột, kiểm tra timestamp/zone/hourly coverage, đo RSS, báo cáo JSON và data card có quyết định lọc.
- Đã chạy: `python -m urbanflow.inspect_data --config configs/eda.json`; stdout là JSON hợp lệ; `python -m pytest` — 4 test passed; `pip check` không có dependency lỗi.
- Kết quả: 3,724,889 raw rows; 7 rows ngoài tháng; 0 null; 5,930 rows thuộc zone 264/265; giữ 3,718,952 rows thuộc 260 zone hợp lệ; đủ 744/744 giờ.
- Tài nguyên lần xác minh cuối: 6.824 giây, RSS đỉnh 78,860,288 bytes, DuckDB 2 threads và memory limit 1 GB.
- Quyết định: diễn giải timestamp nguồn là local wall time `America/New_York`; loại null, ngoài tháng, zone ngoài lookup và zone 264/265; kiểm tra DST lại khi tải tháng 3.
- Vấn đề còn lại: chưa aggregate hoặc tạo label; chuyển sang W2-T1.

### W2-T1 — 2026-09-19

- Trạng thái: hoàn thành trên nhánh `develop`, commit `8ba2e97`.
- Thay đổi: downloader clean cutover sang danh sách tháng; tải đủ Q1/2026; lọc trip theo data contract; nhận diện DST; aggregate từng tháng thành observed `zone_id × target_hour_utc` Parquet.
- Đã chạy: downloader lần hai trả `cached` cho toàn bộ raw files; aggregate hai lần cho cùng SHA-256; `python -m pytest` — 6 test passed; `pip check` không có dependency lỗi.
- Dữ liệu: 11,077,206 raw rows; loại 17,242; giữ 11,059,964 trips; tạo 355,604 observed hourly rows; không có khóa trùng.
- DST: nhận diện local gap `2026-03-08T02:00:00–03:00:00`, raw có 0 rows trong gap; output UTC timezone-aware.
- Tài nguyên lần chạy ghi nhận: 6.737–8.777 giây/tháng; RSS đỉnh lớn nhất 86,032,384 bytes; DuckDB 2 threads, memory limit 1 GB.
- Invariant: tổng `trip_count` bằng số trips giữ lại cho từng tháng và toàn bộ ba tháng.
- Vấn đề còn lại: output chưa có zero rows hoặc source-missing markers; chuyển sang W2-T2.

### W2-T2 — 2026-09-21

- Trạng thái: hoàn thành trên nhánh `develop`, commit `a78b827`.
- [x] Thêm `configs/grid.json` và `urbanflow.build_hourly_grid`, kế thừa nguồn và
  giới hạn tài nguyên từ config aggregate; không thêm dependency.
- [x] Dùng 263 zone từ lookup cố định; dựng lưới UTC liên tục cho các tháng liên tiếp.
- [x] Phân biệt zero/NULL bằng độ phủ raw; đánh dấu cả hai fall-back UTC hours là
  `dst_ambiguous`; spring gap không tạo giờ giả.
- [x] Đối chiếu observed counts với raw/lookup; kiểm tra schema, count, khóa duy nhất;
  báo lỗi khi thiếu file, input sai hoặc tháng không liên tiếp.
- [x] Chạy `python -m pytest`: 18 passed (6 cũ + 12 trường hợp W2-T2).
- [x] Chạy `python -m urbanflow.build_hourly_grid --config configs/grid.json` trên
  Q1/2026: 2,159 giờ × 263 zone = 567,817 rows; 355,604 observed, 212,213 zero,
  0 missing; bảo toàn 11,059,964 trips.
- [x] Cập nhật README, data contract và data card; output/report bị Git ignore.
- [x] Chạy lại dữ liệu thật: SHA-256 cả ba Parquet không đổi. Truy vấn độc lập
  toàn bộ Parquet xác nhận 567,817 khóa duy nhất và không có khoảng nhảy UTC khác 1 giờ.
- [x] `pip check` không có dependency lỗi; `git diff --check` không có lỗi whitespace;
  review độc lập không phát hiện lỗi cần chặn. Repo chưa cấu hình linter riêng.
- Tài nguyên lần đầu: 4.332–6.947 giây/tháng; RSS đỉnh lớn nhất 128,954,368 bytes;
  DuckDB 2 threads / 1 GB. Không có seed vì phép biến đổi xác định, không ngẫu nhiên.
- Môi trường: `.venv` chạy được khi cấp quyền thực thi phù hợp; không sửa venv.
- Giới hạn: độ phủ theo giờ không phát hiện mất dữ liệu một phần; các task sau
  không được dùng `source_status` giờ đích làm feature hoặc tự đổi NULL thành zero.
- Chưa có baseline/model nên chưa có MAE/WAPE để so sánh. Task tiếp theo: W2-T3.

### W2-T3 — 2026-09-22

- Trạng thái: hoàn thành trên nhánh `develop`, commit `6a89189`.
- Thay đổi: thêm `configs/baseline.json` và `urbanflow.evaluate_baseline`; khóa
  ba split UTC liên tiếp, seasonal lag 168h, fallback không rò rỉ và output
  predictions/metrics có hash, schema, tài nguyên.
- Split thật: train 1,416 giờ / 372,408 rows; validation 359 giờ / 94,417 rows;
  test 384 giờ / 100,992 rows; mỗi split có đủ 263 zone.
- Baseline: train MAE 6.230696, WAPE 0.326374; validation MAE 6.397990,
  WAPE 0.308649; test MAE 4.675301, WAPE 0.237388.
- Fallback: 43,921 train rows dùng prior-zone mean; 263 target đầu tiên không có
  prediction. Validation/test có đủ lag 168h nên fallback rate bằng 0.
- Đã chạy `python -m pytest`: 22 passed. Test mới kiểm tra split liên tiếp, khóa
  trùng, WAPE zero-denominator, target thiếu và việc sửa target/tương lai không đổi
  prediction hiện tại.
- Chạy dữ liệu thật và truy vấn độc lập xác nhận 567,817 rows/khóa duy nhất; metrics
  khớp report. Chạy lại cho cùng prediction SHA-256
  `1b11400cad16f56e3819c487799bd8511fdd2b80b4ff4f0942a75c6f745c0128`.
- Một lần chạy ghi nhận 1.283 giây, RSS đỉnh 450,646,016 bytes, DuckDB 2 threads / 1 GB;
  output predictions 1,998,569 bytes. Không có seed vì baseline xác định.
- Giới hạn: test baseline đã được ghi nhận; W3 chỉ dùng validation để chọn
  feature/model và phải giữ nguyên split. Task tiếp theo: W3-T1.

### W3-T1 — 2026-09-23

- Trạng thái: hoàn thành trên nhánh `develop`, commit `3643c04`.
- Thay đổi: thêm `configs/features.json` và `urbanflow.build_features`; tạo
  calendar UTC, lag 1/24/168h, rolling mean 24/168h chỉ từ các hàng trước target,
  rồi gắn đúng split đã khóa. Không dùng `source_status` làm feature.
- Missing policy: lag thiếu giữ `NULL`; rolling chỉ có giá trị khi đủ toàn bộ
  window không NULL; `features_complete` đánh dấu năm history feature đều usable.
- Dữ liệu thật: 567,817 rows/khóa duy nhất. Train có 328,224/372,408 rows đủ
  history; validation 94,417/94,417; test 100,992/100,992. Q1/2026 không có target
  missing. 44,184 train rows đầu thiếu lag/rolling 168h như warm-up dự kiến.
- Đã chạy `python -m pytest`: 25 passed. Test toy sửa actual target/tương lai và
  xác nhận feature target hiện tại không đổi; lag hàng sau phản ánh actual mới;
  missing trong history làm lag/rolling tương ứng thành NULL. Input khóa trùng bị từ chối.
- Truy vấn độc lập xác nhận row count bằng unique-key count và cờ completeness khớp
  nullness ở cả ba split. Chạy lại cho cùng feature SHA-256
  `b089d90c160c71d2f71c3bc53da1528cd5edcef195598f334c79eaf32f10fe99`.
- Một lần chạy ghi nhận 1.903 giây, RSS đỉnh 525,185,024 bytes, DuckDB 2 threads /
  1 GB; output Parquet 3,936,369 bytes. Không có seed vì phép biến đổi xác định.
- Chưa train model hoặc thay metric baseline trong task này. Task tiếp theo: W3-T2.

### W3-T2 — 2026-09-24

- Trạng thái: hoàn thành trên nhánh `develop`, chưa commit.
- Thay đổi: thêm `configs/model.json`, `urbanflow.train_model`, test cô lập test
  target, memory guard, bundle builder và notebook Colab CPU. Sửa conversion validity
  mask PyArrow bằng `zero_copy_only=False` sau lỗi integration đầu tiên.
- Chọn model chỉ bằng validation: `depth4` đạt MAE `4.367897`, WAPE
  `0.210714`, 212 rounds; `depth6` đạt MAE `4.226789`, WAPE `0.203907`,
  305 rounds nên được chọn. Seed 42, 2 threads.
- Sau khi khóa cấu hình và fit lại trên train + validation, test đạt MAE
  `3.766444`, WAPE `0.191241`; baseline cùng test là MAE `4.675301`,
  WAPE `0.237388`, tương ứng cải thiện `19.4395%` cho cả hai metric.
- Input giữ hash feature W3-T1
  `b089d90c160c71d2f71c3bc53da1528cd5edcef195598f334c79eaf32f10fe99`;
  model version `xgboost_bed2c66982d2`, model SHA-256
  `bed2c66982d253e0d05e2f97d7afbc318c87498f3a64174c53411a264ffd3ea2`.
- Test model trên Colab Python 3.11.16: 3 passed. Test thay toàn bộ test targets
  và xác nhận candidate, rounds, model hash, validation/test predictions không đổi;
  chỉ test metric thay đổi. Preflight thiếu RAM và cấu hình >4 threads đều bị từ chối.
- Kiểm tra độc lập archive/model/manifest/config/input hashes đều khớp. Predictions có
  195,409 rows = 195,409 khóa duy nhất, không NULL, prediction không âm; split row
  counts và MAE/WAPE tính lại từ Parquet khớp report.
- Colab train 50.892 giây; RSS đỉnh 451,014,656 bytes; available RAM thấp nhất
  11,961,393,152 bytes. Versions: NumPy 2.4.6, SciPy 1.17.1, PyArrow 23.0.1,
  XGBoost 3.2.0. Artifacts đã import vào `artifacts/model/` và bị Git ignore.
- Giới hạn: kết quả là historical backtest Q1/2026; chưa đánh giá ngoài thời gian này.
  Task tiếp theo: W3-T3 model card và phân tích lỗi theo zone/giờ.

### W3-T3 — 2026-09-24

- Trạng thái: hoàn thành, chưa commit.
- Thay đổi: thêm `configs/model_analysis.json`, `urbanflow.analyze_model`, ba test
  hành vi và `docs/model-card.md`; báo cáo JSON chi tiết nằm trong
  `artifacts/model/error-analysis.json` và bị Git ignore.
- Integrity gate xác minh schema, size/SHA-256, baseline metrics đúng bản dùng khi
  train, 100.992 model/baseline test keys duy nhất, actual khớp và prediction đã clip
  không âm trước khi phân tích.
- Metric tính lại từ predictions: XGBoost MAE `3.766444`, WAPE `0.191241`;
  seasonal naive MAE `4.675301`, WAPE `0.237388`; cải thiện tương đối `19.4395%`.
- Residual XGBoost: 33.784 underprediction, 62.948 overprediction, 4.260 exact;
  4.382/100.992 raw predictions âm được clip về zero. Absolute-error
  p50/p90/p95/p99 là `0.865562`/`8.747569`/`17.723190`/`47.927626`.
- Theo zone MAE, model tốt hơn baseline tại 214/263 zones, hòa 0 và kém hơn tại
  49 zones. Model MAE cao nhất ở Midtown Center (`35.287481`); regression lớn nhất
  ở Battery Park City (`6.492402` so với baseline `6.195312`).
- Quyết định phục vụ `xgboost_bed2c66982d2`: test MAE thấp hơn baseline; test không
  dùng để đổi feature, candidate hoặc 305 boost rounds. API/UI phải giữ nhãn
  historical backtest và model version này.
- Đã chạy command dữ liệu thật thành công; `tests/test_analyze_model.py` — 3 passed;
  `pip check` và `git diff --check` thành công. Full suite local: 30 passed, 1 failed
  vì venv thiếu NumPy/XGBoost cho test W3-T2; model test tương ứng đã chạy 3 passed
  trên Colab Python 3.11.16 trong W3-T2.
- Truy vấn DuckDB độc lập khớp 100.992 rows/keys, overall MAE/WAPE, residual counts,
  214/0/49 zone wins/ties/regressions và regression lớn nhất tại zone 13.
- Giới hạn: chỉ là historical backtest Q1/2026; chưa đo drift hoặc chất lượng ngoài
  kỳ này. Task tiếp theo: W4-T1 API phục vụ artifact đã khóa.
