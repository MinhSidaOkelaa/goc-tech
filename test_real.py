"""Kiểm thử phần nối API thật (OpenAI, Turnstile, Stripe Sandbox) qua máy chủ giả stub_apis.py.

    python test_real.py

Các bài này chứng minh mã gọi HTTP, kiểm tra hostname/action, đối chiếu mã sản phẩm và xử lý Webhook chạy đúng.
Chúng KHÔNG gọi Cloudflare, OpenAI hay Stripe thật.
"""
import hashlib
import hmac
import json
import os
import time
import unittest

import stub_apis

SERVER = stub_apis.start()
BASE = f"http://127.0.0.1:{SERVER.server_address[1]}"
os.environ.update({
    "OPENAI_API_KEY": "stub-openai-key", "OPENAI_MODEL": "stub-model", "OPENAI_BASE_URL": BASE + "/v1", "OPENAI_TIMEOUT_S": "1.5",
    "TURNSTILE_SITE_KEY": "stub-site-key", "TURNSTILE_SECRET_KEY": "stub-turnstile-secret",
    "TURNSTILE_VERIFY_URL": BASE + "/turnstile/v0/siteverify", "TURNSTILE_EXPECTED_HOSTNAME": "localhost",
    "STRIPE_SECRET_KEY": "sk_test_stub", "STRIPE_WEBHOOK_SECRET": "whsec_stub", "STRIPE_API_BASE": BASE,
})
import backend as backend_module   # noqa: E402  (phải đặt biến môi trường trước khi nạp)
from backend import Backend        # noqa: E402
import backend  # noqa: E402 (sau khi đã đặt biến môi trường)

N = [0]


def tok(kind="good"):
    N[0] += 1
    return f"tok-{kind}-{N[0]}"


def sign(raw, secret="whsec_stub"):
    t = int(time.time())
    return f"t={t},v1={hmac.new(secret.encode(), f'{t}.{raw}'.encode(), hashlib.sha256).hexdigest()},v0=bo-qua"


def completed(order, event_id="evt_1", **over):
    obj = {"id": order["session_id"], "object": "checkout.session", "client_reference_id": order["id"],
           "metadata": {"order_id": order["id"]}, "amount_total": order["amount"], "currency": "vnd",
           "payment_status": "paid", "payment_intent": "pi_stub_1"}
    obj.update(over)
    return json.dumps({"id": event_id, "type": "checkout.session.completed", "data": {"object": obj}})


class RealIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.b = Backend(live=True)
        self.assertTrue(self.b.real_ai and self.b.real_turnstile and self.b.real_stripe)

    def fails(self):
        return [e["detail"] for e in self.b.events if e["status"] == "FAIL"]

    # ---------------------------------------------------------------- Turnstile
    def test_turnstile_valid_token_checks_hostname_and_action(self):
        r = self.b.checkout("idtoken-alice", tok(), {"SP01": 1})
        self.assertTrue(r["ok"])
        self.assertTrue(any("Siteverify thật: hostname=localhost, action=checkout" in e["detail"] for e in self.b.events))

    def test_turnstile_reused_wrong_host_wrong_action_error_and_timeout_stop_before_stripe(self):
        b = self.b
        used = tok()
        self.assertTrue(b.checkout("idtoken-alice", used, {"SP01": 1})["ok"])
        calls = stub_apis.STATE["stripe_calls"]
        orders = len(b.admin_orders()["orders"])
        for token in (used, tok("wronghost"), tok("wrongaction"), "khong-hop-le", tok("down"), tok("slow")):
            r = b.checkout("idtoken-alice", token, {"SP01": 1})
            self.assertEqual((r["ok"], r["stage"], r["refresh"], r["message"]),
                             (False, "security", True, "Chưa thể xác minh. Vui lòng thử lại."), token)
        self.assertEqual(stub_apis.STATE["stripe_calls"], calls)                  # không tạo phiên Stripe nào
        self.assertEqual(len(b.admin_orders()["orders"]), orders)                # không tạo đơn nào
        text = " | ".join(self.fails())
        for needle in ("timeout-or-duplicate", "hostname “evil.example”", "action “login”", "invalid-input-response",
                       "HTTP 503", "hết thời gian chờ"):
            self.assertIn(needle, text)
        self.assertNotIn("stub-turnstile-secret", json.dumps(b.events, ensure_ascii=False))   # log không lộ Secret Key
        self.assertNotIn(used, json.dumps(b.events, ensure_ascii=False))                      # log không lộ token

    # ------------------------------------------------------------------- OpenAI
    def test_openai_structured_answer_is_checked_against_catalog(self):
        r = self.b.ask_ai("Tai nghe chống ồn dưới 2 triệu, giao tới 12 Nguyễn Văn Linh, sđt 0901234567")
        self.assertEqual((r["source"], r["mode"]), ("ai", "real"))
        ids = [p["id"] for p in r["products"]]
        self.assertTrue(1 <= len(ids) <= 3 and "SP99" not in ids)                 # mã lạ do AI trả về bị loại
        for p in r["products"]:
            self.assertTrue(p["price"] < 2_000_000 and p["cat"] == "Tai nghe")    # đúng ngân sách, đúng danh mục
            self.assertEqual(p["price"], backend_module.BY_ID[p["id"]]["price"])  # giá lấy từ danh mục
        sent = stub_apis.STATE["ai_inputs"][-1]
        self.assertEqual((sent["model"], sent["text"]["format"]["type"], sent["text"]["format"]["strict"]), ("stub-model", "json_schema", True))
        payload = json.loads(sent["input"])
        self.assertNotIn("0901234567", sent["input"])                             # số điện thoại đã được lọc
        self.assertEqual(set(payload["san_pham"][0]), {"id", "name", "cat", "price", "stock", "spec", "feature"})

    def test_openai_timeout_error_and_no_match(self):
        slow = self.b.ask_ai("tai nghe cham")
        self.assertEqual((slow["source"], slow["reason"]), ("fallback", "timeout"))
        broken = self.b.ask_ai("tai nghe loi")
        self.assertEqual((broken["source"], broken["reason"]), ("fallback", "error"))
        none = self.b.ask_ai("tai nghe chống ồn dưới 100k")
        self.assertEqual((none["source"], none["products"], none["hint"]), ("ai", [], {"cat": "Tai nghe", "budget": 100000}))

    # ------------------------------------------------------------------- Stripe
    def test_stripe_session_reused_and_paid_only_by_verified_matching_webhook(self):
        b = self.b
        r = b.checkout("idtoken-alice", tok(), {"SP05|Đen": 2})
        order = b.order(r["order_id"])
        self.assertEqual((r["pay_mode"], order["amount"], order["pay_mode"]), ("real", 2_505_000, "real"))
        form = next(s["form"] for s in stub_apis.STATE["sessions"].values() if s["id"] == order["session_id"])
        self.assertEqual((form["line_items[0][price_data][unit_amount]"], form["client_reference_id"], form["metadata[order_id]"]),
                         ("2505000", order["id"], order["id"]))                  # tổng tiền backend + mã đơn gắn vào phiên
        calls = stub_apis.STATE["stripe_calls"]
        again = b.start_payment(order["id"])
        self.assertEqual((again["checkout_url"], stub_apis.STATE["stripe_calls"]), (r["checkout_url"], calls))   # dùng lại phiên còn hiệu lực
        self.assertFalse(b.schedule_payment(order["id"], "success")["ok"])        # cổng giả lập bị khóa với đơn thật
        self.assertEqual(b.handle_webhook(completed(order), sign(completed(order), "whsec_ke_gian"))[0], 400)
        self.assertEqual(b.handle_webhook(completed(order, "evt_a", amount_total=1000), sign(completed(order, "evt_a", amount_total=1000)))[1], "amount_mismatch")
        self.assertEqual(b.handle_webhook(completed(order, "evt_b", id="cs_test_khac"), sign(completed(order, "evt_b", id="cs_test_khac")))[1], "ignored")
        self.assertEqual(b.order(order["id"])["payment_status"], "PENDING")
        raw = completed(order, "evt_ok")
        self.assertEqual(b.handle_webhook(raw, sign(raw)), (200, "paid"))
        self.assertEqual(b.handle_webhook(raw, sign(raw)), (200, "duplicate"))    # Webhook đến hai lần
        for _ in range(3):
            b.tick()
        emails = sorted(m["template"] for m in b.brevo.sent.values() if m["params"]["order_id"] == order["id"])
        self.assertEqual((b.order(order["id"])["order_status"], len(b.ghn.orders), emails), ("SHIPPING", 1, ["Mã vận đơn", "Xác nhận thanh toán"]))

    def _stub(self, session_id, status, payment="unpaid"):
        import urllib.request
        urllib.request.urlopen(urllib.request.Request(f"{BASE}/__stub/{session_id}/{status}/{payment}", data=b"", method="POST")).read()

    def test_pay_again_reconciles_with_stripe_before_issuing_a_link(self):
        b = self.b
        r = b.checkout("idtoken-alice", tok(), {"GD01|Bạc": 1})
        order = b.order(r["order_id"]); oid = order["id"]
        form = next(s["form"] for s in stub_apis.STATE["sessions"].values() if s["id"] == order["session_id"])
        self.assertEqual((form["line_items[0][price_data][unit_amount]"], form["line_items[0][quantity]"],
                          form["line_items[1][price_data][unit_amount]"], form["line_items[1][price_data][product_data][name]"]),
                         ("419300", "1", "25000", "Phí giao hàng"))               # Stripe nhận đúng giá sau giảm + phí giao riêng
        self.assertIn(f"/?left={oid}#/order/{oid}", form["cancel_url"])
        calls = stub_apis.STATE["stripe_calls"]
        # 1) phiên còn mở: trả lại đúng link cũ, không tạo phiên mới
        same = b.order_checkout("idtoken-alice", oid)
        self.assertEqual((same["action"], same["url"], same["resumed"], same["amount"], stub_apis.STATE["stripe_calls"]),
                         ("redirect", r["checkout_url"], True, 444_300, calls))
        # 2) Stripe tạm lỗi: giữ nguyên đơn, cho thử lại
        self._stub("down", "on")
        down = b.order_checkout("idtoken-alice", oid)
        self._stub("down", "off")
        self.assertEqual((down["ok"], down["action"], b.order(oid)["session_id"], b.order(oid)["attempt"]), (False, "unknown", order["session_id"], 1))
        # 3) phiên hết hạn trên Stripe: tạo đúng một phiên mới cho cùng đơn, kể cả khi bấm đúp
        self._stub(order["session_id"], "expired")
        self.assertEqual(b.order_status("idtoken-alice", oid)["action"], "retry")
        one = b.order_checkout("idtoken-alice", oid); two = b.order_checkout("idtoken-alice", oid)
        new = b.order(oid)
        self.assertEqual((one["resumed"], two["resumed"], one["url"], stub_apis.STATE["stripe_calls"], new["attempt"], new["amount"]),
                         (False, True, two["url"], calls + 1, 2, 444_300))
        self.assertNotEqual(new["session_id"], order["session_id"])
        # 4) Stripe đã nhận tiền nhưng Webhook chưa về: chỉ báo đang xác nhận, không tạo phiên khác, chưa ghi PAID
        self._stub(new["session_id"], "complete", "paid")
        wait = b.order_checkout("idtoken-alice", oid)
        self.assertEqual((wait["action"], wait["paid_at_stripe"], b.pay_action(b.order(oid)), b.order(oid)["payment_status"],
                          stub_apis.STATE["stripe_calls"]), ("confirming", True, "check", "PENDING", calls + 1))
        raw = completed(b.order(oid), "evt_again")
        self.assertEqual(b.handle_webhook(raw, sign(raw)), (200, "paid"))
        self.assertEqual((b.pay_action(b.order(oid)), b.order_checkout("idtoken-alice", oid)["action"]), ("view", "paid"))

    def test_high_risk_waits_for_admin_then_gets_real_session(self):
        b = self.b
        calls = stub_apis.STATE["stripe_calls"]
        r = b.checkout("idtoken-bob", tok(), {"SP03": 1})
        self.assertEqual((r["risk"], stub_apis.STATE["stripe_calls"]), ("HIGH", calls))
        approved = b.review(r["order_id"], True, actor="Quản trị viên (admin@goctech.example)")
        self.assertTrue(approved["checkout_url"].startswith(BASE) and approved["pay_mode"] == "real")
        self.assertFalse(b.review(r["order_id"], True)["ok"])                     # duyệt lần hai bị chặn
        detail = b.admin_order(r["order_id"])["order"]
        self.assertEqual((detail["review_label"], detail["reviewed_by"]), ("Đã duyệt", "Quản trị viên (admin@goctech.example)"))
        self.assertTrue(any("Người thực hiện: Quản trị viên" in e["detail"] for e in detail["events"]))

    def test_demo_scenarios_stay_simulated_and_admin_role(self):
        b = self.b
        run = b.demo_run("low_paid")
        self.assertEqual(b.order(run["order_id"])["pay_mode"], "sim")
        self.assertEqual(b.public_config(demo=True)["turnstile"]["mode"], "sim")
        cfg = b.public_config()
        self.assertEqual((cfg["turnstile"], cfg["ai"], cfg["stripe"]), ({"mode": "real", "site_key": "stub-site-key"}, "real", "real"))
        self.assertNotIn("secret", json.dumps(cfg).lower().replace("site_key", ""))
        self.assertIsNotNone(b.is_admin("idtoken-admin"))
        self.assertIsNone(b.is_admin("idtoken-alice"))
        self.assertEqual([i["state"] for i in b.integrations()], ["real", "real", "real", "sim", "sim", "sim"])

    # ---------------------------------------- Đợt 1: hủy đơn, đối soát, giữ hàng với Stripe (máy chủ giả)
    def _order(self, sku="GD01|Bạc"):
        oid = self.b.checkout("idtoken-alice", tok(), {sku: 1})["order_id"]
        return oid, self.b.order(oid)["session_id"]

    def _stub_status(self, sid):
        return next(s.get("status", "open") for s in stub_apis.STATE["sessions"].values() if s["id"] == sid)

    def test_cancel_closes_stripe_session_and_never_cancels_a_paid_order(self):
        b = self.b
        oid, sid = self._order()
        done = b.cancel_order("idtoken-alice", oid, "Đổi ý")
        self.assertEqual((done["action"], b.order(oid)["order_status"], self._stub_status(sid)), ("canceled", "CANCELED", "expired"))   # tab Stripe cũ không trả được nữa
        self.assertEqual(b.db.execute("SELECT state FROM stock_holds WHERE order_id=?", (oid,)).fetchone()[0], "RELEASED")
        # giao dịch thành công ngay lúc khách bấm hủy: không hủy, Webhook ghi PAID
        oid2, sid2 = self._order()
        self._stub(sid2, "complete", "paid")
        wait = b.cancel_order("idtoken-alice", oid2, "Đổi ý")
        self.assertEqual((wait["ok"], wait["action"], b.order(oid2)["order_status"]), (False, "confirming", "PAYMENT_PENDING"))
        raw = completed(b.order(oid2), "evt_cancel_race")
        self.assertEqual(b.handle_webhook(raw, sign(raw)), (200, "paid"))
        final = b.order(oid2)
        self.assertEqual((final["order_status"], final["canceled_at"], b.cancel_order("idtoken-alice", oid2, "Đổi ý")["action"]), ("PAID", None, "paid"))
        # Stripe không liên lạc được: chưa hủy
        oid3, _ = self._order()
        self._stub("down", "on")
        self.assertEqual((b.cancel_order("idtoken-alice", oid3, "Đổi ý")["action"], b.order(oid3)["order_status"]), ("unknown", "PAYMENT_PENDING"))
        self._stub("down", "off")

    def test_reconciliation_queue_results_and_webhook_clears_it(self):
        b = self.b
        oid, sid = self._order()
        self.assertEqual(b.admin_check_payment(oid, "admin")["result"], "open")
        self.assertNotIn("recon", b.admin_order(oid)["order"]["buckets"])                 # chỉ đang chờ khách trả: không vào hàng đợi
        self._stub(sid, "complete", "paid")                                               # đã trả trên Stripe, Webhook chưa tới
        calls = stub_apis.STATE["stripe_calls"]
        seen = b.admin_check_payment(oid, "admin")
        row = b.admin_order(oid)["order"]
        self.assertEqual((seen["result"], seen["label"], "recon" in row["buckets"], row["payment_status"], row["task_rows"]),
                         ("paid_wait_webhook", "Stripe đã nhận tiền · Chờ webhook", True, "PENDING", []))
        self.assertEqual(row["recon"]["detail"]["stripe"]["amount"], 444_300)
        again = b.order_checkout("idtoken-alice", oid)
        self.assertEqual((again["action"], again.get("url"), stub_apis.STATE["stripe_calls"]), ("confirming", None, calls))   # không có link thanh toán thứ hai
        raw = completed(b.order(oid), "evt_recon")
        self.assertEqual(b.handle_webhook(raw, sign(raw)), (200, "paid"))
        row = b.admin_order(oid)["order"]
        self.assertEqual(("recon" in row["buckets"], row["payment_status"], len(row["task_rows"])), (False, "SUCCEEDED", 2))
        log = " | ".join(e["detail"] for e in row["events"])
        self.assertIn("Đối soát thủ công bởi admin: Stripe đã nhận tiền · Chờ webhook", log)
        self.assertIn("Ghi PAID một lần", log)
        # lệch số tiền: cần kiểm tra thủ công, chưa kích hoạt giao hàng
        oid2, sid2 = self._order()
        self._stub(sid2, "complete", "paid"); self._stub(sid2, "amount", "1000")
        bad = b.admin_check_payment(oid2, "admin")
        self.assertEqual((bad["result"], bad["label"], b.admin_order(oid2)["order"]["task_rows"]), ("mismatch", "Cần kiểm tra thủ công", []))
        self.assertIn("1.000 ₫", bad["diffs"][0]); self.assertIn("444.300 ₫", bad["diffs"][0])
        # Stripe timeout: chưa có kết quả cuối, giữ đơn
        oid3, _ = self._order()
        self._stub("down", "on")
        self.assertEqual((b.admin_check_payment(oid3)["result"], b.order(oid3)["order_status"]), ("timeout", "PAYMENT_PENDING"))
        self.assertEqual(b.check_holds(time.time() + backend.STOCK_HOLD_S + 5).get(oid3), "kept")   # không liên lạc được: giữ hàng
        self._stub("down", "off")

    def test_stock_released_only_after_stripe_confirms_session_expired(self):
        b, sku = self.b, "GD01|Bạc"
        avail = lambda: next(r["available"] for r in b.stock_view()["rows"] if r["sku"] == sku)
        start = avail()
        oid, sid = self._order(sku)
        late = time.time() + backend.STOCK_HOLD_S + 5
        self.assertEqual((b.check_holds(late), avail()), ({oid: "kept"}, start - 1))       # Stripe báo phiên còn mở
        self._stub(sid, "complete", "unpaid")                                             # Stripe đang xử lý
        self.assertEqual((b.check_holds(late + 4000).get(oid), b.order(oid)["recon_result"], avail()), ("kept", "processing", start - 1))
        self._stub(sid, "expired", "unpaid")
        self.assertEqual((b.check_holds(late + 9000).get(oid), avail()), ("released", start))


if __name__ == "__main__":
    unittest.main(verbosity=1)
