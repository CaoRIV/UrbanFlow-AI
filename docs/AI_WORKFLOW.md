# Quy trình Codex + OMP + Orca IDE

## Vai trò và nguyên tắc

- **Orca IDE** là nơi mở repo, terminal, xem diff và (khi có) tách worktree cho mỗi task. Nó hỗ trợ chạy các agent CLI như Codex; cách thêm OMP phụ thuộc phiên bản và cấu hình của bạn. [Orca Docs](https://www.onorca.dev/).
- **Codex** và **OMP (Oh My Pi)** là hai phiên agent/harness dùng chung yêu cầu và code trong repo. Có thể dùng một hoặc cả hai theo task; không cần gọi một agent bên trong agent kia.
- Một task có **một người/agent chịu trách nhiệm merge**. Trên laptop 8GB, làm tuần tự với data/train; nếu dùng hai worktree, chỉ một phiên ETL/train chạy tại một thời điểm. Không cho cả hai cùng sửa một file trong cùng working tree.
- Hướng dẫn lâu dài trong `AGENTS.md`; yêu cầu việc hiện tại nằm ở prompt; log quyết định và metric nằm trong repo. Session chat có thể mất/ngắt nên repo mới là nguồn sự thật.

## Thiết lập phiên đầu tiên

1. Tạo git repo `urbanflow-ai`, sao chép các file Markdown này vào gốc. Commit đầu tiên `docs: define scope and roadmap`.
2. Mở repo trong Orca; tạo một task/worktree cho W1-T1 nếu Orca hỗ trợ. Chọn Codex hoặc terminal OMP đang có; xác nhận terminal đang ở gốc repo và agent thấy `AGENTS.md`.
3. Đưa agent `TASK_BOARD.md` và mục liên quan trong spec; yêu cầu **chỉ làm một task**. Đọc kế hoạch ngắn trước khi chạy script tải dữ liệu lớn.
4. Khi hoàn thành, xem `git diff`, chạy test/lệnh theo báo cáo, mở demo, ghi nhận kết quả vào task board rồi commit/merge. Lặp lại với task kế tiếp.

Không chép khóa API vào prompt hay commit. Cách cài/đăng nhập CLI thay đổi theo phiên bản và máy; kiểm tra hướng dẫn của chính công cụ bạn đang dùng trước khi cấu hình. Nếu OMP chưa chạy được trong Orca, vẫn có thể mở OMP ở terminal độc lập tại cùng repo, nhưng tránh cho hai phiên chỉnh cùng working tree một lúc.

## Mẫu prompt triển khai

```text
Bạn đang làm UrbanFlow AI. Đọc AGENTS.md, PROJECT_SPEC.md,
DATA_AND_EVALUATION.md và TASK_BOARD.md. Thực hiện duy nhất W1-T1.
Trước khi sửa, nêu ngắn file sẽ sửa và cách xác nhận đầu ra.
Giữ tài nguyên phù hợp laptop 8GB. Sau khi sửa, chạy kiểm tra liên quan,
báo kết quả thực, file đã đổi, câu lệnh tái chạy và mọi giả định.
Không nhận task khác khi W1-T1 chưa được review.
```

## Mẫu prompt review độc lập

```text
Review thay đổi của task W3-T2 so với main. Chỉ đọc, không sửa code.
Tập trung: rò rỉ tương lai trong lag/rolling, split thời gian,
timezone, memory trên máy 8GB và kiểm tra có đủ ý nghĩa.
Liệt kê lỗi theo mức độ với file/dòng và cách tái hiện.
Nếu không thấy lỗi, nêu rủi ro còn lại.
```

## Khi quay lại một phiên đã dừng

Mở đúng repo/worktree và session của công cụ nếu ứng dụng có chức năng resume. Nếu không, mở phiên mới, đưa `AGENTS.md`, `TASK_BOARD.md`, `git status`, commit gần nhất và phần đầu ra cần tiếp tục. Không phụ thuộc vào việc agent “nhớ” cuộc trò chuyện cũ. Không dùng lệnh resume cụ thể khi chưa xác nhận CLI/version hiện có.

## Mẫu bàn giao cuối phiên

```text
Task: W2-T1
Trạng thái: hoàn thành / đang chờ / bị chặn
Nhánh hoặc worktree: ...
Đã sửa: ...
Đã chạy: ... (kết quả thực)
Nguồn dữ liệu, khoảng thời gian, số hàng: ...
Metric (nếu có), so với baseline: ...
Rủi ro/câu hỏi: ...
Task tiếp theo: ...
```

## Nếu hai agent cho kết quả khác nhau

So sánh test, schema, metric và diff trên **cùng dữ liệu, split và yêu cầu**. Chọn phương án đúng hợp đồng dự báo, nhỏ và dễ tái lập; con số MAE tốt hơn riêng lẻ không đủ để merge. Lưu quyết định trong `docs/decisions.md`.
