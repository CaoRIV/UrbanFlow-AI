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

### Hợp đồng full grid W2-T2

- Chạy `python -m urbanflow.build_hourly_grid --config configs/grid.json` sau aggregate.
- Tập zone cố định là tất cả service zones trong lookup; không lọc theo hoạt động
  của các tháng dùng để đánh giá. Cùng tập zone cho mọi tháng.
- Khoảng thời gian bao trọn các tháng local liên tiếp đã cấu hình; lưới chạy theo
  UTC với bước 1 giờ. Spring DST có 23 giờ trong ngày chuyển; fall DST có 25 giờ.
- `source_status = available` khi có ít nhất một raw pickup trong tháng, timestamp
  hợp lệ và không mơ hồ, bất kể zone có bị loại hay không. Zone không có count
  trong giờ này nhận `0`. Đây là giả định vận hành, không chứng minh dữ liệu đầy đủ.
- Không có pickup nguồn trong giờ: `source_missing`, `trip_count = NULL`. Hai UTC
  hours tương ứng local fall-back hour bị loại: `dst_ambiguous`, count cũng `NULL`.
  Spring gap không phải một giờ UTC bị thiếu, không thêm giờ giả cho gap.
- Không suy luận nguồn thiếu một phần từ counts; V1 chưa có metadata outage bên ngoài.
  Thiếu cả file raw/observed là lỗi đầu vào, không tự tạo toàn tháng zero.
- `source_status` là metadata chất lượng label biết khi xử lý lịch sử, không phải
  feature biết trước giờ đích. Các task split/baseline/features sau này phải xử lý
  label/lag thiếu một cách tường minh, không đổi `NULL` thành zero mặc định.
- Khóa `(zone_id, target_hour_utc)` duy nhất; timestamp UTC; count nullable int64,
  không âm khi có giá trị. Tổng count giữ nguyên so với W2-T1. Báo cáo lưu hash
  input/output/config, số giờ/zone/zero/missing và tài nguyên từng tháng.

### Hợp đồng split và seasonal naive W2-T3

- Chạy `python -m urbanflow.evaluate_baseline --config configs/baseline.json` sau
  full grid. Split là ba khoảng UTC half-open, liên tiếp, không shuffle; config đã
  khóa ranh giới train/validation/test và phải được tái sử dụng cho model sau.
- Dự báo seasonal naive của một hàng chỉ đọc count cùng zone tại
  `target_hour_utc - 168h`. Actual của target không tham gia tính prediction.
- Nếu lag train thiếu, fallback là mean cùng zone chỉ trên các target train sớm hơn.
  Nếu lag validation/test thiếu, fallback là mean cùng zone fit trên toàn train;
  không cập nhật bằng actual validation/test. Báo số hàng và tỷ lệ fallback.
- Hàng có actual `NULL` không được chấm điểm. Prediction có thể tồn tại nhưng
  `absolute_error` phải là `NULL`; không đổi label thiếu thành zero.
- MAE/WAPE tổng thể tính trên hàng có cả actual và prediction. WAPE là `NULL` khi
  tổng actual bằng zero. Báo thêm MAE theo zone và UTC hour cho từng split.
- Output predictions giữ split, actual, lag, prediction, nguồn prediction, cờ
  fallback và absolute error. Metrics JSON lưu config/hash/schema/tài nguyên; phép
  tính xác định nên không có seed.
- Baseline cố định được chạy trên test để tạo mốc so sánh. Các quyết định feature
  và model ở W3 chỉ dùng validation; không thay split hoặc baseline sau khi xem test.

### Hợp đồng feature W3-T1

- Chạy `python -m urbanflow.build_features --config configs/features.json` trên
  full grid. Output tái sử dụng nguyên ba split UTC của baseline; window được tính
  xuyên biên split để validation/test có warm-up từ quá khứ, không từ tương lai.
- Calendar gồm UTC hour, ISO day-of-week (1=Monday, 7=Sunday), month và weekend.
  `zone_id` là categorical feature; `source_status`, `split` và target chỉ là
  metadata/label, không phải model input.
- Lag 1/24/168h và rolling mean 24/168h đều partition theo zone. Rolling dùng
  `ROWS BETWEEN N PRECEDING AND 1 PRECEDING`, nên không chứa actual target.
- Lag thiếu giữ `NULL`. Rolling chỉ có giá trị khi đủ N target quá khứ không
  `NULL`; không bỏ qua missing và không impute zero. `features_complete` chỉ
  true khi cả năm history feature có giá trị. Hàng train được dùng khi target cũng
  không `NULL`; chiến lược impute khác, nếu có, phải fit trên train trong W3-T2.
- Output giữ khóa duy nhất `(zone_id, target_hour_utc)`, được sắp xếp xác định.
  Report lưu config/input/output hash, schema, null counts và usable rows mỗi split.

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
