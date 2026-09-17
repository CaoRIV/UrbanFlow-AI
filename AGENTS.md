# Hướng dẫn cho coding agent — UrbanFlow AI

Đặt file này tại **gốc repo** khi bắt đầu code. Codex đọc file theo cơ chế dự án của nó; với OMP, mở phiên ở gốc repo và yêu cầu đọc file này nếu cấu hình hiện tại không tự nạp. Tài liệu đặc tả: `PROJECT_SPEC.md`, `DATA_AND_EVALUATION.md`; tiến độ: `TASK_BOARD.md`.

## Mục tiêu

Xây historical backtest dự báo số Yellow Taxi pickups cho `zone × next hour`, trên laptop i5/8GB. Ưu tiên pipeline đúng và tái lập, sau đó API/UI; không tăng độ phức tạp nếu chưa có baseline và báo cáo test.

## Quy tắc làm việc

1. Trước mỗi task: đọc spec, data contract và code liên quan; đề xuất thay đổi nhỏ và tiêu chí kiểm tra. Chỉ sửa phạm vi task. Báo lại file đã sửa, cách chạy, kết quả thực và phần chưa chắc.
2. Không tự khẳng định metric, số hàng, thời gian chạy hay cấu hình từ dữ liệu chưa tải. Không tạo data giả rồi trình bày như kết quả TLC.
3. Mỗi lệnh ETL/train có config đầu vào, seed nếu áp dụng và output path. Data raw, secrets, virtualenv, artifacts lớn và cache nằm ngoài git; commit sample nhỏ tổng hợp khi cần test.
4. Time series: UTC nội bộ; `target_hour` là giờ cần dự báo, feature chỉ dùng dữ liệu đã biết trước giờ đó. Split theo thời gian, không random; test không dùng để điều chỉnh model. Mọi rolling phải shift.
5. Tài nguyên: chọn cột khi scan Parquet; từng tháng một; dùng query/lazy cho raw; `n_jobs <= 4`; không mở nhiều job ETL/train song song. Không yêu cầu GPU, Docker hoặc cơ sở dữ liệu cho V1.
6. Viết test có ý nghĩa cho leak tương lai, khung giờ, mapping zone, schema API và lỗi đầu vào. Không viết test chỉ lặp lại hàm thực thi.
7. Giữ các interface và tên file theo README hiện có trong repo. Nếu còn ở giai đoạn tài liệu, đề xuất skeleton trước khi tạo code. Không tự thay công nghệ hoặc cài nhiều dependency khi chưa có nhu cầu rõ.

## Định nghĩa hoàn thành một task

- Code/tài liệu và lệnh chạy đã cập nhật; lint/test liên quan chạy thành công hoặc nêu lỗi cụ thể.
- Nếu thay dữ liệu/model: có diff metrics so với baseline trên cùng split, thời gian và config tái lập.
- Nếu thay API/UI: có ví dụ request/response hoặc screenshot demo, cùng lỗi input đã kiểm tra.
- Ghi trạng thái task và commit/nhánh vào `TASK_BOARD.md` hoặc báo người dùng cập nhật.

## Repo đề xuất khi triển khai

```text
urbanflow-ai/
├── AGENTS.md
├── README.md
├── PROJECT_SPEC.md
├── DATA_AND_EVALUATION.md
├── TASK_BOARD.md
├── configs/
├── docs/                  # data-card, model-card, decisions
├── data/raw/              # gitignored
├── data/processed/        # gitignored
├── artifacts/             # gitignored
├── src/                   # ingest, aggregate, features, train, predict
├── api/                   # FastAPI
├── web/                   # Vue
└── tests/
```

Không tạo tất cả thư mục trống bắt buộc ngay từ đầu. Cập nhật cấu trúc theo code thực tế.
