"""Máy chủ GIẢ đóng vai Cloudflare Siteverify, OpenAI Responses API và Stripe API, chỉ dùng cho kiểm thử.

Nhờ nó, các lớp gọi API thật trong real_services.py được chạy thử qua HTTP mà không cần khóa thật hay Internet.
Chạy riêng:  python stub_apis.py 9911
"""
import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {"used": set(), "sessions": {}, "ai_inputs": [], "stripe_calls": 0}


class Stub(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        data = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/__stub/state":                               # cho bài kiểm thử trình duyệt đọc lại dữ liệu đã gửi sang "Stripe"
            return self._json(200, {"stripe_calls": STATE["stripe_calls"],
                                    "sessions": [{"id": s["id"], "status": s.get("status", "open"), "form": s["form"]} for s in STATE["sessions"].values()]})
        if "/v1/checkout/sessions/" in self.path:                      # đối soát phiên
            if self.headers.get("Authorization") != "Bearer sk_test_stub":
                return self._json(401, {"error": {"message": "Invalid API Key provided"}})
            if STATE.get("stripe_down"):
                return self._json(503, {})
            sid = self.path.rsplit("/", 1)[-1]
            session = next((s for s in STATE["sessions"].values() if s["id"] == sid), None)
            if not session:
                return self._json(404, {"error": {"message": "No such checkout.session"}})
            form = session["form"]
            total = sum(int(v) * int(form.get(k.replace("[price_data][unit_amount]", "[quantity]"), 1)) for k, v in form.items() if k.endswith("[price_data][unit_amount]"))
            return self._json(200, {"id": sid, "status": session.get("status", "open"), "payment_status": session.get("payment_status", "unpaid"),
                                    "url": session["url"] if session.get("status", "open") == "open" else None, "expires_at": session["expires_at"],
                                    "amount_total": session.get("amount_override", total), "currency": form.get("line_items[0][price_data][currency]", "vnd"),
                                    "client_reference_id": form.get("client_reference_id")})
        body = "<h1>Stub Stripe Checkout</h1>".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
        if self.path.endswith("/siteverify"):
            form = dict(urllib.parse.parse_qsl(raw))
            token = form.get("response", "")
            if form.get("secret") != "stub-turnstile-secret":
                return self._json(200, {"success": False, "error-codes": ["invalid-input-secret"]})
            if token.startswith("tok-slow"):
                time.sleep(8)
            if token.startswith("tok-down"):
                return self._json(503, {})
            if not token.startswith("tok-"):
                return self._json(200, {"success": False, "error-codes": ["invalid-input-response"]})
            if token in STATE["used"]:
                return self._json(200, {"success": False, "error-codes": ["timeout-or-duplicate"]})
            STATE["used"].add(token)
            return self._json(200, {"success": True, "challenge_ts": "2026-11-13T00:00:00Z", "error-codes": [],
                                    "hostname": "evil.example" if token.startswith("tok-wronghost") else "localhost",
                                    "action": "login" if token.startswith("tok-wrongaction") else "checkout"})
        if self.path.endswith("/responses"):
            if self.headers.get("Authorization") != "Bearer stub-openai-key":
                return self._json(401, {"error": {"message": "Incorrect API key provided"}})
            body = json.loads(raw)
            data = json.loads(body["input"])
            STATE["ai_inputs"].append(body)
            if "cham" in data["cau_hoi"]:
                time.sleep(3)
            if "loi" in data["cau_hoi"]:
                return self._json(500, {"error": {"message": "server error"}})
            picks = [{"id": p["id"], "reason": "Hợp nhu cầu: " + p["spec"]} for p in data["san_pham"][:2]]
            picks.append({"id": "SP99", "reason": "Mã không tồn tại, backend phải loại"})
            text = json.dumps({"reply": "Đây là các lựa chọn phù hợp với yêu cầu của bạn.", "picks": picks}, ensure_ascii=False)
            return self._json(200, {"id": "resp_stub", "output": [{"type": "reasoning", "summary": []},
                                    {"type": "message", "content": [{"type": "output_text", "text": text}]}]})
        if self.path.endswith("/expire") and "/v1/checkout/sessions/" in self.path:
            sid = self.path.split("/")[-2]
            session = next((s for s in STATE["sessions"].values() if s["id"] == sid), None)
            if not session or session.get("status", "open") != "open":
                return self._json(400, {"error": {"message": "Only open sessions can be expired"}})
            session["status"] = "expired"
            return self._json(200, {"id": sid, "status": "expired", "payment_status": "unpaid", "url": None, "expires_at": session["expires_at"]})
        if self.path.startswith("/__stub/"):                           # điều khiển từ bài kiểm thử: /__stub/<session>/<status>/<payment_status>
            _, sid, status, payment = self.path.strip("/").split("/") if self.path.count("/") == 4 else ("", "", "", "")
            if sid == "down":
                STATE["stripe_down"] = status == "on"
                return self._json(200, {"ok": True})
            session = next((s for s in STATE["sessions"].values() if s["id"] == sid), None)
            if session and status == "amount":                         # /__stub/<session>/amount/<số tiền>: giả lập Stripe ghi số tiền khác
                session["amount_override"] = int(payment)
            elif session:
                session["status"], session["payment_status"] = status, payment
            return self._json(200, {"ok": bool(session)})
        if self.path.endswith("/v1/checkout/sessions"):
            if self.headers.get("Authorization") != "Bearer sk_test_stub":
                return self._json(401, {"error": {"message": "Invalid API Key provided"}})
            STATE["stripe_calls"] += 1
            key = self.headers.get("Idempotency-Key")
            form = dict(urllib.parse.parse_qsl(raw))
            if key not in STATE["sessions"]:
                sid = f"cs_test_stub{len(STATE['sessions']) + 1:04d}"
                STATE["sessions"][key] = {"id": sid, "url": f"http://{self.headers['Host']}/pay/{sid}", "payment_intent": None,
                                          "expires_at": int(time.time()) + 1800, "form": form}
            session = STATE["sessions"][key]
            return self._json(200, {k: session[k] for k in ("id", "url", "payment_intent", "expires_at")})
        self._json(404, {})

    def log_message(self, *args):
        pass


def start(port=0):
    server = ThreadingHTTPServer(("127.0.0.1", port), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9911
    print(f"Stub API tại http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Stub).serve_forever()
