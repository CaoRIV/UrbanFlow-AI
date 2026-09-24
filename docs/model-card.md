# Model card — UrbanFlow AI

## Trạng thái và quyết định phục vụ

- **Model được chọn:** `xgboost_bed2c66982d2` (`xgboost_hist_cpu`).
- **Quyết định:** Phục vụ xgboost_bed2c66982d2 vì locked-test MAE 3.766444 thấp hơn seasonal-naive MAE 4.675301; test không được dùng để đổi feature, candidate parameters hoặc boost rounds.
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
| `depth4` | 212 | 4.367897 | 0.210714 |
| `depth6` | 305 | 4.226789 | 0.203907 |

## Kết quả test đã khóa

| Model | MAE | WAPE | Mean signed error |
| --- | --- | --- | --- |
| XGBoost `xgboost_bed2c66982d2` | 3.766444 | 0.191241 | 0.707752 |
| Seasonal naive `seasonal_naive_168h_v1` | 4.675301 | 0.237388 | 1.020695 |

XGBoost cải thiện MAE tương đối **19.4395%** và WAPE tương đối **19.4395%** so với baseline.

## Phân tích residual

- XGBoost underpredict 33,784 rows (33.45%), overpredict 62,948 rows (62.33%).
- Raw prediction âm: 4,382 rows (4.34%).
- Absolute-error p50/p90/p95/p99: 0.866 / 8.748 / 17.723 / 47.928.
- Theo MAE, model tốt hơn baseline tại 214 zones, hòa tại 0 và kém hơn tại 49 zones.

## Zone có MAE model cao nhất

| Zone | Borough | Model MAE | Baseline MAE | Δ MAE baseline-model |
| --- | --- | --- | --- | --- |
| 161 — Midtown Center | Manhattan | 35.287481 | 39.247396 | 3.959915 |
| 132 — JFK Airport | Queens | 34.613680 | 43.671875 | 9.058195 |
| 138 — LaGuardia Airport | Queens | 28.833930 | 46.786458 | 17.952529 |
| 237 — Upper East Side South | Manhattan | 27.878784 | 37.505208 | 9.626424 |
| 186 — Penn Station/Madison Sq West | Manhattan | 27.835307 | 33.458333 | 5.623027 |
| 236 — Upper East Side North | Manhattan | 26.515405 | 41.346354 | 14.830949 |
| 142 — Lincoln Square East | Manhattan | 26.464732 | 33.742188 | 7.277456 |
| 230 — Times Sq/Theatre District | Manhattan | 23.378067 | 25.687500 | 2.309433 |
| 162 — Midtown East | Manhattan | 21.386547 | 27.065104 | 5.678557 |
| 79 — East Village | Manhattan | 20.992636 | 23.898438 | 2.905802 |

## Zone model kém baseline nhiều nhất

| Zone | Borough | Model MAE | Baseline MAE | Δ MAE baseline-model |
| --- | --- | --- | --- | --- |
| 13 — Battery Park City | Manhattan | 6.492402 | 6.195312 | -0.297090 |
| 109 — Great Kills | Staten Island | 0.165300 | 0.036458 | -0.128842 |
| 44 — Charleston/Tottenville | Staten Island | 0.135408 | 0.007812 | -0.127596 |
| 99 — Freshkills Park | Staten Island | 0.130859 | 0.005208 | -0.125651 |
| 84 — Eltingville/Annadale/Prince's Bay | Staten Island | 0.141217 | 0.015625 | -0.125592 |
| 2 — Jamaica Bay | Queens | 0.139575 | 0.015625 | -0.123950 |
| 103 — Governor's Island/Ellis Island/Liberty Island | Manhattan | 0.123947 | 0.000000 | -0.123947 |
| 104 — Governor's Island/Ellis Island/Liberty Island | Manhattan | 0.123947 | 0.000000 | -0.123947 |
| 105 — Governor's Island/Ellis Island/Liberty Island | Manhattan | 0.123947 | 0.000000 | -0.123947 |
| 110 — Great Kills Park | Staten Island | 0.123947 | 0.000000 | -0.123947 |

## UTC hours có MAE model cao nhất

| UTC hour | Model MAE | Baseline MAE | Δ MAE baseline-model |
| --- | --- | --- | --- |
| 6 | 6.110481 | 6.683460 | 0.572980 |
| 9 | 5.602322 | 6.317728 | 0.715406 |
| 5 | 5.186824 | 7.077234 | 1.890410 |
| 4 | 5.075725 | 7.439401 | 2.363676 |
| 8 | 5.073375 | 7.308222 | 2.234848 |
| 10 | 4.938063 | 5.227186 | 0.289123 |
| 7 | 4.634389 | 6.668726 | 2.034338 |
| 3 | 4.411348 | 5.998574 | 1.587226 |
| 20 | 4.383458 | 4.562738 | 0.179280 |
| 19 | 4.267687 | 4.894724 | 0.627037 |

## Tái lập và provenance

```powershell
python -m urbanflow.analyze_model --config configs/model_analysis.json
```

- Model SHA-256: `bed2c66982d253e0d05e2f97d7afbc318c87498f3a64174c53411a264ffd3ea2`.
- Model predictions SHA-256: `a57f20cb6f5d9e2fcf44b604058dbd7d85a9b7a1f292854053d0d37be89283e3`.
- Baseline predictions SHA-256: `1b11400cad16f56e3819c487799bd8511fdd2b80b4ff4f0942a75c6f745c0128`.
- Error-analysis report SHA-256: `7dca0a4d053a3298648736beb43bf32d1f3b4d0847da67b48b5bca0393dd3cf9`.
- Train resources: 2 threads, 50.892s, RSS peak 451,014,656 bytes.

## Giới hạn

- Chỉ đánh giá Q1/2026; chưa đo drift, mùa khác, holiday dài hạn hoặc thay đổi vận hành.
- Target là pickup được ghi nhận, không phải toàn bộ nhu cầu đi lại.
- Không có weather/event/traffic và không có luồng trip near-real-time trong V1.
- MAE tổng thể có thể che subgroup yếu; các zone/hour ở trên cần được hiển thị trung thực trong demo.
- Test đã được xem để quyết định model phục vụ, nhưng không được dùng để đổi feature, candidate hoặc rounds.
- API/UI phải gắn nhãn historical backtest và trả đúng model version.
