"""Sáu Internet Service bên ngoài, ở dạng GIẢ LẬP chạy trong máy.

Mỗi lớp bắt chước đúng "hợp đồng" của dịch vụ thật mà nhóm đã đặc tả:
tên thao tác, cách xác thực, dữ liệu gửi/nhận và kiểu lỗi. Không lớp nào
gọi ra Internet, nên demo không cần API key thật và không phát sinh chi phí.

Muốn nối dịch vụ thật: giữ nguyên tên hàm, thay phần thân bằng lời gọi HTTP.
"""
import hashlib
import hmac
import json
import re
import secrets
import time


class ServiceError(Exception):
    """Dịch vụ ngoài lỗi 5xx hoặc timeout."""


class ServiceUnavailable(ServiceError):
    """Dịch vụ từ chối tạm thời (HTTP 503) TRƯỚC khi xử lý: biết chắc yêu cầu chưa được thực hiện, thử lại an toàn."""


class AuthError(Exception):
    """Sai API key hoặc token."""


class ServiceHub:
    """Công tắc lỗi và nhật ký gọi API dùng chung cho cả sáu dịch vụ."""

    def __init__(self):
        self.faults = set()      # tên các dịch vụ đang bị "tắt" để thử nhánh lỗi
        self.faults_once = set() # dịch vụ sẽ lỗi đúng một lần gọi kế tiếp (dùng cho trang demo)
        self.calls = []          # nhật ký: ai gọi dịch vụ nào, xác thực kiểu gì

    def call(self, key, service, action, auth):
        down = key in self.faults
        if key in self.faults_once:
            self.faults_once.discard(key)
            down = True
        self.calls.append({"time": time.time(), "service": service, "action": action,
                           "auth": auth, "ok": not down})
        if key + "_503" in self.faults:
            self.calls[-1]["ok"] = False
            raise ServiceUnavailable(f"{service} tạm từ chối (HTTP 503), yêu cầu chưa được thực hiện")
        if down:
            raise ServiceError(f"{service} không phản hồi (timeout giả lập)")


# ---------------------------------------------------------------- 1. AI Service
STOPWORDS = set("giup minh tim duoi trieu can cho mot toi muon mua nao tot nhat loai voi gia khoang tam tren den de di va la co "
                "khong nhung hay duoc ban em anh chi nhe a oi xin hoi tu van chon san pham".split())


def strip_accents(text):
    import unicodedata
    text = unicodedata.normalize("NFD", str(text).lower().replace("đ", "d"))
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


CAT_WORDS = {"tai nghe": "Tai nghe", "ban phim": "Bàn phím", "lot chuot": "Phụ kiện", "chuot": "Chuột",
             "sac": "Sạc & cáp", "cap": "Sạc & cáp", "gia do": "Phụ kiện", "phu kien": "Phụ kiện",
             "gia dung": "Gia dụng", "am sieu toc": "Gia dụng", "noi com": "Gia dụng", "may xay": "Gia dụng", "noi chien": "Gia dụng",
             "bo noi": "Gia dụng", "may hut bui": "Gia dụng", "quat": "Gia dụng", "ban ui": "Gia dụng", "do bep": "Gia dụng"}


def shortlist(question, catalog):
    """Truy vấn danh mục trước khi hỏi AI: đọc ngân sách + loại sản phẩm trong câu hỏi, lọc hàng còn bán và chấm độ khớp.

    Trả về (ngân sách hoặc None, tập danh mục khách nêu, [(điểm, sản phẩm)] đã lọc theo ngân sách/danh mục/tồn kho).
    """
    text = strip_accents(question)
    budget = None
    match = re.search(r"duoi\s+(\d+(?:[.,]\d+)?)\s*(trieu|tr)\b", text)
    if match:
        budget = float(match.group(1).replace(",", ".")) * 1_000_000
    else:
        match = re.search(r"duoi\s+(\d{2,4})\s*(k|nghin|ngan)\b", text)
        if match:
            budget = float(match.group(1)) * 1_000
    words = [w for w in re.findall(r"[a-z0-9]+", text) if len(w) > 2 and w not in STOPWORDS and not w.isdigit()]
    tokens = re.findall(r"[a-z0-9]+", text)
    phrases = {f"{a} {b}" for a, b in zip(tokens, tokens[1:]) if a not in STOPWORDS and b not in STOPWORDS}
    wanted = {cat for word, cat in CAT_WORDS.items() if re.search(rf"\b{word}\b", text)}   # khách nêu rõ loại thì chỉ xét loại đó
    if "lot chuot" in text:
        wanted.discard("Chuột")
    scored = []
    for product in catalog:
        if not product.get("in_stock", True) or (budget is not None and product["price"] > budget):
            continue
        if wanted and product.get("cat") not in wanted:
            continue
        haystack = strip_accents(f"{product['name']} {product.get('cat', '')} {product['feature']} {product.get('spec', '')}")
        score = sum(1 for w in set(words) if w in haystack)
        score += 2 * sum(1 for pair in phrases if pair in haystack)      # cụm hai từ khớp liền nhau được cộng điểm
        scored.append((score, product))
    scored.sort(key=lambda item: (-item[0], item[1]["price"]))
    return budget, wanted, scored


class OpenAIService:
    """Giả lập OpenAI: nhận câu hỏi + danh mục, trả JSON gợi ý tối đa 3 sản phẩm."""
    KEY = "ai"

    def __init__(self, hub, api_key):
        self.hub, self.api_key = hub, api_key

    def recommend(self, api_key, question, catalog):
        self.hub.call(self.KEY, "OpenAI", "POST /v1/responses (giả lập)", "Bearer API key")
        if api_key != self.api_key:
            raise AuthError("OpenAI: sai API key")
        budget, _, scored = shortlist(question, catalog)
        scored = [item for item in scored if item[0]]
        best = scored[0][0] if scored else 0
        picks = [product for score, product in scored if score >= 0.6 * best][:3]
        if not picks:
            return {"reply_text": "Mình chưa tìm thấy sản phẩm khớp yêu cầu trong danh mục hiện có.",
                    "recommended_product_ids": [], "reasons": {}}
        reasons = {}
        for p in picks:
            spec = p.get("spec") or p["feature"]
            reasons[p["id"]] = spec[0].upper() + spec[1:] + ("; nằm trong ngân sách bạn đưa ra" if budget else "")
        return {"reply_text": f"Mình gợi ý {len(picks)} sản phẩm đang có hàng phù hợp với nhu cầu của bạn.",
                "recommended_product_ids": [p["id"] for p in picks], "reasons": reasons}


# ---------------------------------------------------- 2. Security Service 1: Firebase
class FirebaseAuthService:
    """Giả lập Firebase Authentication: xác minh ID Token, trả UID và thông tin tài khoản."""
    KEY = "firebase"

    def __init__(self, hub):
        self.hub = hub
        now = time.time()
        self.users = {
            "idtoken-alice": {"uid": "u_alice", "email": "alice@example.com", "name": "Alice",
                              "created_at": now - 400 * 86400},
            "idtoken-bob": {"uid": "u_bob", "email": "bob@example.com", "name": "Bob",
                            "created_at": now - 2 * 3600},        # tài khoản mới tạo 2 giờ
            "idtoken-admin": {"uid": "u_admin", "email": "admin@goctech.example", "name": "Quản trị viên",
                              "created_at": now - 900 * 86400},   # quyền admin do backend cấp theo ADMIN_EMAILS
        }

    def new_customer(self, fresh=False, name=""):
        """Tạo thêm một khách demo; fresh=True là tài khoản vừa đăng ký 2 giờ trước."""
        n = len(self.users) + 1
        token = f"idtoken-khach-{n}"
        self.users[token] = {"uid": f"u_khach_{n}", "email": f"khach{n}@example.com", "name": name or f"Khách {n}",
                             "created_at": time.time() - (2 * 3600 if fresh else 200 * 86400)}
        return token

    def verify_id_token(self, id_token):
        self.hub.call(self.KEY, "Firebase Authentication", "verifyIdToken", "Admin SDK")
        return self.users.get(id_token)          # None = token không hợp lệ


# --------------------------------------------------- 3. Security Service 2: Turnstile
class TurnstileService:
    """Giả lập Cloudflare Turnstile Siteverify: token dùng một lần, phân biệt người và bot."""
    KEY = "turnstile"

    def __init__(self, hub, secret_key):
        self.hub, self.secret_key = hub, secret_key
        self.used = set()

    def siteverify(self, secret_key, token):
        self.hub.call(self.KEY, "Cloudflare Turnstile", "POST /siteverify", "Secret Key")
        if secret_key != self.secret_key:
            raise AuthError("Turnstile: sai Secret Key")
        if not token or not token.startswith("ts-ok") or token in self.used:
            return {"success": False, "error-codes": ["invalid-or-reused-token"]}
        self.used.add(token)
        return {"success": True}


# ------------------------------------------------------------ 4. Payment Service
class StripeService:
    """Giả lập Stripe: Checkout Session có Idempotency-Key, Webhook ký HMAC-SHA256, Refund."""
    KEY = "stripe"

    def __init__(self, hub, secret_key, webhook_secret):
        self.hub, self.secret_key, self.webhook_secret = hub, secret_key, webhook_secret
        self.sessions = {}       # idempotency_key -> session
        self.refunds = {}

    def create_checkout_session(self, api_key, *, order_id, amount, currency, idempotency_key):
        self.hub.call(self.KEY, "Stripe", "POST /v1/checkout/sessions", "Bearer Secret Key")
        if api_key != self.secret_key:
            raise AuthError("Stripe: sai Secret Key")
        if idempotency_key in self.sessions:     # gửi lại cùng khóa -> trả lại đúng phiên cũ
            return self.sessions[idempotency_key]
        token = secrets.token_hex(6)
        session = {"id": f"cs_test_{token}", "payment_intent": f"pi_test_{token}",
                   "url": f"https://checkout.stripe.test/pay/cs_test_{token}",
                   "amount": amount, "currency": currency, "metadata": {"order_id": order_id}}
        self.sessions[idempotency_key] = session
        return session

    def create_refund(self, api_key, *, payment_intent, idempotency_key):
        self.hub.call(self.KEY, "Stripe", "POST /v1/refunds", "Bearer Secret Key")
        if api_key != self.secret_key:
            raise AuthError("Stripe: sai Secret Key")
        self.refunds.setdefault(idempotency_key, {"id": f"re_test_{secrets.token_hex(4)}",
                                                  "payment_intent": payment_intent})
        return self.refunds[idempotency_key]

    # Phía Stripe: tạo sự kiện và chữ ký để gửi về endpoint Webhook của sàn
    def build_event(self, event_type, session, *, amount=None):
        succeeded = event_type == "payment_intent.succeeded"
        amount = session["amount"] if amount is None else amount
        return {"id": f"evt_test_{secrets.token_hex(6)}", "type": event_type,
                "data": {"object": {"id": session["payment_intent"], "amount": amount,
                                    "amount_received": amount if succeeded else 0,
                                    "currency": session["currency"],
                                    "status": "succeeded" if succeeded else "requires_payment_method",
                                    "metadata": session["metadata"]}}}

    def sign(self, raw_body, *, secret=None, timestamp=None):
        timestamp = int(time.time()) if timestamp is None else int(timestamp)
        signed_payload = f"{timestamp}.{raw_body}".encode("utf-8")
        digest = hmac.new((secret or self.webhook_secret).encode("utf-8"),
                          signed_payload, hashlib.sha256).hexdigest()
        return f"t={timestamp},v1={digest}"


# ----------------------------------------------------------- 5. Shipping Service
class GHNService:
    """Giả lập Giao Hàng Nhanh: tạo vận đơn theo client_order_code, tra cứu để đối soát."""
    KEY = "ghn"

    def __init__(self, hub, token, shop_id):
        self.hub, self.token, self.shop_id = hub, token, shop_id
        self.orders = {}         # client_order_code -> vận đơn

    def _auth(self, token, shop_id):
        if token != self.token or shop_id != self.shop_id:
            raise AuthError("GHN: sai Token hoặc ShopId")

    def create_order(self, token, shop_id, *, client_order_code, to_name, to_address, cod_amount):
        self.hub.call(self.KEY, "GHN", "POST /shipping-order/create", "Token + ShopId")
        self._auth(token, shop_id)
        if client_order_code not in self.orders:
            self.orders[client_order_code] = {
                "order_code": "GHN" + secrets.token_hex(4).upper(), "to_name": to_name,
                "to_address": to_address, "cod_amount": cod_amount,
                "client_order_code": client_order_code, "created_at": time.time()}
        if "ghn_lost_response" in self.hub.faults:
            # Tình huống khó: GHN đã tạo vận đơn nhưng phản hồi bị rớt giữa đường
            raise ServiceError("GHN: mất kết nối trước khi nhận phản hồi")
        return self._with_status(self.orders[client_order_code])

    # Giả lập hành trình giao hàng: vài giây sau khi tạo vận đơn thì chuyển "đang giao", rồi "đã giao"
    DELIVERING_AFTER_S = 8
    DELIVERED_AFTER_S = 16

    def _with_status(self, order):
        elapsed = time.time() - order["created_at"]
        status = ("delivered" if elapsed >= self.DELIVERED_AFTER_S
                  else "delivering" if elapsed >= self.DELIVERING_AFTER_S else "ready_to_pick")
        return {**order, "status": status}

    def lookup(self, token, shop_id, client_order_code):
        self.hub.call(self.KEY, "GHN", "POST /shipping-order/detail-by-client-code", "Token + ShopId")
        self._auth(token, shop_id)
        order = self.orders.get(client_order_code)
        return self._with_status(order) if order else None


# ------------------------------------------------------- 6. Notification Service
class BrevoService:
    """Giả lập Brevo: gửi email giao dịch theo mẫu, chống gửi lặp theo mã tác vụ."""
    KEY = "brevo"

    def __init__(self, hub, api_key):
        self.hub, self.api_key = hub, api_key
        self.sent = {}           # task_id -> email đã gửi

    def send_email(self, api_key, *, to, template, params, task_id):
        self.hub.call(self.KEY, "Brevo", "POST /v3/smtp/email", "api-key header")
        if api_key != self.api_key:
            raise AuthError("Brevo: sai api-key")
        if task_id not in self.sent:
            self.sent[task_id] = {"messageId": f"<{secrets.token_hex(6)}@brevo.test>", "to": to,
                                  "template": template, "params": params, "time": time.time()}
        return self.sent[task_id]


def dumps(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))
