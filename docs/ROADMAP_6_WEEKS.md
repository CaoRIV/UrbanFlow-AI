# Roadmap 4–6 tuần

Nhịp tham khảo: 8–12 giờ/tuần. Mỗi tuần kết thúc bằng một demo chạy được và commit nhỏ; nếu thiếu thời gian, ưu tiên cổng kiểm tra trước việc mở rộng.

| Tuần | Việc cần làm | Đầu ra kiểm chứng | Điều kiện qua tuần |
| --- | --- | --- | --- |
| 1 — Khởi tạo và dữ liệu | Tạo repo/env, tải 1 tháng Yellow Taxi + taxi zone lookup; đọc dictionary, kiểm tra schema, ngày giờ, zone, null, dung lượng; ghi data card | `README`, `docs/data-card.md`, script lấy dữ liệu với nguồn và checksum hoặc kích thước | Chạy trên 1 tháng từ đầu; có thống kê rows, khoảng thời gian, cột và lỗi |
| 2 — ETL & baseline | Nâng lên 3 tháng; quét Parquet chọn cột, lọc dữ liệu ngoài khoảng tháng, tạo grid zone × UTC hour, đánh dấu khoảng thiếu; tạo seasonal naive | `data/processed/*.parquet`, bảng phân phối, metrics baseline trên split thời gian | Không lẫn giờ đích vào đặc trưng; baseline tái chạy cho cùng kết quả |
| 3 — Features & ML | Lag/rolling chỉ từ quá khứ; train một model CPU, validation và so sánh; cố định test | `src/features.py`, `src/train.py`, model artifact, `docs/model-card.md` | Có MAE/WAPE tổng thể, theo zone/giờ; test không dùng để chọn tham số |
| 4 — API & demo tối thiểu | FastAPI load artifact, endpoint health/forecast/history; Vue bảng top zone và chart forecast vs actual; chốt V1 có thể demo | API contract, UI chạy local, screenshot | Chạy một lệnh mỗi service, UI phản ánh đúng một cutoff trong test |
| 5 — Độ tin cậy & trình bày | Test ETL theo sample nhỏ, rò rỉ tương lai, schema API; cải thiện README; thêm cache/precomputed predictions nếu API chậm | `tests/`, hướng dẫn tái lập, ảnh/video demo, bảng baseline vs model | Người khác có thể chạy từ hướng dẫn; lỗi input cho thông báo dễ hiểu |
| 6 — Phần cộng điểm tùy chọn | Chọn **một**: weather có kiểm soát thời điểm biết dữ liệu; bản đồ zone; residual anomaly; hoặc CI. Viết tradeoff và giới hạn | Một tính năng mở rộng + tài liệu đánh giá; CV bullets nháp | V1 vẫn chạy ổn; số liệu mở rộng được so sánh công bằng |

## Lịch rút gọn 4 tuần

- Tuần 1: gộp tuần 1–2; chỉ 3 tháng Yellow Taxi và baseline.
- Tuần 2: ML + kiểm tra leakage, khóa tập test.
- Tuần 3: API + dashboard tối thiểu.
- Tuần 4: tests, README, demo, model card và CV bullets. Bỏ hẳn tính năng tuần 6; bản đồ/weather không nằm trong đường găng.

## Gợi ý chia phiên làm việc

Mỗi phiên 60–120 phút: (1) nhận một task trong `TASK_BOARD.md`, (2) cho agent đọc `AGENTS.md` và tài liệu liên quan, (3) yêu cầu plan ngắn, (4) agent sửa và tự chạy kiểm tra, (5) xem diff/metrics, (6) ghi decision hoặc issue mới. Trên RAM 8GB, đừng chạy nhiều training/ETL cùng lúc dù Orca có nhiều worktree.

## Quy tắc đổi phạm vi

Nếu ETL không chạy sau tuần 2: giảm số zone hoặc số tháng thí nghiệm, nhưng giữ đủ ba đoạn train/validation/test và ghi giới hạn. Nếu model không hơn baseline ở tuần 3: dùng baseline làm bản demo, tìm nguyên nhân, không đổi chỉ số hoặc test để làm đẹp kết quả. Nếu thiếu thời gian tuần 4: dashboard bảng + chart đủ; không chờ bản đồ.
