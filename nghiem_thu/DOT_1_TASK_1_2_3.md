# Đợt 1 (task 1 → 2 → 3): giữ hàng theo SKU, khách hủy đơn, hàng đợi đối soát

## Hiện trạng repo trước khi sửa

| Hạng mục | Vị trí thật |
|---|---|
| Database | SQLite, file `goctech.db` cạnh `app.py` (đổi bằng `DB_PATH`); test dùng `:memory:` |
| Tạo Stripe Session | `backend.py`: `start_payment()` (giả lập) và `_start_payment_real()` (Stripe Sandbox), gọi từ `_checkout()`, `review()`, `order_checkout()` |
| Xác minh webhook | `POST /webhooks/stripe` → `backend.py`: `handle_webhook()` → `_handle_webhook()` / `_handle_checkout_event()` |
| Tác vụ hậu thanh toán | bảng `outbox`, `run_worker()` chạy trong `tick()`; `app.py` có luồng nền `worker_loop()` mỗi 2 giây |

## Route

| Route | Ai gọi | Việc làm |
|---|---|---|
| `POST /api/orders/<mã đơn>/cancel` | Chủ đơn | Hủy đơn chưa trả tiền (`cancel_order`) |
| `POST /api/admin/check-payment` | Admin | "Kiểm tra với Stripe" cho một đơn (`admin_check_payment`), chỉ ghi kết quả kiểm tra |
| `POST /api/admin/stock` | Admin | Xem tồn kho theo SKU; có `sku` + `on_hand` thì đặt lại tồn (công cụ thử) |
| `/admin/orders?f=recon` | Admin | Bộ lọc "Cần đối soát" |
| `/admin/stock` | Admin | Trang Kho hàng |
| `/admin/orders/<mã đơn>?tab=workflow` | Admin | Thêm khối "Giữ hàng theo SKU" và "Đối soát thanh toán" (nội bộ cạnh Stripe) |

## File đã sửa

`backend.py`, `app.py`, `real_services.py`, `stub_apis.py`, `shop.html`, `admin.html`, `test_demo.py`, `test_real.py`.

## Thay đổi database

- Bảng mới `inventory(sku, on_hand, sold)`. SKU có dạng `mã sản phẩm|phiên bản`, ví dụ `GD01|Bạc`. Khởi tạo từ danh mục ở lần chạy đầu.
- Bảng mới `stock_holds(id, order_id, sku, quantity, state, created_at, check_at, updated_at, note)`. `state` gồm `HELD`, `CONSUMED`, `RELEASED`. `check_at` là thời điểm cần kiểm tra lại.
- Bảng `orders` thêm cột `canceled_at`, `canceled_by`, `cancel_reason`, `recon_result`, `recon_detail`, `recon_at`.
- Biến môi trường mới `STOCK_HOLD_S` (mặc định 1800 giây).

## Quy tắc đã cài

**Giữ hàng.** Có thể bán = tồn kho − hàng đang giữ. Hàng được giữ trong `start_payment()` ngay trước khi mở Stripe, tất cả SKU trong một transaction; thiếu một món thì không giữ món nào và trả về tên món thiếu. Đơn HIGH chưa giữ hàng; sau khi duyệt mới kiểm tra và giữ. Bấm lại trên cùng đơn dùng lượt giữ cũ. Webhook thành công chuyển `HELD` thành `CONSUMED` một lần. Đến `check_at`, `check_holds()` hỏi trạng thái thanh toán trước: phiên còn mở thì giữ tiếp; Stripe đang xử lý hoặc không liên lạc được thì giữ hàng và đưa đơn sang đối soát; phiên hết hạn chưa thu tiền thì trả hàng về kho. Đơn của kịch bản demo dựng sẵn (`/demo`, bộ dữ liệu A–D) không trừ kho.

**Hủy đơn.** Chỉ chủ đơn. Phiên Stripe còn mở thì đóng phiên và xác nhận chưa nhận tiền rồi mới ghi `CANCELED` và trả hàng. Stripe vừa ghi nhận giao dịch thì trả về `confirming`; không liên lạc được thì trả về `unknown`; cả hai trường hợp đơn giữ nguyên. Hủy lần hai trả về `already`, không thêm sự kiện. Timeline lưu thời điểm, người thao tác, lý do.

**Đối soát.** Đơn vào hàng đợi khi khách đã quay về mà Webhook chưa tới, khi Stripe đang xử lý, không liên lạc được, hoặc lệch số tiền. Nút "Kiểm tra với Stripe" trả một trong bốn kết quả theo bảng trong yêu cầu. Kết quả kiểm tra không ghi PAID; chỉ Webhook hợp lệ mới ghi.

## Dữ liệu test và kết quả

"Máy chủ giả" là `stub_apis.py` trả lời theo định dạng của Stripe và Cloudflare. **Stripe Sandbox thật: Chưa chạy trên môi trường thật.** Cấu hình còn thiếu: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` trong `.env` trên máy chủ project, `stripe listen` đang chạy, và thao tác trả tiền bằng thẻ thử 4242.

| Ca | Mong đợi | Thực tế | Chạy với |
|---|---|---|---|
| GD01 (Bạc) còn 1, Alice và Bob cùng đặt 1 | Chỉ một đơn giữ được hàng, người kia được báo tên món thiếu | Đạt (1a, 1b) | Giả lập |
| Gửi lại webhook của đơn đó 2 lần | Số bán vẫn 1 | Đạt (1c) | Giả lập |
| Phiên hết hạn, chưa thu tiền | Số có thể bán về 1 | Đạt (1d) | Giả lập |
| Đơn HIGH | Chưa giữ hàng khi chờ duyệt; duyệt xong kiểm tra lại tồn | Đạt (`test_demo.py`) | Giả lập |
| Đến hạn kiểm tra khi Stripe đang xử lý hoặc không liên lạc được | Giữ hàng, đơn sang đối soát | Đạt (`test_demo.py`, `test_real.py`) | Giả lập, máy chủ giả |
| Hộp xác nhận hủy | Có mã đơn, số tiền, 4 lý do, nút Xác nhận hủy, trạng thái "Đang kiểm tra thanh toán…" | Đạt (2a, 2e ở 390 px) | Giả lập |
| Alice mở trang thanh toán ở tab A, hủy ở tab B | Phiên tab A không trả tiền được | Đạt (2c giả lập; 2f phiên Stripe thành `expired`) | Giả lập, máy chủ giả |
| Bấm hủy hai lần | Một sự kiện hủy | Đạt (2b) | Giả lập |
| Giao dịch thành công trong lúc đang hủy | Không vừa Đã thanh toán vừa Đã hủy | Đạt (2d, `test_real.py`) | Giả lập, máy chủ giả |
| Đơn 444.300 ₫ đã trả, webhook chưa đến | Admin thấy "Chờ webhook", khách không có link thứ hai | Đạt (3a–3d) | Máy chủ giả |
| Webhook hợp lệ được xử lý | Đơn rời hàng đợi, nhật ký có cả đối soát và webhook | Đạt (3e) | Máy chủ giả |
| Lệch số tiền | "Cần kiểm tra thủ công", hiện chênh lệch, chưa giao hàng | Đạt (3f) | Máy chủ giả |
| Stripe timeout / phiên còn mở | "Chưa có kết quả cuối" / "Chờ khách thanh toán" | Đạt (3g, 3h) | Máy chủ giả |

## Luồng cũ chạy lại

`test_demo.py` 39/39, `test_real.py` 11/11. Sáu bộ kiểm thử trình duyệt: 41 + 43 + 41 + 45 + 33 + 20 = 223/223, gồm thanh toán lại, giá sale, phí giao, hỗ trợ theo đơn, dashboard, tab Workflow, khởi động lại.
