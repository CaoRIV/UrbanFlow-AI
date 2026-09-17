# Dữ liệu, đặc trưng và đánh giá

## Nguồn và data card cần điền

- [NYC TLC Yellow Taxi Parquet](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page) và [data dictionary](https://www.nyc.gov/assets/tlc/downloads/pdf/data_dictionary_trip_records_yellow.pdf). Chỉ chọn `tpep_pickup_datetime`, `PULocationID`; tên cột xác nhận trên file tháng thực tế. Lưu URL, tháng, ngày tải, kích thước, số dòng và phiên bản script.
- Taxi zone lookup cùng trang TLC; dùng để xác thực zone và gắn tên. Nếu làm bản đồ, thêm [NYC Taxi Zones](https://data.cityofnewyork.us/Transportation/NYC-Taxi-Zones/8meu-9t5y).
- V1 không có weather. Mở rộng weather cần nêu rõ `known_at`: [Historical Weather](https://open-meteo.com/en/docs/historical-weather-api) thường phản ánh điều kiện đã xảy ra, còn [Historical Forecast](https://open-meteo.com/en/docs/historical-forecast-api) phù hợp hơn khi mô phỏng dự báo biết trước giờ đích. Không lấy thời tiết thực tế của giờ đích làm feature cho dự báo trước giờ đó.

## Pipeline

1. Đọc từng file Parquet theo tháng, chỉ chọn pickup time/zone; giữ các bản ghi pickup thuộc đúng khoảng tháng theo giờ NYC được công bố, xác nhận diễn giải timezone bằng kiểm tra mẫu.
2. Chuyển về giờ UTC một cách xác định; kiểm tra DST và quy tắc timezone nguồn trước khi aggregate. Dữ liệu giờ mơ hồ chưa xử lý được phải loại và đếm riêng, không âm thầm đoán.
3. Chỉ giữ zone trong lookup (loại hoặc ghi riêng unknown/N/A), bỏ pickup null, tạo `count(zone, hour_utc)`.
4. Tạo full grid cho các zone được chọn và toàn khoảng giờ; điền `0` cho giờ hợp lệ không có chuyến, tách missing-source intervals. Ghi số zone, giờ và số hàng thực tế.
5. Split theo `target_hour_utc`, ví dụ 3 tháng liên tục: tháng 1–2 train, nửa đầu tháng 3 validation, nửa cuối tháng 3 test. Feature cần 7 ngày lịch sử: giữ warm-up từ phần trước nhưng chỉ chấm điểm trên đoạn split của chính nó.
6. Tính lag và rolling bằng `shift` theo từng zone trước; rolling không bao gồm giờ đích. Fit encoder/transformer chỉ trên train; áp dụng validation/test theo cùng schema.
7. Baseline: số chuyến cùng zone ở giờ tương ứng 7 ngày trước (`target_hour - 168h`); thiếu lịch sử thì dùng fallback đã định trước từ train, và báo tỷ lệ fallback.

## Metric và chống tự đánh lừa

| Chỉ số | Ý nghĩa / cách dùng |
| --- | --- |
| MAE | Sai số tuyệt đối trung bình, đơn vị trips/zone/hour; chỉ số chính |
| WAPE | `sum(abs(y-pred))/sum(y)` trên toàn đoạn có mẫu số > 0; dễ đọc khi tổng lượng khác nhau |
| MAE theo zone/giờ | Tìm vùng ít chuyến, giờ cao điểm và lỗi bị trung bình che |

- Giá trị dự báo âm được clip về 0 theo quy tắc cố định từ trước khi đánh giá; lưu cả raw nếu cần phân tích.
- Chỉ dùng validation để thử feature/tham số; test chạy một lần cho phiên bản đã chốt. Kết quả trên 3 tháng phản ánh một giai đoạn ngắn, không chứng minh khả năng dự báo các mùa khác.
- Không tuyên bố ảnh hưởng nhân quả của rain/holiday khi chỉ có mối liên hệ quan sát.
- Lưu `metrics.json`, cấu hình split, seed, phiên bản dữ liệu và thời gian train. Model card nêu tập train/test, baseline, subgroup yếu, giới hạn historical backtest.

## Hai kiểm tra tối thiểu

1. Tạo dữ liệu toy 1 zone × 10 giờ; sửa giá trị ở giờ đích và tương lai; feature của các hàng trước giờ đó phải giữ nguyên.
2. Kiểm tra target hours của train < validation < test, không trùng; `forecast(target_hour)` không đọc actual target như input. Với DST, kiểm tra giờ trùng được phân biệt bằng UTC hoặc bị loại và ghi log.
