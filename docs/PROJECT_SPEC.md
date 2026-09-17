# Đặc tả sản phẩm — UrbanFlow AI

## 1. Bài toán

Với dữ liệu đã quan sát đến hết giờ `t`, dự đoán `trip_count` của giờ `t+1` cho từng pickup taxi zone. `trip_count` là số bản ghi Yellow Taxi có pickup hợp lệ trong zone và giờ đó; đây là **lượt đón được ghi nhận**, không phải toàn bộ nhu cầu đi lại hay nhu cầu chưa được phục vụ. Không gán nhãn “real time” khi ứng dụng chỉ dùng dữ liệu lịch sử.

Người xem demo chọn thời điểm nằm trong tập test và thấy dự báo cho giờ tiếp theo, số thực tế khi đã có, sai số, top zone, biểu đồ theo giờ. Demo là hồi cứu (backtest), ghi rõ thời điểm chốt dữ liệu. Nếu muốn dự báo thời điểm hiện tại thật sự thì cần luồng trip gần thời gian thực, nằm ngoài V1.

## 2. Phạm vi V1

| Có trong V1 | Để sau V1 |
| --- | --- |
| Yellow Taxi 3 tháng liên tục; pickup zone hợp lệ | Yellow/Green/FHV gộp chung |
| Hourly zone counts; full hourly grid; đặc trưng lịch và lag | Weather, sự kiện, bản đồ địa lý |
| Seasonal naive + một model CPU; split theo thời gian | LSTM, Transformer, AutoML nặng |
| FastAPI đọc artifact và một dashboard bảng/line chart | PostgreSQL/PostGIS, Kubernetes, retraining tự động |
| Test pipeline, metrics và README tái lập | Phát hiện bất thường và giải thích nguyên nhân |

## 3. Hợp đồng dự báo

- Trục thời gian nội bộ: UTC; hiển thị `America/New_York` có ghi múi giờ. Không gom theo giờ địa phương trước khi xử lý giờ trùng/thiếu do DST.
- Một hàng: `(zone_id, target_hour_utc)` với `y = trips` trong `[target_hour, target_hour+1h)`.
- Tại cutoff `t_end`, những giờ `< t_end` đã quan sát; dự báo cho `[t_end, t_end+1h)`. Các lag lấy ở những giờ `< t_end` mà data thực sự đã sẵn sàng. Nếu mô phỏng có độ trễ công bố dữ liệu, ghi và áp dụng độ trễ đó nhất quán.
- Đặc trưng hợp lệ: `zone_id`, giờ trong ngày/tuần của giờ đích, `count` của giờ vừa đóng, giờ tương ứng hôm trước và tuần trước, trung bình trượt **dịch lùi**. Không dùng count của giờ đích, rolling có chứa giờ đích, hoặc thống kê tính trên toàn bộ train+test.
- Các zone không có lượt đón ở một giờ có count `0` sau khi đã dựng lưới; giờ thiếu do hỏng dữ liệu nguồn phải đánh dấu riêng, không tự coi là zero.

## 4. Đầu ra và tiêu chí chấp nhận

1. Chạy pipeline từ các file tháng đã chỉ định ra tập tổng hợp + feature và artifact model với seed/config đã lưu; raw không commit.
2. Split train/validation/test theo thời gian, không shuffle; báo cáo MAE và WAPE tổng thể, thêm MAE theo zone và theo giờ. WAPE không dùng cho nhóm có tổng thực tế bằng zero.
3. So với seasonal naive cùng zone/giờ tuần trước. Nếu model không cải thiện thì ghi đúng kết quả và phục vụ baseline tốt hơn; không chỉnh test sau khi xem kết quả.
4. API trả dự báo, thời điểm UTC, zone, model/version; dữ liệu đầu vào thiếu trả lỗi rõ ràng. UI hiển thị biểu đồ dự báo với actual và chú thích “historical backtest”.
5. `README` repo có cách chạy tuần tự, ảnh demo, bảng metrics từ test, giới hạn và nguồn dữ liệu. Có bài test ngắn xác minh không rò rỉ tương lai và API/schema.

## 5. Tài nguyên

- Không đặt cứng mục tiêu “100k rows”: 3 tháng × 24 giờ × số zone hợp lệ có thể lớn hơn; đo sau ETL và lọc zone nếu máy thiếu RAM, ghi lý do lọc.
- Giới hạn luồng train `n_jobs=2–4`, model vài trăm cây/độ sâu vừa phải, không grid search rộng. Ưu tiên dtype gọn và Parquet; đóng browser/Docker khi train trên 8GB.
- Chỉ tải từng tháng và giữ các cột cần thiết. Có thể bắt đầu 1 tháng để kiểm tra pipeline rồi mở rộng sang 3 tháng cho đánh giá.
