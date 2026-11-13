# Góc Tech — prototype Bài 1, Chương 6 (Web Service, API)

Sàn thương mại điện tử mẫu để trình bày Automation Workflow nối 6 Internet Services.
Chỉ cần Python 3.9+ (không cài thêm thư viện). Mọi dịch vụ bên ngoài đều là **giả lập chạy trong máy**.

## Chạy

    python app.py              # mở http://localhost:8000 (dùng localhost, không dùng 127.0.0.1)
    python test_demo.py        # 32 kiểm thử workflow (giả lập)
    python test_real.py        # 8 kiểm thử phần nối API thật, chạy qua máy chủ giả stub_apis.py

Khi khởi động, `app.py` in ra từng tích hợp đang ở trạng thái nào: API THẬT, CHỜ CẤU HÌNH hay GIẢ LẬP.
Dữ liệu nằm trong bộ nhớ: tắt `app.py` là mất đơn. Giỏ hàng, tài khoản đang đăng nhập, form giao hàng và
lựa chọn "Mua ngay" lưu ở trình duyệt nên tải lại trang vẫn còn.

## Nối API thật (tùy chọn)

Sao chép `.env.example` thành `.env` rồi tự điền khóa. `.env` đã nằm trong `.gitignore`. Biến nào để trống thì dịch vụ đó
chạy giả lập và giao diện ghi nhãn "Giả lập".

| Biến | Dùng cho | Ghi chú |
|---|---|---|
| `OPENAI_API_KEY`, `OPENAI_MODEL` | Tư vấn sản phẩm (Responses API, trả JSON có cấu trúc) | Chỉ backend đọc. Phải điền cả tên model |
| `TURNSTILE_SITE_KEY` | Hiện widget ở bước Kiểm tra đơn | Mã công khai |
| `TURNSTILE_SECRET_KEY` | Backend gọi Siteverify | Chỉ backend đọc |
| `TURNSTILE_EXPECTED_HOSTNAME` | Đối chiếu `hostname` Cloudflare trả về | Mặc định `localhost` |
| `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET` | Checkout Session thật + xác minh Webhook | Chỉ nhận khóa Sandbox `sk_test_…` |
| `ADMIN_EMAILS`, `ADMIN_PASSCODE` | Ai được vào admin; mã truy cập thêm (tùy chọn) | Mặc định `admin@goctech.example` |
| `PORT`, `APP_BASE_URL` | Cổng và địa chỉ để Stripe chuyển khách quay về | Mặc định 8000 |

Stripe gửi Webhook tới `POST /webhooks/stripe`. Khi chạy local cần Stripe CLI chuyển tiếp:
`stripe listen --forward-to localhost:8000/webhooks/stripe`, rồi lấy `whsec_…` nó in ra điền vào `STRIPE_WEBHOOK_SECRET`.

Trang `/demo` và bảng kỹ thuật luôn chạy giả lập, kể cả khi đã điền khóa thật.

## Admin

Mở `http://localhost:8000/admin`, chọn tài khoản **Quản trị viên**. Quyền do backend kiểm tra: email tài khoản phải nằm trong
`ADMIN_EMAILS`. Muốn cấp quyền cho tài khoản demo khác (ví dụ Alice) thì thêm `alice@example.com` vào `ADMIN_EMAILS` rồi chạy lại.
Tài khoản khách mở `/admin`, `/demo` hoặc gọi `/api/admin/*` đều bị từ chối (401/403). Đăng nhập là giả lập Firebase, không có
mật khẩu; nếu muốn chặn thêm thì đặt `ADMIN_PASSCODE`.

## Các màn hình

| Đường dẫn | Nội dung |
|---|---|
| `/` | Cửa hàng: `#/` trang chủ, `#/dm` danh mục + bộ lọc, `#/p/SP01` chi tiết, `#/cart`, `#/checkout`, `#/pay/ORD-001` (cổng thanh toán giả lập), `#/result/ORD-001`, `#/orders`, `#/order/ORD-001`, `#/support` |
| `/admin` | Tổng quan: 4 chỉ số, bảng Đơn cần xử lý, trạng thái tích hợp |
| `/admin/orders` | Danh sách đơn: tìm theo mã, lọc trạng thái (`?q=&f=&p=`), phân trang |
| `/admin/orders?f=hold` | Hàng chờ duyệt |
| `/admin/orders/ORD-001` | Chi tiết đơn: sản phẩm, khối Cần xử lý (Duyệt/Từ chối, Đối soát vận đơn/Thử tạo lại), xem trước email, nhật ký |
| `/demo` | 6 tình huống: màn hình khách cạnh kết quả cần kiểm tra + nhật ký sự kiện |
| `/admin/tech` | Bảng kỹ thuật: 10 kịch bản backend, tắt thử từng dịch vụ, nhật ký gọi API |

## Tính năng mua sắm (bản v6)

| Tính năng | Mở ở đâu | Ghi chú |
|---|---|---|
| Sale Gia Dụng 30% | Trang chủ (thanh thông báo, 3 banner, khu "Deal gia dụng hôm nay"), `#/khuyen-mai/gia-dung-30`, `…/do-bep`, `…/cham-soc-nha-cua` | Giá sale do backend tính (`unit_price`, `quote`). Mốc thời gian đặt bằng `SALE_GIA_DUNG_START` / `SALE_GIA_DUNG_END` trong `.env` (giờ Việt Nam, ví dụ `2026-11-20 23:59`); không đặt thì kết thúc sau 7 ngày kể từ lúc chạy |
| Thanh toán tiếp / thanh toán lại | Danh sách đơn `#/orders`, chi tiết đơn `#/order/ORD-001`, màn hình quay về từ Stripe | API `POST /api/orders/<mã đơn>/checkout` và `…/status`. Backend đối soát phiên với Stripe trước khi cấp link |
| So sánh sản phẩm | Nút "So sánh" trên thẻ và trang chi tiết hàng gia dụng, bảng ở `#/so-sanh` | Tối đa 3 món cùng loại; thông số lấy từ `specs` trong danh mục |
| Phí và thời gian giao | Khối "Giao đến" ở trang sản phẩm, giỏ hàng, bước Thông tin giao hàng | **Bảng phí ước tính nội bộ** (`AREAS` trong `backend.py`): phí cơ bản theo khu vực cho 3 kg đầu, mỗi kg thêm 5.000 ₫. Chưa lấy phí từ GHN vì GHN đang giả lập |
| Hỗ trợ theo đơn | Nút "Cần hỗ trợ?" dưới timeline ở chi tiết đơn; admin: `/admin/support` | API `POST /api/orders/<mã đơn>/tickets`. Chỉ ghi nhận yêu cầu, không đổi trạng thái thanh toán hay giao hàng |

Công cụ thử cho admin: nút "Cho phiên thanh toán hết hạn" ở chi tiết đơn đang chờ thanh toán; ô "Bảng phí giao hàng" ở `/admin/tech`
để giả lập lúc không tính được phí; `POST /api/admin/campaign {"ends_in": 5}` để cho chiến dịch kết thúc sau 5 giây.

## Sáu tình huống ở /demo

1. **LOW Risk, thanh toán thành công** — PAID đúng một lần, email xác nhận, vận đơn GHN, có mã GHN rồi mới có email thứ hai.
2. **HIGH Risk** — khách thấy "Đơn đang được kiểm tra", chưa có phiên Stripe; bấm *Admin: Duyệt đơn* thì khách thanh toán tiếp được.
3. **Xác minh bảo mật lỗi** — "Chưa thể xác minh. Vui lòng thử lại.", giữ giỏ và thông tin, không tạo đơn, không tạo phiên Stripe.
4. **Thanh toán chưa rõ kết quả** — "Đang xác nhận thanh toán" + mã đơn + nút *Kiểm tra trạng thái*; bấm *Cho kết quả thanh toán về ngay* để kết thúc.
5. **Sự kiện thanh toán đến hai lần** — nhật ký ghi "Trùng, bỏ qua"; vẫn 1 vận đơn, 2 email. Nút *Gửi lại sự kiện thanh toán* để thử thêm.
6. **GHN tạm lỗi sau khi đã trả tiền** — "Đã thanh toán · Đang chuẩn bị giao", đơn vẫn PAID; *Khôi phục GHN và đối soát vận đơn* (hoặc làm ở /admin/orders) thì mới có mã và email thứ hai.

"Đặt lại demo" chỉ xóa đơn, email, vận đơn, nhật ký của các phiên demo; đơn khách tự đặt ở cửa hàng được giữ.

## Dữ liệu mẫu để bấm thử

- Tài khoản: **Alice** (khách quen, LOW), **Bob** (tài khoản mới 2 giờ; mua SP03 Aurea Studio Pro 6.490.000 ₫ sẽ thành HIGH). Không có mật khẩu.
- Tính tiền: `#/p/SP05` Bàn phím cơ Kova K75, màu Đen, số lượng 2, giao TP.HCM → 1.290.000 ₫ × 2 + 25.000 ₫ phí giao − 100.000 ₫ giảm giá = **2.505.000 ₫** (giống nhau ở giỏ, checkout, chi tiết đơn, admin).
- Sale: `#/p/GD01` Ấm siêu tốc 599.000 ₫ → **419.300 ₫**; giao TP.HCM 25.000 ₫ → tổng **444.300 ₫**; giao Hà Nội 32.000 ₫ → 451.300 ₫; thêm nồi chiên GD04 thì phí cả giỏ là 45.000 ₫.
- So sánh: ba nồi chiên GD04, GD09, GD13 (GD09 thiếu "Kích thước" để thấy "Chưa có thông tin").
- Không giao được: chọn khu vực "Huyện đảo".
- Biến thể: SP05 Trắng ngà +100.000 ₫; SP01 Xanh rêu và SP12 Trắng hết hàng; SP14 hết hàng toàn bộ.
- AI: gõ "tai nghe chống ồn dưới 2 triệu" → JBL Tune Beam, Sony WF-C700N.

## Quy tắc chính trong backend (`backend.py`)

- `quote()` là nơi duy nhất tính tiền: giá sau khuyến mãi, giảm 100.000 ₫ cho đơn từ 2.500.000 ₫, phí giao theo khu vực và khối lượng cả giỏ. Giao diện chỉ hiển thị số backend trả về. Nếu tổng khách đang xem khác tổng hiện tại, backend yêu cầu xác nhận lại trước khi tạo đơn.
- Bảo mật fail-closed: Firebase hoặc Turnstile lỗi thì dừng, không tạo đơn. Turnstile thật còn kiểm tra `hostname` và `action=checkout`.
- AI: backend lọc danh mục trước, chỉ gửi mã, tên, giá, tồn kho, thông số; mã AI trả về phải có trong tập đã lọc.
- Rủi ro HIGH: tài khoản < 24 giờ và đơn > 5.000.000 ₫; ≥ 3 lần thanh toán lỗi/30 phút; ≥ 5 đơn/10 phút.
- Chỉ Webhook có chữ ký HMAC hợp lệ mới ghi PAID; `processed_events` chặn sự kiện trùng; outbox + worker tạo vận đơn và email.
- Vận đơn lỗi không tự tạo lại: chuyển "cần xử lý", phải đối soát theo mã đơn trước, chỉ khi GHN xác nhận chưa có mới cho tạo lại.

## Bản v7: workflow chịu lỗi, tab Workflow, Tổng quan vận hành

- **Lưu bền**: đơn, lần thanh toán, sự kiện webhook, tác vụ sau thanh toán và log nằm trong `goctech.db` (SQLite, cạnh `app.py`; đổi bằng `DB_PATH`). Tắt rồi chạy lại `python app.py` thì dữ liệu còn nguyên, worker chạy tiếp tác vụ còn chờ. Muốn bắt đầu sạch: xóa `goctech.db`.
- **Worker nền** chạy mỗi 2 giây. Lỗi biết chắc chưa thực hiện (ví dụ GHN 503) thì thử lại tối đa 3 lần, giãn cách theo `TASK_RETRY_DELAY_S` (mặc định 5 giây). Lỗi chưa rõ kết quả (timeout) chuyển "Cần đối soát".
- **Tab Workflow**: `/admin/orders/<mã đơn>?tab=workflow`. Mỗi bước có thời gian, dịch vụ, nhãn môi trường (thật hay giả lập), kết quả, mã tham chiếu. Có khung "Bước đang kẹt" với nút Đối soát/Thử lại, bảng tác vụ, bảng lần thanh toán và sự kiện webhook. Đơn giả lập có nút "Gửi lại sự kiện thanh toán" để thử Stripe gửi trùng.
- **Tổng quan vận hành**: `/admin`. Số liệu lấy từ `GET /api/admin/dashboard?from=YYYY-MM-DD&to=YYYY-MM-DD&env=all|real|sim` (chỉ admin, người khác nhận 403). Ngày tính theo giờ Việt Nam.
- **Dữ liệu thử cho dashboard**: nút "Tạo bộ dữ liệu mẫu A–D (giả lập)" ở cuối trang Tổng quan, hoặc `POST /api/admin/seed-dashboard`. Xóa bằng "Đặt lại demo" ở trang Demo workflow.
- **Lỗi giả lập mới** ở Bảng kỹ thuật: `ghn_503`, `brevo_503` (biết chắc chưa thực hiện, sẽ tự thử lại), bên cạnh `ghn`, `brevo` (timeout, cần đối soát).
- Log che email của khách (`a***@example.com`).
- Tư liệu báo cáo TV6: thư mục `bao_cao_tv6/` (sơ đồ workflow, bảng rủi ro, Câu 3, Câu 7, kịch bản demo, kết quả kiểm thử, ảnh minh chứng).

## Bản v8 (đợt 1 của 20 task): giữ hàng, hủy đơn, đối soát

- **Giữ hàng theo SKU** trước khi mở Stripe. Trang `/admin/stock` xem tồn kho, đang giữ, đã bán, có thể bán và đặt lại tồn để thử.
- **Khách hủy đơn chưa trả tiền**: nút "Hủy đơn" ở chi tiết đơn, `POST /api/orders/<mã đơn>/cancel`.
- **Hàng đợi đối soát**: `/admin/orders?f=recon`, nút "Kiểm tra với Stripe" (`POST /api/admin/check-payment`). Tab Workflow có khối so sánh nội bộ với Stripe.
- Chi tiết, thay đổi database và kết quả từng ca: `nghiem_thu/DOT_1_TASK_1_2_3.md`.

## Thật và giả lập

Luôn thật: máy chủ HTTP, cơ sở dữ liệu SQLite (trong bộ nhớ), logic workflow, tính tiền, chấm rủi ro, kiểm tra chữ ký
HMAC-SHA256, idempotency, outbox/worker, phân quyền khách và admin.

Đã nối, chạy thật khi có cấu hình (`real_services.py`): OpenAI, Cloudflare Turnstile, Stripe Sandbox.
Luôn giả lập (`services.py`): Firebase Authentication, GHN, Brevo (email chỉ được ghi lại, không gửi đi).
