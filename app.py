"""Máy chủ Góc Tech: python app.py
    http://localhost:8000              cửa hàng (giao diện khách)
    http://localhost:8000/admin        admin: Tổng quan, Đơn hàng, Chờ duyệt (cần đăng nhập tài khoản admin)
    http://localhost:8000/demo         6 tình huống Automation Workflow (cần quyền admin)
    http://localhost:8000/admin/tech   bảng kỹ thuật: kịch bản backend, tắt thử dịch vụ (cần quyền admin)

Chỉ dùng thư viện chuẩn của Python. Cấu hình API thật đặt trong file .env (xem .env.example).
"""
import json
import re
import secrets
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import os
import time
import urllib.parse
from threading import Lock, Thread

import config
from backend import (AREAS, CAMPAIGNS, DISCOUNT_AMOUNT, DISCOUNT_FROM, SCENARIOS, SHIPPING_FEE, Backend, campaigns_view, catalog_view,
                     quote, scenario)

HERE = Path(__file__).parent
DB_PATH = config.get("DB_PATH") or str(HERE / "goctech.db")      # đặt DB_PATH=:memory: nếu không muốn lưu xuống đĩa
backend = Backend(live=True, db_path=DB_PATH)
LOCK = Lock()                      # backend dùng chung một kết nối SQLite: xử lý tuần tự từng yêu cầu
ADMIN_SESSIONS = {}                # mã phiên (cookie HttpOnly) -> tài khoản admin
ADMIN_PASSCODE = config.get("ADMIN_PASSCODE")
FAULT_KEYS = {"ghn_503": "GHN từ chối tạm thời (503)", "brevo_503": "Brevo từ chối tạm thời (503)", "shipfee": "Bảng phí giao hàng", "ai": "OpenAI", "firebase": "Firebase Auth", "turnstile": "Turnstile",
              "stripe": "Stripe", "ghn": "GHN", "ghn_lost_response": "GHN rớt phản hồi", "brevo": "Brevo"}
ADMIN_ACCOUNTS = {"admin": "idtoken-admin", "alice": "idtoken-alice", "bob": "idtoken-bob"}
# API chỉ dành cho admin: mọi thứ dưới /api/admin/, /api/demo/ và các API của bảng kỹ thuật
TECH_APIS = {"/api/state", "/api/scenario", "/api/reset", "/api/ask", "/api/checkout", "/api/review",
             "/api/pay", "/api/retry", "/api/worker", "/api/faults"}
OPEN_ADMIN_APIS = {"/api/admin/login", "/api/admin/logout", "/api/admin/me"}


def needs_admin(path):
    return (path.startswith(("/api/admin/", "/api/demo/")) or path in TECH_APIS) and path not in OPEN_ADMIN_APIS


def _token():
    return "ts-ok-" + secrets.token_hex(4)


def handle_api(path, body, ctx):
    b = backend
    admin = ctx.get("admin")
    actor = f"{admin['name']} ({admin['email']})" if admin else ""
    # ---- đăng nhập admin
    if path == "/api/admin/login":
        user = b.is_admin(ADMIN_ACCOUNTS.get(body.get("account", ""), ""))
        if not user:
            b.log("Admin", "Backend", "FAIL", f"Từ chối đăng nhập admin bằng tài khoản “{str(body.get('account', ''))[:20]}”: không có quyền quản trị")
            return 403, {"ok": False, "message": "Tài khoản này không có quyền quản trị."}
        if ADMIN_PASSCODE and not secrets.compare_digest(str(body.get("passcode", "")), ADMIN_PASSCODE):
            return 403, {"ok": False, "message": "Mã truy cập admin không đúng."}
        sid = secrets.token_urlsafe(24)
        ADMIN_SESSIONS[sid] = {"name": user["name"], "email": user["email"]}
        ctx["set_cookie"] = f"gt_admin={sid}; Path=/; HttpOnly; SameSite=Lax"
        return 200, {"ok": True, "name": user["name"], "email": user["email"]}
    if path == "/api/admin/logout":
        ADMIN_SESSIONS.pop(ctx.get("sid"), None)
        ctx["set_cookie"] = "gt_admin=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        return 200, {"ok": True}
    if path == "/api/admin/me":
        return 200, ({"ok": True, **admin} if admin else {"ok": False, "need_passcode": bool(ADMIN_PASSCODE)})
    # ---- màn hình admin xử lý đơn
    if path == "/api/admin/orders":
        b.tick()
        return 200, b.admin_orders()
    if path == "/api/admin/order":
        return 200, b.admin_order(body.get("order_id", ""))
    if path == "/api/admin/review":
        if not body.get("approve") and not str(body.get("reason", "")).strip():
            return 200, {"ok": False, "message": "Cần ghi lý do khi từ chối đơn."}
        return 200, b.review(body["order_id"], bool(body.get("approve")), body.get("reason", ""), actor=actor)
    if path == "/api/admin/reconcile":
        return 200, b.reconcile_shipment(body.get("order_id", ""))
    if path == "/api/admin/retry-shipment":
        return 200, b.retry_shipment(body.get("order_id", ""))
    # ---- trang demo
    if path == "/api/demo/run":
        return 200, b.demo_run(body.get("name", ""))
    if path == "/api/demo/status":
        b.tick()
        return 200, b.demo_status(body.get("run_id", ""))
    if path == "/api/demo/action":
        return 200, b.demo_action(body.get("run_id", ""), body.get("action", ""), actor=actor)
    if path == "/api/demo/reset":
        return 200, b.reset_demo()
    if path == "/api/demo/list":
        return 200, {"scenarios": b.DEMO}
    # ---- bảng kỹ thuật (luôn chạy bằng dịch vụ giả lập)
    if path == "/api/scenario":
        return 200, {"order_id": scenario(b, body["name"])}
    if path == "/api/reset":
        b.reset(wipe=True)
        return 200, {"ok": True}
    if path in ("/api/ask", "/api/checkout"):
        b._force_sim = True
        try:
            if path == "/api/ask":
                return 200, b.ask_ai(body.get("question", ""))
            return 200, b.checkout(body.get("id_token", ""), _token() if body.get("human") else "ts-bot", body.get("items", {}))
        finally:
            b._force_sim = False
    if path == "/api/review":
        return 200, b.review(body["order_id"], bool(body["approve"]), body.get("reason", ""), actor=actor)
    if path == "/api/pay":
        return 200, b.simulate_payment(body["order_id"], body["outcome"])
    if path == "/api/retry":
        return 200, b.start_payment(body["order_id"], new_attempt=bool(body.get("new_attempt")))
    if path == "/api/worker":
        return 200, b.tick()
    if path == "/api/faults":
        (b.hub.faults.add if body["on"] else b.hub.faults.discard)(body["key"])
        return 200, {"faults": sorted(b.hub.faults)}
    if path == "/api/admin/tickets":
        return 200, b.admin_tickets()
    if path == "/api/admin/ticket":
        return 200, b.update_ticket(body.get("ticket_id", ""), body.get("status", ""), body.get("reply", ""), actor=actor)
    if path == "/api/admin/reconcile-email":
        return 200, b.reconcile_email(body.get("order_id", ""), body.get("task_key", ""))
    if path == "/api/admin/seed-dashboard":       # bộ dữ liệu kiểm thử A–D cho dashboard (giả lập)
        return 200, b.seed_dashboard()
    if path == "/api/admin/resend-event":
        return 200, b.resend_event(body.get("order_id", ""))
    if path == "/api/admin/expire-session":      # công cụ thử: cho phiên thanh toán hết hạn
        return 200, b.expire_session(body.get("order_id", ""))
    if path == "/api/admin/check-payment":       # hàng đợi đối soát: hỏi Stripe trạng thái phiên, chỉ ghi kết quả kiểm tra
        return 200, b.admin_check_payment(body.get("order_id", ""), actor=actor)
    if path == "/api/admin/stock":               # tồn kho theo SKU; có sku + on_hand thì đặt lại tồn (công cụ thử)
        if body.get("sku"):
            result = b.set_stock(body.get("sku", ""), body.get("on_hand"), actor=actor)
            if not result["ok"]:
                return 200, result
        return 200, b.stock_view()
    if path == "/api/admin/campaign":            # công cụ thử: đổi mốc kết thúc chiến dịch (giây tính từ bây giờ)
        import time as _t
        CAMPAIGNS[0]["ends_at"] = _t.time() + float(body.get("ends_in", 0))
        return 200, {"ok": True, "campaigns": campaigns_view()}
    # ---- cửa hàng (khách)
    match = re.fullmatch(r"/api/orders/([^/]{1,40})/(checkout|status|tickets|cancel)", path)
    if match:                                    # thanh toán tiếp / thanh toán lại / kiểm tra trạng thái / hỗ trợ cho đơn đã đặt
        order_id, verb = match.groups()
        if verb == "tickets":
            return 200, b.create_ticket(body.get("id_token", ""), order_id, body.get("topic", ""), body.get("body", ""),
                                        item=body.get("item", ""), client_key=str(body.get("client_key", ""))[:64])
        if verb == "cancel":
            return 200, b.cancel_order(body.get("id_token", ""), order_id, reason=body.get("reason", ""), note=str(body.get("note", ""))[:300])
        if verb == "checkout":
            return 200, b.order_checkout(body.get("id_token", ""), order_id, confirm_price=bool(body.get("confirm_price")))
        return 200, b.order_status(body.get("id_token", ""), order_id)
    if path == "/api/shop/whoami":
        return 200, b.whoami(body.get("id_token", ""))
    if path == "/api/shop/quote":
        return 200, b.quote_for(body.get("items", {}), body.get("area"))
    if path == "/api/shop/login":
        return 200, b.login(body.get("account", ""), body.get("name", ""))
    if path == "/api/shop/ask":
        return 200, b.ask_ai(body.get("question", ""))
    if path == "/api/shop/checkout":
        # Token Turnstile thật do widget cấp; ô tick "giả lập" chỉ dùng khi chưa cấu hình Turnstile hoặc ở trang demo
        token = str(body.get("turnstile_token") or "")[:2100] or (_token() if body.get("human") else "ts-bot")
        return 200, b.checkout(body.get("id_token", ""), token, body.get("items", {}),
                               address=body.get("address", ""), client_total=body.get("client_total"),
                               email=body.get("email", ""), remote_ip=ctx.get("ip"), area=body.get("area"))
    if path == "/api/shop/orders":
        b.tick()                            # nhịp nền: mỗi lần khách tải đơn thì nhận Webhook tới hạn, chạy worker
        return 200, b.my_orders(body.get("id_token", ""))
    if path in ("/api/shop/pay", "/api/shop/retry", "/api/shop/returned"):
        if not b.owns(body.get("id_token", ""), body.get("order_id", "")):
            return 403, {"ok": False, "message": "Bạn không có quyền với đơn này."}
        if path == "/api/shop/pay":
            outcome = "fail" if body.get("outcome") == "fail" else "success"
            return 200, b.schedule_payment(body["order_id"], outcome)
        if path == "/api/shop/returned":
            return 200, b.mark_returned(body["order_id"])
        return 200, b.order_checkout(body.get("id_token", ""), body["order_id"])
    return 404, {"error": "not found"}


ADMIN_PAGES = ("/admin", "/demo")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload, content_type="application/json; charset=utf-8", cookie=None):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def _admin(self):
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        sid = jar["gt_admin"].value if "gt_admin" in jar else None
        return sid, ADMIN_SESSIONS.get(sid)

    def _page(self, name, code=200):
        self._send(code, (HERE / name).read_bytes(), "text/html; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        _, admin = self._admin()
        if path == "/":
            return self._page("shop.html")
        if path.startswith(ADMIN_PAGES):
            if not admin:                       # khách hoặc người chưa đăng nhập: không nhận được trang admin
                return self._page("login.html", 401)
            if path == "/demo":
                return self._page("demo.html")
            if path == "/admin/tech":
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return self._page("admin.html")
        if path == "/api/shop/catalog":
            demo = "demo=1" in self.path
            with LOCK:
                cfg = backend.public_config(demo)
            return self._send(200, {"products": catalog_view(), "campaigns": campaigns_view(), "shipping_fee": SHIPPING_FEE,
                                    "areas": [{"code": x["code"], "name": x["name"]} for x in AREAS],
                                    "discount_from": DISCOUNT_FROM, "discount_amount": DISCOUNT_AMOUNT, "integrations": cfg})
        if path == "/api/admin/dashboard":       # số liệu tổng hợp cho trang Tổng quan vận hành
            if not admin:
                with LOCK:
                    backend.log("Admin", "Backend", "FAIL", "Từ chối gọi /api/admin/dashboard: không có phiên admin")
                    backend.save_state()
                return self._send(403, {"ok": False, "message": "Cần quyền admin."})
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            with LOCK:
                backend.tick()
                data = backend.dashboard(q.get("from", [""])[0], q.get("to", [""])[0], q.get("env", ["all"])[0])
                backend.save_state()
            return self._send(200, data)
        if path == "/api/state":
            if not admin:
                return self._send(403, {"ok": False, "message": "Cần quyền admin."})
            with LOCK:
                state = backend.state()
            state["scenarios"] = SCENARIOS
            state["fault_keys"] = FAULT_KEYS
            return self._send(200, state)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8")
        path = self.path.split("?")[0]
        if path == "/webhooks/stripe":            # endpoint Webhook: nhận raw body + header chữ ký, không cần đăng nhập
            with LOCK:
                code, text = backend.handle_webhook(raw, self.headers.get("Stripe-Signature", ""))
                backend.save_state()              # PAID + hai tác vụ đã nằm trong database trước khi trả lời Stripe
            return self._send(code, {"result": text})
        sid, admin = self._admin()
        if needs_admin(path) and not admin:
            with LOCK:
                backend.log("Admin", "Backend", "FAIL", f"Từ chối gọi {path}: không có phiên admin")
            return self._send(403, {"ok": False, "message": "Cần quyền admin."})
        ctx = {"admin": admin, "sid": sid, "ip": self.client_address[0]}
        try:
            with LOCK:
                try:
                    code, result = handle_api(path, json.loads(raw or "{}"), ctx)
                finally:
                    backend.save_state()
        except Exception as exc:                  # lỗi dữ liệu đầu vào
            return self._send(400, {"ok": False, "error": type(exc).__name__, "message": "Yêu cầu không hợp lệ."})
        self._send(code, result, cookie=ctx.get("set_cookie"))

    def log_message(self, *args):
        pass


PAGE = r"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><title>Góc Tech — Bảng điều khiển vận hành</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--ink:#1c2330;--mute:#5d6778;--line:#d9dee7;--bg:#f4f6f9;--card:#fff;--ok:#16794c;--okbg:#e3f4ea;--warn:#9a5b00;--warnbg:#fdf0d5;--fail:#b3261e;--failbg:#fbe5e3;--info:#2457a6;--infobg:#e4edfb;--accent:#2457a6}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,"Segoe UI",Roboto,Arial,sans-serif;color:var(--ink);background:var(--bg)}
header{background:var(--ink);color:#fff;padding:14px 22px}header h1{margin:0;font-size:18px}header p{margin:4px 0 0;color:#c4ccda;font-size:13px}
main{display:grid;grid-template-columns:330px 1fr;gap:14px;padding:14px 22px;align-items:start}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:14px}
h2{font-size:13px;margin:0 0 8px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute)}
button{font:inherit;border:1px solid var(--line);background:#fff;border-radius:6px;padding:5px 9px;cursor:pointer;color:var(--ink)}
button:hover{border-color:var(--accent);color:var(--accent)}button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.sc{display:block;width:100%;text-align:left;margin-bottom:5px}button.sm{padding:2px 7px;font-size:12px;margin:1px}
input,select{font:inherit;padding:5px 7px;border:1px solid var(--line);border-radius:6px;width:100%}
label{display:block;font-size:12px;color:var(--mute);margin:7px 0 3px}.row{display:flex;gap:8px}.row>*{flex:1}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:5px 7px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute);font-weight:600}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11.5px;font-weight:600;white-space:nowrap}
.OK{background:var(--okbg);color:var(--ok)}.WARN{background:var(--warnbg);color:var(--warn)}.FAIL{background:var(--failbg);color:var(--fail)}.INFO{background:var(--infobg);color:var(--info)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}.grid2 section{margin:0}
.mute{color:var(--mute)}.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
.faults label{display:inline-flex;align-items:center;gap:5px;margin:3px 10px 3px 0;color:var(--ink);font-size:13px}.faults input{width:auto}
tr.sel{background:#eef3fb}tr.clk{cursor:pointer}#ai{margin-top:8px;font-size:13px}.scroll{max-height:420px;overflow:auto}
</style></head><body>
<header><h1>Góc Tech — Bảng kỹ thuật <a href="/" target="_blank" style="color:#9fd8cf;font-size:13px;font-weight:400;margin-left:10px">Sàn Góc Tech ↗</a> <a href="/admin" style="color:#9fd8cf;font-size:13px;font-weight:400;margin-left:10px">Admin</a> <a href="/demo" style="color:#9fd8cf;font-size:13px;font-weight:400;margin-left:10px">Trang demo</a></h1>
<p>Backend điều phối 6 dịch vụ ngang hàng: OpenAI · Firebase Authentication · Cloudflare Turnstile · Stripe · GHN · Brevo. Các dịch vụ ngoài được giả lập trong máy, không dùng API key hay tiền thật.</p></header>
<main><div>
<section><h2>Kịch bản dựng sẵn</h2><div id="scenarios"></div><button onclick="post('/api/reset')">Xóa dữ liệu, làm lại từ đầu</button></section>
<section><h2>Tắt thử một dịch vụ</h2><div class="faults" id="faults"></div></section>
<section><h2>1. Hỏi AI tư vấn</h2><input id="q" value="Tìm giúp mình tai nghe chống ồn dưới 2 triệu"><div style="margin-top:6px"><button class="primary" onclick="ask()">Hỏi AI</button></div><div id="ai" class="mute"></div></section>
<section><h2>2–4. Đặt hàng</h2>
<label>Khách hàng (ID Token Firebase)</label><select id="user"><option value="idtoken-alice">Alice — tài khoản lâu năm</option><option value="idtoken-bob">Bob — tài khoản mới tạo 2 giờ</option><option value="idtoken-sai">Token không hợp lệ</option></select>
<label>Kiểm tra bot (Turnstile)</label><select id="human"><option value="1">Người thật</option><option value="0">Bot</option></select>
<label>Sản phẩm</label><div class="row"><select id="product"></select><input id="qty" type="number" min="1" value="1" style="flex:0 0 64px"></div>
<div style="margin-top:8px"><button class="primary" onclick="checkout()">Đặt hàng</button></div><div id="co" class="mute" style="margin-top:6px"></div></section>
</div><div>
<section><h2>Đơn hàng <span class="mute" style="text-transform:none;letter-spacing:0">— bấm một dòng để lọc dòng thời gian theo đơn</span></h2>
<table><thead><tr><th>Mã đơn</th><th>Khách</th><th>Số tiền</th><th>Rủi ro</th><th>Trạng thái đơn</th><th>Thanh toán</th><th>Vận đơn</th><th>Thao tác</th></tr></thead><tbody id="orders"></tbody></table>
<div style="margin-top:8px"><button class="primary" onclick="post('/api/worker')">Chạy worker (6–7. Giao hàng và thông báo)</button> <span id="pending" class="mute"></span></div></section>
<section><h2>Dòng thời gian workflow <button class="sm" onclick="sel=null;render()">Xem tất cả</button></h2><div class="scroll"><table><thead><tr><th>Bước</th><th>Dịch vụ</th><th>Kết quả</th><th>Chi tiết</th><th>Đơn</th></tr></thead><tbody id="events"></tbody></table></div></section>
<div class="grid2">
<section><h2>Outbox (tác vụ chờ worker)</h2><table><thead><tr><th>Tác vụ</th><th>Trạng thái</th><th>Lần thử</th></tr></thead><tbody id="outbox"></tbody></table></section>
<section><h2>Email đã gửi (Brevo)</h2><table><thead><tr><th>Mẫu</th><th>Người nhận</th><th>Đơn</th></tr></thead><tbody id="emails"></tbody></table></section>
<section><h2>Vận đơn (GHN)</h2><table><thead><tr><th>Mã vận đơn</th><th>Mã đơn</th><th>COD</th></tr></thead><tbody id="ships"></tbody></table></section>
<section><h2>Cảnh báo cho người vận hành</h2><table><thead><tr><th>Đơn</th><th>Lý do HIGH</th></tr></thead><tbody id="alerts"></tbody></table></section>
</div>
<section style="margin-top:14px"><h2>Nhật ký gọi API tới dịch vụ ngoài (30 lần gần nhất)</h2><div class="scroll" style="max-height:230px"><table><thead><tr><th>Dịch vụ</th><th>Thao tác</th><th>Xác thực</th><th>Kết quả</th></tr></thead><tbody id="calls"></tbody></table></div></section>
</div></main>
<script>
let S=null, sel=null;
const $=id=>document.getElementById(id), esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const vnd=n=>Number(n).toLocaleString('vi-VN')+'đ', pill=(t,c)=>`<span class="pill ${c}">${esc(t)}</span>`;
const stCls={PAID:'OK',SHIPPING:'OK',DELIVERED:'OK',REVIEW:'WARN',SUCCEEDED:'OK',REFUNDED:'INFO',PAYMENT_PENDING:'INFO',PENDING:'INFO',ON_HOLD:'WARN',REFUND_PENDING:'WARN',PAYMENT_FAILED:'FAIL',FAILED:'FAIL',CANCELED:'FAIL',LOW:'OK',HIGH:'WARN',DONE:'OK'};
async function load(){S=await (await fetch('/api/state')).json();render()}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});const j=await r.json();await load();return j}
function actions(o){const b=(t,f)=>`<button class="sm" onclick="event.stopPropagation();${f}">${t}</button>`,id=`'${o.id}'`;
 if(o.order_status==='ON_HOLD')return b('Duyệt',`post('/api/review',{order_id:${id},approve:true})`)+b('Từ chối',`post('/api/review',{order_id:${id},approve:false})`);
 if(o.order_status==='PAYMENT_PENDING'&&!o.session_id)return b('Thử lại thanh toán',`post('/api/retry',{order_id:${id}})`);
 if(o.order_status==='PAYMENT_PENDING')return ['success:Trả tiền','fail:Thất bại','fake:Webhook giả','expired:Phát lại cũ','tamper:Sửa số tiền','duplicate:Gửi trùng'].map(x=>{const[k,t]=x.split(':');return b(t,`post('/api/pay',{order_id:${id},outcome:'${k}'})`)}).join('');
 if(o.order_status==='PAYMENT_FAILED')return b('Thanh toán lại',`post('/api/retry',{order_id:${id},new_attempt:true})`);
 if(o.payment_status==='SUCCEEDED')return b('Gửi lại webhook',`post('/api/pay',{order_id:${id},outcome:'success'})`);return ''}
function render(){
 if(!$('scenarios').children.length){$('scenarios').innerHTML=Object.entries(S.scenarios).map(([k,v],i)=>`<button class="sc" onclick="post('/api/scenario',{name:'${k}'}).then(r=>{sel=r.order_id||null;render()})">${i+1}. ${esc(v)}</button>`).join('');
  $('product').innerHTML=S.catalog.map(p=>`<option value="${p.id}">${esc(p.name)} — ${vnd(p.price)}</option>`).join('')}
 $('faults').innerHTML=Object.entries(S.fault_keys).map(([k,v])=>`<label><input type="checkbox" ${S.faults.includes(k)?'checked':''} onchange="post('/api/faults',{key:'${k}',on:this.checked})">${esc(v)}</label>`).join('');
 $('orders').innerHTML=S.orders.map(o=>`<tr class="clk ${sel===o.id?'sel':''}" onclick="sel='${o.id}';render()"><td class="mono">${o.id}</td><td>${esc(o.name)}</td><td>${vnd(o.amount)}</td><td>${pill(o.risk,stCls[o.risk])}</td><td>${pill(o.order_status,stCls[o.order_status]||'INFO')}</td><td>${pill(o.payment_status,stCls[o.payment_status]||'INFO')}</td><td class="mono">${esc(o.tracking_code||'—')}</td><td>${actions(o)}</td></tr>`).join('')||'<tr><td colspan="8" class="mute">Chưa có đơn. Chọn một kịch bản bên trái hoặc tự đặt hàng.</td></tr>';
 const ev=S.events.filter(e=>!sel||e.order_id===sel).slice().reverse();
 $('events').innerHTML=ev.map(e=>`<tr><td>${esc(e.step)}</td><td>${esc(e.service)}</td><td>${pill(e.status,e.status)}</td><td>${esc(e.detail)}</td><td class="mono">${esc(e.order_id||'')}</td></tr>`).join('')||'<tr><td colspan="5" class="mute">Chưa có sự kiện.</td></tr>';
 $('outbox').innerHTML=S.outbox.map(t=>`<tr><td class="mono">${esc(t.task_key)}</td><td>${pill(t.status,stCls[t.status]||'INFO')}</td><td>${t.attempts}</td></tr>`).join('');
 $('pending').textContent=S.outbox.filter(t=>t.status==='PENDING').length+' tác vụ đang chờ';
 $('emails').innerHTML=S.emails.map(m=>`<tr><td>${esc(m.template)}</td><td>${esc(m.to)}</td><td class="mono">${esc(m.params.order_id)}</td></tr>`).join('');
 $('ships').innerHTML=S.shipments.map(s=>`<tr><td class="mono">${esc(s.order_code)}</td><td class="mono">${esc(s.client_order_code)}</td><td>${s.cod_amount}</td></tr>`).join('');
 $('alerts').innerHTML=S.alerts.map(a=>`<tr><td class="mono">${esc(a.order_id)}</td><td>${esc(a.reasons.join('; '))}</td></tr>`).join('');
 $('calls').innerHTML=S.calls.slice(-30).reverse().map(c=>`<tr><td>${esc(c.service)}</td><td class="mono">${esc(c.action)}</td><td>${esc(c.auth)}</td><td>${pill(c.ok?'OK':'TIMEOUT',c.ok?'OK':'FAIL')}</td></tr>`).join('');
}
async function ask(){const r=await post('/api/ask',{question:$('q').value});$('ai').innerHTML=`<b>${r.source==='ai'?'AI trả lời':'AI lỗi, hiển thị danh mục'}:</b> ${esc(r.reply_text)}`}
async function checkout(){const items={};items[$('product').value]=Number($('qty').value)||1;const r=await post('/api/checkout',{id_token:$('user').value,human:$('human').value==='1',items});if(r.order_id)sel=r.order_id;$('co').textContent=r.message||'';render()}
load();setInterval(load,3000);
</script></body></html>"""


def worker_loop(interval=2.0):
    """Tiến trình nền: cứ 2 giây xử lý các tác vụ hậu thanh toán đã lưu (kể cả tác vụ còn dang dở từ lần chạy trước)."""
    while True:
        time.sleep(interval)
        try:
            with LOCK:
                backend.tick()
                backend.save_state()
        except Exception as exc:                  # không để một lỗi làm dừng worker
            print(f"[worker] lỗi: {type(exc).__name__}: {exc}", flush=True)


if __name__ == "__main__":
    port = int(config.get("PORT", "8000") or 8000)
    print(f"Góc Tech đang chạy: http://localhost:{port}")
    print(f"  Admin:        http://localhost:{port}/admin   (đăng nhập tài khoản “Quản trị viên”)")
    print(f"  Demo:         http://localhost:{port}/demo")
    print("Tích hợp:")
    for item in backend.integrations():
        label = {"real": "API THẬT ", "pending": "CHỜ CẤU HÌNH", "sim": "GIẢ LẬP  "}[item["state"]]
        print(f"  [{label}] {item['name']}: {item['note']}")
    if backend.real_turnstile and backend.turnstile_hostname == "localhost":
        print("  Lưu ý: Turnstile đang kiểm tra hostname “localhost”, hãy mở web bằng http://localhost chứ không phải 127.0.0.1.")
    pending = backend.db.execute("SELECT COUNT(*) FROM outbox WHERE status='PENDING'").fetchone()[0]
    orders = backend.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    print(f"Database: {DB_PATH} · {orders} đơn đã lưu · {pending} tác vụ hậu thanh toán đang chờ sẽ được xử lý tiếp.")
    Thread(target=worker_loop, daemon=True).start()
    print("Ctrl+C để dừng.")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
