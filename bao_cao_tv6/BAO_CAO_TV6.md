# Tư liệu báo cáo Bài 1 · TV6 — Automation Workflow chịu lỗi (Góc Tech v7)

Tài liệu này mô tả đúng những gì code trong `backend.py`, `app.py`, `admin.html` đang làm. Mọi chỗ ghi "Giả lập" nghĩa là dịch vụ chạy trong máy, không gọi ra ngoài.

## 1. Dịch vụ nào là API thật, dịch vụ nào giả lập

| Dịch vụ | Vai trò trong workflow | Trạng thái trong prototype |
|---|---|---|
| OpenAI (Responses API) | Tư vấn sản phẩm | **API thật** khi có `OPENAI_API_KEY` và `OPENAI_MODEL` trong `.env`; để trống thì giả lập |
| Cloudflare Turnstile (Siteverify) | Chặn bot ở bước đặt đơn | **API thật** khi có `TURNSTILE_SITE_KEY` và `TURNSTILE_SECRET_KEY`; để trống thì giả lập |
| Stripe Checkout + Webhook | Thanh toán | **Stripe Sandbox** (chỉ nhận khóa `sk_test_…`) khi có `STRIPE_SECRET_KEY` và `STRIPE_WEBHOOK_SECRET`; để trống thì giả lập |
| Firebase Authentication | Đăng nhập, xác minh ID Token | Giả lập |
| GHN | Tạo và đối soát vận đơn; phí giao là bảng ước tính nội bộ | Giả lập |
| Brevo | Email xác nhận thanh toán, email mã vận đơn | Giả lập |

Trên giao diện admin, mỗi dòng trong tab Workflow có nhãn môi trường riêng (ví dụ "Stripe Sandbox", "Stripe giả lập", "Turnstile thật", "GHN giả lập"). Mục "Tích hợp dịch vụ" ở trang Tổng quan ghi "Đã nối", "Chờ cấu hình" hoặc "Giả lập" cho từng dịch vụ.

## 2. Sơ đồ Automation Workflow

Hình: `so_do_workflow_tv6.png` (cùng thư mục).

Diễn giải theo đúng thứ tự trong hàm `_checkout`, `handle_webhook`, `run_worker`:

1. Khách bấm "Đặt hàng và thanh toán". Trình duyệt gọi `POST /api/shop/checkout`.
2. Firebase Authentication xác minh ID Token. Token sai hoặc hết hạn: dừng, không tạo đơn, khách đăng nhập lại.
3. Backend tính lại giá, khuyến mãi và phí giao. Tổng tiền khác số khách đang xem, hoặc chưa tính được phí giao: không tạo đơn, khách xác nhận lại.
4. Cloudflare Turnstile Siteverify. Không qua, hoặc Cloudflare lỗi hay quá thời gian: dừng (fail-closed), không tạo đơn, không gọi Stripe.
5. Chấm rủi ro bằng 3 quy tắc rồi tạo đơn. Điểm là số quy tắc bị kích hoạt; từ 1 trở lên là HIGH.
   - Tài khoản tạo dưới 24 giờ và đơn trên 5.000.000 ₫.
   - Từ 3 lần thanh toán thất bại trong 30 phút.
   - Từ 5 đơn trong 10 phút.
6. LOW: tạo Stripe Checkout Session, đơn ở trạng thái chờ thanh toán. HIGH: giữ đơn (ON_HOLD), chưa tạo Session, chờ admin duyệt hoặc từ chối. Duyệt xong khách bấm "Tiếp tục thanh toán" trên cùng mã đơn.
7. Stripe gọi `POST /webhooks/stripe`. Backend kiểm chữ ký HMAC, chống trùng theo Event ID, khớp mã đơn, phiên và số tiền.
   - Chữ ký sai: HTTP 400, đơn và hàng đợi tác vụ giữ nguyên.
   - Sự kiện đã xử lý được gửi lại: ghi "Đã nhận lại sự kiện, không phát sinh tác vụ mới".
   - Khách quay về trước khi Webhook tới: trang kết quả ghi "Đang xác nhận thanh toán", chưa ghi PAID.
8. Webhook hợp lệ lần đầu: ghi PAID một lần và xếp hai tác vụ (tạo vận đơn, email xác nhận thanh toán) trong cùng một transaction.
9. Worker chạy nền xử lý tác vụ. GHN trả mã vận đơn thì mới xếp tác vụ email mã vận đơn.
   - GHN trả lỗi 503 (biết chắc chưa tạo): tự thử lại tối đa 3 lần, có giãn cách.
   - GHN quá thời gian (chưa rõ đã tạo hay chưa): chuyển "Cần đối soát". Admin bấm Đối soát rồi mới Thử lại. Đơn vẫn PAID.
10. Trạng thái nằm trong SQLite (`goctech.db`): `orders`, `payment_attempts`, `processed_events`, `outbox`, `events`. Tắt rồi chạy lại `app.py`, worker tiếp tục các tác vụ còn chờ.

Mỗi đơn có bốn trạng thái tách riêng: duyệt, thanh toán, giao hàng, thông báo.

## 3. Bảng rủi ro bảo mật

| Rủi ro | Tác động | Cách xử lý trong code | Ca kiểm thử minh chứng |
|---|---|---|---|
| Lộ khóa API (OpenAI, Turnstile Secret, Stripe Secret, Webhook Secret) | Người khác dùng khóa để gọi API, tạo phiên thanh toán hoặc giả Webhook | Khóa chỉ nằm trong `.env` ở backend, `.gitignore` loại `.env` và `goctech.db`. Frontend chỉ nhận Site Key (mã công khai). Chỉ nhận khóa Stripe `sk_test_…`. Màn hình và terminal chỉ in trạng thái đã che. Log che email khách, không ghi ID Token | `test_real.py`: cấu hình gửi cho trình duyệt không chứa secret; log không chứa Turnstile Secret Key. D16: log Workflow che email khách |
| Webhook giả (không có chữ ký hợp lệ) | Đơn bị ghi "đã thanh toán" dù chưa thu tiền | Kiểm chữ ký HMAC với `STRIPE_WEBHOOK_SECRET` trước khi đọc nội dung; sai thì trả 400 và không đổi gì | W5: gửi Webhook ký bằng khóa khác, kết quả HTTP 400, đơn vẫn chờ thanh toán, không có tác vụ |
| Webhook gửi lại hoặc đến trễ | Ghi PAID hai lần, tạo hai vận đơn, gửi hai email | Bảng `processed_events` khóa theo Event ID; `paid_at` chỉ ghi một lần; mỗi tác vụ là một dòng duy nhất trong `outbox` | W4, D5, D16: gửi lại sự kiện 2–3 lần, vẫn một lần PAID và một bộ 3 tác vụ; số trên dashboard không đổi. W3: quay về trước Webhook thì chưa ghi PAID |
| Bot hoặc token xác minh giả ở bước đặt đơn | Đơn rác, dò thẻ | Turnstile Siteverify ở backend, kiểm `success`, `hostname`, `action=checkout`. Lỗi hay quá thời gian thì dừng (fail-closed) | `test_real.py`: token sai, sai hostname, Cloudflare ngừng phản hồi đều không tạo đơn và không gọi Stripe |
| Dịch vụ ngoài ngừng hoạt động sau khi đã thu tiền (GHN, Brevo) | Tạo trùng vận đơn nếu tự thử lại mù, hoặc khách trả tiền mà không có đơn giao | Tách thanh toán khỏi giao hàng. Lỗi biết chắc chưa làm thì thử lại có giới hạn; lỗi chưa rõ kết quả thì chuyển "Cần đối soát" cho admin | W8, W9: GHN lỗi sau khi trả tiền, đơn vẫn PAID, email xác nhận đã gửi, chưa có email mã vận đơn; admin đối soát rồi thử lại thì có mã vận đơn |
| Mất dữ liệu khi server khởi động lại | Mất đơn đã trả tiền, tác vụ dang dở không chạy tiếp | Lưu vào SQLite sau mỗi request; worker đọc lại `outbox` khi khởi động | R1–R4: tắt `app.py` khi tác vụ tạo vận đơn còn chờ, chạy lại thì đơn còn nguyên và workflow tự chạy tiếp |
| Khách sửa giá ở trình duyệt | Thanh toán thiếu tiền | Backend tự tính giá từ danh mục; tổng tiền khách gửi lên chỉ dùng để so, lệch thì yêu cầu xác nhận lại. Webhook so số tiền với đơn | `test_demo.py`: `test_sale_price_is_computed_by_backend…`, `test_price_change_needs_customer_confirmation…` |
| Khách gọi API quản trị | Xem đơn và số liệu của người khác | `/admin*` và `/api/admin/*` kiểm phiên admin ở backend | D0: gọi `GET /api/admin/dashboard` khi không phải admin, kết quả 403 |

## 4. Câu 3 và Câu 7

Hai đoạn dưới viết theo ý trong yêu cầu của TV6 (khóa nằm ở đâu, phần nào tự động, phần nào chuyển admin). Đối chiếu lại số câu với đề trước khi dán.

**Câu 3. Khóa API nằm ở đâu và được bảo vệ thế nào**

Toàn bộ khóa bí mật (OpenAI API Key, Turnstile Secret Key, Stripe Secret Key, Stripe Webhook Secret) nằm trong file `.env` cạnh `app.py` và chỉ backend đọc qua `config.py`. File `.env` không đưa lên Git. Trình duyệt chỉ nhận Turnstile Site Key, là mã công khai theo thiết kế của Cloudflare. Mọi lời gọi tới OpenAI, Cloudflare và Stripe đều đi từ backend; trình duyệt không gọi trực tiếp các API này. Backend chỉ chấp nhận khóa Stripe dạng `sk_test_…` nên prototype không thể thu tiền thật. Khi khởi động, terminal chỉ in trạng thái "đã nối" hay "chờ cấu hình". Log workflow che email của khách và không ghi ID Token; câu hỏi gửi sang OpenAI được lọc số điện thoại và email trước khi gửi.

**Câu 7. Phần nào tự động, phần nào chuyển cho admin**

Tự động: xác minh đăng nhập và Turnstile, tính giá và phí giao, chấm rủi ro, tạo phiên thanh toán cho đơn LOW, kiểm chữ ký và chống trùng Webhook, ghi PAID, tạo vận đơn, gửi email xác nhận thanh toán và email mã vận đơn, thử lại tối đa 3 lần khi biết chắc tác vụ chưa được thực hiện, chạy tiếp tác vụ dang dở sau khi khởi động lại.

Chuyển admin: duyệt hoặc từ chối đơn HIGH (lưu người duyệt, thời điểm, lý do); đối soát vận đơn hoặc email khi dịch vụ ngoài quá thời gian và chưa rõ kết quả; tác vụ đã thử đủ 3 lần vẫn lỗi; yêu cầu hỗ trợ của khách. Các đơn này xuất hiện ở bảng "Đơn cần xử lý" trên trang Tổng quan vận hành, nút "Xem đơn" mở thẳng tab Workflow tại bước đang kẹt.

## 5. Kịch bản demo khoảng 3 phút (dữ liệu giả lập)

Chuẩn bị trước khi quay: xóa file `goctech.db` (nếu có) để mã đơn bắt đầu từ ORD-001, chạy `python app.py`, mở `http://localhost:8000/admin`, bấm "Quản trị viên" (nhập passcode nếu đã đặt `ADMIN_PASSCODE`), kéo xuống cuối trang bấm "Tạo bộ dữ liệu mẫu A–D (giả lập)".

| Thời gian | Thao tác | Kết quả mong đợi |
|---|---|---|
| 0:00–0:30 | Ở `http://localhost:8000/admin`, chọn bộ lọc "Giả lập" | 4 thẻ: Đơn đã tạo 4, Đã thanh toán 2, Giá trị đơn đã thanh toán 1.372.300 ₫, Đơn cần xử lý 2. Donut: LOW 3, HIGH 1. Bảng "Đơn cần xử lý" có ORD-002 (Chờ duyệt HIGH) và ORD-003 (Cần đối soát vận đơn) |
| 0:30–1:15 | Ở dòng ORD-003 bấm "Xem đơn" (URL: `http://localhost:8000/admin/orders/ORD-003?tab=workflow`) | Mở tab Workflow tại khung "Bước đang kẹt: Tạo vận đơn". Bảng các bước: Firebase → Turnstile → Chấm rủi ro LOW → Stripe tạo phiên → Webhook ghi PAID → GHN "Cảnh báo, Đang kẹt" → Brevo gửi email xác nhận. Đầu đơn: "Đã thanh toán" cùng lúc với "Cần xử lý" |
| 1:15–1:40 | Bấm "Đối soát vận đơn", rồi "Thử tạo lại vận đơn" | Có mã vận đơn GHN. Sau vài giây bảng tác vụ có thêm "Email mã vận đơn: Thành công" |
| 1:40–2:10 | Mở `http://localhost:8000/admin/orders/ORD-001?tab=workflow`, bấm "Gửi lại sự kiện thanh toán" | Thêm một dòng Stripe Webhook "Trùng, không tạo tác vụ mới" với nội dung "Đã nhận lại sự kiện…". Bảng tác vụ vẫn 3 dòng. Cột "Nhận lại" tăng 1 |
| 2:10–2:40 | Mở `http://localhost:8000/admin/orders/ORD-002?tab=workflow`, bấm "Mở phần duyệt đơn", bấm "Duyệt cho thanh toán" | Trước khi duyệt: HIGH, điểm 1/3, lý do "tài khoản tạo 2 giờ trước và đơn trên 5.000.000đ", "Chưa tạo phiên thanh toán nào". Sau khi duyệt: lưu người duyệt và thời điểm, đơn chuyển sang chờ khách thanh toán |
| 2:40–3:00 | Về terminal bấm Ctrl+C, chạy lại `python app.py`, tải lại `http://localhost:8000/admin` (bấm lại "Quản trị viên" nếu trang yêu cầu đăng nhập) | Terminal in dòng "Database: … 4 đơn đã lưu". Dashboard giữ nguyên số liệu; "Đơn cần xử lý" còn 0 vì hai đơn trên đã được xử lý |

Phần chạy với API thật (nếu muốn quay thêm): đặt một đơn ở `http://localhost:8000` bằng tài khoản Alice, qua Turnstile, trả tiền trên trang Stripe bằng thẻ thử `4242 4242 4242 4242`, rồi mở tab Workflow của đơn đó. Các dòng sẽ mang nhãn "Turnstile thật" và "Stripe Sandbox".

## 6. Kết quả kiểm thử

Môi trường kiểm thử của Claude không có khóa thật. "Máy chủ giả" là `stub_apis.py`, trả lời theo đúng định dạng của Cloudflare và Stripe để kiểm luồng code thật trong `real_services.py`.

### 6.1 Workflow

| Ca | Kết quả | Chạy với gì |
|---|---|---|
| LOW + thanh toán thành công | Đạt (W1, W6, W7) | Stripe và Turnstile qua máy chủ giả |
| LOW + thanh toán thành công trên **Stripe Sandbox thật** | **Chưa chạy**. Bước còn thiếu: trên máy chủ project chạy `stripe listen --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,checkout.session.expired --forward-to localhost:8000/webhooks/stripe`, chép `whsec_…` vào `STRIPE_WEBHOOK_SECRET`, khởi động lại `app.py`, đặt đơn và trả bằng thẻ thử 4242 | Cần chủ project thao tác |
| HIGH → admin duyệt → thanh toán | Đạt (W2a, W2b) | Máy chủ giả |
| HIGH → duyệt → thanh toán trên **Stripe Sandbox thật** | **Chưa chạy**. Bước còn thiếu: như trên, dùng tài khoản mới tạo và đơn trên 5.000.000 ₫ | Cần chủ project thao tác |
| Webhook đến trễ | Đạt (W3) | Máy chủ giả |
| Stripe gửi lại sự kiện đã xử lý | Đạt (W4, D5, D16) | Máy chủ giả và giả lập |
| Webhook không có chữ ký hợp lệ | Đạt (W5) | Chữ ký tự tạo bằng khóa sai |
| GHN lỗi sau khi đã thanh toán | Đạt (W8, W9) | GHN giả lập |
| Khởi động lại khi còn tác vụ chờ | Đạt (R1–R4) | Database file, tắt và chạy lại `app.py` |

### 6.2 Dashboard

| Ca | Kết quả |
|---|---|
| Bộ dữ liệu A–D: 4 đơn tạo, 2 đã thanh toán, 2 cần xử lý, donut 3 LOW và 1 HIGH | Đạt (D2–D4) |
| Giá trị đơn đã thanh toán | Đạt với **1.372.300 ₫** (xem ghi chú) |
| Gửi lại sự kiện thanh toán của đơn A, các số giữ nguyên | Đạt (D5) |
| Bấm HIGH mở ra đơn B | Đạt (D6) |
| Bấm lỗi giao hàng dẫn tới đơn C và thấy bước đang kẹt | Đạt (D8, D9) |
| Đơn tạo 23:55 và thanh toán 00:05 giờ Việt Nam tính vào hai ngày khác nhau | Đạt (`test_demo.py`) |
| Kỳ không có dữ liệu | Đạt (D1, D12) |
| API lỗi: "Chưa tải được số liệu" và nút Thử lại | Đạt (D13) |
| Tài khoản không phải admin gọi API dashboard | Đạt, trả 403 (D0) |
| Bố cục 1.440 px và 390 px | Đạt (D14, D15) |

Ghi chú về số liệu:
- Đơn C trong bộ dữ liệu mẫu có tổng 928.000 ₫ (903.000 ₫ tiền hàng + 25.000 ₫ phí giao) vì không có giỏ hàng nào trong danh mục cho ra đúng 903.000 ₫. Tổng giá trị đã thanh toán vì vậy là 444.300 + 928.000 = 1.372.300 ₫, khác con số 1.347.300 ₫ trong yêu cầu.
- Mọi số trên dashboard khi chạy bộ dữ liệu mẫu đều là giả lập. Thẻ "Giá trị đơn đã thanh toán" tách riêng "Stripe Sandbox" và "Giả lập"; phần Stripe Sandbox chỉ khác 0 sau khi có đơn trả qua Stripe Sandbox thật.

### 6.3 Bộ kiểm thử v6 chạy lại

`test_demo.py` 35/35, `test_real.py` 8/8, bốn bộ kiểm thử giao diện cũ 170/170, bộ mới cho v7 33/33.

## 7. Ảnh minh chứng

Thư mục `minh_chung/`, tất cả chụp từ dữ liệu **giả lập** (tên file có đuôi `_gia_lap`):

1. `01_dashboard_1440_gia_lap.png`: trang Tổng quan vận hành.
2. `02_workflow_don_A_…`: đơn LOW thành công, có dòng sự kiện gửi lại.
3. `03_workflow_don_B_…`: đơn HIGH chờ duyệt, có điểm và lý do.
4. `04_workflow_don_C_…`: GHN lỗi, bước đang kẹt và nút Đối soát.
5. `05_danh_sach_loc_tu_dashboard_…`: danh sách đơn sau khi bấm từ dashboard.
6. `06_dashboard_390_gia_lap.png`: dashboard trên màn hình 390 px.

Ảnh minh chứng **API thật** phải chụp trên máy chủ project sau khi chạy phần API thật ở mục 5: tab Workflow của đơn đó (nhãn "Turnstile thật", "Stripe Sandbox"), và mục "Tích hợp dịch vụ" ở trang Tổng quan ghi "Đã nối".
