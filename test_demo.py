"""Kiểm thử tự động cho prototype. Chạy: python test_demo.py"""
import json
import time
import unittest

import backend
from datetime import datetime

from backend import Backend, _token, scenario


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.b = Backend()
        self.addCleanup(lambda: self.b.db.close())

    def run_scenario(self, name):
        order_id = scenario(self.b, name)
        return self.b.order(order_id) if order_id else None

    def emails(self, order_id):
        return sorted(m["template"] for m in self.b.brevo.sent.values() if m["params"]["order_id"] == order_id)

    def test_happy_path(self):
        order = self.run_scenario("happy")
        self.assertEqual((order["risk"], order["order_status"], order["payment_status"]), ("LOW", "SHIPPING", "SUCCEEDED"))
        self.assertEqual(order["amount"], 1_915_000)                    # 1.890.000 + 25.000 phí giao; bỏ qua 1000đ client gửi
        self.assertEqual(self.b.ghn.orders[order["id"]]["cod_amount"], 0)
        self.assertEqual(self.emails(order["id"]), ["Mã vận đơn", "Xác nhận thanh toán"])
        self.assertTrue(any("lọc thông tin cá nhân" in e["detail"] for e in self.b.events))
        self.assertTrue(any("HTTP 403" in e["detail"] for e in self.b.events))   # Bob không xem được đơn của Alice

    def test_high_risk_hold_then_approve(self):
        b = self.b
        r = b.checkout("idtoken-bob", _token(), {"SP03": 1})
        self.assertEqual(r["risk"], "HIGH")
        self.assertEqual(b.order(r["order_id"])["order_status"], "ON_HOLD")
        self.assertIsNone(b.order(r["order_id"])["session_id"])         # chưa tạo phiên thanh toán
        self.assertEqual(len(b.alerts), 1)
        b.review(r["order_id"], approve=True)
        self.assertEqual(b.order(r["order_id"])["order_status"], "PAYMENT_PENDING")

    def test_high_risk_reject(self):
        order = self.run_scenario("high_risk_reject")
        self.assertEqual(order["order_status"], "CANCELED")
        self.assertEqual(self.emails(order["id"]), ["Đơn bị từ chối"])
        self.assertNotIn(order["id"], self.b.ghn.orders)

    def test_security_fail_closed(self):
        self.run_scenario("security_down")
        self.assertEqual(self.b.state()["orders"], [])                  # không đơn nào được tạo
        fails = [e for e in self.b.events if e["status"] == "FAIL"]
        self.assertEqual(len(fails), 3)

    def test_turnstile_token_single_use(self):
        token = _token()
        self.assertTrue(self.b.checkout("idtoken-alice", token, {"SP04": 1})["ok"])
        self.assertFalse(self.b.checkout("idtoken-alice", token, {"SP04": 1})["ok"])

    def test_payment_api_down_then_recover(self):
        b = self.b
        b.hub.faults.add("stripe")
        r = b.checkout("idtoken-alice", _token(), {"SP02": 1})
        order = b.order(r["order_id"])
        self.assertEqual(order["order_status"], "PAYMENT_PENDING")
        self.assertIsNone(order["session_id"])
        self.assertEqual(b.state()["outbox"], [])                       # chưa giao hàng
        b.hub.faults.clear()
        b.start_payment(r["order_id"])
        b.start_payment(r["order_id"])                                  # bấm đúp
        self.assertEqual(len(b.stripe.sessions), 1)                     # cùng khóa -> một phiên duy nhất

    def test_fake_replayed_and_tampered_webhooks_rejected(self):
        order = self.run_scenario("fake_webhook")
        self.assertEqual((order["order_status"], order["payment_status"]), ("PAYMENT_PENDING", "PENDING"))
        self.assertEqual(self.b.state()["outbox"], [])

    def test_duplicate_webhook_processed_once(self):
        order = self.run_scenario("duplicate_webhook")
        self.assertEqual(order["order_status"], "SHIPPING")
        self.assertEqual(len(self.b.ghn.orders), 1)
        self.assertEqual(self.emails(order["id"]), ["Mã vận đơn", "Xác nhận thanh toán"])
        self.assertEqual(self.b.state()["processed_events"], 1)

    def test_failed_then_retry_and_late_failure(self):
        order = self.run_scenario("payment_failed")
        self.assertEqual((order["order_status"], order["attempt"]), ("SHIPPING", 2))
        self.assertIn("Thanh toán thất bại", self.emails(order["id"]))
        self.b.simulate_payment(order["id"], "fail")                    # sự kiện lỗi đến muộn
        self.assertEqual(self.b.order(order["id"])["payment_status"], "SUCCEEDED")

    def test_ghn_lost_response_no_duplicate_shipment(self):
        order = self.run_scenario("ghn_lost")
        self.assertEqual(order["order_status"], "SHIPPING")
        self.assertEqual(len(self.b.ghn.orders), 1)
        self.assertTrue(any("vận đơn đã tồn tại" in e["detail"] for e in self.b.events))

    def test_ai_down_does_not_block_checkout(self):
        self.b.hub.faults.add("ai")
        answer = self.b.ask_ai("tai nghe")
        self.assertEqual(answer["source"], "fallback")
        self.b.hub.faults.clear()
        self.assertTrue(self.b.checkout("idtoken-alice", _token(), {"SP01": 1})["ok"])

    def test_paid_but_canceled_order_is_refunded(self):
        b = self.b
        r = b.checkout("idtoken-alice", _token(), {"SP04": 1})
        b.db.execute("UPDATE orders SET order_status='CANCELED' WHERE id=?", (r["order_id"],))
        b.simulate_payment(r["order_id"], "success")
        b.run_worker()
        self.assertEqual(b.order(r["order_id"])["payment_status"], "REFUNDED")
        self.assertNotIn(r["order_id"], b.ghn.orders)

    def test_on_hold_order_cannot_be_paid_or_confirmed(self):
        b = self.b
        r = b.checkout("idtoken-bob", _token(), {"SP03": 1})
        order = b.order(r["order_id"])
        self.assertIsNone(b.start_payment(order["id"])["checkout_url"])          # không vượt qua bước duyệt
        event = {"id": "evt_x", "type": "payment_intent.succeeded", "data": {"object": {
            "id": None, "amount": order["amount"], "amount_received": order["amount"], "currency": "vnd",
            "metadata": {"order_id": order["id"]}}}}
        import json
        raw = json.dumps(event)
        self.assertEqual(b.handle_webhook(raw, b.stripe.sign(raw)), (200, "ignored"))
        self.assertEqual(b.order(order["id"])["order_status"], "ON_HOLD")

    def test_all_scenarios_run_back_to_back(self):
        from backend import SCENARIOS
        for name in SCENARIOS:
            scenario(self.b, name)
        status = {o["id"]: (o["risk"], o["order_status"]) for o in self.b.state()["orders"]}
        self.assertEqual(sorted(status.values()), sorted([
            ("LOW", "SHIPPING"), ("HIGH", "SHIPPING"), ("HIGH", "CANCELED"), ("LOW", "SHIPPING"),
            ("LOW", "PAYMENT_PENDING"), ("LOW", "SHIPPING"), ("LOW", "SHIPPING"), ("LOW", "SHIPPING"),
            ("LOW", "PAYMENT_PENDING")]))
        self.assertEqual(len(self.b.ghn.orders), 6)

    def test_variant_price_stock_and_shipping_fee(self):
        b = self.b
        r = b.checkout("idtoken-alice", _token(), {"SP05|Trắng ngà": 1, "SP10|Đen": 2})
        order = b.order(r["order_id"])
        self.assertEqual((order["subtotal"], order["shipping_fee"], order["amount"]), (1_690_000, 25_000, 1_715_000))
        small = b.order(b.checkout("idtoken-alice", _token(), {"SP04|Đen": 1})["order_id"])
        self.assertEqual((small["subtotal"], small["shipping_fee"], small["amount"]), (350_000, 25_000, 375_000))
        out = b.checkout("idtoken-alice", _token(), {"SP14|Đen": 1})                 # hết hàng
        self.assertEqual((out["ok"], out["stage"], out["problems"][0]["message"]), (False, "cart", "Hết hàng"))
        many = b.checkout("idtoken-alice", _token(), {"SP03|Bạc": 5})                # chỉ còn 2
        self.assertIn("Chỉ còn 2", many["problems"][0]["message"])

    def test_sample_total_from_single_pricing_source(self):
        from backend import quote
        q = quote({"SP05|Đen": 2})                       # 1.290.000 ₫ × 2 + 25.000 ₫ phí giao − 100.000 ₫ giảm giá
        self.assertEqual((q["subtotal"], q["shipping_fee"], q["discount"], q["total"]), (2_580_000, 25_000, 100_000, 2_505_000))
        order = self.b.order(self.b.checkout("idtoken-alice", _token(), {"SP05|Đen": 2})["order_id"])
        self.assertEqual((order["subtotal"], order["shipping_fee"], order["discount"], order["amount"]),
                         (2_580_000, 25_000, 100_000, 2_505_000))               # đơn lưu đúng số của quote()

    def test_admin_reject_needs_reason_is_saved_in_history(self):
        b = self.b
        r = b.checkout("idtoken-bob", _token(), {"SP03": 1})
        b.review(r["order_id"], False, "Địa chỉ không xác minh được")
        detail = b.admin_order(r["order_id"])["order"]
        self.assertEqual(detail["reject_reason"], "Địa chỉ không xác minh được")
        self.assertTrue(any("Lý do: Địa chỉ không xác minh được" in e["detail"] for e in detail["events"]))

    def test_shipment_review_reconcile_then_retry(self):
        b = self.b
        r = b.checkout("idtoken-alice", _token(), {"SP01": 1}); oid = r["order_id"]
        b.simulate_payment(oid, "success")
        b.hub.faults.add("ghn"); b.tick(); b.tick()
        row = [o for o in b.admin_orders()["orders"] if o["id"] == oid][0]
        self.assertEqual((row["payment_status"], row["ship_check"], "ship_issue" in row["buckets"]), ("SUCCEEDED", "unknown", True))
        self.assertEqual(len(b.ghn.orders), 0)                                   # worker không tự tạo lại
        self.assertFalse(b.retry_shipment(oid)["ok"])                            # chưa đối soát thì chưa được tạo lại
        self.assertFalse(b.reconcile_shipment(oid)["ok"])                        # GHN vẫn lỗi
        b.hub.faults.clear()
        self.assertEqual(b.reconcile_shipment(oid), {"ok": True, "found": False})
        self.assertTrue(b.retry_shipment(oid)["ok"]); b.tick()
        self.assertEqual(len(b.ghn.orders), 1)
        self.assertEqual(self.emails(oid), ["Mã vận đơn", "Xác nhận thanh toán"])

    def test_double_click_pay_and_resend_create_no_duplicates(self):
        b = self.b
        r = b.checkout("idtoken-alice", _token(), {"SP01": 1}); oid = r["order_id"]
        b.schedule_payment(oid, "success", delay=0); b.schedule_payment(oid, "success", delay=0)   # bấm đúp
        self.assertEqual(len(b.pending_webhooks), 1)
        b.tick(); b.tick()
        raw, _ = b.last_event[oid]
        self.assertEqual(b.handle_webhook(raw, b.stripe.sign(raw)), (200, "duplicate"))           # gửi lại sự kiện
        b.tick()
        self.assertEqual((len(b.ghn.orders), self.emails(oid)), (1, ["Mã vận đơn", "Xác nhận thanh toán"]))

    def test_six_demo_scenarios_and_reset_keeps_other_data(self):
        b = self.b
        keep = b.checkout("idtoken-alice", _token(), {"SP04": 1})["order_id"]     # đơn của khách thường
        runs = {name: b.demo_run(name)["run_id"] for name in b.DEMO}
        for _ in range(3):
            b.tick()
        st = {name: b.demo_status(rid) for name, rid in runs.items()}
        self.assertTrue(all(c["ok"] for c in st["low_paid"]["checks"]))
        self.assertEqual((st["high"]["order"]["order_status"], st["high"]["order"]["session_id"]), ("ON_HOLD", None))
        self.assertIsNone(st["security_fail"]["order"])
        self.assertEqual((st["payment_unknown"]["order"]["payment_status"], st["payment_unknown"]["order"]["confirming"]), ("PENDING", True))
        self.assertTrue(all(c["ok"] for c in st["duplicate_event"]["checks"]))
        self.assertEqual((st["ghn_down"]["order"]["payment_status"], st["ghn_down"]["counts"]["emails"]), ("SUCCEEDED", ["Xác nhận thanh toán"]))
        b.demo_action(runs["high"], "approve"); b.demo_action(runs["payment_unknown"], "deliver_now")
        b.demo_action(runs["ghn_down"], "restore_ghn"); b.demo_action(runs["duplicate_event"], "resend_event"); b.tick()
        for name in ("high", "payment_unknown", "ghn_down", "duplicate_event"):
            self.assertTrue(all(c["ok"] for c in b.demo_status(runs[name])["checks"]), name)
        self.assertEqual(b.demo_status(runs["duplicate_event"])["counts"]["shipments"], 1)
        b.reset_demo()
        self.assertEqual([o["id"] for o in b.admin_orders()["orders"]], [keep])   # chỉ xóa dữ liệu demo

    # ------------------------------------------------------------ Sale Gia Dụng 30%
    def test_sale_price_is_computed_by_backend_and_ends_on_time(self):
        import backend as m
        q = m.quote({"GD01|Bạc": 1})
        line = q["lines"][0]
        self.assertEqual((line["price"], line["list_price"], line["saving"]), (419_300, 599_000, 179_700))
        self.assertEqual((q["subtotal"], q["savings"], q["shipping_fee"], q["total"]), (419_300, 179_700, 25_000, 444_300))
        shown = {p["id"]: p for p in m.catalog_view()}
        self.assertEqual((shown["GD01"]["price"], shown["GD01"]["old"], shown["GD01"]["sale_pct"]), (419_300, 599_000, 30))
        self.assertEqual((shown["SP04"]["price"], shown["SP04"]["sale_pct"]), (350_000, 0))            # hàng công nghệ không bị ảnh hưởng
        self.assertEqual(sum(v["stock"] for v in shown["GD08"]["variants"]), 0)                        # có một sản phẩm hết hàng
        after = m.CAMPAIGNS[0]["ends_at"] + 1
        self.assertEqual(m.quote({"GD01|Bạc": 1}, now=after)["lines"][0]["price"], 599_000)            # hết giờ thì về giá gốc
        self.assertEqual({p["id"]: p for p in m.catalog_view(after)}["GD01"]["sale_pct"], 0)

    def test_price_change_needs_customer_confirmation_before_order(self):
        import backend as m
        b = self.b
        seen = m.quote({"GD01|Bạc": 1})["total"]                    # khách xem giỏ lúc còn sale: 444.300 ₫
        end = m.CAMPAIGNS[0]["ends_at"]
        try:
            m.CAMPAIGNS[0]["ends_at"] = time.time() - 1              # chiến dịch hết hạn khi khách còn ở giỏ
            r = b.checkout("idtoken-alice", _token(), {"GD01|Bạc": 1}, client_total=seen)
            self.assertEqual((r["ok"], r["stage"], r["seen_total"], r["quote"]["total"]), (False, "price", 444_300, 624_000))
            self.assertEqual(b.admin_orders()["orders"], [])       # chưa tạo đơn
            ok = b.checkout("idtoken-alice", _token(), {"GD01|Bạc": 1}, client_total=r["quote"]["total"])
            self.assertEqual(b.order(ok["order_id"])["amount"], 624_000)
        finally:
            m.CAMPAIGNS[0]["ends_at"] = end
        sale = b.checkout("idtoken-alice", _token(), {"GD01|Bạc": 1}, client_total=444_300)
        order = b.order(sale["order_id"])
        self.assertEqual((order["amount"], json.loads(order["items"])[0]["price"], json.loads(order["items"])[0]["list_price"]),
                         (444_300, 419_300, 599_000))               # đơn lưu giá sau giảm

    # ------------------------------------------- Lưu bền, thử lại, đối soát, dashboard
    def test_restart_keeps_orders_and_resumes_pending_tasks(self):
        import os, tempfile
        from backend import Backend
        path = os.path.join(tempfile.mkdtemp(), "goctech.db")
        b1 = Backend(db_path=path)
        hold = b1.checkout("idtoken-bob", _token(), {"SP03": 1})["order_id"]
        b1.review(hold, True, actor="Quản trị viên (admin@goctech.example)")
        oid = b1.checkout("idtoken-alice", _token(), {"GD01|Bạc": 1})["order_id"]
        raw = dumps_event(b1, oid)
        self.assertEqual(b1.handle_webhook(raw, b1.stripe.sign(raw)), (200, "paid"))       # PAID + 2 tác vụ đã commit, worker CHƯA chạy
        b1.save_state(); b1.db.close()
        b2 = Backend(db_path=path)                                                          # khởi động lại
        order = b2.order(oid)
        self.assertEqual((order["order_status"], order["amount"], order["risk"], order["uid"]), ("PAID", 444_300, "LOW", "u_alice"))
        kept = b2.order(hold)
        self.assertEqual((kept["risk"], kept["review_result"], kept["reviewed_by"], bool(kept["risk_reasons"])),
                         ("HIGH", "approved", "Quản trị viên (admin@goctech.example)", True))
        tasks = [(t["task"], t["status"]) for t in b2.db.execute("SELECT * FROM outbox WHERE order_id=? ORDER BY id", (oid,))]
        self.assertEqual(tasks, [("create_shipment", "PENDING"), ("email_paid", "PENDING")])
        self.assertEqual([(r["session_id"] == order["session_id"], r["status"]) for r in b2.db.execute("SELECT * FROM payment_attempts WHERE order_id=?", (oid,))], [(True, "paid")])
        self.assertEqual([(r["type"], r["result"]) for r in b2.db.execute("SELECT * FROM processed_events WHERE order_id=?", (oid,))], [("payment_intent.succeeded", "paid")])
        self.assertEqual(b2.handle_webhook(raw, b2.stripe.sign(raw)), (200, "duplicate"))   # sự kiện cũ gửi lại sau khi khởi động lại
        b2.tick(); b2.tick()
        done = b2.order(oid)
        emails = sorted(m["template"] for m in b2.brevo.sent.values() if m["params"]["order_id"] == oid)
        self.assertEqual((done["order_status"], bool(done["tracking_code"]), emails, len(b2.ghn.orders)), ("SHIPPING", True, ["Mã vận đơn", "Xác nhận thanh toán"], 1))
        self.assertTrue(any("Chữ ký hợp lệ" in e["detail"] for e in b2.admin_order(oid)["order"]["events"]))   # nhật ký workflow còn sau khởi động lại
        b2.save_state(); b2.db.close()
        b3 = Backend(db_path=path)
        self.assertEqual((b3.order(oid)["tracking_code"], len(b3.ghn.orders), b3.checkout("idtoken-alice", _token(), {"SP04": 1})["order_id"]),
                         (done["tracking_code"], 1, "ORD-003"))                              # mã đơn tiếp nối, không cấp lại mã cũ
        b3.db.close()

    def test_known_not_done_errors_retry_with_limit_and_unknown_goes_to_review(self):
        from backend import Backend
        b = self.b
        Backend.TASK_RETRY_DELAY = 0
        try:
            oid = b.checkout("idtoken-alice", _token(), {"SP01": 1})["order_id"]
            b.hub.faults.add("ghn_503")                                                     # GHN từ chối trước khi xử lý: biết chắc chưa tạo
            b.simulate_payment(oid, "success"); b.run_worker()
            ship = lambda: dict(b.db.execute("SELECT * FROM outbox WHERE order_id=? AND task='create_shipment'", (oid,)).fetchone())
            self.assertEqual((ship()["status"], ship()["attempts"], b.order(oid)["order_status"], self.emails(oid)), ("PENDING", 1, "PAID", ["Xác nhận thanh toán"]))
            b.hub.faults.clear(); b.run_worker()                                            # lần thử lại thành công
            self.assertEqual((ship()["status"], ship()["attempts"], len(b.ghn.orders)), ("DONE", 2, 1))
            two = b.checkout("idtoken-alice", _token(), {"SP04": 1})["order_id"]
            b.hub.faults.add("ghn_503"); b.simulate_payment(two, "success")
            for _ in range(5):
                b.run_worker()
            t = dict(b.db.execute("SELECT * FROM outbox WHERE order_id=? AND task='create_shipment'", (two,)).fetchone())
            self.assertEqual((t["status"], t["attempts"], b.order(two)["ship_check"], b.order(two)["payment_status"]), ("REVIEW", 3, "confirmed_none", "SUCCEEDED"))
            b.hub.faults.clear()
            self.assertTrue(b.retry_shipment(two)["ok"])                                    # đã xác định chưa tạo: admin được Thử lại ngay
            mail = b.checkout("idtoken-alice", _token(), {"SP04": 1})["order_id"]
            b.hub.faults.add("brevo"); b.simulate_payment(mail, "success"); b.run_worker(); b.run_worker()
            m = dict(b.db.execute("SELECT * FROM outbox WHERE order_id=? AND task='email_paid'", (mail,)).fetchone())
            self.assertEqual((m["status"], m["attempts"], self.emails(mail)), ("REVIEW", 1, []))   # timeout: không tự gửi lại
            row = [o for o in b.admin_orders()["orders"] if o["id"] == mail][0]
            self.assertEqual((row["notify_label"], row["pay_label"], [i["label"] for i in row["issues"]]), ("Email cần đối soát", "Đã thanh toán", ["Email cần đối soát"]))
            b.hub.faults.clear()
            self.assertEqual(b.reconcile_email(mail, m["task_key"]), {"ok": True, "already_sent": False})
            self.assertEqual((self.emails(mail)[-1], b.reconcile_email(mail, m["task_key"])["ok"]), ("Xác nhận thanh toán", False))
        finally:
            Backend.TASK_RETRY_DELAY = 5

    def test_dashboard_numbers_for_seed_orders_and_resend(self):
        import backend as m
        b = self.b
        seed = b.seed_dashboard()["orders"]
        check = lambda d: (d["created"], d["paid"], d["paid_value"], d["needs"], d["risk"])
        expect = (4, 2, [{"currency": "VND", "amount": 444_300 + 928_000}], 2, {"LOW": 3, "HIGH": 1, "none": 0})
        d = b.dashboard(env="sim")
        self.assertEqual(check(d), expect)
        self.assertEqual([(q["id"], [i["label"] for i in q["issues"]], q["pay_label"]) for q in d["queue"]],
                         [(seed["B"], ["Chờ duyệt HIGH"], "Chưa mở thanh toán"), (seed["C"], ["Cần đối soát vận đơn"], "Đã thanh toán")])
        tasks = {t["key"]: (t["pending"], t["done"], t["review"]) for t in d["tasks"]}
        self.assertEqual(tasks, {"create_shipment": (0, 1, 1), "email_paid": (0, 2, 0), "email_tracking": (0, 1, 0)})
        self.assertEqual((d["series"][-1]["created"], d["series"][-1]["paid"], len(d["series"])), (4, 2, 7))
        for _ in range(3):
            self.assertEqual(b.resend_event(seed["A"])["result"], "duplicate")              # Stripe gửi lại sự kiện của đơn A
        again = b.dashboard(env="sim")
        self.assertEqual((check(again), again["webhook_duplicates"], {t["key"]: (t["pending"], t["done"], t["review"]) for t in again["tasks"]}),
                         (expect, 3, tasks))                                               # số liệu giữ nguyên, chỉ nhật ký tăng
        self.assertEqual((b.dashboard(env="real")["created"], b.dashboard(env="all")["paid_split"]["sim"]["count"],
                          b.dashboard(env="all")["paid_split"]["real"]["count"]), (0, 2, 0))
        wf = b.admin_order(seed["C"])["order"]["workflow"]
        self.assertEqual((wf["stuck"]["action"], any(r["stuck"] and r["env"] == "GHN giả lập" for r in wf["rows"])), ("reconcile", True))
        hi = b.admin_order(seed["B"])["order"]["workflow"]["risk"]
        self.assertEqual((hi["level"], hi["score"], len(hi["rules"]), hi["decision"]), ("HIGH", 1, 1, None))
        # ---- không có dữ liệu trong kỳ: trả 0, không lỗi
        empty = b.dashboard("2020-01-01", "2020-01-03")
        self.assertEqual((empty["created"], empty["paid"], empty["paid_value"], [s["created"] for s in empty["series"]], empty["needs"]), (0, 0, [], [0, 0, 0], 2))
        # ---- đơn sát nửa đêm giờ Việt Nam: 23:59:30 ngày 10 và 00:00:30 ngày 11 (UTC+7) rơi vào hai ngày khác nhau
        late = datetime(2026, 11, 10, 23, 59, 30, tzinfo=m.VN_TZ).timestamp()
        early = datetime(2026, 11, 11, 0, 0, 30, tzinfo=m.VN_TZ).timestamp()
        b.db.execute("UPDATE orders SET created_at=?, paid_at=? WHERE id=?", (late, early, seed["A"]))
        b.db.execute("UPDATE orders SET created_at=? WHERE id=?", (early, seed["D"]))
        night = {s["date"]: (s["created"], s["paid"]) for s in b.dashboard("2026-11-10", "2026-11-11", env="sim")["series"]}
        self.assertEqual(night, {"2026-11-10": (1, 0), "2026-11-11": (1, 1)})              # tạo ngày 10, thanh toán ngày 11
        self.assertEqual(b.dashboard("2026-11-10", "2026-11-10", env="sim")["paid"], 0)

    # ------------------------------------------------------ Phí giao theo khu vực
    def test_shipping_fee_is_quoted_for_whole_cart_and_saved_on_order(self):
        import backend as m
        b = self.b
        kettle = m.quote({"GD01|Bạc": 1}, area="hcm")
        self.assertEqual((kettle["goods_total"], kettle["shipping_fee"], kettle["total"], kettle["shipping"]["eta_text"], kettle["shipping"]["estimate"]),
                         (419_300, 25_000, 444_300, "1–2 ngày", True))
        both = m.quote({"GD01|Bạc": 1, "GD04|Đen": 1}, area="hcm")              # ấm 1,2 kg + nồi chiên 5,5 kg = 6,7 kg
        self.assertEqual((both["shipping"]["weight_kg"], both["shipping_fee"]), (6.7, 45_000))   # phí cả giỏ khác phí một món
        self.assertEqual(m.quote({"GD01|Bạc": 1}, area="hn")["shipping_fee"], 32_000)            # đổi khu vực thì phí đổi
        island = b.quote_for({"GD01|Bạc": 1}, "dao")
        self.assertEqual((island["shipping_error"], island["total"]), ("undeliverable", 419_300))
        self.assertEqual(b.checkout("idtoken-alice", _token(), {"GD01|Bạc": 1}, area="dao")["stage"], "shipping")
        b.hub.faults.add("shipfee")
        self.assertEqual(b.quote_for({"GD01|Bạc": 1}, "hcm")["shipping_error"], "unavailable")
        r = b.checkout("idtoken-alice", _token(), {"GD01|Bạc": 1}, area="hcm")
        self.assertEqual((r["ok"], r["stage"], r["error"], b.admin_orders()["orders"]), (False, "shipping", "unavailable", []))
        b.hub.faults.clear()
        stale = b.checkout("idtoken-alice", _token(), {"GD01|Bạc": 1}, area="hn", client_total=444_300)   # khách đổi địa chỉ, tổng cũ không còn đúng
        self.assertEqual((stale["stage"], stale["quote"]["total"]), ("price", 451_300))
        oid = b.checkout("idtoken-alice", _token(), {"GD01|Bạc": 1}, area="hn", client_total=451_300)["order_id"]
        order = b.order(oid)
        self.assertEqual((order["shipping_fee"], order["amount"], order["area"], order["area_name"], order["eta"]), (32_000, 451_300, "hn", "Hà Nội", "2–4 ngày"))
        b.expire_session(oid)
        self.assertEqual(b.order_checkout("idtoken-alice", oid)["amount"], 451_300)             # quay lại thanh toán: đúng số tiền đã lưu

    # ------------------------------------------------------------ So sánh sản phẩm
    def test_compare_specs_come_from_catalog(self):
        import backend as m
        view = {p["id"]: p for p in m.catalog_view()}
        a, c = view["GD04"], view["GD09"]
        self.assertEqual((a["kind"], c["kind"], a["compare_rows"]), ("Nồi chiên không dầu", "Nồi chiên không dầu",
                                                                      ["Dung tích", "Công suất", "Khoảng nhiệt", "Kích thước", "Bảo hành"]))
        self.assertEqual(dict(a["specs"])["Kích thước"], "30 × 36 × 32 cm")
        self.assertNotIn("Kích thước", dict(c["specs"]))                                         # thiếu dữ liệu -> "Chưa có thông tin"
        self.assertEqual((a["price"], c["price"], c["sale_pct"]), (1_393_000, 973_000, 30))     # giá sale cùng nguồn với trang sản phẩm
        self.assertEqual({p["id"]: p for p in m.catalog_view(m.CAMPAIGNS[0]["ends_at"] + 1)}["GD09"]["price"], 1_390_000)
        for kind, rows in m.COMPARE_ROWS.items():
            self.assertGreaterEqual(sum(1 for p in m.CATALOG if p["kind"] == kind), 2, kind)    # mỗi loại có ít nhất 2 món để so sánh

    # ------------------------------------------------------------- Hỗ trợ theo đơn
    def test_support_ticket_ownership_dedupe_and_admin_flow(self):
        b = self.b
        oid = b.checkout("idtoken-alice", _token(), {"SP01": 1, "SP04": 1})["order_id"]
        b.simulate_payment(oid, "success"); b.tick(); b.tick()
        before = (b.order(oid)["order_status"], b.order(oid)["payment_status"])
        self.assertEqual(b.create_ticket("idtoken-bob", oid, "Giao hàng", "Đơn của tôi chưa thấy giao tới.")["status"], 403)   # không phải chủ đơn
        self.assertEqual(b.create_ticket("idtoken-alice", oid, "Khác", "Nội dung đủ dài để gửi.")["field"], "topic")
        self.assertEqual(b.create_ticket("idtoken-alice", oid, "Giao hàng", "ngắn")["field"], "body")
        one = b.create_ticket("idtoken-alice", oid, "Giao hàng", "Đơn của tôi chưa thấy giao tới.", item="Chuột không dây Kova M2 (Đen)", client_key="k1")
        two = b.create_ticket("idtoken-alice", oid, "Giao hàng", "Đơn của tôi chưa thấy giao tới.", client_key="k1")       # bấm gửi hai lần
        self.assertEqual((one["ticket"]["id"], one["ticket"]["status"], two["ticket"]["id"], two.get("duplicate"), len(b.admin_tickets()["tickets"])),
                         ("HT-001", "Đã tiếp nhận", "HT-001", True, 1))
        mine = [o for o in b.my_orders("idtoken-alice")["orders"] if o["id"] == oid][0]["tickets"]
        self.assertEqual((len(mine), mine[0]["item"]), (1, "Chuột không dây Kova M2 (Đen)"))
        self.assertEqual(b.my_orders("idtoken-bob")["orders"], [])                                # tài khoản khác không thấy đơn lẫn yêu cầu
        self.assertFalse(b.update_ticket("HT-001", "Đã phản hồi")["ok"])                          # phản hồi thì phải có nội dung
        self.assertTrue(b.update_ticket("HT-001", "Đang xử lý", actor="Quản trị viên")["ok"])
        self.assertFalse(b.update_ticket("HT-001", "Đã tiếp nhận")["ok"])                         # không lùi trạng thái
        done = b.update_ticket("HT-001", "Đã phản hồi", "GHN sẽ giao trong hôm nay.", actor="Quản trị viên")
        self.assertEqual((done["ticket"]["status"], done["ticket"]["reply"], [h["status"] for h in done["ticket"]["history"]]),
                         ("Đã phản hồi", "GHN sẽ giao trong hôm nay.", ["Đã tiếp nhận", "Đang xử lý", "Đã phản hồi"]))
        seen = [o for o in b.my_orders("idtoken-alice")["orders"] if o["id"] == oid][0]["tickets"][0]
        self.assertEqual(seen["reply"], "GHN sẽ giao trong hôm nay.")                             # khách đọc được phản hồi
        b.create_ticket("idtoken-alice", oid, "Yêu cầu đổi trả", "Tôi muốn đổi sang màu trắng.", client_key="k2")
        self.assertEqual((b.order(oid)["order_status"], b.order(oid)["payment_status"]), before)  # yêu cầu đổi trả không đổi trạng thái đơn
        self.assertTrue(b.update_ticket("HT-001", "Đóng")["ok"])

    # ------------------------------------------------- Thanh toán tiếp / thanh toán lại
    def test_resume_then_expired_session_creates_one_new_session_for_same_order(self):
        b = self.b
        oid = b.checkout("idtoken-alice", _token(), {"SP01": 1})["order_id"]
        first = b.order(oid)
        self.assertEqual(b.pay_action(first), "resume")
        again = b.order_checkout("idtoken-alice", oid)
        self.assertEqual((again["action"], again["resumed"], again["amount"], b.order(oid)["session_id"]),
                         ("redirect", True, 1_915_000, first["session_id"]))       # cùng phiên, cùng số tiền
        self.assertTrue(b.expire_session(oid)["ok"])
        self.assertEqual(b.pay_action(b.order(oid)), "retry")
        self.assertTrue(b.schedule_payment(oid, "success").get("expired"))         # phiên cũ không thu tiền được nữa
        one = b.order_checkout("idtoken-alice", oid)
        two = b.order_checkout("idtoken-alice", oid)                               # bấm đúp / mở hai tab
        new = b.order(oid)
        self.assertEqual((one["resumed"], two["resumed"], new["attempt"], new["amount"]), (False, True, 2, 1_915_000))
        self.assertNotEqual(new["session_id"], first["session_id"])
        self.assertEqual(len(b.stripe.sessions), 2)                                # tổng cộng chỉ có 2 phiên cho đơn này
        b.simulate_payment(oid, "success"); b.tick(); b.tick()
        self.assertEqual((b.order(oid)["order_status"], b.pay_action(b.order(oid)), b.order_checkout("idtoken-alice", oid)["action"]),
                         ("SHIPPING", "view", "paid"))

    def test_pay_actions_by_state_and_ownership(self):
        b = self.b
        hold = b.checkout("idtoken-bob", _token(), {"SP03": 1})["order_id"]
        self.assertEqual((b.pay_action(b.order(hold)), b.order_checkout("idtoken-bob", hold)["action"]), ("hold", "hold"))
        self.assertIsNone(b.order(hold)["session_id"])
        self.assertEqual(b.order_checkout("idtoken-alice", hold)["status"], 403)   # không phải chủ đơn
        self.assertTrue(b.order_checkout("khong-hop-le", hold)["expired"])
        b.review(hold, True)
        self.assertEqual(b.order_checkout("idtoken-bob", hold)["action"], "redirect")   # được duyệt thì thanh toán tiếp được
        gone = b.checkout("idtoken-bob", _token(), {"SP03": 1})["order_id"]
        b.review(gone, False, "thử")
        self.assertEqual((b.pay_action(b.order(gone)), b.order_checkout("idtoken-bob", gone)["action"]), ("canceled", "canceled"))
        failed = b.checkout("idtoken-alice", _token(), {"SP04": 1})["order_id"]
        b.simulate_payment(failed, "fail")
        self.assertEqual(b.pay_action(b.order(failed)), "retry")
        r = b.order_checkout("idtoken-alice", failed)
        self.assertEqual((r["action"], r["resumed"], b.order(failed)["attempt"], b.order(failed)["order_status"]), ("redirect", False, 2, "PAYMENT_PENDING"))
        waiting = b.checkout("idtoken-alice", _token(), {"SP04": 1})["order_id"]
        b.schedule_payment(waiting, "success", delay=3600)
        self.assertEqual((b.pay_action(b.order(waiting)), b.order_checkout("idtoken-alice", waiting)["action"]), ("check", "confirming"))
        self.assertEqual(b.order(waiting)["attempt"], 1)                            # đang xác nhận: không tạo phiên khác

    def test_expired_price_hold_requires_confirmation_of_new_total(self):
        import backend as m
        b = self.b
        oid = b.checkout("idtoken-alice", _token(), {"GD01|Bạc": 1})["order_id"]   # đặt lúc còn sale: 444.300 ₫
        b.expire_session(oid)
        self.assertEqual(b.order_checkout("idtoken-alice", oid)["amount"], 444_300)   # còn trong thời gian giữ giá: dùng giá đã chốt
        b.expire_session(oid)
        end = m.CAMPAIGNS[0]["ends_at"]
        try:
            m.CAMPAIGNS[0]["ends_at"] = time.time() - 1
            b.db.execute("UPDATE orders SET priced_at=? WHERE id=?", (time.time() - m.ORDER_PRICE_HOLD_S - 5, oid))
            ask = b.order_checkout("idtoken-alice", oid)
            self.assertEqual((ask["action"], ask["old_total"], ask["new_total"], b.order(oid)["attempt"]), ("reprice", 444_300, 624_000, 2))
            done = b.order_checkout("idtoken-alice", oid, confirm_price=True)
            self.assertEqual((done["action"], done["amount"], b.order(oid)["amount"], b.order(oid)["attempt"]), ("redirect", 624_000, 624_000, 3))
        finally:
            m.CAMPAIGNS[0]["ends_at"] = end

    def test_payment_confirmed_only_after_webhook_arrives(self):
        b = self.b
        r = b.checkout("idtoken-alice", _token(), {"SP01|Đen": 1})
        b.schedule_payment(r["order_id"], "success", delay=0.2)
        b.tick()
        mine = b.my_orders("idtoken-alice")["orders"][0]
        self.assertEqual((mine["order_status"], mine["confirming"]), ("PAYMENT_PENDING", True))   # chưa ghi PAID
        import time
        time.sleep(0.25)
        b.tick(); b.tick()
        mine = b.my_orders("idtoken-alice")["orders"][0]
        self.assertEqual((mine["order_status"], mine["confirming"], mine["ship_status"]), ("SHIPPING", False, "ready_to_pick"))
        self.assertEqual(mine["lines"][0]["variant"], "Đen")

    def test_delivery_status_progresses_to_delivered(self):
        b = self.b
        b.ghn.DELIVERING_AFTER_S, b.ghn.DELIVERED_AFTER_S = 0.1, 0.25
        r = b.checkout("idtoken-alice", _token(), {"SP01": 1})
        b.simulate_payment(r["order_id"], "success")
        b.tick()
        import time
        time.sleep(0.12); b.tick()
        self.assertEqual(b.order(r["order_id"])["ship_status"], "delivering")
        time.sleep(0.2); b.tick()
        order = b.order(r["order_id"])
        self.assertEqual((order["order_status"], order["ship_status"]), ("DELIVERED", "delivered"))
        self.assertIsNotNone(order["delivered_at"])

    def test_ai_returns_at_most_three_in_stock_with_reasons(self):
        answer = self.b.ask_ai("tai nghe chống ồn dưới 2 triệu để đi học")
        ids = [p["id"] for p in answer["products"]]
        self.assertEqual(ids, ["SP01", "SP02"])
        self.assertTrue(all(p["reason"] for p in answer["products"]))
        self.assertLessEqual(len(self.b.ask_ai("tai nghe")["products"]), 3)
        self.assertNotIn("SP14", [p["id"] for p in self.b.ask_ai("lót chuột cỡ lớn")["products"]])   # hết hàng

    def test_third_failed_payment_makes_next_order_high(self):
        b = self.b
        for _ in range(3):
            r = b.checkout("idtoken-alice", _token(), {"SP04": 1})
            b.simulate_payment(r["order_id"], "fail")
        self.assertEqual(b.checkout("idtoken-alice", _token(), {"SP04": 1})["risk"], "HIGH")

    # ------------------------------------------- Đợt 1: giữ hàng theo SKU, khách hủy đơn, đối soát
    def _stock(self, sku):
        return next(r for r in self.b.stock_view()["rows"] if r["sku"] == sku)

    def test_stock_hold_one_winner_consumed_once_and_released_only_after_checking_payment(self):
        b, sku = self.b, "GD01|Bạc"
        b.set_stock(sku, 1)
        alice = b.checkout("idtoken-alice", _token(), {sku: 1})
        bob = b.checkout("idtoken-bob", _token(), {sku: 1})
        self.assertTrue(alice["ok"] and alice["checkout_url"])
        self.assertEqual((bob["ok"], bob.get("order_id")), (False, None))                 # chỉ một đơn giữ được hàng
        self.assertIn("Ấm siêu tốc Voltix", json.dumps(bob, ensure_ascii=False))          # trả về đúng tên món thiếu
        oid = alice["order_id"]
        self.assertEqual({k: self._stock(sku)[k] for k in ("on_hand", "held", "sold", "available")}, {"on_hand": 1, "held": 1, "sold": 0, "available": 0})
        b.order_checkout("idtoken-alice", oid)                                            # bấm lại: dùng lượt giữ cũ
        self.assertEqual(b.db.execute("SELECT COUNT(*) FROM stock_holds WHERE order_id=?", (oid,)).fetchone()[0], 1)
        b.simulate_payment(oid, "success"); b.tick()
        for _ in range(2):
            self.assertEqual(b.resend_event(oid)["result"], "duplicate")
        self.assertEqual({k: self._stock(sku)[k] for k in ("on_hand", "held", "sold", "available")}, {"on_hand": 0, "held": 0, "sold": 1, "available": 0})
        self.assertEqual([r["state"] for r in b.db.execute("SELECT state FROM stock_holds WHERE order_id=?", (oid,))], ["CONSUMED"])
        # phiên hết hạn, chưa thu tiền: số có thể bán trở về 1
        b.set_stock(sku, 1)
        second = b.checkout("idtoken-alice", _token(), {sku: 1})["order_id"]
        self.assertEqual(self._stock(sku)["available"], 0)
        self.assertEqual(b.check_holds(time.time() + backend.STOCK_HOLD_S + 5), {second: "kept"})    # phiên còn mở: giữ tiếp
        b.db.execute("UPDATE orders SET session_expires_at=? WHERE id=?", (time.time() - 1, second))
        self.assertEqual(b.check_holds(time.time() + 10 * backend.STOCK_HOLD_S), {second: "released"})
        self.assertEqual((self._stock(sku)["available"], self._stock(sku)["sold"]), (1, 1))
        self.assertTrue(b.checkout("idtoken-bob", _token(), {sku: 1})["ok"])                # giờ Bob giữ được
        self.assertEqual(b.order_checkout("idtoken-alice", second)["action"], "unavailable")  # đơn cũ của Alice không mở Stripe được nữa

    def test_sample_data_and_demo_orders_do_not_depend_on_stock(self):
        b = self.b
        for sku in ("GD01|Bạc", "GD02|Trắng", "GD07|Trắng", "SP03|Đen"):
            b.set_stock(sku, 0)                                # ví dụ: một đơn thật đang giữ hết hàng
        seed = b.seed_dashboard()
        self.assertEqual(sorted(seed["orders"]), ["A", "B", "C", "D"])
        self.assertEqual(b.db.execute("SELECT COUNT(*) FROM stock_holds").fetchone()[0], 0)
        self.assertFalse(b.checkout("idtoken-alice", _token(), {"GD01|Bạc": 1})["ok"])     # khách thật vẫn bị chặn

    def test_hold_is_kept_and_order_goes_to_reconciliation_while_payment_is_processing(self):
        b, sku = self.b, "GD01|Bạc"
        oid = b.checkout("idtoken-alice", _token(), {sku: 1})["order_id"]
        before = self._stock(sku)["available"]
        b.schedule_payment(oid, "success", delay=9999)                                    # khách đã trả, Webhook chưa tới
        self.assertEqual(b.check_holds(time.time() + backend.STOCK_HOLD_S + 5), {oid: "kept"})
        row = b.admin_order(oid)["order"]
        self.assertEqual((self._stock(sku)["available"], row["recon"]["result"], "recon" in row["buckets"]), (before, "processing", True))

    def test_high_order_holds_stock_only_after_approval_and_rechecks(self):
        b, sku = self.b, "SP03|" + backend.BY_ID["SP03"]["variants"][0]["name"]
        hold = b.checkout("idtoken-bob", _token(), {sku: 1})
        self.assertEqual((hold["risk"], b.db.execute("SELECT COUNT(*) FROM stock_holds").fetchone()[0]), ("HIGH", 0))
        b.set_stock(sku, 0)
        approved = b.review(hold["order_id"], True, actor="admin")
        self.assertEqual((approved["checkout_url"], b.order(hold["order_id"])["session_id"]), (None, None))
        self.assertIn("Aurea Studio Pro", approved["message"])
        b.set_stock(sku, 1)
        self.assertEqual(b.order_checkout("idtoken-bob", hold["order_id"])["action"], "redirect")
        self.assertEqual(self._stock(sku)["held"], 1)

    def test_customer_cancels_unpaid_order_once_and_never_paid_and_canceled(self):
        b, sku = self.b, "GD01|Bạc"
        before = self._stock(sku)["available"]
        oid = b.checkout("idtoken-alice", _token(), {sku: 1})["order_id"]
        self.assertEqual(b.cancel_order("idtoken-bob", oid, "Đổi ý").get("status"), 403)   # không phải chủ đơn
        done = b.cancel_order("idtoken-alice", oid, "Muốn sửa địa chỉ", "sai số nhà")
        again = b.cancel_order("idtoken-alice", oid, "Đổi ý")
        order = b.order(oid)
        self.assertEqual((done["action"], again.get("already"), order["order_status"], order["cancel_reason"], self._stock(sku)["available"]),
                         ("canceled", True, "CANCELED", "Muốn sửa địa chỉ: sai số nhà", before))
        self.assertEqual(sum(1 for e in b.events if e["order_id"] == oid and e["step"] == "Hủy đơn"), 1)   # bấm hai lần, một sự kiện hủy
        self.assertFalse(b.schedule_payment(oid, "success")["ok"])                         # tab thanh toán cũ không trả tiền được nữa
        self.assertEqual(b.pay_action(b.order(oid)), "canceled")
        # giao dịch đã ghi nhận, đang chờ Webhook: không hủy
        paid = b.checkout("idtoken-alice", _token(), {sku: 1})["order_id"]
        b.schedule_payment(paid, "success", delay=9999)
        wait = b.cancel_order("idtoken-alice", paid, "Đổi ý")
        self.assertEqual((wait["ok"], wait["action"], b.order(paid)["order_status"]), (False, "confirming", "PAYMENT_PENDING"))
        b.pending_webhooks[0]["due"] = 0; b.tick()
        late = b.cancel_order("idtoken-alice", paid, "Đổi ý")
        self.assertEqual((late["action"], b.order(paid)["order_status"] in b.PAID_STATES, b.order(paid)["canceled_at"]), ("paid", True, None))
        # đơn HIGH đang chờ duyệt cũng hủy được, sau đó admin không duyệt được nữa
        high = b.checkout("idtoken-bob", _token(), {"SP03": 1})["order_id"]
        self.assertEqual(b.cancel_order("idtoken-bob", high, "Đặt nhầm sản phẩm")["action"], "canceled")
        self.assertFalse(b.review(high, True)["ok"])


def dumps_event(b, order_id):
    """Sự kiện thanh toán thành công do Stripe giả lập phát cho đơn (chưa gửi)."""
    from services import dumps
    order = b.order(order_id)
    session = {"payment_intent": order["payment_intent"], "amount": order["amount"], "currency": order["currency"], "metadata": {"order_id": order_id}}
    return dumps(b.stripe.build_event("payment_intent.succeeded", session))


if __name__ == "__main__":
    unittest.main(verbosity=2)

