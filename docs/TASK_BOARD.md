# Task board — UrbanFlow AI

Quy ước: `[ ]` chưa làm, `[x]` xong sau khi đã kiểm tra. Giao một mã task mỗi lần. Đây là kế hoạch, chưa task nào được triển khai.

## Tuần 1

- [x] **W1-T1** — Khởi tạo repo, môi trường Python, `.gitignore`, README chạy thử. Xong khi tạo venv, import dependency, test smoke chạy; raw/artifact bị ignore.
- [ ] **W1-T2** — Viết `docs/data-card.md` và script tải đúng **một** tháng Yellow Taxi + zone lookup từ TLC, lưu URL/size/hash hoặc dấu vết tương đương. Xong khi tải lại được và ghi số hàng/schema.
- [ ] **W1-T3** — EDA: null pickup/zone, range ngày, bản ghi ngoài tháng, các zone lạ và thống kê RAM/thời gian. Xong khi có bảng chất lượng và quyết định lọc.

## Tuần 2

- [ ] **W2-T1** — Tải thêm tháng thứ hai, ba; scan chỉ cột cần, aggregate theo UTC hour và zone. Xong khi pipeline tái chạy được và không đọc mọi cột vào RAM.
- [ ] **W2-T2** — Dựng full hourly grid, phân biệt zero thật với khoảng nguồn thiếu và xử lý DST; test toy. Xong khi khóa chính `zone_id,target_hour_utc` duy nhất.
- [ ] **W2-T3** — Split time và seasonal naive; ghi MAE/WAPE, số hàng/số zone mỗi split. Xong khi có baseline lưu file và config split.

## Tuần 3

- [ ] **W3-T1** — Tạo calendar + lag 1/24/168h, rolling shift theo zone; test không rò rỉ tương lai.
- [ ] **W3-T2** — Train model CPU nhỏ, dùng validation để chọn một cấu hình; khóa test, lưu model/config/metrics.
- [ ] **W3-T3** — Model card: bảng baseline/model test, lỗi theo zone/giờ, giới hạn; chọn model phục vụ một cách trung thực.

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

- Trạng thái: hoàn thành trên nhánh `main`, chưa commit.
- Thay đổi: cấu hình Python 3.11, dependency cố định, package `urbanflow`, quy tắc ignore, smoke test DuckDB/Arrow và hướng dẫn cài đặt.
- Đã chạy: `.venv/Scripts/python.exe -m pytest` — 1 test passed; import `duckdb`, `pyarrow`, `urbanflow` thành công.
- Phiên bản đã kiểm tra: DuckDB 1.5.5, PyArrow 23.0.1, pytest 9.1.1.
- Kiểm tra ignore: `.venv/`, `data/raw/`, `data/processed/` và `artifacts/` đều được Git bỏ qua.
- Vấn đề còn lại: chưa có; W1-T2 chưa bắt đầu.
