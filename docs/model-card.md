# Model card — UrbanFlow AI

## Trạng thái và quyết định phục vụ

- **Model được chọn:** `xgboost_bd51e85845a0` (`xgboost_hist_cpu`).
- **Quyết định:** Phục vụ xgboost_bd51e85845a0 vì locked-test MAE 3.755908 thấp hơn seasonal-naive MAE 4.675301; test không được dùng để đổi feature, candidate parameters hoặc boost rounds.
- **Bối cảnh:** historical backtest, không phải dự báo production hoặc real time.

## Mục đích

Dự báo số lượt đón Yellow Taxi được ghi nhận cho từng taxi zone trong giờ kế tiếp.
Đầu ra hỗ trợ demo hồi cứu trên test set; không đo nhu cầu chưa được phục vụ và
không được diễn giải như quan hệ nhân quả.

## Dữ liệu và hợp đồng đánh giá

- Test UTC: `2026-03-16T04:00:00Z` đến `2026-04-01T04:00:00Z` (half-open).
- Test rows: 100,992; zones: 263; hours: 384.
- Feature chỉ dùng calendar UTC, zone và lag/rolling kết thúc trước target hour.
- Candidate được chọn bằng validation MAE; test chỉ dùng sau khi khóa candidate và rounds.
- Prediction âm được clip về 0 trước khi chấm điểm; raw prediction vẫn được lưu để audit.

## Chọn cấu hình trên validation

| Candidate | Rounds | MAE | WAPE |
| --- | --- | --- | --- |
| `depth4` | 196 | 4.384689 | 0.211524 |
| `depth6` | 431 | 4.204104 | 0.202812 |

## Kết quả test đã khóa

| Model | MAE | WAPE | Mean signed error |
| --- | --- | --- | --- |
| XGBoost `xgboost_bd51e85845a0` | 3.755908 | 0.190706 | 0.701036 |
| Seasonal naive `seasonal_naive_168h_v1` | 4.675301 | 0.237388 | 1.020695 |

XGBoost cải thiện MAE tương đối **19.6649%** và WAPE tương đối **19.6649%** so với baseline.

## Phân tích residual

- XGBoost underpredict 33,673 rows (33.34%), overpredict 62,430 rows (61.82%).
- Raw prediction âm: 5,093 rows (5.04%).
- Absolute-error p50/p90/p95/p99: 0.872 / 8.706 / 17.526 / 47.800.
- Theo MAE, model tốt hơn baseline tại 215 zones, hòa tại 0 và kém hơn tại 48 zones.

## Zone có MAE model cao nhất

| Zone | Borough | Model MAE | Baseline MAE | Δ MAE baseline-model |
| --- | --- | --- | --- | --- |
| 161 — Midtown Center | Manhattan | 35.257822 | 39.247396 | 3.989574 |
| 132 — JFK Airport | Queens | 34.227469 | 43.671875 | 9.444406 |
| 138 — LaGuardia Airport | Queens | 29.022421 | 46.786458 | 17.764037 |
| 186 — Penn Station/Madison Sq West | Manhattan | 27.693353 | 33.458333 | 5.764980 |
| 237 — Upper East Side South | Manhattan | 27.484579 | 37.505208 | 10.020629 |
| 142 — Lincoln Square East | Manhattan | 26.629784 | 33.742188 | 7.112403 |
| 236 — Upper East Side North | Manhattan | 26.204098 | 41.346354 | 15.142257 |
| 230 — Times Sq/Theatre District | Manhattan | 23.122221 | 25.687500 | 2.565279 |
| 162 — Midtown East | Manhattan | 21.329137 | 27.065104 | 5.735968 |
| 79 — East Village | Manhattan | 20.785054 | 23.898438 | 3.113384 |

## Zone model kém baseline nhiều nhất

| Zone | Borough | Model MAE | Baseline MAE | Δ MAE baseline-model |
| --- | --- | --- | --- | --- |
| 13 — Battery Park City | Manhattan | 6.441898 | 6.195312 | -0.246586 |
| 103 — Governor's Island/Ellis Island/Liberty Island | Manhattan | 0.191552 | 0.000000 | -0.191552 |
| 104 — Governor's Island/Ellis Island/Liberty Island | Manhattan | 0.191552 | 0.000000 | -0.191552 |
| 105 — Governor's Island/Ellis Island/Liberty Island | Manhattan | 0.191552 | 0.000000 | -0.191552 |
| 110 — Great Kills Park | Staten Island | 0.191552 | 0.000000 | -0.191552 |
| 199 — Rikers Island | Bronx | 0.191552 | 0.000000 | -0.191552 |
| 44 — Charleston/Tottenville | Staten Island | 0.189747 | 0.007812 | -0.181935 |
| 99 — Freshkills Park | Staten Island | 0.186412 | 0.005208 | -0.181203 |
| 84 — Eltingville/Annadale/Prince's Bay | Staten Island | 0.194750 | 0.015625 | -0.179125 |
| 5 — Arden Heights | Staten Island | 0.168137 | 0.005208 | -0.162929 |

## UTC hours có MAE model cao nhất

| UTC hour | Model MAE | Baseline MAE | Δ MAE baseline-model |
| --- | --- | --- | --- |
| 6 | 6.006200 | 6.683460 | 0.677260 |
| 9 | 5.695166 | 6.317728 | 0.622562 |
| 5 | 5.269975 | 7.077234 | 1.807258 |
| 4 | 5.110567 | 7.439401 | 2.328834 |
| 8 | 5.060444 | 7.308222 | 2.247779 |
| 10 | 4.856145 | 5.227186 | 0.371042 |
| 7 | 4.616581 | 6.668726 | 2.052145 |
| 20 | 4.457345 | 4.562738 | 0.105392 |
| 3 | 4.437320 | 5.998574 | 1.561255 |
| 19 | 4.339955 | 4.894724 | 0.554770 |

## Tái lập và provenance

```powershell
python -m urbanflow.analyze_model --config configs/model_analysis.json
```

- Model SHA-256: `bd51e85845a086fb15adc47a2a3329092af1262d72595f3ef1b38e5c2154b8bb`.
- Model predictions SHA-256: `052f3b1c3409ea3f13379c2174eb09625d011f2de216f9768b8d6745f0ef0336`.
- Baseline predictions SHA-256: `1b11400cad16f56e3819c487799bd8511fdd2b80b4ff4f0942a75c6f745c0128`.
- Error-analysis report SHA-256: `948bb2eca74019ab12396e9a46133762c9cd0f5c9a4d201a623c4042d34a79fc`.
- Train resources: 2 threads, 55.088s, RSS peak 349,003,776 bytes.

## Giới hạn

- Chỉ đánh giá Q1/2026; chưa đo drift, mùa khác, holiday dài hạn hoặc thay đổi vận hành.
- Target là pickup được ghi nhận, không phải toàn bộ nhu cầu đi lại.
- Không có weather/event/traffic và không có luồng trip near-real-time trong V1.
- MAE tổng thể có thể che subgroup yếu; các zone/hour ở trên cần được hiển thị trung thực trong demo.
- Test đã được xem để quyết định model phục vụ, nhưng không được dùng để đổi feature, candidate hoặc rounds.
- API/UI phải gắn nhãn historical backtest và trả đúng model version.
