"""Lời gọi API THẬT tới OpenAI, Cloudflare Turnstile và Stripe Sandbox (chỉ dùng thư viện chuẩn).

Chỉ được dùng khi backend có đủ cấu hình trong .env. Không hàm nào ghi khóa hay token ra log.
"""
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from services import AuthError, ServiceError


class ServiceTimeout(ServiceError):
    """Dịch vụ không trả lời trong thời gian chờ."""


def _http(method, url, *, headers=None, data=None, timeout=8, name="Dịch vụ"):
    # Đặt User-Agent rõ ràng: Cloudflare (đứng trước cả Siteverify lẫn api.openai.com) hay chặn mặc định "Python-urllib".
    headers = {"User-Agent": "GocTech-Prototype/1.0 (+localhost)", "Accept": "application/json", **(headers or {})}
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except ValueError:
            return exc.code, {}
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(exc):
            raise ServiceTimeout(f"{name} hết thời gian chờ ({timeout} giây)") from None
        raise ServiceError(f"{name} không kết nối được") from None
    except ValueError:
        raise ServiceError(f"{name} trả dữ liệu không đọc được") from None


class RealTurnstile:
    KEY = "turnstile"

    def __init__(self, hub, secret_key, verify_url=""):
        self.hub, self.secret_key = hub, secret_key
        self.url = verify_url or "https://challenges.cloudflare.com/turnstile/v0/siteverify"

    def siteverify(self, token, remote_ip=None):
        self.hub.call(self.KEY, "Cloudflare Turnstile", "POST /turnstile/v0/siteverify (API thật)", "Secret Key")
        fields = {"secret": self.secret_key, "response": token or ""}
        # Chỉ gửi remoteip khi đó là IP công khai của khách. Chạy local thì backend chỉ thấy 127.0.0.1,
        # khác với IP mà Cloudflare đã cấp token, nên không gửi.
        try:
            if remote_ip and ipaddress.ip_address(remote_ip).is_global:
                fields["remoteip"] = remote_ip
        except ValueError:
            pass
        status, payload = _http("POST", self.url, data=urllib.parse.urlencode(fields).encode("utf-8"),
                                headers={"Content-Type": "application/x-www-form-urlencoded"},
                                timeout=6, name="Cloudflare Siteverify")
        # In kết quả ra terminal để dễ chẩn đoán. Không in secret hay token.
        print(f"[Turnstile] Siteverify HTTP {status}: success={payload.get('success')}, "
              f"error-codes={payload.get('error-codes')}, hostname={payload.get('hostname')}, action={payload.get('action')}", flush=True)
        if status >= 500 or "success" not in payload:      # ví dụ bị chặn 403: không có kết quả xác minh để tin
            raise ServiceError(f"Cloudflare Siteverify lỗi HTTP {status}")
        return payload


AI_INSTRUCTIONS = (
    "Bạn là trợ lý tư vấn của cửa hàng Góc Tech. Chỉ được chọn sản phẩm trong danh sách san_pham được cung cấp, "
    "dùng đúng mã id. Chọn tối đa 3 sản phẩm phù hợp nhất với cau_hoi; nếu không có sản phẩm nào phù hợp thì trả "
    "picks rỗng và nói rõ điều đó. reply là 1-2 câu tiếng Việt, không nêu giá hay tồn kho. Mỗi reason là một câu "
    "ngắn tiếng Việt giải thích vì sao sản phẩm hợp nhu cầu.")
AI_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["reply", "picks"],
    "properties": {
        "reply": {"type": "string"},
        "picks": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "reason"],
            "properties": {"id": {"type": "string"}, "reason": {"type": "string"}}}},
    },
}


class RealOpenAI:
    KEY = "ai"

    def __init__(self, hub, api_key, model, base_url="", timeout=20):
        self.hub, self.api_key, self.model, self.timeout = hub, api_key, model, timeout
        self.url = (base_url or "https://api.openai.com/v1").rstrip("/") + "/responses"

    def recommend(self, question, products):
        self.hub.call(self.KEY, "OpenAI", f"POST /v1/responses (API thật, model {self.model})", "Bearer API key")
        body = {"model": self.model, "instructions": AI_INSTRUCTIONS, "store": False,
                "input": json.dumps({"cau_hoi": question, "san_pham": products}, ensure_ascii=False),
                "text": {"format": {"type": "json_schema", "name": "goi_y_san_pham", "strict": True, "schema": AI_SCHEMA}}}
        status, payload = _http("POST", self.url, data=json.dumps(body).encode("utf-8"), timeout=self.timeout, name="OpenAI",
                                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        if status in (401, 403):
            raise AuthError("OpenAI từ chối API key")
        if status >= 400:
            message = str((payload.get("error") or {}).get("message", ""))[:160]
            raise ServiceError(f"OpenAI lỗi HTTP {status}" + (f": {message}" if message else ""))
        text = "".join(part.get("text", "") for item in payload.get("output", []) if item.get("type") == "message"
                       for part in item.get("content", []) if part.get("type") == "output_text")
        try:
            data = json.loads(text)
            return {"reply": str(data["reply"]), "picks": [{"id": str(p["id"]), "reason": str(p["reason"])} for p in data["picks"]]}
        except (ValueError, KeyError, TypeError):
            raise ServiceError("OpenAI trả về dữ liệu không đúng cấu trúc") from None


class RealStripe:
    KEY = "stripe"

    def __init__(self, hub, secret_key, api_base=""):
        self.hub, self.secret_key = hub, secret_key
        self.base = (api_base or "https://api.stripe.com").rstrip("/")

    def create_checkout_session(self, *, order_id, amount, currency, idempotency_key, success_url, cancel_url, email="", lines=None):
        """lines: [{"name", "unit_amount", "quantity"}] lấy từ bản ghi đơn; tổng các dòng phải đúng bằng amount."""
        self.hub.call(self.KEY, "Stripe", "POST /v1/checkout/sessions (Sandbox thật)", "Bearer Secret Key")
        if not lines or sum(l["unit_amount"] * l["quantity"] for l in lines) != amount:
            lines = [{"name": f"Đơn hàng {order_id} tại Góc Tech", "unit_amount": amount, "quantity": 1}]
        fields = {"mode": "payment", "client_reference_id": order_id, "metadata[order_id]": order_id,
                  "payment_intent_data[metadata][order_id]": order_id, "success_url": success_url, "cancel_url": cancel_url}
        for i, line in enumerate(lines):
            fields[f"line_items[{i}][quantity]"] = str(line["quantity"])
            fields[f"line_items[{i}][price_data][currency]"] = currency
            fields[f"line_items[{i}][price_data][unit_amount]"] = str(line["unit_amount"])   # VND không có đơn vị lẻ: gửi nguyên số đồng
            fields[f"line_items[{i}][price_data][product_data][name]"] = line["name"][:120]
        if email:
            fields["customer_email"] = email
        status, payload = _http("POST", self.base + "/v1/checkout/sessions", timeout=12, name="Stripe",
                                data=urllib.parse.urlencode(fields).encode("utf-8"),
                                headers={"Authorization": f"Bearer {self.secret_key}", "Idempotency-Key": idempotency_key,
                                         "Content-Type": "application/x-www-form-urlencoded"})
        if status in (401, 403):
            raise AuthError("Stripe từ chối Secret Key")
        if status >= 500 or status == 429:
            raise ServiceError(f"Stripe lỗi HTTP {status}")
        if status >= 400 or not payload.get("url"):
            message = str((payload.get("error") or {}).get("message", ""))[:160]
            raise AuthError(f"Stripe từ chối yêu cầu (HTTP {status})" + (f": {message}" if message else ""))
        return {"id": payload["id"], "url": payload["url"], "payment_intent": payload.get("payment_intent"),
                "expires_at": payload.get("expires_at")}

    def _session(self, method, path):
        status, payload = _http(method, self.base + path, timeout=10, name="Stripe",
                                data=b"" if method == "POST" else None,
                                headers={"Authorization": f"Bearer {self.secret_key}"})
        if status in (401, 403):
            raise AuthError("Stripe từ chối Secret Key")
        if status >= 400 or "status" not in payload:
            raise ServiceError(f"Stripe lỗi HTTP {status}")
        return {"id": payload.get("id"), "status": payload["status"], "payment_status": payload.get("payment_status"),
                "url": payload.get("url"), "expires_at": payload.get("expires_at"), "amount_total": payload.get("amount_total"),
                "currency": payload.get("currency"),
                "client_reference_id": payload.get("client_reference_id") or (payload.get("metadata") or {}).get("order_id")}

    def retrieve_session(self, session_id):
        """Đối soát: hỏi Stripe trạng thái thật của phiên (open | complete | expired)."""
        self.hub.call(self.KEY, "Stripe", "GET /v1/checkout/sessions/:id (Sandbox thật)", "Bearer Secret Key")
        return self._session("GET", "/v1/checkout/sessions/" + urllib.parse.quote(session_id, safe=""))

    def expire_session(self, session_id):
        """Đóng phiên cũ để nó không còn thu tiền được trước khi tạo phiên mới cho cùng đơn."""
        self.hub.call(self.KEY, "Stripe", "POST /v1/checkout/sessions/:id/expire (Sandbox thật)", "Bearer Secret Key")
        return self._session("POST", "/v1/checkout/sessions/" + urllib.parse.quote(session_id, safe="") + "/expire")
