"""Backend của sàn TMĐT: điều phối sáu Internet Service theo Automation Workflow.

Luồng chính:  tư vấn AI -> kiểm tra bảo mật -> chấm rủi ro LOW/HIGH -> thanh toán
              -> xác nhận qua Webhook -> giao hàng -> thông báo.
Backend là nơi DUY NHẤT giữ API key và gọi dịch vụ ngoài; frontend không có khóa nào.
"""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import config
from real_services import RealOpenAI, RealStripe, RealTurnstile, ServiceTimeout
from services import (AuthError, BrevoService, FirebaseAuthService, GHNService, OpenAIService,
                      ServiceError, ServiceHub, ServiceUnavailable, StripeService, TurnstileService, dumps, shortlist)

# ---- Khóa bí mật: đọc từ biến môi trường (thực tế: Secret Manager). Giá trị mặc định chỉ dùng cho demo.
SECRETS = {name: os.environ.get(name, default) for name, default in {
    "OPENAI_API_KEY": "demo-openai-key",
    "TURNSTILE_SECRET_KEY": "demo-turnstile-secret",
    "STRIPE_SECRET_KEY": "sk_test_demo",
    "STRIPE_WEBHOOK_SECRET": "whsec_demo",
    "GHN_TOKEN": "demo-ghn-token",
    "GHN_SHOP_ID": "demo-shop",
    "BREVO_API_KEY": "demo-brevo-key",
}.items()}

def V(name, color, stock, delta=0):
    return {"name": name, "hex": color, "stock": stock, "delta": delta}


BLACK, WHITE, IVORY, SILVER, GREY, NAVY = "#1f1f1f", "#f4f4f2", "#ece6d8", "#c9ccd1", "#8a8d91", "#27364f"

# Dữ liệu mẫu. SP01 và SP02 lấy theo ví dụ ở phần AI Service; các thương hiệu còn lại là tên đặt cho demo.
CATALOG = [
    {"id": "SP01", "name": "Tai nghe Sony WF-C700N", "brand": "Sony", "cat": "Tai nghe", "shape": "earbuds",
     "price": 1_890_000, "old": 2_490_000, "spec": "chống ồn chủ động, pin 15 giờ", "feature": "chống ồn chủ động ANC, pin 15h, đi học đi làm",
     "attrs": {"anc": True, "wear": "Nhét tai"}, "warranty": 12, "featured": True,
     "variants": [V("Đen", BLACK, 15), V("Trắng ngà", IVORY, 6), V("Xanh rêu", "#7c8f77", 0)],
     "specs": [["Kết nối", "Bluetooth 5.2"], ["Chống ồn", "Chủ động (ANC)"], ["Pin", "15 giờ kèm hộp sạc"], ["Kháng nước", "IPX4"]]},
    {"id": "SP02", "name": "Tai nghe JBL Tune Beam", "brand": "JBL", "cat": "Tai nghe", "shape": "earbuds",
     "price": 1_650_000, "old": None, "spec": "chống ồn, kháng nước IP54", "feature": "chống ồn ANC, kháng nước IP54, tập luyện thể thao",
     "attrs": {"anc": True, "wear": "Nhét tai"}, "warranty": 12, "featured": True,
     "variants": [V("Đen", BLACK, 8), V("Tím", "#6b5aa8", 3)],
     "specs": [["Kết nối", "Bluetooth 5.3"], ["Chống ồn", "Chủ động (ANC)"], ["Pin", "12 giờ kèm hộp sạc"], ["Kháng nước", "IP54"]]},
    {"id": "SP03", "name": "Tai nghe chụp tai Aurea Studio Pro", "brand": "Aurea", "cat": "Tai nghe", "shape": "headphones",
     "price": 6_490_000, "old": None, "spec": "chống ồn cao cấp, pin 30 giờ", "feature": "tai nghe chụp tai chống ồn cao cấp, pin 30h, phòng thu và làm việc",
     "attrs": {"anc": True, "wear": "Chụp tai"}, "warranty": 24, "featured": False,
     "variants": [V("Đen", BLACK, 4), V("Bạc", SILVER, 2, 200_000)],
     "specs": [["Kết nối", "Bluetooth 5.3, jack 3,5 mm"], ["Chống ồn", "Chủ động, 3 mức"], ["Pin", "30 giờ"], ["Khối lượng", "254 g"]]},
    {"id": "SP04", "name": "Chuột không dây Kova M2", "brand": "Kova", "cat": "Chuột", "shape": "mouse",
     "price": 350_000, "old": None, "spec": "bluetooth, pin 12 tháng", "feature": "chuột không dây bluetooth, pin 12 tháng, văn phòng",
     "attrs": {"conn": "Không dây"}, "warranty": 12, "featured": True,
     "variants": [V("Đen", BLACK, 40), V("Trắng", WHITE, 25), V("Hồng phấn", "#e7b8c2", 12)],
     "specs": [["Kết nối", "Bluetooth, USB 2,4 GHz"], ["Độ phân giải", "1.600 DPI"], ["Pin", "1 pin AA, khoảng 12 tháng"]]},
    {"id": "SP05", "name": "Bàn phím cơ Kova K75", "brand": "Kova", "cat": "Bàn phím", "shape": "keyboard",
     "price": 1_290_000, "old": 1_590_000, "spec": "layout 75%, hot-swap", "feature": "bàn phím cơ layout 75%, hot-swap, 3 chế độ kết nối, gõ văn bản và lập trình",
     "attrs": {"conn": "Không dây"}, "warranty": 12, "featured": True,
     "variants": [V("Đen", BLACK, 9), V("Trắng ngà", IVORY, 5, 100_000)],
     "specs": [["Layout", "75%, 82 phím"], ["Switch", "Linear, thay nóng được"], ["Kết nối", "Bluetooth, 2,4 GHz, USB-C"], ["Pin", "4.000 mAh"]]},
    {"id": "SP06", "name": "Tai nghe có dây Lumo E1", "brand": "Lumo", "cat": "Tai nghe", "shape": "earbuds",
     "price": 190_000, "old": None, "spec": "jack 3,5 mm, có micro", "feature": "tai nghe có dây giá rẻ, có micro, học trực tuyến",
     "attrs": {"anc": False, "wear": "Nhét tai"}, "warranty": 6, "featured": False,
     "variants": [V("Đen", BLACK, 60), V("Trắng", WHITE, 44)],
     "specs": [["Kết nối", "Jack 3,5 mm"], ["Micro", "Có"], ["Dây dài", "1,2 m"]]},
    {"id": "SP07", "name": "Tai nghe chụp tai Lumo H3", "brand": "Lumo", "cat": "Tai nghe", "shape": "headphones",
     "price": 890_000, "old": None, "spec": "không dây, pin 40 giờ", "feature": "tai nghe chụp tai không dây, pin 40h, nghe nhạc và học tập",
     "attrs": {"anc": False, "wear": "Chụp tai"}, "warranty": 12, "featured": False,
     "variants": [V("Đen", BLACK, 12), V("Xanh navy", NAVY, 7)],
     "specs": [["Kết nối", "Bluetooth 5.1"], ["Chống ồn", "Không"], ["Pin", "40 giờ"]]},
    {"id": "SP08", "name": "Sạc nhanh Voltix 65W GaN", "brand": "Voltix", "cat": "Sạc & cáp", "shape": "charger",
     "price": 520_000, "old": 650_000, "spec": "65 W, 2 cổng USB-C", "feature": "củ sạc nhanh 65W GaN, sạc laptop và điện thoại",
     "attrs": {}, "warranty": 18, "featured": True,
     "variants": [V("Trắng", WHITE, 30), V("Đen", BLACK, 18)],
     "specs": [["Công suất", "65 W"], ["Cổng", "2 USB-C, 1 USB-A"], ["Công nghệ", "GaN"]]},
    {"id": "SP09", "name": "Sạc dự phòng Voltix 20.000 mAh", "brand": "Voltix", "cat": "Sạc & cáp", "shape": "powerbank",
     "price": 690_000, "old": None, "spec": "20.000 mAh, sạc nhanh 22,5 W", "feature": "pin sạc dự phòng 20000mAh, sạc nhanh, đi du lịch",
     "attrs": {}, "warranty": 12, "featured": False,
     "variants": [V("Đen", BLACK, 22), V("Xám", GREY, 9)],
     "specs": [["Dung lượng", "20.000 mAh"], ["Công suất", "22,5 W"], ["Cổng", "2 USB-C, 1 USB-A"]]},
    {"id": "SP10", "name": "Cáp USB-C Voltix 100W 1,5 m", "brand": "Voltix", "cat": "Sạc & cáp", "shape": "cable",
     "price": 150_000, "old": None, "spec": "100 W, bọc dù", "feature": "cáp sạc USB-C 100W bọc dù bền",
     "attrs": {}, "warranty": 12, "featured": False,
     "variants": [V("Đen", BLACK, 100), V("Trắng", WHITE, 80)],
     "specs": [["Công suất", "100 W"], ["Chiều dài", "1,5 m"], ["Vỏ", "Bọc dù"]]},
    {"id": "SP11", "name": "Chuột gaming Kova G7", "brand": "Kova", "cat": "Chuột", "shape": "mouse",
     "price": 990_000, "old": 1_190_000, "spec": "26.000 DPI, 59 g", "feature": "chuột gaming siêu nhẹ, cảm biến 26000 DPI, chơi game",
     "attrs": {"conn": "Có dây"}, "warranty": 24, "featured": False,
     "variants": [V("Đen", BLACK, 14), V("Trắng", WHITE, 0)],
     "specs": [["Cảm biến", "26.000 DPI"], ["Khối lượng", "59 g"], ["Kết nối", "USB-C có dây"]]},
    {"id": "SP12", "name": "Bàn phím không dây Lumo Slim", "brand": "Lumo", "cat": "Bàn phím", "shape": "keyboard",
     "price": 790_000, "old": None, "spec": "mỏng, phím êm", "feature": "bàn phím không dây mỏng nhẹ, phím êm, văn phòng",
     "attrs": {"conn": "Không dây"}, "warranty": 12, "featured": False,
     "variants": [V("Xám", GREY, 16), V("Trắng ngà", IVORY, 11)],
     "specs": [["Layout", "Fullsize"], ["Kết nối", "Bluetooth, 2,4 GHz"], ["Pin", "Sạc USB-C"]]},
    {"id": "SP13", "name": "Giá đỡ laptop nhôm Aurea Stand", "brand": "Aurea", "cat": "Phụ kiện", "shape": "stand",
     "price": 450_000, "old": None, "spec": "nhôm nguyên khối, gập gọn", "feature": "giá đỡ laptop bằng nhôm gập gọn, tản nhiệt",
     "attrs": {}, "warranty": 12, "featured": False,
     "variants": [V("Bạc", SILVER, 20), V("Xám đậm", "#55585c", 13)],
     "specs": [["Chất liệu", "Nhôm"], ["Kích thước laptop", "11–16 inch"], ["Góc nâng", "6 mức"]]},
    {"id": "SP14", "name": "Lót chuột cỡ lớn Kova Pad XL", "brand": "Kova", "cat": "Phụ kiện", "shape": "pad",
     "price": 220_000, "old": None, "spec": "900 × 400 mm, đế cao su", "feature": "lót chuột bàn phím cỡ lớn, đế cao su chống trượt",
     "attrs": {}, "warranty": 6, "featured": False,
     "variants": [V("Đen", BLACK, 0)],
     "specs": [["Kích thước", "900 × 400 × 3 mm"], ["Bề mặt", "Vải mịn"], ["Đế", "Cao su"]]},
]


def G(pid, name, brand, sub, shape, price, spec, feature, warranty, variants, specs):
    return {"id": pid, "name": name, "brand": brand, "cat": "Gia dụng", "sub": sub, "shape": shape, "price": price, "old": None,
            "spec": spec, "feature": feature, "attrs": {}, "warranty": warranty, "featured": False, "variants": variants, "specs": specs}


# Hàng gia dụng (dữ liệu mẫu) cho chiến dịch "Sale Gia Dụng 30%". Giá ở đây là GIÁ GỐC; giá sale do backend tính.
CATALOG += [
    G("GD01", "Ấm siêu tốc Voltix 1,8 L", "Voltix", "Đồ bếp", "kettle", 599_000, "inox 304, tự ngắt khi sôi",
      "ấm siêu tốc đun nước 1,8 lít inox 304 tự ngắt, pha trà cà phê", 12, [V("Bạc", SILVER, 24), V("Trắng", WHITE, 10)],
      [["Dung tích", "1,8 lít"], ["Công suất", "1.800 W"], ["Chất liệu", "Inox 304 hai lớp"], ["An toàn", "Tự ngắt khi sôi, chống đun khô"]]),
    G("GD02", "Nồi cơm điện Aurea 1,8 L", "Aurea", "Đồ bếp", "ricecooker", 1_290_000, "lòng nồi chống dính, giữ ấm 12 giờ",
      "nồi cơm điện 1,8 lít lòng chống dính giữ ấm 12 giờ, gia đình 4-6 người", 24, [V("Trắng", WHITE, 14), V("Đen", BLACK, 6)],
      [["Dung tích", "1,8 lít (4–6 người)"], ["Công suất", "700 W"], ["Lòng nồi", "Hợp kim phủ chống dính"], ["Giữ ấm", "12 giờ"]]),
    G("GD03", "Máy xay sinh tố Lumo 600 W", "Lumo", "Đồ bếp", "blender", 890_000, "cối thủy tinh 1,5 L, 2 tốc độ",
      "máy xay sinh tố 600W cối thủy tinh 1,5 lít xay đá, sinh tố, nước ép", 12, [V("Trắng", WHITE, 18), V("Xám", GREY, 7)],
      [["Công suất", "600 W"], ["Cối xay", "Thủy tinh 1,5 lít"], ["Tốc độ", "2 mức và chế độ nhồi"], ["Lưỡi dao", "Inox 6 cánh"]]),
    G("GD04", "Nồi chiên không dầu Kova 5 L", "Kova", "Đồ bếp", "airfryer", 1_990_000, "5 lít, màn hình cảm ứng",
      "nồi chiên không dầu 5 lít màn hình cảm ứng 8 chế độ, chiên nướng ít dầu", 24, [V("Đen", BLACK, 11)],
      [["Dung tích", "5 lít"], ["Công suất", "1.500 W"], ["Điều khiển", "Cảm ứng, 8 chế độ"], ["Nhiệt độ", "80–200 °C"]]),
    G("GD05", "Bộ nồi inox 3 đáy Aurea (3 món)", "Aurea", "Đồ bếp", "potset", 1_490_000, "3 nồi 16/20/24 cm, dùng bếp từ",
      "bộ nồi inox 3 đáy 3 món 16 20 24 cm dùng được bếp từ, nấu ăn gia đình", 24, [V("Bạc", SILVER, 9)],
      [["Số món", "3 nồi: 16, 20, 24 cm"], ["Chất liệu", "Inox 3 đáy"], ["Bếp sử dụng", "Từ, hồng ngoại, gas"], ["Nắp", "Kính cường lực"]]),
    G("GD06", "Máy hút bụi cầm tay Voltix V8", "Voltix", "Chăm sóc nhà cửa", "vacuum", 2_490_000, "không dây, pin 40 phút",
      "máy hút bụi cầm tay không dây pin 40 phút lực hút mạnh, dọn nhà và xe hơi", 24, [V("Xám", GREY, 8), V("Đen", BLACK, 4)],
      [["Loại", "Cầm tay không dây"], ["Pin", "40 phút"], ["Lực hút", "18.000 Pa"], ["Hộp bụi", "0,6 lít"]]),
    G("GD07", "Quạt đứng Lumo Breeze 5 cánh", "Lumo", "Chăm sóc nhà cửa", "fan", 790_000, "3 tốc độ, hẹn giờ, có điều khiển",
      "quạt đứng 5 cánh 3 tốc độ hẹn giờ điều khiển từ xa, phòng khách phòng ngủ", 12, [V("Trắng", WHITE, 20), V("Đen", BLACK, 12)],
      [["Số cánh", "5"], ["Tốc độ", "3 mức"], ["Hẹn giờ", "Đến 8 giờ"], ["Điều khiển", "Từ xa"]]),
    G("GD08", "Bàn ủi hơi nước Kova Steam", "Kova", "Chăm sóc nhà cửa", "iron", 450_000, "1.600 W, mặt đế chống dính",
      "bàn ủi hơi nước 1600W mặt đế chống dính, ủi quần áo", 12, [V("Xanh navy", NAVY, 0)],
      [["Công suất", "1.600 W"], ["Bình nước", "250 ml"], ["Mặt đế", "Chống dính"], ["Phun hơi", "Liên tục và tăng cường"]]),
    G("GD09", "Nồi chiên không dầu Lumo 3,5 L", "Lumo", "Đồ bếp", "airfryer", 1_390_000, "3,5 lít, núm xoay cơ",
      "nồi chiên không dầu 3,5 lít núm xoay cơ nhỏ gọn cho 2-3 người", 12, [V("Trắng", WHITE, 15), V("Đen", BLACK, 5)],
      [["Dung tích", "3,5 lít"], ["Công suất", "1.300 W"], ["Điều khiển", "Núm xoay cơ"], ["Khoảng nhiệt", "80–200 °C"]]),
    G("GD10", "Nồi cơm điện tử Kova 1 L", "Kova", "Đồ bếp", "ricecooker", 1_690_000, "điện tử 8 chế độ, hẹn giờ",
      "nồi cơm điện tử 1 lít 8 chế độ nấu hẹn giờ, 2-3 người", 24, [V("Trắng", WHITE, 7)],
      [["Dung tích", "1 lít (2–3 người)"], ["Công suất", "500 W"], ["Chế độ nấu", "8 chế độ: cơm, cháo, hấp, nấu chậm…"], ["Lòng nồi", "Gốm ceramic dày 2 mm"], ["Giữ ấm", "24 giờ"]]),
    G("GD11", "Ấm siêu tốc thủy tinh Lumo 1,5 L", "Lumo", "Đồ bếp", "kettle", 459_000, "thủy tinh chịu nhiệt, đèn LED",
      "ấm siêu tốc thủy tinh 1,5 lít chịu nhiệt đèn LED, pha trà", 12, [V("Trắng", WHITE, 16)],
      [["Dung tích", "1,5 lít"], ["Công suất", "1.500 W"], ["Chất liệu", "Thủy tinh borosilicate"], ["Tự ngắt", "Có, khi nước sôi"]]),
    G("GD12", "Máy hút bụi có dây Kova C2", "Kova", "Chăm sóc nhà cửa", "vacuum", 1_590_000, "có dây 5 m, hộp bụi 1,2 lít",
      "máy hút bụi có dây 5 mét hộp bụi 1,2 lít lực hút mạnh, dọn nhà", 12, [V("Xám", GREY, 9)],
      [["Loại", "Có dây (dây 5 m)"], ["Dung tích hộp bụi", "1,2 lít"], ["Cân nặng", "3,4 kg"], ["Lực hút", "20.000 Pa"]]),
    G("GD13", "Nồi chiên không dầu Aurea 7 L", "Aurea", "Đồ bếp", "airfryer", 2_690_000, "7 lít, cửa kính, 12 chế độ",
      "nồi chiên không dầu 7 lít cửa kính quan sát 12 chế độ cho gia đình đông người", 24, [V("Đen", BLACK, 6)],
      [["Dung tích", "7 lít"], ["Công suất", "1.800 W"], ["Điều khiển", "Cảm ứng, 12 chế độ"], ["Khoảng nhiệt", "60–220 °C"], ["Kích thước", "34 × 40 × 35 cm"]]),
]
# Loại sản phẩm (để chỉ so sánh các món cùng loại) và khối lượng đóng gói (để tính phí giao). Dữ liệu demo.
KINDS = {"GD01": "Ấm siêu tốc", "GD11": "Ấm siêu tốc", "GD02": "Nồi cơm điện", "GD10": "Nồi cơm điện", "GD03": "Máy xay sinh tố",
         "GD04": "Nồi chiên không dầu", "GD09": "Nồi chiên không dầu", "GD13": "Nồi chiên không dầu", "GD05": "Bộ nồi", "GD06": "Máy hút bụi", "GD12": "Máy hút bụi",
         "GD07": "Quạt đứng", "GD08": "Bàn ủi"}
WEIGHTS = {"GD01": 1.2, "GD11": 1.4, "GD02": 2.8, "GD10": 2.6, "GD03": 2.5, "GD04": 5.5, "GD09": 4.2, "GD05": 4.5,
           "GD06": 2.9, "GD12": 4.6, "GD07": 4.0, "GD08": 1.3, "GD13": 7.2}
COMPARE_ROWS = {
    "Nồi chiên không dầu": ["Dung tích", "Công suất", "Khoảng nhiệt", "Kích thước", "Bảo hành"],
    "Nồi cơm điện": ["Dung tích", "Công suất", "Chế độ nấu", "Lòng nồi", "Bảo hành"],
    "Ấm siêu tốc": ["Dung tích", "Công suất", "Chất liệu", "Tự ngắt", "Bảo hành"],
    "Máy hút bụi": ["Loại", "Dung tích hộp bụi", "Cân nặng", "Thời lượng pin", "Bảo hành"],
}
SPEC_PATCH = {      # bổ sung/đổi tên thông số để trang chi tiết và bảng so sánh dùng chung một nguồn
    "GD01": [["Dung tích", "1,8 lít"], ["Công suất", "1.800 W"], ["Chất liệu", "Inox 304 hai lớp"], ["Tự ngắt", "Có, khi nước sôi và khi cạn nước"]],
    "GD02": [["Dung tích", "1,8 lít (4–6 người)"], ["Công suất", "700 W"], ["Chế độ nấu", "Nấu và giữ ấm (cơ)"], ["Lòng nồi", "Hợp kim phủ chống dính"], ["Giữ ấm", "12 giờ"]],
    "GD04": [["Dung tích", "5 lít"], ["Công suất", "1.500 W"], ["Điều khiển", "Cảm ứng, 8 chế độ"], ["Khoảng nhiệt", "80–200 °C"], ["Kích thước", "30 × 36 × 32 cm"]],
    "GD06": [["Loại", "Không dây, cầm tay"], ["Dung tích hộp bụi", "0,6 lít"], ["Cân nặng", "1,6 kg"], ["Thời lượng pin", "40 phút"], ["Lực hút", "18.000 Pa"]],
}
for _p in CATALOG:
    _p["kind"] = KINDS.get(_p["id"], "")
    _p["weight"] = WEIGHTS.get(_p["id"], 0.4)
    if _p["id"] in SPEC_PATCH:
        _p["specs"] = SPEC_PATCH[_p["id"]]
BY_ID = {p["id"]: p for p in CATALOG}
STOCK0 = {f"{p['id']}|{v['name']}": v["stock"] for p in CATALOG for v in p["variants"]}   # tồn ban đầu theo SKU (mã sản phẩm|phiên bản)
STOCK_HOLD_S = int(config.get("STOCK_HOLD_S", "1800") or 1800)        # sau bao lâu thì kiểm tra lại một lượt giữ hàng
CANCEL_REASONS = ["Đổi ý", "Muốn sửa địa chỉ", "Đặt nhầm sản phẩm", "Khác"]
RECON_LABELS = {"open": "Chờ khách thanh toán", "processing": "Chưa có kết quả cuối", "timeout": "Chưa có kết quả cuối",
                "paid_wait_webhook": "Stripe đã nhận tiền · Chờ webhook", "mismatch": "Cần kiểm tra thủ công",
                "expired": "Phiên đã hết hạn, chưa thu tiền", "paid": "Đã thanh toán (Webhook đã xác nhận)"}
RECON_NOTES = {"open": "Giữ phiên hiện có.", "processing": "Giữ đơn và lần thanh toán.", "timeout": "Giữ đơn và lần thanh toán.",
               "paid_wait_webhook": "Đơn chỉ được ghi PAID khi Webhook hợp lệ tới. Khách không được cấp link thanh toán thứ hai.",
               "mismatch": "Chưa kích hoạt giao hàng.", "expired": "Khách có thể bấm Thanh toán lại.", "paid": ""}

# ---- Chiến dịch khuyến mãi. Mốc thời gian theo giờ Việt Nam (UTC+7), cấu hình trong .env:
#      SALE_GIA_DUNG_START=2026-11-13 00:00    SALE_GIA_DUNG_END=2026-11-20 23:59
VN_TZ = timezone(timedelta(hours=7))


def _vn_time(text):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text.strip(), fmt).replace(tzinfo=VN_TZ).timestamp()
        except ValueError:
            continue
    return None


CAMPAIGNS = [{
    "id": "gia-dung-30", "title": "Sale Gia Dụng 30%", "percent": 30,
    "product_ids": ["GD01", "GD02", "GD03", "GD04", "GD05", "GD06", "GD07", "GD08", "GD09", "GD10", "GD11", "GD12", "GD13"],
    "filters": [{"slug": "do-bep", "label": "Đồ bếp"}, {"slug": "cham-soc-nha-cua", "label": "Chăm sóc nhà cửa"}],
    "starts_at": _vn_time(config.get("SALE_GIA_DUNG_START")) or 0,
    # Chưa cấu hình ngày kết thúc: mặc định 7 ngày kể từ lúc khởi động, hết ngày theo giờ Việt Nam
    "ends_at": _vn_time(config.get("SALE_GIA_DUNG_END"))
               or (datetime.now(VN_TZ) + timedelta(days=7)).replace(hour=23, minute=59, second=59, microsecond=0).timestamp(),
}]


def active_campaign(product_id, now=None):
    now = time.time() if now is None else now
    return next((c for c in CAMPAIGNS if product_id in c["product_ids"] and c["starts_at"] <= now < c["ends_at"]), None)


def unit_price(product, variant, now=None):
    """Giá bán của một phiên bản tại thời điểm now: (giá bán, giá gốc, chiến dịch đang áp dụng hoặc None)."""
    list_price = product["price"] + variant.get("delta", 0)
    campaign = active_campaign(product["id"], now)
    if campaign:
        return list_price * (100 - campaign["percent"]) // 100, list_price, campaign
    return list_price, list_price, None


def catalog_view(now=None):
    """Danh mục kèm giá đã tính ở backend. Frontend chỉ hiển thị price/old/sale_pct, không tự tính giảm giá."""
    out = []
    for product in CATALOG:
        variants = []
        for variant in product["variants"]:
            price, list_price, campaign = unit_price(product, variant, now)
            old = list_price if campaign else (product["old"] + variant.get("delta", 0) if product["old"] else None)
            variants.append({**variant, "price": price, "old": old})
        campaign = active_campaign(product["id"], now)
        first = next((v for v in variants if v["stock"] > 0), variants[0])
        out.append({**product, "variants": variants, "price": first["price"], "old": first["old"], "list_price": product["price"],
                    "sale_pct": campaign["percent"] if campaign else 0, "campaign": campaign["id"] if campaign else None,
                    "compare_rows": COMPARE_ROWS.get(product["kind"]) or [k for k, _ in product["specs"]] + ["Bảo hành"]})
    return out


def campaigns_view(now=None):
    now = time.time() if now is None else now
    return [{"id": c["id"], "title": c["title"], "percent": c["percent"], "product_ids": c["product_ids"], "filters": c["filters"],
             "starts_at": c["starts_at"], "ends_at": c["ends_at"], "active": c["starts_at"] <= now < c["ends_at"], "now": now}
            for c in CAMPAIGNS]

ORDER_PRICE_HOLD_S = float(config.get("ORDER_PRICE_HOLD_HOURS") or 24) * 3600   # giá của đơn chưa trả được giữ trong 24 giờ
SIM_SESSION_TTL_S = 1800

# ---- Quy tắc giá của demo. Mọi màn hình (giỏ, checkout, chi tiết đơn) lấy số tiền từ hàm quote() dưới đây.
SHIPPING_FEE = 25_000               # phí giao hàng mỗi đơn
DISCOUNT_FROM = 2_500_000           # tạm tính từ mức này được giảm
DISCOUNT_AMOUNT = 100_000
DISCOUNT_LABEL = "Ưu đãi đơn từ 2.500.000 ₫"


# ---- Phí và thời gian giao: BẢNG ƯỚC TÍNH NỘI BỘ của demo (GHN đang giả lập nên chưa lấy phí từ GHN).
AREAS = [
    {"code": "hcm", "name": "TP. Hồ Chí Minh", "base": 25_000, "eta": (1, 2)},
    {"code": "hn", "name": "Hà Nội", "base": 32_000, "eta": (2, 4)},
    {"code": "dn", "name": "Đà Nẵng", "base": 32_000, "eta": (2, 4)},
    {"code": "mien-nam", "name": "Tỉnh khác ở miền Nam", "base": 30_000, "eta": (2, 3)},
    {"code": "mien-trung", "name": "Tỉnh khác ở miền Trung", "base": 36_000, "eta": (3, 5)},
    {"code": "mien-bac", "name": "Tỉnh khác ở miền Bắc", "base": 38_000, "eta": (3, 5)},
    {"code": "dao", "name": "Huyện đảo (Côn Đảo, Trường Sa, Hoàng Sa)", "base": None, "eta": None},
]
AREA_BY_CODE = {a["code"]: a for a in AREAS}
DEFAULT_AREA = "hcm"
SHIP_INCLUDED_KG = 3            # phí cơ bản áp dụng cho 3 kg đầu
SHIP_PER_EXTRA_KG = 5_000       # mỗi kg tiếp theo (làm tròn lên)
SHIP_SOURCE = "Bảng phí ước tính nội bộ"


def shipping_for(lines, area_code):
    """Phí và thời gian giao cho TOÀN BỘ giỏ tới một khu vực."""
    area = AREA_BY_CODE.get(area_code or DEFAULT_AREA)
    weight = round(sum(BY_ID[l["id"]]["weight"] * l["qty"] for l in lines if l.get("id") in BY_ID), 2)
    base = {"area": area["code"] if area else area_code, "area_name": area["name"] if area else "", "weight_kg": weight,
            "source": SHIP_SOURCE, "estimate": True}
    if not area:
        return {**base, "ok": False, "error": "unknown_area", "fee": 0, "message": "Chưa chọn khu vực giao hàng."}
    if area["base"] is None:
        return {**base, "ok": False, "error": "undeliverable", "fee": 0, "message": f"Chưa giao được đến {area['name']}."}
    extra = max(0, -(-(weight - SHIP_INCLUDED_KG) // 1))                    # số kg vượt mức, làm tròn lên
    fee = area["base"] + int(extra) * SHIP_PER_EXTRA_KG if lines else 0
    lo, hi = area["eta"]
    return {**base, "ok": True, "error": "", "fee": fee, "eta_days": [lo, hi], "eta_text": f"{lo}–{hi} ngày"}


def quote(items, now=None, area=None, own=None):
    """Nguồn tính tiền duy nhất: giá sau khuyến mãi -> tạm tính − giảm giá đơn + phí giao hàng = tổng thanh toán."""
    lines = []
    for key, qty in (items or {}).items():
        pid, _, variant_name = str(key).partition("|")
        product = BY_ID.get(pid)
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            qty = 0
        if not product or qty <= 0:
            continue
        if variant_name:
            variant = next((v for v in product["variants"] if v["name"] == variant_name), None)
        else:
            variant = next((v for v in product["variants"] if v["stock"] > 0), product["variants"][0])
        if variant is None:
            lines.append({"key": key, "id": pid, "variant": variant_name, "qty": qty, "price": 0, "line_total": 0,
                          "problem": "Phiên bản không còn bán"})
            continue
        price, list_price, campaign = unit_price(product, variant, now)
        problem = ""
        can_sell = variant["stock"] + (own or {}).get(f"{pid}|{variant['name']}", 0)   # own: hàng chính đơn này đang giữ
        if can_sell == 0:
            problem = "Hết hàng"
        elif can_sell < qty:
            problem = f"Chỉ còn {can_sell} sản phẩm"
        lines.append({"key": key, "id": pid, "variant": variant["name"], "qty": qty, "price": price, "list_price": list_price,
                      "saving": (list_price - price) * qty, "campaign": campaign["title"] if campaign else "",
                      "line_total": price * qty, "stock": variant["stock"], "problem": problem})
    subtotal = sum(line["line_total"] for line in lines)
    savings = sum(line.get("saving", 0) for line in lines)
    discount = DISCOUNT_AMOUNT if subtotal >= DISCOUNT_FROM else 0
    shipping = shipping_for([l for l in lines if l["price"]], area)
    fee = shipping["fee"]
    return {"lines": lines, "problems": [{"key": l["key"], "message": l["problem"],
                                          "name": f"{BY_ID[l['id']]['name']} ({l['variant']})" if l["id"] in BY_ID else l["key"]}
                                         for l in lines if l["problem"]],
            "subtotal": subtotal, "discount": discount, "discount_label": DISCOUNT_LABEL if discount else "",
            "savings": savings, "savings_label": ", ".join(sorted({l["campaign"] for l in lines if l.get("campaign")})),
            "goods_total": subtotal - discount, "shipping": shipping, "shipping_error": shipping["error"],
            "shipping_fee": fee, "total": subtotal - discount + fee}


# ---- Quy tắc chấm rủi ro (nhóm đề xuất)
RISK_NEW_ACCOUNT_HOURS = 24
RISK_NEW_ACCOUNT_AMOUNT = 5_000_000
RISK_FAILED_PAYMENTS = 3            # trong 30 phút
RISK_ORDER_BURST = 5                # trong 10 phút

WEBHOOK_TOLERANCE_S = 300           # cửa sổ thời gian chống phát lại Webhook
PAYMENT_MAX_RETRIES = 3
RETRY_BASE_DELAY = 0.05             # giây; để nhỏ cho demo, thực tế là 1-2 giây


class Backend:
    TABLES = ("orders", "tickets", "payment_failures", "processed_events", "outbox", "events", "payment_attempts", "kv",
              "inventory", "stock_holds")

    def __init__(self, live=False, db_path=":memory:"):
        """live=True (app.py): dịch vụ nào đủ cấu hình trong .env thì gọi API thật, còn lại chạy giả lập.
        live=False (kiểm thử tự động): luôn giả lập."""
        self.live = live
        self.db_path = db_path          # ":memory:" cho kiểm thử; app.py dùng file SQLite để giữ dữ liệu qua các lần khởi động
        self.reset()

    # ------------------------------------------------------------------ khởi tạo
    def reset(self, wipe=False):
        self.hub = ServiceHub()
        self.ai = OpenAIService(self.hub, SECRETS["OPENAI_API_KEY"])
        self.firebase = FirebaseAuthService(self.hub)
        self.turnstile = TurnstileService(self.hub, SECRETS["TURNSTILE_SECRET_KEY"])
        self.stripe = StripeService(self.hub, SECRETS["STRIPE_SECRET_KEY"], SECRETS["STRIPE_WEBHOOK_SECRET"])
        self.ghn = GHNService(self.hub, SECRETS["GHN_TOKEN"], SECRETS["GHN_SHOP_ID"])
        self.brevo = BrevoService(self.hub, SECRETS["BREVO_API_KEY"])
        # ---- Tích hợp thật: chỉ bật khi chạy app.py và có đủ cấu hình
        self.real_ai = self.real_turnstile = self.real_stripe = None
        self.turnstile_site_key = config.get("TURNSTILE_SITE_KEY")
        self.turnstile_hostname = config.get("TURNSTILE_EXPECTED_HOSTNAME", "localhost") or "localhost"
        self.app_base_url = (config.get("APP_BASE_URL") or "http://localhost:" + config.get("PORT", "8000")).rstrip("/")
        self.admin_emails = {e.strip().lower() for e in (config.get("ADMIN_EMAILS") or "admin@goctech.example").split(",") if e.strip()}
        if self.live:
            if config.get("OPENAI_API_KEY") and config.get("OPENAI_MODEL"):
                self.real_ai = RealOpenAI(self.hub, config.get("OPENAI_API_KEY"), config.get("OPENAI_MODEL"), config.get("OPENAI_BASE_URL"),
                                          timeout=float(config.get("OPENAI_TIMEOUT_S") or 20))
            if config.get("TURNSTILE_SECRET_KEY") and self.turnstile_site_key:
                self.real_turnstile = RealTurnstile(self.hub, config.get("TURNSTILE_SECRET_KEY"), config.get("TURNSTILE_VERIFY_URL"))
            stripe_key = config.get("STRIPE_SECRET_KEY")
            if stripe_key.startswith(("sk_test_", "rk_test_")) and config.get("STRIPE_WEBHOOK_SECRET"):   # chỉ nhận khóa Sandbox
                self.real_stripe = RealStripe(self.hub, stripe_key, config.get("STRIPE_API_BASE"))
        self.boot_id = os.urandom(4).hex()
        self._force_sim = False      # True khi chạy kịch bản dựng sẵn / trang demo: luôn dùng dịch vụ giả lập
        self.events = []             # dòng thời gian hiển thị trên giao diện
        self.alerts = []
        self.pending_webhooks = []   # Webhook Stripe đang trên đường tới (giả lập độ trễ)
        self.last_event = {}         # order_id -> (raw, header) của sự kiện thanh toán gần nhất, để gửi lại
        self.demo_runs = {}          # run_id -> thông tin một lần chạy tình huống ở trang /demo
        self._uid = None             # khách đang được xử lý (gắn vào nhật ký khi chưa có mã đơn)
        self._demo = None            # tên tình huống demo đang tạo đơn
        if getattr(self, "db", None) is not None:
            self.db.close()
        self.db = sqlite3.connect(self.db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if wipe:
            for table in self.TABLES:
                self.db.execute(f"DROP TABLE IF EXISTS {table}")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, uid TEXT, email TEXT, name TEXT, address TEXT, items TEXT,
                amount INTEGER, currency TEXT, risk TEXT, risk_reasons TEXT,
                order_status TEXT, payment_status TEXT, attempt INTEGER DEFAULT 0,
                session_id TEXT, payment_intent TEXT, checkout_url TEXT,
                tracking_code TEXT, created_at REAL, paid_at REAL,
                subtotal INTEGER, discount INTEGER DEFAULT 0, shipping_fee INTEGER, ship_status TEXT,
                shipped_at REAL, delivering_at REAL, delivered_at REAL,
                ship_check TEXT, demo TEXT, reject_reason TEXT,
                pay_mode TEXT DEFAULT 'sim', session_expires_at REAL, returned_at REAL, priced_at REAL, stripe_seen TEXT,
                area TEXT, area_name TEXT, eta TEXT,
                reviewed_by TEXT, reviewed_at REAL, review_result TEXT);
            CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY, order_id TEXT, uid TEXT, topic TEXT, item TEXT, body TEXT, status TEXT, reply TEXT,
                created_at REAL, updated_at REAL, client_key TEXT UNIQUE, history TEXT);
            CREATE TABLE IF NOT EXISTS payment_failures (uid TEXT, at REAL);
            CREATE TABLE IF NOT EXISTS processed_events (
                event_id TEXT PRIMARY KEY, type TEXT, at REAL, order_id TEXT, result TEXT, dup_count INTEGER DEFAULT 0, last_at REAL);
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_key TEXT UNIQUE, order_id TEXT,
                task TEXT, status TEXT DEFAULT 'PENDING', attempts INTEGER DEFAULT 0, note TEXT,
                created_at REAL, updated_at REAL, next_attempt_at REAL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, time REAL, order_id TEXT, uid TEXT, step TEXT, service TEXT,
                status TEXT, detail TEXT, ref TEXT);
            CREATE TABLE IF NOT EXISTS payment_attempts (
                session_id TEXT PRIMARY KEY, order_id TEXT, attempt INTEGER, mode TEXT, status TEXT, created_at REAL, updated_at REAL);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            CREATE TABLE IF NOT EXISTS inventory (sku TEXT PRIMARY KEY, on_hand INTEGER, sold INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS stock_holds (
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, sku TEXT, quantity INTEGER, state TEXT,
                created_at REAL, check_at REAL, updated_at REAL, note TEXT);
        """)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(orders)")}
        for column, kind in (("risk_score", "INTEGER"), ("canceled_at", "REAL"), ("canceled_by", "TEXT"), ("cancel_reason", "TEXT"),
                             ("recon_result", "TEXT"), ("recon_detail", "TEXT"), ("recon_at", "REAL")):
            if column not in have:
                self.db.execute(f"ALTER TABLE orders ADD COLUMN {column} {kind}")
        self.db.executemany("INSERT OR IGNORE INTO inventory (sku, on_hand, sold) VALUES (?,?,0)", list(STOCK0.items()))
        self.db.commit()
        self._sync_stock()
        self._ref = None
        self._saved = None
        self._load_state()

    # ---------------------------------------------- Lưu bền: chạy tiếp sau khi khởi động lại
    def _load_state(self):
        """Đơn, lần thanh toán, sự kiện webhook và tác vụ nằm trong các bảng SQLite. Ở đây nạp lại nhật ký workflow
        và trạng thái của các dịch vụ GIẢ LẬP (GHN, Brevo, Stripe giả...) để sau khi khởi động lại chúng vẫn "nhớ" việc đã làm."""
        self.events = [dict(r) for r in self.db.execute("SELECT time, order_id, uid, step, service, status, detail, ref FROM events ORDER BY id")]
        last = self.db.execute("SELECT id FROM orders ORDER BY CAST(SUBSTR(id, 5) AS INTEGER) DESC LIMIT 1").fetchone()
        self._seq = int(last["id"][4:]) if last else 0
        row = self.db.execute("SELECT v FROM kv WHERE k='sim_state'").fetchone()
        if not row:
            return
        state = json.loads(row["v"])
        self.ghn.orders, self.brevo.sent = state["ghn"], state["brevo"]
        self.stripe.sessions, self.stripe.refunds = state["stripe_sessions"], state["stripe_refunds"]
        self.firebase.users.update(state["users"])
        self.turnstile.used = set(state["turnstile_used"])
        self.pending_webhooks, self.demo_runs, self.alerts = state["pending_webhooks"], state["demo_runs"], state["alerts"]
        self.last_event = {k: tuple(v) for k, v in state["last_event"].items()}
        self._saved = row["v"]

    def save_state(self):
        """Gọi sau mỗi yêu cầu và mỗi nhịp worker. Chỉ ghi khi có thay đổi."""
        if self.db_path == ":memory:":
            return
        blob = dumps({"ghn": self.ghn.orders, "brevo": self.brevo.sent, "stripe_sessions": self.stripe.sessions,
                      "stripe_refunds": self.stripe.refunds, "users": self.firebase.users, "turnstile_used": sorted(self.turnstile.used),
                      "pending_webhooks": self.pending_webhooks, "demo_runs": self.demo_runs, "alerts": self.alerts,
                      "last_event": {k: list(v) for k, v in self.last_event.items()}})
        if blob != self._saved:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES ('sim_state', ?)", (blob,))
            self._saved = blob
        self.db.commit()


    def log(self, step, service, status, detail, order_id=None, ref=None):
        """status: OK | WARN | FAIL | INFO. ref: mã tham chiếu của bước (Session ID, Event ID, mã vận đơn...)."""
        detail = re.sub(r"(?<![<\w.+-])([\w.+-])[\w.+-]*@([\w-]+(?:\.[\w-]+)+)", r"\1***@\2", detail)   # không ghi email đầy đủ của khách vào log
        event = {"time": time.time(), "order_id": order_id, "uid": self._uid, "step": step, "service": service,
                 "status": status, "detail": detail, "ref": ref or self._ref}
        self.events.append(event)
        self.db.execute("INSERT INTO events (time, order_id, uid, step, service, status, detail, ref) VALUES (?,?,?,?,?,?,?,?)",
                        tuple(event[k] for k in ("time", "order_id", "uid", "step", "service", "status", "detail", "ref")))

    # ------------------------------------------------------------ Kho: giữ hàng theo SKU
    def _sync_stock(self):
        """Số CÓ THỂ BÁN = tồn kho − hàng đang giữ (HELD). Ghi vào danh mục để mọi chỗ tính giá/giỏ hàng dùng chung một số."""
        held = {r["sku"]: r["q"] for r in self.db.execute("SELECT sku, SUM(quantity) q FROM stock_holds WHERE state='HELD' GROUP BY sku")}
        on_hand = {r["sku"]: r["on_hand"] for r in self.db.execute("SELECT sku, on_hand FROM inventory")}
        for product in CATALOG:
            for variant in product["variants"]:
                sku = f"{product['id']}|{variant['name']}"
                variant["stock"] = max(0, on_hand.get(sku, STOCK0[sku]) - held.get(sku, 0))

    @staticmethod
    def sku_name(sku):
        pid, _, variant = sku.partition("|")
        return f"{BY_ID[pid]['name']} ({variant})" if pid in BY_ID else sku

    def _order_skus(self, order):
        need = {}
        for line in json.loads(order["items"]):
            sku = f"{line['id']}|{line['variant']}"
            need[sku] = need.get(sku, 0) + line["qty"]
        return need

    def _hold_stock(self, order):
        """Giữ TOÀN BỘ SKU của đơn trong một transaction. Thiếu một món thì không giữ món nào và trả về tên các món thiếu.
        Đơn đã có lượt giữ còn hiệu lực thì dùng lại. Đơn của kịch bản demo dựng sẵn không đụng tới kho."""
        if order["demo"]:
            return []
        now, need = time.time(), self._order_skus(order)
        mine = {r["sku"]: r["quantity"] for r in self.db.execute(
            "SELECT sku, SUM(quantity) quantity FROM stock_holds WHERE order_id=? AND state='HELD' GROUP BY sku", (order["id"],))}
        if mine and all(mine.get(sku, 0) >= qty for sku, qty in need.items()):
            return []
        held = {r["sku"]: r["q"] for r in self.db.execute("SELECT sku, SUM(quantity) q FROM stock_holds WHERE state='HELD' GROUP BY sku")}
        on_hand = {r["sku"]: r["on_hand"] for r in self.db.execute("SELECT sku, on_hand FROM inventory")}
        missing = [self.sku_name(sku) for sku, qty in need.items()
                   if on_hand.get(sku, 0) - held.get(sku, 0) + mine.get(sku, 0) < qty]
        if missing:
            self.log("Giữ hàng", "Kho", "FAIL", f"Không đủ hàng: {', '.join(missing)}. Không giữ món nào, chưa mở Stripe", order["id"])
            return missing
        self.db.execute("UPDATE stock_holds SET state='RELEASED', updated_at=?, note='thay bằng lượt giữ mới' WHERE order_id=? AND state='HELD'",
                        (now, order["id"]))
        self.db.executemany("INSERT INTO stock_holds (order_id, sku, quantity, state, created_at, check_at, updated_at, note) VALUES (?,?,?,?,?,?,?,?)",
                            [(order["id"], sku, qty, "HELD", now, now + STOCK_HOLD_S, now, "") for sku, qty in need.items()])
        self.db.commit()
        self._sync_stock()
        self.log("Giữ hàng", "Kho", "OK", "Giữ " + ", ".join(f"{qty} × {self.sku_name(sku)}" for sku, qty in need.items()) + " cho đơn trước khi mở Stripe", order["id"])
        return []

    def _consume_stock(self, order_id):
        """Webhook thanh toán thành công: hàng đang giữ thành hàng đã bán, đúng một lần."""
        order = self.order(order_id)
        if not order or order["demo"]:
            return
        now = time.time()
        rows = self.db.execute("SELECT id, sku, quantity FROM stock_holds WHERE order_id=? AND state='HELD'", (order_id,)).fetchall()
        if not rows:
            if self.db.execute("SELECT 1 FROM stock_holds WHERE order_id=? AND state='CONSUMED'", (order_id,)).fetchone():
                return                                      # đã trừ kho ở lần trước
            for sku, qty in self._order_skus(order).items():  # tiền về sau khi lượt giữ đã được giải phóng: vẫn ghi nhận đã bán
                self.db.execute("INSERT INTO stock_holds (order_id, sku, quantity, state, created_at, check_at, updated_at, note) VALUES (?,?,?,?,?,?,?,?)",
                                (order_id, sku, qty, "HELD", now, now, now, "tạo lại khi tiền về sau lúc giải phóng"))
            rows = self.db.execute("SELECT id, sku, quantity FROM stock_holds WHERE order_id=? AND state='HELD'", (order_id,)).fetchall()
            self.log("Giữ hàng", "Kho", "WARN", "Tiền về khi đơn không còn lượt giữ hàng: ghi nhận đã bán, cần kiểm tra tồn kho", order_id)
        for row in rows:
            self.db.execute("UPDATE inventory SET on_hand=MAX(0, on_hand-?), sold=sold+? WHERE sku=?", (row["quantity"], row["quantity"], row["sku"]))
            self.db.execute("UPDATE stock_holds SET state='CONSUMED', updated_at=? WHERE id=?", (now, row["id"]))
        self._sync_stock()
        self.log("Giữ hàng", "Kho", "OK", "Chuyển hàng đã giữ thành hàng đã bán (một lần)", order_id)

    def _release_stock(self, order_id, why):
        changed = self.db.execute("UPDATE stock_holds SET state='RELEASED', updated_at=?, note=? WHERE order_id=? AND state='HELD'",
                                  (time.time(), why, order_id)).rowcount
        if changed:
            self._sync_stock()
            self.log("Giữ hàng", "Kho", "INFO", f"Trả hàng đã giữ về kho: {why}", order_id)
        return changed

    def _set_recon(self, order_id, result, detail=None):
        self.db.execute("UPDATE orders SET recon_result=?, recon_detail=?, recon_at=? WHERE id=?",
                        (result, dumps(detail or {}), time.time(), order_id))

    def check_holds(self, now=None):
        """Đến thời điểm kiểm tra lại của một lượt giữ: hỏi trạng thái thanh toán TRƯỚC khi giải phóng.
        Còn phiên mở: giữ tiếp. Stripe đang xử lý hoặc không liên lạc được: giữ hàng và đưa đơn sang đối soát."""
        now = time.time() if now is None else now
        out = {}
        for (order_id,) in self.db.execute("SELECT DISTINCT order_id FROM stock_holds WHERE state='HELD' AND check_at<=?", (now,)).fetchall():
            order = self.order(order_id)
            status = order["order_status"] if order else "CANCELED"
            again = None
            if status in self.PAID_STATES:
                self._consume_stock(order_id)
                out[order_id] = "consumed"
            elif status != "PAYMENT_PENDING":
                self._release_stock(order_id, {"CANCELED": "đơn đã hủy", "PAYMENT_FAILED": "thanh toán thất bại, chưa thu tiền"}.get(status, "đơn chưa mở thanh toán"))
                out[order_id] = "released"
            elif any(w["order_id"] == order_id for w in self.pending_webhooks):
                again = now + 60
                self._set_recon(order_id, "processing", {"note": "Cổng thanh toán đang xử lý giao dịch"})
            else:
                state = self._reconcile(order)
                fresh = self.order(order_id)
                if state == "open":
                    again = max(now + 30, (fresh["session_expires_at"] or now) + 1)
                elif state in ("complete", "unknown"):
                    again = now + 120
                    result = "timeout" if state == "unknown" else "paid_wait_webhook" if fresh["stripe_seen"] == "paid" else "processing"
                    self._set_recon(order_id, result, {"note": "Kiểm tra tự động khi lượt giữ hàng đến hạn"})
                    self.log("Giữ hàng", "Kho", "WARN", "Lượt giữ hàng đến hạn nhưng " + ("chưa liên lạc được Stripe" if state == "unknown" else "Stripe đang xử lý hoặc đã nhận tiền")
                             + ": giữ hàng, chuyển đơn sang đối soát", order_id)
                else:
                    self._release_stock(order_id, "phiên thanh toán đã hết hạn và chưa thu tiền")
                    out[order_id] = "released"
            if again:
                self.db.execute("UPDATE stock_holds SET check_at=?, updated_at=? WHERE order_id=? AND state='HELD'", (again, now, order_id))
                out[order_id] = "kept"
        if out:
            self.db.commit()
        return out

    def stock_view(self):
        held = {r["sku"]: r["q"] for r in self.db.execute("SELECT sku, SUM(quantity) q FROM stock_holds WHERE state='HELD' GROUP BY sku")}
        return {"ok": True, "rows": [{"sku": r["sku"], "name": self.sku_name(r["sku"]), "on_hand": r["on_hand"], "held": held.get(r["sku"], 0),
                                      "sold": r["sold"], "available": max(0, r["on_hand"] - held.get(r["sku"], 0))}
                                     for r in self.db.execute("SELECT * FROM inventory ORDER BY sku")]}

    def set_stock(self, sku, on_hand, actor=""):
        """Công cụ thử cho admin: đặt lại tồn kho của một SKU."""
        try:
            on_hand = int(on_hand)
        except (TypeError, ValueError):
            on_hand = -1
        if sku not in STOCK0 or on_hand < 0:
            return {"ok": False, "message": "SKU hoặc số lượng không hợp lệ."}
        self.db.execute("UPDATE inventory SET on_hand=? WHERE sku=?", (on_hand, sku))
        self.db.commit()
        self._sync_stock()
        self.log("Giữ hàng", "Người vận hành", "INFO", f"Đặt tồn kho {self.sku_name(sku)} = {on_hand}." + (f" Người thực hiện: {actor}." if actor else ""))
        return {"ok": True}

    def order(self, order_id):
        row = self.db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------- Bước 1: tư vấn bằng AI
    @staticmethod
    def scrub_pii(text):
        """Lọc email và số điện thoại khỏi câu hỏi trước khi gửi cho AI Service."""
        text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "[email]", text)
        return re.sub(r"\b0\d{8,10}\b", "[số điện thoại]", text)

    def _sim(self, uid=None):
        """Khách demo và kịch bản dựng sẵn luôn dùng dịch vụ giả lập, kể cả khi đã cấu hình API thật."""
        return self._force_sim or bool(self._demo) or any(run["uid"] == uid for run in self.demo_runs.values())

    def ask_ai(self, question):
        clean = self.scrub_pii(question)[:400]
        if clean != question[:400]:
            self.log("1. Tư vấn", "Backend", "INFO", "Đã lọc thông tin cá nhân khỏi câu hỏi trước khi gửi AI")
        # Backend truy vấn danh mục TRƯỚC. AI chỉ nhận mã, tên, giá, tồn kho, thông số; không có địa chỉ, SĐT hay thanh toán.
        for_ai = [{"id": p["id"], "name": p["name"], "cat": p["cat"], "feature": p["feature"], "spec": p["spec"],
                   "price": p["price"], "in_stock": any(v["stock"] > 0 for v in p["variants"]),
                   "stock": sum(v["stock"] for v in p["variants"])} for p in catalog_view()]
        budget, wanted, scored = shortlist(clean, for_ai)
        cat = next(iter(wanted)) if len(wanted) == 1 else ""
        hint = {"cat": cat, "budget": int(budget) if budget else None}      # để giao diện mở đúng bộ lọc
        real = self.real_ai if not self._sim() else None
        mode = "real" if real else "sim"
        try:
            if real:
                relevant = [p for score, p in scored if score] or [p for _, p in scored]
                candidates = relevant[:8]
                if not candidates:
                    answer = {"reply_text": "Danh mục hiện không có sản phẩm đáp ứng điều kiện này.", "recommended_product_ids": [], "reasons": {}}
                else:
                    out = real.recommend(clean, [{k: p[k] for k in ("id", "name", "cat", "price", "stock", "spec", "feature")} for p in candidates])
                    allowed = {p["id"] for p in candidates}          # đối chiếu từng mã AI trả về với danh mục đã lọc
                    picks = [p for p in out["picks"] if p["id"] in allowed]
                    dropped = len(out["picks"]) - len(picks)
                    if dropped:
                        self.log("1. Tư vấn", "Backend", "WARN", f"Bỏ {dropped} mã sản phẩm AI trả về vì không có trong danh mục đã lọc")
                    answer = {"reply_text": out["reply"], "recommended_product_ids": [p["id"] for p in picks],
                              "reasons": {p["id"]: p["reason"] for p in picks}}
            else:
                answer = self.ai.recommend(SECRETS["OPENAI_API_KEY"], clean, for_ai)
        except (ServiceError, AuthError) as exc:
            reason = "timeout" if isinstance(exc, ServiceTimeout) else "error"
            self.log("1. Tư vấn", "OpenAI", "WARN", f"{exc}. Khách tiếp tục tự chọn từ danh mục")
            return {"source": "fallback", "reason": reason, "mode": mode, "hint": hint, "products": [],
                    "reply_text": "Tư vấn đang gián đoạn. Bạn vẫn có thể xem và mua sản phẩm trong danh mục."}
        ids = []
        for i in answer["recommended_product_ids"]:
            if i in BY_ID and i not in ids:
                ids.append(i)
        ids = ids[:3]                                                   # chỉ nhận mã có thật, tối đa 3
        priced = {p["id"]: p for p in catalog_view()}
        products = [{**priced[i], "reason": answer.get("reasons", {}).get(i, "")} for i in ids]
        self.log("1. Tư vấn", "OpenAI", "OK",
                 f"{'API thật' if real else 'Giả lập'}: gợi ý {len(products)} sản phẩm: {', '.join(ids) or 'không có'}")
        return {"source": "ai", "mode": mode, "reply_text": answer["reply_text"], "products": products, "hint": hint}

    # ---------------------------------- Bước 2 + 3: kiểm tra bảo mật và chấm rủi ro
    def quote_for(self, items, area=None):
        """Báo giá cho giao diện. Phí giao tính ở backend; nếu bảng phí tạm không dùng được thì báo rõ thay vì đoán."""
        # đơn của kịch bản demo dựng sẵn / bộ dữ liệu mẫu không đụng tới kho nên cũng không bị chặn vì hết hàng
        q = quote(items, area=area, own={sku: 10 ** 6 for sku in items} if self._demo else None)
        if "shipfee" in self.hub.faults and not self._sim():
            q["shipping"] = {**q["shipping"], "ok": False, "error": "unavailable", "fee": 0, "message": "Chưa tính được phí giao hàng."}
            q["shipping_error"], q["total"], q["shipping_fee"] = "unavailable", q["goods_total"], 0
        return q

    def checkout(self, id_token, turnstile_token, items, address="", client_total=None, email="", remote_ip=None, area=None):
        try:
            return self._checkout(id_token, turnstile_token, items, address, client_total, email, remote_ip, area)
        finally:
            self._uid = None

    def _verify_turnstile(self, token, user, remote_ip):
        """Trả về (đạt?, chi tiết). Lỗi API hay timeout đều coi là không đạt (fail closed). Không ghi token ra log."""
        real = self.real_turnstile if not self._sim(user["uid"]) else None
        try:
            verdict = real.siteverify(token, remote_ip) if real else self.turnstile.siteverify(SECRETS["TURNSTILE_SECRET_KEY"], token)
        except (ServiceError, AuthError) as exc:
            self.log("2. Kiểm tra bảo mật", "Cloudflare Turnstile", "FAIL", f"{exc}. Dừng checkout (fail closed)")
            return False, ""
        if not verdict.get("success"):
            codes = ", ".join(str(c) for c in verdict.get("error-codes", [])) or "không rõ"
            hint = " (token hết hạn hoặc đã dùng: yêu cầu widget cấp token mới)" if "timeout-or-duplicate" in codes else ""
            self.log("2. Kiểm tra bảo mật", "Cloudflare Turnstile", "FAIL",
                     f"Siteverify không đạt, mã lỗi: {codes}{hint}. Dừng checkout" if real else "Không qua kiểm tra bot. Dừng checkout")
            return False, ""
        if real:
            if verdict.get("hostname") != self.turnstile_hostname:
                self.log("2. Kiểm tra bảo mật", "Cloudflare Turnstile", "FAIL",
                         f"Siteverify trả hostname “{verdict.get('hostname')}”, cần “{self.turnstile_hostname}”. Dừng checkout")
                return False, ""
            if verdict.get("action") != "checkout":
                self.log("2. Kiểm tra bảo mật", "Cloudflare Turnstile", "FAIL",
                         f"Siteverify trả action “{verdict.get('action')}”, cần “checkout”. Dừng checkout")
                return False, ""
            return True, f" (Siteverify thật: hostname={verdict['hostname']}, action=checkout)"
        return True, ""

    def _checkout(self, id_token, turnstile_token, items, address, client_total, email, remote_ip=None, area=None):
        # Bước 2: cả hai Security Service phải đạt. API lỗi -> dừng (fail closed), không bỏ qua.
        try:
            user = self.firebase.verify_id_token(id_token)
        except ServiceError as exc:
            self.log("2. Kiểm tra bảo mật", "Firebase Authentication", "FAIL", f"{exc}. Dừng checkout (fail closed)")
            return {"ok": False, "stage": "security", "message": "Hệ thống xác thực đang lỗi, vui lòng thử lại sau."}
        if not user:
            self.log("2. Kiểm tra bảo mật", "Firebase Authentication", "FAIL", "ID Token không hợp lệ. Dừng checkout")
            return {"ok": False, "stage": "auth", "expired": True,
                    "message": "Phiên đăng nhập đã hết hạn. Hãy đăng nhập lại để tiếp tục."}
        self._uid = user["uid"]
        # Giá, khuyến mãi và tồn kho tính tại backend bằng quote(). Số tiền client gửi lên chỉ dùng để phát hiện giá đã đổi.
        q = self.quote_for(items, area)                      # phí giao tính lại cho toàn bộ giỏ theo khu vực của đơn
        if q["problems"]:
            return {"ok": False, "stage": "cart", "problems": q["problems"],
                    "message": "Không đủ hàng: " + "; ".join(f"{p['name']}: {p['message'].lower()}" for p in q["problems"]) + ". Đơn chưa được tạo."}
        if not q["lines"]:
            return {"ok": False, "stage": "cart", "message": "Giỏ hàng trống hoặc sản phẩm không tồn tại."}
        if q["shipping_error"]:
            self.log("2. Kiểm tra bảo mật", "Backend", "WARN", f"Chưa có phí giao hàng ({q['shipping_error']}): chưa tạo đơn")
            return {"ok": False, "stage": "shipping", "error": q["shipping_error"], "message": q["shipping"]["message"]}
        if client_total is not None and int(client_total) != q["total"]:
            self.log("2. Kiểm tra bảo mật", "Backend", "WARN",
                     f"Tổng tiền khách đang xem ({int(client_total):,}đ) khác giá hiện tại ({q['total']:,}đ): yêu cầu khách xác nhận lại, chưa tạo đơn".replace(",", "."))
            return {"ok": False, "stage": "price", "quote": q, "seen_total": int(client_total),
                    "message": "Giá hoặc khuyến mãi đã thay đổi so với lúc bạn xem giỏ hàng."}
        passed, detail = self._verify_turnstile(turnstile_token, user, remote_ip)
        if not passed:
            return {"ok": False, "stage": "security", "refresh": True, "message": "Chưa thể xác minh. Vui lòng thử lại."}
        mark = len(self.events)                   # hai dòng xác minh dưới đây thuộc về đúng đơn sắp tạo
        self.log("2. Kiểm tra bảo mật", "Firebase Authentication", "OK", f"ID Token hợp lệ, xác thực {user['uid']}")
        self.log("2. Kiểm tra bảo mật", "Cloudflare Turnstile", "OK", f"Token hợp lệ, không phải bot{detail}")

        lines = [{"id": l["id"], "variant": l["variant"], "qty": l["qty"], "price": l["price"], "list_price": l["list_price"]} for l in q["lines"]]
        subtotal, discount, fee, amount = q["subtotal"], q["discount"], q["shipping_fee"], q["total"]
        contact_email = email.strip() if re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", email.strip() or "") else user["email"]

        # Bước 3: chấm rủi ro theo quy tắc nội bộ
        now = time.time()
        reasons = []
        age_hours = (now - user["created_at"]) / 3600
        if age_hours < RISK_NEW_ACCOUNT_HOURS and amount > RISK_NEW_ACCOUNT_AMOUNT:
            reasons.append(f"tài khoản tạo {age_hours:.0f} giờ trước và đơn trên {RISK_NEW_ACCOUNT_AMOUNT:,}đ".replace(",", "."))
        failures = self.db.execute("SELECT COUNT(*) FROM payment_failures WHERE uid=? AND at>?",
                                   (user["uid"], now - 1800)).fetchone()[0]
        if failures >= RISK_FAILED_PAYMENTS:
            reasons.append(f"{failures} lần thanh toán thất bại trong 30 phút")
        burst = self.db.execute("SELECT COUNT(*) FROM orders WHERE uid=? AND created_at>?",
                                (user["uid"], now - 600)).fetchone()[0]
        if burst + 1 >= RISK_ORDER_BURST:
            reasons.append(f"{burst + 1} đơn trong 10 phút")
        risk = "HIGH" if reasons else "LOW"

        self._seq += 1
        order_id = f"ORD-{self._seq:03d}"
        for e in self.events[mark:]:
            if e["order_id"] is None:
                e["order_id"] = order_id
        self.db.execute("UPDATE events SET order_id=? WHERE order_id IS NULL AND uid IS ? AND step LIKE '2.%' AND time>=?",
                        (order_id, self.events[mark]["uid"], self.events[mark]["time"]))
        self.db.execute(
            "INSERT INTO orders (id, uid, email, name, address, items, amount, currency, risk, risk_reasons,"
            " order_status, payment_status, created_at, subtotal, shipping_fee, discount, demo, pay_mode, priced_at,"
            " area, area_name, eta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, user["uid"], contact_email, user["name"], address or "12 Nguyễn Văn Linh, TP.HCM",
             dumps(lines), amount, "vnd", risk, "; ".join(reasons),
             "ON_HOLD" if risk == "HIGH" else "CREATED", "NONE", now, subtotal, fee, discount,
             self._demo or next((rid for rid, run in self.demo_runs.items() if run["uid"] == user["uid"]), None),
             "real" if self.real_stripe and not self._sim(user["uid"]) else "sim", now,
             q["shipping"]["area"], q["shipping"]["area_name"], q["shipping"].get("eta_text", "")))
        self.db.commit()

        self.db.execute("UPDATE orders SET risk_score=? WHERE id=?", (len(reasons), order_id))
        self.db.commit()
        if risk == "HIGH":
            self.alerts.append({"time": now, "order_id": order_id, "reasons": reasons})
            self.log("3. Chấm rủi ro", "Backend", "WARN",
                     f"HIGH: {'; '.join(reasons)}. Giữ đơn, gửi ALERT cho người vận hành", order_id)
            return {"ok": True, "order_id": order_id, "risk": "HIGH", "message": "Đơn đang chờ duyệt thủ công."}
        self.log("3. Chấm rủi ro", "Backend", "OK", "LOW: chuyển sang thanh toán", order_id)
        payment = self.start_payment(order_id)
        return {"ok": True, "order_id": order_id, "risk": "LOW", **payment}

    def review(self, order_id, approve, reason="", actor=""):
        """Người vận hành duyệt hoặc từ chối đơn đang kiểm tra (HIGH). Quyết định được lưu vào lịch sử đơn."""
        order = self.order(order_id)
        if not order or order["order_status"] != "ON_HOLD":
            return {"ok": False, "message": "Đơn không ở trạng thái chờ duyệt."}
        who = f" Người thực hiện: {actor}." if actor else ""
        if approve:
            self.db.execute("UPDATE orders SET order_status='CREATED', reviewed_by=?, reviewed_at=?, review_result='approved' WHERE id=?",
                            (actor, time.time(), order_id))
            self.db.commit()
            self.log("3. Chấm rủi ro", "Người vận hành", "OK", "Duyệt đơn HIGH, chuyển sang thanh toán." + who, order_id)
            return {"ok": True, **self.start_payment(order_id)}
        reason = (reason or "").strip()
        self.db.execute("UPDATE orders SET order_status='CANCELED', reject_reason=?, reviewed_by=?, reviewed_at=?, review_result='rejected' WHERE id=?",
                        (reason, actor, time.time(), order_id))
        self._enqueue(order_id, "email_rejected")
        self._release_stock(order_id, "admin từ chối đơn")
        self.db.commit()
        self.log("3. Chấm rủi ro", "Người vận hành", "FAIL",
                 "Từ chối đơn HIGH: hủy đơn, email báo khách" + (f". Lý do: {reason}" if reason else "") + "." + who, order_id)
        return {"ok": True, "message": "Đã hủy đơn."}

    def view_order(self, id_token, order_id):
        """Phân quyền: khách chỉ xem được đơn của chính mình."""
        user = self.firebase.verify_id_token(id_token)
        order = self.order(order_id)
        if not user or not order or order["uid"] != user["uid"]:
            self.log("Phân quyền", "Backend", "FAIL", f"Từ chối yêu cầu xem đơn {order_id} (HTTP 403)", order_id)
            return {"ok": False, "status": 403}
        return {"ok": True, "order": order}

    # ------------------------------------------------ API cho giao diện khách hàng
    def login(self, account, name=""):
        """Giả lập đăng nhập Firebase: chọn tài khoản demo hoặc đăng ký tài khoản mới."""
        if account == "new":
            token = self.firebase.new_customer(fresh=True, name=name.strip()[:40])
        else:
            token = {"alice": "idtoken-alice", "bob": "idtoken-bob"}.get(account)
        user = self.firebase.users.get(token)
        if not user:
            return {"ok": False, "message": "Tài khoản không tồn tại."}
        self.log("Đăng nhập", "Firebase Authentication", "OK", f"Cấp ID Token cho {user['uid']}")
        return {"ok": True, "id_token": token, "name": user["name"], "email": user["email"]}

    def my_orders(self, id_token):
        """Danh sách đơn của chính khách đang đăng nhập (đã kiểm tra quyền)."""
        try:
            user = self.firebase.verify_id_token(id_token)
        except ServiceError:
            return {"ok": False, "message": "Hệ thống xác thực đang lỗi, vui lòng thử lại sau."}
        if not user:
            return {"ok": False, "expired": True, "message": "Phiên đăng nhập đã hết hạn."}
        orders = []
        confirming = {w["order_id"] for w in self.pending_webhooks}
        for row in self.db.execute("SELECT * FROM orders WHERE uid=? ORDER BY created_at DESC", (user["uid"],)):
            order = dict(row)
            order["lines"] = [{**line, "name": BY_ID[line["id"]]["name"], "shape": BY_ID[line["id"]]["shape"],
                               "hex": next((v["hex"] for v in BY_ID[line["id"]]["variants"] if v["name"] == line["variant"]), "#1f1f1f")}
                              for line in json.loads(order["items"])]
            order["confirming"] = order["id"] in confirming or self._awaiting_real(order)
            order["pay_action"] = self.pay_action(order)
            order["tickets"] = self._tickets("order_id=? AND uid=?", (order["id"], user["uid"]))
            order["paid_at_stripe"] = order["stripe_seen"] == "paid"
            order["savings"] = sum((l.get("list_price", l["price"]) - l["price"]) * l["qty"] for l in order["lines"])
            order["events"] = [e for e in self.events if e["order_id"] == order["id"]]
            order["emails"] = [m for m in self.brevo.sent.values() if m["params"]["order_id"] == order["id"]]
            orders.append(order)
        return {"ok": True, "orders": orders, "name": user["name"], "email": user["email"]}

    def owns(self, id_token, order_id):
        try:
            user = self.firebase.verify_id_token(id_token)
        except ServiceError:
            return False
        order = self.order(order_id)
        return bool(user and order and order["uid"] == user["uid"])

    # --------------------------------------------------------- Bước 4: thanh toán
    def start_payment(self, order_id, new_attempt=False):
        order = self.order(order_id)
        if not order or order["order_status"] not in ("CREATED", "PAYMENT_PENDING", "PAYMENT_FAILED"):
            return {"checkout_url": None, "message": "Đơn không ở trạng thái cho phép thanh toán."}
        missing = self._hold_stock(order)                     # giữ hàng TRƯỚC khi mở Stripe
        if missing:
            return {"checkout_url": None, "out_of_stock": missing, "pay_mode": order["pay_mode"],
                    "message": "Không đủ hàng để giữ cho đơn: " + ", ".join(missing) + ". Chưa mở trang thanh toán."}
        if order["pay_mode"] == "real":
            return self._start_payment_real(order, new_attempt)
        attempt = order["attempt"] + 1 if (new_attempt or order["attempt"] == 0) else order["attempt"]
        idempotency_key = f"checkout:{order_id}:attempt:{attempt}"
        self.db.execute("UPDATE orders SET order_status='PAYMENT_PENDING', payment_status='PENDING', attempt=? WHERE id=?",
                        (attempt, order_id))
        self.db.commit()
        for retry in range(PAYMENT_MAX_RETRIES):
            try:
                session = self.stripe.create_checkout_session(
                    SECRETS["STRIPE_SECRET_KEY"], order_id=order_id, amount=order["amount"],
                    currency=order["currency"], idempotency_key=idempotency_key)
            except ServiceError as exc:
                self.log("4. Thanh toán", "Stripe", "WARN",
                         f"{exc}. Thử lại {retry + 1}/{PAYMENT_MAX_RETRIES} với cùng Idempotency-Key", order_id)
                time.sleep(RETRY_BASE_DELAY * (2 ** retry))
                continue
            self.db.execute("UPDATE orders SET session_id=?, payment_intent=?, checkout_url=?, session_expires_at=? WHERE id=?",
                            (session["id"], session["payment_intent"], session["url"], time.time() + SIM_SESSION_TTL_S, order_id))
            self._attempt(order_id, attempt, session["id"], "sim")
            self.db.commit()
            self.log("4. Thanh toán", "Stripe", "OK",
                     f"Tạo Checkout Session {session['id']} (Idempotency-Key: {idempotency_key}). Đơn PAYMENT_PENDING", order_id, ref=session["id"])
            return {"checkout_url": session["url"], "pay_mode": "sim", "message": "Chuyển khách sang trang thanh toán Stripe (giả lập)."}
        self.log("4. Thanh toán", "Stripe", "FAIL",
                 "Payment API vẫn lỗi sau khi thử lại. Giữ đơn PAYMENT_PENDING, chưa giao hàng, chờ đối soát", order_id)
        return {"checkout_url": None, "pay_mode": "sim", "message": "Cổng thanh toán đang lỗi. Đơn được giữ, vui lòng thử lại sau."}

    def _attempt(self, order_id, attempt, session_id, mode):
        """Ghi một lần thanh toán. Phiên trước của cùng đơn (nếu còn mở) được đánh dấu là đã thay thế."""
        now = time.time()
        self.db.execute("UPDATE payment_attempts SET status='replaced', updated_at=? WHERE order_id=? AND status='open' AND session_id<>?",
                        (now, order_id, session_id))
        self.db.execute("INSERT OR IGNORE INTO payment_attempts VALUES (?,?,?,?,?,?,?)", (session_id, order_id, attempt, mode, "open", now, now))

    def _attempt_status(self, order_id, status):
        self.db.execute("UPDATE payment_attempts SET status=?, updated_at=? WHERE session_id=(SELECT session_id FROM orders WHERE id=?)",
                        (status, time.time(), order_id))

    def _awaiting_real(self, order):
        """Khách đã quay về từ trang Stripe thật nhưng Webhook chưa tới: đơn vẫn chờ, chưa ghi PAID."""
        return order["pay_mode"] == "real" and order["order_status"] == "PAYMENT_PENDING" and bool(order["returned_at"])

    def mark_returned(self, order_id):
        self.db.execute("UPDATE orders SET returned_at=? WHERE id=? AND pay_mode='real' AND order_status='PAYMENT_PENDING'",
                        (time.time(), order_id))
        self.db.commit()
        return {"ok": True}

    # --------------------------------------------------------- Hỗ trợ theo đơn hàng
    TOPICS = ["Thanh toán", "Giao hàng", "Sản phẩm có vấn đề", "Yêu cầu đổi trả"]
    TICKET_FLOW = ["Đã tiếp nhận", "Đang xử lý", "Đã phản hồi", "Đóng"]

    def _tickets(self, where="1=1", args=()):
        rows = [dict(r) for r in self.db.execute(f"SELECT * FROM tickets WHERE {where} ORDER BY created_at DESC", args).fetchall()]
        for row in rows:
            row["history"] = json.loads(row["history"] or "[]")
            row.pop("client_key", None)
        return rows

    def create_ticket(self, id_token, order_id, topic, body, item="", client_key=""):
        """Khách tạo yêu cầu hỗ trợ cho đơn của chính mình. Chỉ ghi nhận yêu cầu; không đổi trạng thái thanh toán hay giao hàng."""
        order, error = self._owned_order(id_token, order_id)
        if error:
            return error
        body = (body or "").strip()
        if topic not in self.TOPICS:
            return {"ok": False, "field": "topic", "message": "Hãy chọn một chủ đề."}
        if len(body) < 10:
            return {"ok": False, "field": "body", "message": "Hãy mô tả vấn đề (ít nhất 10 ký tự)."}
        names = {f"{BY_ID[l['id']]['name']} ({l['variant']})" for l in json.loads(order["items"])}
        item = item if item in names else ""
        key = f"{order['uid']}:{client_key}" if client_key else None
        if key:                                   # bấm gửi hai lần liên tiếp: trả lại đúng yêu cầu đã tạo
            found = self._tickets("client_key=?", (key,))
            if found:
                return {"ok": True, "ticket": found[0], "duplicate": True}
        now = time.time()
        number = self.db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] + 1
        ticket_id = f"HT-{number:03d}"
        self.db.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ticket_id, order_id, order["uid"], topic, item, body[:1000], self.TICKET_FLOW[0], "", now, now, key,
                         dumps([{"time": now, "status": self.TICKET_FLOW[0], "by": "Khách hàng"}])))
        self.db.commit()
        self.log("Hỗ trợ", "Backend", "INFO", f"Khách gửi yêu cầu hỗ trợ {ticket_id} (chủ đề: {topic})", order_id)
        return {"ok": True, "ticket": self._tickets("id=?", (ticket_id,))[0]}

    def admin_tickets(self):
        return {"tickets": self._tickets(), "flow": self.TICKET_FLOW}

    def update_ticket(self, ticket_id, status, reply="", actor=""):
        found = self._tickets("id=?", (ticket_id,))
        if not found:
            return {"ok": False, "message": "Không tìm thấy yêu cầu hỗ trợ."}
        ticket = found[0]
        if status not in self.TICKET_FLOW or self.TICKET_FLOW.index(status) < self.TICKET_FLOW.index(ticket["status"]):
            return {"ok": False, "message": "Trạng thái chỉ chuyển theo thứ tự: Đã tiếp nhận → Đang xử lý → Đã phản hồi → Đóng."}
        reply = (reply or "").strip()[:1000]
        if status == "Đã phản hồi" and not (reply or ticket["reply"]):
            return {"ok": False, "message": "Cần ghi nội dung phản hồi cho khách."}
        now = time.time()
        history = ticket["history"] + [{"time": now, "status": status, "by": actor or "Admin"}]
        self.db.execute("UPDATE tickets SET status=?, reply=?, updated_at=?, history=? WHERE id=?",
                        (status, reply or ticket["reply"], now, dumps(history), ticket_id))
        self.db.commit()
        self.log("Hỗ trợ", "Người vận hành", "INFO", f"Yêu cầu {ticket_id}: {status}." + (f" Người thực hiện: {actor}." if actor else ""), ticket["order_id"])
        return {"ok": True, "ticket": self._tickets("id=?", (ticket_id,))[0]}

    def _stripe_lines(self, order):
        """Dòng hàng gửi sang Stripe lấy từ bản ghi đơn: giá từng sản phẩm (sau khuyến mãi) và phí giao hàng riêng.
        Đơn có giảm giá theo tổng đơn thì gộp một dòng, vì Stripe không nhận dòng âm."""
        if order["discount"]:
            return None
        lines = [{"name": f"{BY_ID[l['id']]['name']} ({l['variant']})", "unit_amount": l["price"], "quantity": l["qty"]}
                 for l in json.loads(order["items"])]
        if order["shipping_fee"]:
            lines.append({"name": "Phí giao hàng", "unit_amount": order["shipping_fee"], "quantity": 1})
        return lines

    # ---------------------------------------- Khách quay lại thanh toán đơn đã đặt
    PAID_STATES = ("PAID", "SHIPPING", "DELIVERED")

    def pay_action(self, order, now=None):
        """Thao tác thanh toán đang hợp lệ với đơn (không gọi dịch vụ ngoài):
        resume | retry | check | view | hold | canceled."""
        now = time.time() if now is None else now
        status = order["order_status"]
        if status == "ON_HOLD":
            return "hold"
        if status == "CANCELED":
            return "canceled"
        if status in self.PAID_STATES:
            return "view"
        if self._awaiting_real(order) or any(w["order_id"] == order["id"] for w in self.pending_webhooks):
            return "check"
        if status == "PAYMENT_FAILED":
            return "retry"
        return "resume" if order["session_id"] and (order["session_expires_at"] or 0) > now else "retry"

    def _owned_order(self, id_token, order_id):
        """(đơn, lỗi). Chỉ chủ đơn mới thao tác được."""
        try:
            user = self.firebase.verify_id_token(id_token)
        except ServiceError:
            return None, {"ok": False, "action": "unknown", "message": "Hệ thống xác thực đang lỗi, vui lòng thử lại sau."}
        if not user:
            return None, {"ok": False, "stage": "auth", "expired": True, "message": "Phiên đăng nhập đã hết hạn. Hãy đăng nhập lại."}
        order = self.order(order_id)
        if not order or order["uid"] != user["uid"]:
            self.log("Phân quyền", "Backend", "FAIL", f"Từ chối thao tác thanh toán với đơn {order_id} (HTTP 403)", order_id if order else None)
            return None, {"ok": False, "status": 403, "message": "Không tìm thấy đơn hàng trong tài khoản này."}
        return order, None

    def _reconcile(self, order):
        """Đối soát phiên hiện tại với Stripe. Trả về open | complete | expired | none | unknown và cập nhật bản ghi đơn."""
        if not order["session_id"]:
            return "none"
        if order["pay_mode"] != "real":
            return "open" if (order["session_expires_at"] or 0) > time.time() else "expired"
        try:
            session = self.real_stripe.retrieve_session(order["session_id"])
        except (ServiceError, AuthError) as exc:
            self.log("4. Thanh toán", "Stripe", "WARN", f"Đối soát phiên chưa thực hiện được: {exc}. Giữ nguyên đơn", order["id"])
            return "unknown"
        state = session["status"]
        if state == "complete":
            self.db.execute("UPDATE orders SET returned_at=COALESCE(returned_at, ?), stripe_seen=? WHERE id=?",
                            (time.time(), session["payment_status"] or "unpaid", order["id"]))
            self.log("4. Thanh toán", "Stripe", "INFO",
                     f"Đối soát: Stripe báo phiên đã hoàn tất (payment_status={session['payment_status']}). Chờ Webhook xác nhận, không tạo phiên mới", order["id"])
        elif state == "open":
            self.db.execute("UPDATE orders SET returned_at=NULL, stripe_seen=NULL, checkout_url=COALESCE(?, checkout_url),"
                            " session_expires_at=COALESCE(?, session_expires_at) WHERE id=?", (session["url"], session["expires_at"], order["id"]))
        else:
            state = "expired"
            self._attempt_status(order["id"], "expired")
            self.db.execute("UPDATE orders SET returned_at=NULL, stripe_seen=NULL, session_expires_at=? WHERE id=?", (time.time() - 1, order["id"]))
            self._release_stock(order["id"], "Stripe báo phiên đã hết hạn, chưa thu tiền")
        self.db.commit()
        return state

    def order_status(self, id_token, order_id):
        """Nút "Kiểm tra trạng thái": cập nhật từ cổng thanh toán, không tạo phiên mới."""
        order, error = self._owned_order(id_token, order_id)
        if error:
            return error
        self.tick()
        order = self.order(order_id)
        state = "skip"
        if order["order_status"] in ("PAYMENT_PENDING", "PAYMENT_FAILED") and order["pay_mode"] == "real":
            state = self._reconcile(order)
        order = self.order(order_id)
        return {"ok": True, "action": self.pay_action(order), "session": state, "order_status": order["order_status"],
                "paid_at_stripe": order["stripe_seen"] == "paid",
                "message": "Chưa kiểm tra được với cổng thanh toán. Đơn vẫn được giữ, bạn thử lại sau ít phút." if state == "unknown" else ""}

    def order_checkout(self, id_token, order_id, confirm_price=False):
        """POST /api/orders/<mã đơn>/checkout: cấp đường dẫn thanh toán cho CÙNG đơn.
        Sản phẩm, số tiền, tiền tệ lấy từ bản ghi đơn trên máy chủ; không nhận số tiền từ trình duyệt."""
        order, error = self._owned_order(id_token, order_id)
        if error:
            return error
        action = self.pay_action(order)
        if action == "hold":
            return {"ok": True, "action": "hold", "message": "Đơn đang được kiểm tra. Bạn có thể thanh toán sau khi đơn được duyệt."}
        if action == "canceled":
            return {"ok": True, "action": "canceled", "message": "Đơn đã hủy nên không thể thanh toán."}
        if action == "view":
            return {"ok": True, "action": "paid", "message": "Đơn này đã được thanh toán."}
        if action == "check" and order["pay_mode"] != "real":
            return {"ok": True, "action": "confirming", "message": "Đang xác nhận thanh toán. Bạn chưa cần thanh toán lại."}
        real = order["pay_mode"] == "real"
        state = self._reconcile(order)
        order = self.order(order_id)
        if state == "unknown":
            return {"ok": False, "action": "unknown", "message": "Chưa kiểm tra được với cổng thanh toán. Đơn vẫn được giữ, bạn thử lại sau ít phút."}
        if state == "complete":
            return {"ok": True, "action": "confirming", "paid_at_stripe": order["stripe_seen"] == "paid",
                    "message": "Cổng thanh toán đã ghi nhận giao dịch. Đang chờ xác nhận, bạn không cần thanh toán lại."}
        reprice = self._reprice(order)
        if reprice and reprice["action"] == "unavailable":
            return reprice
        failed = order["order_status"] == "PAYMENT_FAILED"
        if state == "open" and not failed and not reprice:
            url = order["checkout_url"] if real else f"#/pay/{order_id}"
            return {"ok": True, "action": "redirect", "url": url, "pay_mode": order["pay_mode"], "resumed": True, "amount": order["amount"]}
        if reprice and not confirm_price:
            return reprice
        if state == "open":                      # phiên cũ còn mở nhưng không dùng nữa: đóng lại để nó không thu tiền được
            if real:
                try:
                    self.real_stripe.expire_session(order["session_id"])
                except (ServiceError, AuthError) as exc:
                    self.log("4. Thanh toán", "Stripe", "WARN", f"Chưa đóng được phiên cũ: {exc}. Chưa tạo phiên mới", order_id)
                    return {"ok": False, "action": "unknown", "message": "Chưa kiểm tra được với cổng thanh toán. Đơn vẫn được giữ, bạn thử lại sau ít phút."}
            self.log("4. Thanh toán", "Stripe", "INFO", "Đóng phiên thanh toán cũ trước khi tạo phiên mới cho cùng đơn", order_id)
        if reprice:
            q = reprice["quote"]
            lines = [{"id": l["id"], "variant": l["variant"], "qty": l["qty"], "price": l["price"], "list_price": l["list_price"]} for l in q["lines"]]
            self.db.execute("UPDATE orders SET items=?, amount=?, subtotal=?, discount=?, shipping_fee=?, priced_at=? WHERE id=?",
                            (dumps(lines), q["total"], q["subtotal"], q["discount"], q["shipping_fee"], time.time(), order_id))
            self.db.commit()
            self.log("4. Thanh toán", "Backend", "INFO",
                     f"Giá giữ cho đơn đã hết hiệu lực. Khách xác nhận giá mới {q['total']:,}đ (trước đó {order['amount']:,}đ)".replace(",", "."), order_id)
        self.log("4. Thanh toán", "Backend", "INFO", "Khách yêu cầu thanh toán lại: tạo phiên mới cho cùng mã đơn", order_id)
        result = self.start_payment(order_id, new_attempt=True)
        if result.get("out_of_stock"):
            return {"ok": False, "action": "unavailable", "out_of_stock": result["out_of_stock"],
                    "message": "Đã hết hàng: " + ", ".join(result["out_of_stock"]) + ". Chưa mở trang thanh toán cho đơn này."}
        if not result.get("checkout_url"):
            return {"ok": False, "action": "unknown", "message": result.get("message", "Cổng thanh toán đang lỗi. Đơn vẫn được giữ.")}
        fresh = self.order(order_id)
        return {"ok": True, "action": "redirect", "url": result["checkout_url"] if real else f"#/pay/{order_id}",
                "pay_mode": fresh["pay_mode"], "resumed": False, "amount": fresh["amount"]}

    def cancel_order(self, id_token, order_id, reason="", note=""):
        """POST /api/orders/<mã đơn>/cancel: khách hủy đơn CHƯA trả tiền. Đóng phiên Stripe và xác nhận chưa nhận tiền
        trước khi ghi hủy và trả hàng về kho. Kết quả cuối không bao giờ vừa Đã thanh toán vừa Đã hủy."""
        order, error = self._owned_order(id_token, order_id)
        if error:
            return error
        self.tick()                                           # nhận các Webhook đã tới trước khi quyết định
        order = self.order(order_id)
        status = order["order_status"]
        if status == "CANCELED":
            return {"ok": True, "action": "canceled", "already": True, "message": "Đơn này đã được hủy trước đó."}
        if status in self.PAID_STATES:
            return {"ok": False, "action": "paid", "message": "Đơn đã được thanh toán nên không hủy ở đây được. Bạn có thể gửi yêu cầu ở mục Cần hỗ trợ?"}
        if any(w["order_id"] == order_id for w in self.pending_webhooks) or (order["stripe_seen"] and self._awaiting_real(order)):
            return {"ok": False, "action": "confirming", "message": "Cổng thanh toán vừa ghi nhận giao dịch cho đơn này. Chưa hủy đơn, bạn xem lại đơn sau ít phút."}
        reason = reason if reason in CANCEL_REASONS else "Khác"
        note = self.scrub_pii((note or "").strip())[:200]
        now = time.time()
        if order["session_id"] and status in ("PAYMENT_PENDING", "PAYMENT_FAILED"):
            if order["pay_mode"] == "real":
                unknown = {"ok": False, "action": "unknown", "message": "Chưa kiểm tra được với cổng thanh toán nên chưa hủy đơn. Bạn thử lại sau ít phút."}
                confirming = {"ok": False, "action": "confirming", "message": "Cổng thanh toán vừa ghi nhận giao dịch cho đơn này. Chưa hủy đơn, bạn xem lại đơn sau ít phút."}
                state = self._reconcile(order)
                if state == "unknown":
                    return unknown
                if state == "complete":
                    return confirming
                if state == "open":
                    try:
                        closed = self.real_stripe.expire_session(order["session_id"])
                    except (ServiceError, AuthError) as exc:
                        self.log("4. Thanh toán", "Stripe", "WARN", f"Hủy đơn: chưa đóng được phiên thanh toán ({exc}). Chưa ghi hủy", order_id, ref=order["session_id"])
                        return confirming if self._reconcile(self.order(order_id)) == "complete" else unknown
                    if closed["status"] != "expired" or closed["payment_status"] == "paid":
                        return confirming
                    self.log("4. Thanh toán", "Stripe", "OK", "Hủy đơn: đã đóng phiên thanh toán, Stripe xác nhận chưa nhận tiền", order_id, ref=order["session_id"])
            self._attempt_status(order_id, "canceled")
        self.db.execute("UPDATE orders SET order_status='CANCELED', payment_status=CASE WHEN payment_status='NONE' THEN 'NONE' ELSE 'CANCELED' END,"
                        " canceled_at=?, canceled_by=?, cancel_reason=?, session_expires_at=CASE WHEN session_id IS NULL THEN NULL ELSE ? END,"
                        " returned_at=NULL, stripe_seen=NULL, recon_result=NULL WHERE id=?",
                        (now, f"Khách hàng ({order['uid']})", reason + (f": {note}" if note else ""), now - 1, order_id))
        self._release_stock(order_id, "khách hủy đơn chưa thanh toán")
        self.db.commit()
        self.log("Hủy đơn", "Khách hàng", "INFO", f"Khách hủy đơn chưa thanh toán. Lý do: {reason}" + (f" ({note})" if note else "")
                 + ". Phiên thanh toán đã đóng, hàng trả về kho", order_id)
        return {"ok": True, "action": "canceled", "message": "Đã hủy đơn. Bạn chưa bị trừ tiền."}

    def admin_check_payment(self, order_id, actor=""):
        """Nút "Kiểm tra với Stripe" ở hàng đợi đối soát. CHỈ ghi kết quả kiểm tra; việc ghi PAID vẫn do Webhook hợp lệ quyết định."""
        order = self.order(order_id)
        if not order:
            return {"ok": False, "message": "Không tìm thấy đơn."}
        if order["order_status"] in self.PAID_STATES:
            return {"ok": True, "result": "paid", "label": RECON_LABELS["paid"], "note": ""}
        if not order["session_id"]:
            return {"ok": False, "message": "Đơn chưa có phiên thanh toán để kiểm tra."}
        now, diffs, error = time.time(), [], ""
        if order["pay_mode"] != "real":
            waiting = any(w["order_id"] == order_id for w in self.pending_webhooks)
            is_open = (order["session_expires_at"] or 0) > now
            view = {"status": "complete" if waiting else "open" if is_open else "expired", "payment_status": "paid" if waiting else "unpaid",
                    "amount": order["amount"], "currency": order["currency"], "order_ref": order_id}
            result = "paid_wait_webhook" if waiting else "open" if is_open else "expired"
        else:
            try:
                session = self.real_stripe.retrieve_session(order["session_id"])
            except (ServiceError, AuthError) as exc:
                result, view, error = "timeout", None, str(exc)
            else:
                view = {"status": session["status"], "payment_status": session["payment_status"], "amount": session.get("amount_total"),
                        "currency": session.get("currency"), "order_ref": session.get("client_reference_id")}
                if session["status"] == "open":
                    result = "open"
                elif session["status"] != "complete":
                    result = "expired"
                else:
                    if view["order_ref"] and view["order_ref"] != order_id:
                        diffs.append(f"Mã đơn trên Stripe là {view['order_ref']}, đơn nội bộ là {order_id}")
                    if view["amount"] is not None and int(view["amount"]) != order["amount"]:
                        diffs.append(f"Stripe ghi {int(view['amount']):,} ₫, đơn nội bộ ghi {order['amount']:,} ₫".replace(",", "."))
                    if view["currency"] and str(view["currency"]).lower() != order["currency"]:
                        diffs.append(f"Tiền tệ trên Stripe là {str(view['currency']).upper()}, đơn nội bộ là {order['currency'].upper()}")
                    result = "mismatch" if diffs else "paid_wait_webhook" if session["payment_status"] == "paid" else "processing"
        self._set_recon(order_id, result, {"stripe": view, "diffs": diffs, "error": error, "by": actor})
        if result in ("paid_wait_webhook", "processing"):
            self.db.execute("UPDATE orders SET returned_at=COALESCE(returned_at, ?), stripe_seen=? WHERE id=?",
                            (now, "paid" if result == "paid_wait_webhook" else "unpaid", order_id))
        elif result in ("open", "expired"):
            self.db.execute("UPDATE orders SET returned_at=NULL, stripe_seen=NULL WHERE id=?", (order_id,))
        self.db.commit()
        label = RECON_LABELS[result] if order["pay_mode"] == "real" else RECON_LABELS[result].replace("Stripe", "Cổng giả lập")
        self.log("4. Thanh toán", "Stripe", "WARN" if result in ("mismatch", "timeout") else "INFO",
                 f"Đối soát thủ công{f' bởi {actor}' if actor else ''}: {label}"
                 + (". " + "; ".join(diffs) if diffs else "") + (f". Lỗi: {error}" if error else "") + ". Chỉ ghi kết quả kiểm tra, không đổi trạng thái thanh toán",
                 order_id, ref=order["session_id"])
        return {"ok": True, "result": result, "label": label, "note": RECON_NOTES[result], "diffs": diffs}

    def _reprice(self, order):
        """Đơn chưa trả quá thời gian giữ giá: tính lại theo giá hiện tại và yêu cầu khách xác nhận nếu số tiền thay đổi."""
        if time.time() - (order["priced_at"] or order["created_at"]) <= ORDER_PRICE_HOLD_S:
            return None
        mine = {r["sku"]: r["quantity"] for r in self.db.execute("SELECT sku, quantity FROM stock_holds WHERE order_id=? AND state='HELD'", (order["id"],))}
        q = quote({f"{l['id']}|{l['variant']}": l["qty"] for l in json.loads(order["items"])}, area=order["area"], own=mine)
        if q["problems"] or q["shipping_error"]:
            return {"ok": False, "action": "unavailable", "message": "Một số sản phẩm trong đơn đã hết hàng. Vui lòng đặt đơn mới."}
        if q["total"] == order["amount"]:
            return None
        return {"ok": True, "action": "reprice", "old_total": order["amount"], "new_total": q["total"], "quote": q,
                "message": "Giá giữ cho đơn này đã hết hiệu lực. Hãy xác nhận giá mới trước khi thanh toán."}

    def expire_session(self, order_id):
        """Công cụ thử nghiệm cho admin: cho phiên thanh toán của đơn hết hạn để thử nút "Thanh toán lại"."""
        order = self.order(order_id)
        if not order or not order["session_id"] or order["order_status"] != "PAYMENT_PENDING":
            return {"ok": False, "message": "Đơn không có phiên thanh toán đang mở."}
        if order["pay_mode"] == "real":
            try:
                self.real_stripe.expire_session(order["session_id"])
            except (ServiceError, AuthError) as exc:
                return {"ok": False, "message": f"Stripe chưa đóng được phiên: {exc}"}
        self._attempt_status(order_id, "expired")
        self.db.execute("UPDATE orders SET session_expires_at=?, returned_at=NULL WHERE id=?", (time.time() - 1, order_id))
        self.db.commit()
        self.log("4. Thanh toán", "Người vận hành", "INFO", "Cho phiên thanh toán hết hạn (thử nghiệm)", order_id)
        self._release_stock(order_id, "phiên thanh toán đã hết hạn và chưa thu tiền")
        self.db.commit()
        return {"ok": True}

    def _start_payment_real(self, order, new_attempt):
        """Stripe Sandbox thật: tổng tiền lấy từ đơn trong Database, gắn mã đơn vào phiên, giữ một phiên còn hiệu lực cho mỗi đơn."""
        order_id, now = order["id"], time.time()
        if (order["session_id"] and not new_attempt and order["order_status"] == "PAYMENT_PENDING"
                and (order["session_expires_at"] or 0) > now + 60):
            return {"checkout_url": order["checkout_url"], "pay_mode": "real", "message": "Dùng lại phiên thanh toán còn hiệu lực."}
        attempt = order["attempt"] + 1
        # Database nằm trong bộ nhớ nên mã đơn lặp lại sau mỗi lần khởi động: thêm mã phiên chạy để khóa không trùng với lần trước
        idempotency_key = f"checkout:{self.boot_id}:{order_id}:attempt:{attempt}"
        self.db.execute("UPDATE orders SET order_status='PAYMENT_PENDING', payment_status='PENDING', attempt=?, returned_at=NULL,"
                        " stripe_seen=NULL, session_id=NULL, checkout_url=NULL, session_expires_at=NULL WHERE id=?", (attempt, order_id))
        self.db.commit()
        for retry in range(PAYMENT_MAX_RETRIES):
            try:
                session = self.real_stripe.create_checkout_session(
                    order_id=order_id, amount=order["amount"], currency=order["currency"], idempotency_key=idempotency_key,
                    email=order["email"], lines=self._stripe_lines(order),
                    success_url=f"{self.app_base_url}/?paid={order_id}#/result/{order_id}",
                    cancel_url=f"{self.app_base_url}/?left={order_id}#/order/{order_id}")
            except AuthError as exc:
                self.log("4. Thanh toán", "Stripe", "FAIL", f"{exc}. Giữ đơn PAYMENT_PENDING, kiểm tra cấu hình Stripe Sandbox", order_id)
                break
            except ServiceError as exc:
                self.log("4. Thanh toán", "Stripe", "WARN",
                         f"{exc}. Thử lại {retry + 1}/{PAYMENT_MAX_RETRIES} với cùng Idempotency-Key", order_id)
                time.sleep(RETRY_BASE_DELAY * (2 ** retry))
                continue
            self.db.execute("UPDATE orders SET session_id=?, payment_intent=?, checkout_url=?, session_expires_at=? WHERE id=?",
                            (session["id"], session["payment_intent"], session["url"], session["expires_at"] or now + 1800, order_id))
            self._attempt(order_id, attempt, session["id"], "real")
            self.db.commit()
            self.log("4. Thanh toán", "Stripe", "OK",
                     f"Sandbox thật: tạo Checkout Session {session['id'][:14]}… (Idempotency-Key: {idempotency_key}). Đơn PAYMENT_PENDING", order_id, ref=session["id"])
            return {"checkout_url": session["url"], "pay_mode": "real", "message": "Chuyển khách sang trang thanh toán Stripe."}
        else:
            self.log("4. Thanh toán", "Stripe", "FAIL",
                     "Stripe vẫn lỗi sau khi thử lại. Giữ đơn PAYMENT_PENDING, chưa giao hàng", order_id)
        return {"checkout_url": None, "pay_mode": "real", "message": "Cổng thanh toán đang lỗi. Đơn được giữ, vui lòng thử lại sau."}

    # --------------------------------------------- Bước 5: xác nhận đơn qua Webhook
    def verify_signature(self, raw_body, header):
        try:
            pairs = [item.split("=", 1) for item in header.split(",")]
            timestamp = int(next(v for k, v in pairs if k.strip() == "t"))
            signatures = [v for k, v in pairs if k.strip() == "v1"]
            if not signatures:
                raise KeyError("v1")
        except (ValueError, KeyError, AttributeError, StopIteration):
            return False, "Header Stripe-Signature sai định dạng"
        signed_payload = f"{timestamp}.{raw_body}".encode("utf-8")
        expected = hmac.new(SECRETS["STRIPE_WEBHOOK_SECRET"].encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
        if not any(hmac.compare_digest(expected, signature) for signature in signatures):
            return False, "Chữ ký không khớp"
        if abs(time.time() - timestamp) > WEBHOOK_TOLERANCE_S:
            return False, f"Timestamp ngoài cửa sổ {WEBHOOK_TOLERANCE_S} giây"
        return True, ""

    def handle_webhook(self, raw_body, signature_header):
        """Endpoint POST /webhooks/stripe. Trả về (mã HTTP, thông điệp). Lưu kết quả xử lý từng sự kiện vào processed_events."""
        self._ref = None
        try:
            code, text = self._handle_webhook(raw_body, signature_header)
            if code == 200 and self._ref:
                now = time.time()
                if text == "duplicate":
                    self.db.execute("UPDATE processed_events SET dup_count=dup_count+1, last_at=? WHERE event_id=?", (now, self._ref))
                else:
                    obj = json.loads(raw_body)["data"]["object"]
                    order_id = (obj.get("metadata") or {}).get("order_id") or obj.get("client_reference_id")
                    self.db.execute("UPDATE processed_events SET order_id=?, result=?, last_at=? WHERE event_id=?", (order_id, text, now, self._ref))
                    if text in ("paid", "failed", "expired") and order_id:
                        self._attempt_status(order_id, text)
            self.db.commit()
            return code, text
        finally:
            self._ref = None

    def _handle_webhook(self, raw_body, signature_header):
        valid, reason = self.verify_signature(raw_body, signature_header)
        if not valid:
            self.log("5. Xác nhận đơn", "Stripe Webhook", "FAIL", f"Từ chối Webhook (HTTP 400): {reason}. Không cập nhật đơn")
            return 400, reason
        event = json.loads(raw_body)
        self._ref = event["id"]
        obj = event["data"]["object"]
        order_id = (obj.get("metadata") or {}).get("order_id") or obj.get("client_reference_id")
        try:
            with self.db:        # một transaction: sự kiện + trạng thái + outbox cùng commit hoặc cùng rollback
                inserted = self.db.execute("INSERT OR IGNORE INTO processed_events (event_id, type, at) VALUES (?,?,?)",
                                           (event["id"], event["type"], time.time())).rowcount
                if not inserted:
                    self.log("5. Xác nhận đơn", "Stripe Webhook", "INFO",
                             f"Đã nhận lại sự kiện {event['id']}, không phát sinh tác vụ mới (sự kiện đã xử lý: bỏ qua)", order_id)
                    return 200, "duplicate"
                order = self.order(order_id)
                if event["type"].startswith("checkout.session."):
                    return self._handle_checkout_event(event, obj, order)
                if not order_id or (order and order["pay_mode"] == "real"):
                    return 200, "unhandled"       # Stripe thật gửi nhiều loại sự kiện; đơn thật chỉ xác nhận qua checkout.session.*
                if not order or not order["payment_intent"] or order["payment_intent"] != obj.get("id"):
                    self.log("5. Xác nhận đơn", "Stripe Webhook", "FAIL", "Không khớp đơn hoặc mã giao dịch: bỏ qua", order_id)
                    return 200, "ignored"

                if event["type"] == "payment_intent.succeeded":
                    if obj["amount_received"] != order["amount"] or obj["currency"] != order["currency"]:
                        self.log("5. Xác nhận đơn", "Stripe Webhook", "FAIL",
                                 f"Số tiền {obj['amount_received']:,} không khớp đơn {order['amount']:,}: không ghi PAID".replace(",", "."), order_id)
                        return 200, "amount_mismatch"
                    if order["order_status"] == "CANCELED":
                        self.db.execute("UPDATE orders SET payment_status='REFUND_PENDING' WHERE id=?", (order_id,))
                        self._enqueue(order_id, "refund")
                        self.log("5. Xác nhận đơn", "Stripe Webhook", "WARN", "Đã thu tiền nhưng đơn đã hủy: tạo tác vụ hoàn tiền, không giao hàng", order_id)
                        return 200, "refund"
                    if order["payment_status"] == "SUCCEEDED":
                        return 200, "already_paid"
                    self.db.execute("UPDATE orders SET order_status='PAID', payment_status='SUCCEEDED', paid_at=? WHERE id=?",
                                    (time.time(), order_id))
                    self._consume_stock(order_id)
                    self._enqueue(order_id, "create_shipment")
                    self._enqueue(order_id, "email_paid")
                    self.log("5. Xác nhận đơn", "Stripe Webhook", "OK",
                             "Chữ ký hợp lệ, khớp mã đơn, số tiền, tiền tệ. Ghi PAID một lần + 2 tác vụ outbox", order_id)
                    return 200, "paid"

                if event["type"] == "payment_intent.payment_failed":
                    self.db.execute("INSERT INTO payment_failures VALUES (?,?)", (order["uid"], time.time()))
                    if order["payment_status"] == "SUCCEEDED":
                        self.log("5. Xác nhận đơn", "Stripe Webhook", "INFO", "Sự kiện lỗi đến muộn: không hạ trạng thái PAID", order_id)
                        return 200, "late_failure_ignored"
                    self.db.execute("UPDATE orders SET order_status='PAYMENT_FAILED', payment_status='FAILED' WHERE id=?", (order_id,))
                    self._enqueue(order_id, "email_failed", suffix=event["id"])
                    self.log("5. Xác nhận đơn", "Stripe Webhook", "FAIL", "Thanh toán thất bại: PAYMENT_FAILED, email báo lỗi, cho khách thử lại", order_id)
                    return 200, "failed"
            return 200, "unhandled"
        except sqlite3.Error as exc:
            return 500, f"Lỗi Database, Stripe sẽ gửi lại: {exc}"

    def _handle_checkout_event(self, event, obj, order):
        """Sự kiện của Stripe Checkout thật. Chạy trong cùng transaction với bảng processed_events."""
        order_id = order["id"] if order else None
        if not order or order["pay_mode"] != "real" or order["session_id"] != obj.get("id"):
            self.log("5. Xác nhận đơn", "Stripe Webhook", "FAIL", "Không khớp đơn hoặc phiên thanh toán: bỏ qua", order_id)
            return 200, "ignored"
        kind = event["type"]
        if kind in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
            if obj.get("payment_status") != "paid":
                self.log("5. Xác nhận đơn", "Stripe Webhook", "INFO", "Phiên hoàn tất nhưng chưa thu được tiền: chưa ghi PAID", order_id)
                return 200, "not_paid"
            if obj.get("amount_total") != order["amount"] or str(obj.get("currency", "")).lower() != order["currency"]:
                self.log("5. Xác nhận đơn", "Stripe Webhook", "FAIL",
                         f"Số tiền {obj.get('amount_total')} {obj.get('currency')} không khớp đơn {order['amount']} {order['currency']}: không ghi PAID", order_id)
                self._set_recon(order_id, "mismatch", {"diffs": [f"Webhook báo {obj.get('amount_total')} {str(obj.get('currency', '')).upper()}, đơn ghi {order['amount']} {order['currency'].upper()}"],
                                                       "stripe": {"status": "complete", "payment_status": obj.get("payment_status"), "amount": obj.get("amount_total"),
                                                                  "currency": obj.get("currency"), "order_ref": order_id}})
                return 200, "amount_mismatch"
            if order["payment_status"] == "SUCCEEDED":
                return 200, "already_paid"
            if order["order_status"] == "CANCELED":
                self.db.execute("UPDATE orders SET payment_status='REFUND_PENDING', payment_intent=? WHERE id=?", (obj.get("payment_intent"), order_id))
                self.log("5. Xác nhận đơn", "Stripe Webhook", "WARN", "Đã thu tiền nhưng đơn đã hủy: cần hoàn tiền trên Stripe Dashboard, không giao hàng", order_id)
                return 200, "refund"
            self.db.execute("UPDATE orders SET order_status='PAID', payment_status='SUCCEEDED', paid_at=?, payment_intent=? WHERE id=?",
                            (time.time(), obj.get("payment_intent"), order_id))
            self._consume_stock(order_id)
            self._enqueue(order_id, "create_shipment")
            self._enqueue(order_id, "email_paid")
            self.log("5. Xác nhận đơn", "Stripe Webhook", "OK",
                     "Chữ ký hợp lệ, khớp mã đơn, phiên, số tiền, tiền tệ. Ghi PAID một lần + 2 tác vụ outbox", order_id)
            return 200, "paid"
        if kind == "checkout.session.async_payment_failed":
            self.db.execute("INSERT INTO payment_failures VALUES (?,?)", (order["uid"], time.time()))
            if order["payment_status"] != "SUCCEEDED":
                self.db.execute("UPDATE orders SET order_status='PAYMENT_FAILED', payment_status='FAILED' WHERE id=?", (order_id,))
                self._enqueue(order_id, "email_failed", suffix=event["id"])
                self.log("5. Xác nhận đơn", "Stripe Webhook", "FAIL", "Thanh toán thất bại: PAYMENT_FAILED, cho khách thử lại", order_id)
            return 200, "failed"
        if kind == "checkout.session.expired" and order["order_status"] == "PAYMENT_PENDING":
            self.db.execute("UPDATE orders SET session_id=NULL, checkout_url=NULL, session_expires_at=NULL, returned_at=NULL WHERE id=?", (order_id,))
            self.log("5. Xác nhận đơn", "Stripe Webhook", "INFO", "Phiên thanh toán hết hạn. Đơn được giữ, khách có thể mở phiên mới", order_id)
            self._release_stock(order_id, "Stripe báo phiên đã hết hạn, chưa thu tiền")
            return 200, "expired"
        return 200, "unhandled"

    def integrations(self):
        """Trạng thái từng tích hợp: real = gọi API thật, pending = đã nối nhưng thiếu cấu hình (đang chạy giả lập), sim = giả lập."""
        def missing(*names):
            return [n for n in names if not config.get(n)]
        stripe_missing = missing("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET")
        if not stripe_missing and not config.get("STRIPE_SECRET_KEY").startswith(("sk_test_", "rk_test_")):
            stripe_missing = ["STRIPE_SECRET_KEY phải là khóa Sandbox sk_test_…"]
        rows = [
            ("OpenAI", "Tư vấn sản phẩm (Responses API)", self.real_ai, missing("OPENAI_API_KEY", "OPENAI_MODEL")),
            ("Cloudflare Turnstile", "Kiểm tra bot ở bước đặt đơn (Siteverify)", self.real_turnstile, missing("TURNSTILE_SITE_KEY", "TURNSTILE_SECRET_KEY")),
            ("Stripe", "Checkout Session + Webhook (Sandbox)", self.real_stripe, stripe_missing),
        ]
        out = [{"name": name, "role": role, "state": "real" if client else "pending",
                "note": "Gọi API thật" if client else "Đã nối, chờ cấu hình: thiếu " + ", ".join(miss) + ". Đang chạy giả lập."}
               for name, role, client, miss in rows]
        out += [{"name": name, "role": role, "state": "sim", "note": "Giả lập trong máy"} for name, role in
                [("Firebase Authentication", "Đăng nhập và xác minh ID Token"), ("GHN", "Tạo và đối soát vận đơn"), ("Brevo", "Email xác nhận và mã vận đơn")]]
        return out

    def public_config(self, demo=False):
        """Cấu hình công khai cho frontend. Không bao giờ chứa Secret Key hay API key."""
        real_ts = bool(self.real_turnstile) and not demo
        return {"turnstile": {"mode": "real" if real_ts else "sim", "site_key": self.turnstile_site_key if real_ts else ""},
                "ai": "real" if self.real_ai and not demo else "sim",
                "stripe": "real" if self.real_stripe and not demo else "sim"}

    def is_admin(self, id_token):
        """Quyền admin do backend quyết định: email của tài khoản phải nằm trong ADMIN_EMAILS."""
        user = self.firebase.users.get(id_token)
        return user if user and user["email"].lower() in self.admin_emails else None

    def _enqueue(self, order_id, task, suffix=""):
        key = f"{order_id}:{task}" + (f":{suffix}" if suffix else "")
        now = time.time()
        self.db.execute("INSERT OR IGNORE INTO outbox (task_key, order_id, task, created_at, updated_at) VALUES (?,?,?,?,?)",
                        (key, order_id, task, now, now))

    # ------------------------------- Bước 6 + 7: worker gọi Shipping và Notification
    MAX_TASK_ATTEMPTS = 3
    TASK_RETRY_DELAY = float(config.get("TASK_RETRY_DELAY_S") or 5)       # giây; lần sau gấp đôi lần trước

    def run_worker(self):
        """Tiến trình hậu thanh toán: đọc các tác vụ đã lưu trong outbox và gọi GHN / Brevo / Stripe Refund.

        - Thành công: DONE.
        - Lỗi tạm thời và BIẾT CHẮC yêu cầu chưa được thực hiện (HTTP 503): thử lại có giới hạn, giãn cách tăng dần.
        - Timeout / chưa rõ dịch vụ đã làm hay chưa: REVIEW (Cần đối soát). Không tự gửi lại; admin đối soát trước.
        """
        done, now = 0, time.time()
        tasks = self.db.execute("SELECT * FROM outbox WHERE status='PENDING' AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY id",
                                (now,)).fetchall()
        for task in tasks:
            order = self.order(task["order_id"])
            kind = task["task"]
            service, step = (("GHN", "6. Giao hàng") if kind == "create_shipment" else ("Stripe", "Hoàn tiền") if kind == "refund"
                             else ("Brevo", "7. Thông báo"))
            attempts = task["attempts"] + 1
            try:
                if kind == "create_shipment":
                    self._ship(task, order)
                elif kind == "refund":
                    self.stripe.create_refund(SECRETS["STRIPE_SECRET_KEY"], payment_intent=order["payment_intent"],
                                              idempotency_key=f"refund:{order['id']}:{order['payment_intent']}:full")
                    self.db.execute("UPDATE orders SET payment_status='REFUNDED' WHERE id=?", (order["id"],))
                    self.log("Hoàn tiền", "Stripe", "OK", "Đã hoàn tiền cho đơn đã hủy", order["id"])
                else:
                    self._email(task, order)
                self.db.execute("UPDATE outbox SET status='DONE', attempts=?, note='Thành công', updated_at=?, next_attempt_at=NULL WHERE id=?",
                                (attempts, time.time(), task["id"]))
                done += 1
            except ServiceUnavailable as exc:             # biết chắc chưa thực hiện: thử lại có giới hạn
                if attempts >= self.MAX_TASK_ATTEMPTS:
                    self.db.execute("UPDATE outbox SET status='REVIEW', attempts=?, note=?, updated_at=?, next_attempt_at=NULL WHERE id=?",
                                    (attempts, f"{exc}. Đã thử {attempts} lần", time.time(), task["id"]))
                    if kind == "create_shipment":
                        self.db.execute("UPDATE orders SET ship_check='confirmed_none' WHERE id=?", (order["id"],))
                    self.log(step, service, "WARN", f"{exc}. Đã thử {attempts}/{self.MAX_TASK_ATTEMPTS} lần, dừng tự động và chuyển admin "
                             f"(đã xác định lần trước chưa thực hiện nên được phép Thử lại)", order["id"])
                else:
                    delay = self.TASK_RETRY_DELAY * (2 ** (attempts - 1))
                    self.db.execute("UPDATE outbox SET attempts=?, note=?, updated_at=?, next_attempt_at=? WHERE id=?",
                                    (attempts, str(exc), time.time(), time.time() + delay, task["id"]))
                    self.log(step, service, "WARN", f"{exc}. Lần {attempts}/{self.MAX_TASK_ATTEMPTS}, thử lại sau {delay:g} giây", order["id"])
            except (ServiceError, AuthError) as exc:      # timeout hoặc lỗi không rõ: không biết phía dịch vụ đã làm chưa
                if kind == "refund":
                    self.db.execute("UPDATE outbox SET attempts=?, note=?, updated_at=? WHERE id=?", (attempts, str(exc), time.time(), task["id"]))
                    self.log(step, service, "WARN", f"{exc}. Giữ tác vụ, sẽ thử lại sau", order["id"])
                else:
                    self.db.execute("UPDATE outbox SET status='REVIEW', attempts=?, note=?, updated_at=?, next_attempt_at=NULL WHERE id=?",
                                    (attempts, str(exc), time.time(), task["id"]))
                    if kind == "create_shipment":
                        # Chưa rõ GHN đã tạo vận đơn hay chưa: dừng tự động, chờ đối soát rồi mới được tạo lại
                        self.db.execute("UPDATE orders SET ship_check='unknown' WHERE id=?", (order["id"],))
                        self.log(step, service, "WARN", f"{exc}. Chưa rõ kết quả tạo vận đơn: giữ đơn PAID, chờ đối soát", order["id"])
                    else:
                        self.log(step, service, "WARN", f"{exc}. Chưa rõ email đã gửi hay chưa: cần đối soát trước khi gửi lại", order["id"])
            self.db.commit()
        return {"processed": done, "pending": len(tasks) - done}

    def reconcile_email(self, order_id, task_key):
        """Admin đối soát một email đang "Cần đối soát": hỏi Brevo theo mã tác vụ; đã gửi thì ghi nhận, chưa gửi thì gửi lại (một lần)."""
        task = self.db.execute("SELECT * FROM outbox WHERE task_key=? AND order_id=? AND status='REVIEW' AND task LIKE 'email_%'",
                               (task_key, order_id)).fetchone()
        if not task:
            return {"ok": False, "message": "Không có email cần đối soát."}
        order = self.order(order_id)
        sent = self.brevo.sent.get(task_key)             # tra cứu theo mã tác vụ (khóa chống gửi lặp)
        try:
            if sent:
                self.log("7. Thông báo", "Brevo", "INFO", "Đối soát: email đã được gửi ở lần trước, ghi nhận, không gửi lại", order_id, ref=sent["messageId"])
            else:
                self.log("7. Thông báo", "Brevo", "INFO", "Đối soát: Brevo chưa có email này. Gửi lại", order_id)
                self._email(task, order)
        except (ServiceError, AuthError) as exc:
            self.db.execute("UPDATE outbox SET note=?, updated_at=? WHERE id=?", (str(exc), time.time(), task["id"]))
            self.db.commit()
            return {"ok": False, "message": "Brevo chưa phản hồi nên chưa đối soát được. Thử lại sau ít phút."}
        self.db.execute("UPDATE outbox SET status='DONE', note='Thành công (sau đối soát)', updated_at=? WHERE id=?", (time.time(), task["id"]))
        self.db.commit()
        return {"ok": True, "already_sent": bool(sent)}

    def _ship(self, task, order):
        shipment = self.ghn.create_order(SECRETS["GHN_TOKEN"], SECRETS["GHN_SHOP_ID"], client_order_code=order["id"],
                                         to_name=order["name"], to_address=order["address"], cod_amount=0)
        self._shipment_done(order["id"], shipment, "Tạo vận đơn")

    def _shipment_done(self, order_id, shipment, how):
        self.db.execute("UPDATE orders SET tracking_code=?, order_status='SHIPPING', ship_status='ready_to_pick',"
                        " shipped_at=?, ship_check=NULL WHERE id=?", (shipment["order_code"], time.time(), order_id))
        self.db.execute("UPDATE outbox SET status='DONE', note='Thành công', updated_at=?, next_attempt_at=NULL WHERE order_id=? AND task='create_shipment'",
                        (time.time(), order_id))
        self._enqueue(order_id, "email_tracking")
        self.log("6. Giao hàng", "GHN", "OK", f"{how} {shipment['order_code']}, COD = 0", order_id, ref=shipment["order_code"])

    def reconcile_shipment(self, order_id):
        """Đối soát vận đơn theo mã đơn nội bộ. Có thì dùng lại mã; không có thì mới cho phép tạo lại."""
        order = self.order(order_id)
        if not order or order["ship_check"] is None:
            return {"ok": False, "message": "Đơn không có vận đơn cần đối soát."}
        try:
            shipment = self.ghn.lookup(SECRETS["GHN_TOKEN"], SECRETS["GHN_SHOP_ID"], order_id)
        except (ServiceError, AuthError) as exc:
            self.log("6. Giao hàng", "GHN", "WARN", f"Đối soát chưa thực hiện được: {exc}", order_id)
            return {"ok": False, "message": "GHN chưa phản hồi nên chưa đối soát được. Thử lại sau ít phút."}
        if shipment:
            self.log("6. Giao hàng", "GHN", "INFO", "Đối soát theo mã đơn: vận đơn đã tồn tại, dùng lại mã, không tạo lại", order_id)
            self._shipment_done(order_id, shipment, "Ghi nhận vận đơn")
            self.db.commit()
            return {"ok": True, "found": True, "tracking_code": shipment["order_code"]}
        self.db.execute("UPDATE orders SET ship_check='confirmed_none' WHERE id=?", (order_id,))
        self.db.commit()
        self.log("6. Giao hàng", "GHN", "INFO", "Đối soát theo mã đơn: GHN chưa có vận đơn. Được phép tạo lại", order_id)
        return {"ok": True, "found": False}

    def retry_shipment(self, order_id):
        """Tạo lại vận đơn. Chỉ chạy sau khi đối soát xác nhận GHN chưa có vận đơn."""
        order = self.order(order_id)
        if not order or order["ship_check"] != "confirmed_none":
            return {"ok": False, "message": "Cần đối soát vận đơn trước khi tạo lại."}
        try:
            shipment = self.ghn.create_order(SECRETS["GHN_TOKEN"], SECRETS["GHN_SHOP_ID"], client_order_code=order_id,
                                             to_name=order["name"], to_address=order["address"], cod_amount=0)
        except (ServiceError, AuthError) as exc:
            self.db.execute("UPDATE orders SET ship_check='unknown' WHERE id=?", (order_id,))
            self.db.commit()
            self.log("6. Giao hàng", "GHN", "WARN", f"{exc}. Lại chưa rõ kết quả: cần đối soát trước khi thử tiếp", order_id)
            return {"ok": False, "message": "GHN chưa phản hồi. Cần đối soát lại trước khi thử tiếp."}
        self._shipment_done(order_id, shipment, "Tạo vận đơn")
        self.db.commit()
        return {"ok": True, "tracking_code": shipment["order_code"]}

    def _email(self, task, order):
        templates = {"email_paid": "Xác nhận thanh toán", "email_tracking": "Mã vận đơn",
                     "email_failed": "Thanh toán thất bại", "email_rejected": "Đơn bị từ chối"}
        params = {"order_id": order["id"]}
        if task["task"] == "email_tracking":
            params["tracking_code"] = self.order(order["id"])["tracking_code"]
        mail = self.brevo.send_email(SECRETS["BREVO_API_KEY"], to=order["email"], template=templates[task["task"]],
                                     params=params, task_id=task["task_key"])
        self.log("7. Thông báo", "Brevo", "OK", f"Gửi email \"{templates[task['task']]}\" tới {order['email']} ({mail['messageId']})", order["id"], ref=mail["messageId"])

    # ----------------------------------------------- Nhịp nền của hệ thống (demo)
    def tick(self):
        """Một nhịp xử lý nền: nhận Webhook đã tới hạn, chạy worker outbox, cập nhật trạng thái giao hàng."""
        now = time.time()
        due = [w for w in self.pending_webhooks if w["due"] <= now]
        self.pending_webhooks = [w for w in self.pending_webhooks if w["due"] > now]
        for webhook in due:
            self.handle_webhook(webhook["raw"], webhook["header"])
        self.check_holds(now)
        result = self.run_worker()
        self.sync_shipments()
        return result

    def sync_shipments(self):
        """Hỏi GHN trạng thái các vận đơn đang giao và cập nhật đơn hàng."""
        labels = {"delivering": "đang giao hàng", "delivered": "đã giao thành công"}
        for row in self.db.execute("SELECT id, ship_status FROM orders WHERE order_status='SHIPPING'").fetchall():
            try:
                shipment = self.ghn.lookup(SECRETS["GHN_TOKEN"], SECRETS["GHN_SHOP_ID"], row["id"])
            except (ServiceError, AuthError):
                continue
            if not shipment or shipment["status"] == row["ship_status"] or shipment["status"] not in labels:
                continue
            now = time.time()
            if shipment["status"] == "delivering":
                self.db.execute("UPDATE orders SET ship_status='delivering', delivering_at=? WHERE id=?", (now, row["id"]))
            else:
                self.db.execute("UPDATE orders SET ship_status='delivered', order_status='DELIVERED',"
                                " delivering_at=COALESCE(delivering_at, ?), delivered_at=? WHERE id=?", (now, now, row["id"]))
            self.log("6. Giao hàng", "GHN", "INFO", f"GHN cập nhật vận đơn: {labels[shipment['status']]}", row["id"])
        self.db.commit()

    def schedule_payment(self, order_id, outcome, delay=3.0):
        """Khách hoàn tất trên trang Stripe; Webhook xác nhận sẽ tới backend sau một khoảng trễ."""
        order = self.order(order_id)
        if order and order["pay_mode"] == "real":
            return {"ok": False, "message": "Đơn này thanh toán qua Stripe thật, không dùng cổng giả lập."}
        if not order or not order["session_id"] or order["order_status"] != "PAYMENT_PENDING":
            return {"ok": False, "message": "Đơn không ở trạng thái chờ thanh toán."}
        if (order["session_expires_at"] or 0) <= time.time():
            return {"ok": False, "expired": True, "message": "Phiên thanh toán đã hết hạn."}
        session = {"payment_intent": order["payment_intent"], "amount": order["amount"],
                   "currency": order["currency"], "metadata": {"order_id": order_id}}
        kind = "payment_intent.payment_failed" if outcome == "fail" else "payment_intent.succeeded"
        if any(w["order_id"] == order_id for w in self.pending_webhooks):
            return {"ok": True, "already": True}      # bấm đúp nút thanh toán: không tạo thêm sự kiện
        raw = dumps(self.stripe.build_event(kind, session))
        header = self.stripe.sign(raw)
        self.last_event[order_id] = (raw, header)
        self.pending_webhooks.append({"due": time.time() + delay, "order_id": order_id, "raw": raw, "header": header})
        self.log("4. Thanh toán", "Stripe", "INFO", "Khách hoàn tất trên trang Stripe. Chờ Webhook xác nhận, chưa ghi PAID", order_id)
        return {"ok": True}

    # ------------------------------------------------------ Màn hình admin xử lý đơn
    PAY_LABELS = {"SUCCEEDED": "Đã thanh toán", "FAILED": "Thanh toán thất bại",
                  "REFUND_PENDING": "Chờ hoàn tiền", "REFUNDED": "Đã hoàn tiền", "CANCELED": "Đã hủy, chưa thu tiền"}
    SHIP_LABELS = {"ready_to_pick": "Đã tạo vận đơn", "delivering": "Đang giao", "delivered": "Đã giao"}

    def _admin_row(self, order, confirming):
        pay = order["payment_status"]
        if pay == "NONE":
            pay_label = "Không thanh toán" if order["order_status"] == "CANCELED" else "Chưa mở thanh toán"
        elif pay == "PENDING":
            pay_label = ("Chờ xác nhận thanh toán" if order["id"] in confirming or self._awaiting_real(order)
                         else "Chờ thanh toán" if order["session_id"] else "Chưa tạo được phiên thanh toán")
        else:
            pay_label = self.PAY_LABELS.get(pay, pay)
        if order["tracking_code"]:
            ship_label = self.SHIP_LABELS.get(order["ship_status"], "Đã tạo vận đơn")
        elif order["ship_check"] == "unknown":
            ship_label = "Cần xử lý: chưa rõ đã tạo vận đơn hay chưa"
        elif order["ship_check"] == "confirmed_none":
            ship_label = "Cần xử lý: GHN chưa có vận đơn"
        elif pay == "SUCCEEDED":
            ship_label = "Chưa có vận đơn (đang tạo)"
        else:
            ship_label = "Chưa có vận đơn"
        review_label = ("Chờ duyệt" if order["order_status"] == "ON_HOLD" else "Đã duyệt" if order["review_result"] == "approved"
                        else "Đã từ chối" if order["review_result"] == "rejected" else "Không cần duyệt")
        tasks = [dict(t) for t in self.db.execute("SELECT * FROM outbox WHERE order_id=? ORDER BY id", (order["id"],))]
        mails = [t for t in tasks if t["task"].startswith("email_")]
        if any(t["status"] == "REVIEW" for t in mails):
            notify_label = "Email cần đối soát"
        elif any(t["status"] == "PENDING" for t in mails):
            notify_label = "Đang chờ gửi email"
        elif mails:
            names = {"email_paid": "xác nhận thanh toán", "email_tracking": "mã vận đơn", "email_failed": "báo thanh toán lỗi", "email_rejected": "báo từ chối"}
            notify_label = "Đã gửi email " + ", ".join(dict.fromkeys(names.get(t["task"], t["task"]) for t in mails))
        else:
            notify_label = "Chưa có thông báo"
        issues = []                               # việc cần người xử lý, mỗi đơn chỉ tính một lần ở dashboard
        if order["order_status"] == "ON_HOLD":
            issues.append({"label": "Chờ duyệt HIGH", "since": order["created_at"], "kind": "hold"})
        waiting = order["order_status"] == "PAYMENT_PENDING"
        flagged = waiting and order["recon_result"] in ("processing", "timeout", "paid_wait_webhook", "mismatch")
        if waiting and (order["stripe_seen"] == "paid" or flagged):
            issues.append({"label": "Cần đối soát thanh toán", "since": order["returned_at"] or order["recon_at"] or order["created_at"], "kind": "payment"})
        recon_needed = waiting and bool(order["id"] in confirming or self._awaiting_real(order) or order["stripe_seen"] or flagged)
        result = order["recon_result"] if waiting or order["recon_result"] == "mismatch" else None
        recon = {"needed": recon_needed, "result": result, "label": RECON_LABELS.get(result, "Chưa kiểm tra") if result else "Chưa kiểm tra",
                 "note": RECON_NOTES.get(result, ""), "at": order["recon_at"], "since": order["returned_at"] or order["recon_at"] or order["created_at"],
                 "detail": json.loads(order["recon_detail"] or "{}")}
        if pay == "REFUND_PENDING":
            issues.append({"label": "Chờ hoàn tiền", "since": order["created_at"], "kind": "payment"})
        for t in tasks:
            if t["status"] == "REVIEW":
                label = (("Cần tạo lại vận đơn" if order["ship_check"] == "confirmed_none" else "Cần đối soát vận đơn")
                         if t["task"] == "create_shipment" else "Email cần đối soát")
                if not any(i["label"] == label for i in issues):       # nhiều email cùng kẹt vẫn chỉ hiện một nhãn
                    issues.append({"label": label, "since": t["updated_at"] or order["created_at"], "kind": "ship" if t["task"] == "create_shipment" else "notify"})
        buckets = [name for name, hit in [("hold", order["order_status"] == "ON_HOLD"), ("confirm", pay == "PENDING"),
                                         ("paid", pay == "SUCCEEDED"), ("ship_issue", bool(order["ship_check"])),
                                         ("needs", bool(issues)), ("recon", recon_needed)] if hit]
        times = [e["time"] for e in self.events if e["order_id"] == order["id"]]
        return {**order, "pay_label": pay_label, "ship_label": ship_label, "review_label": review_label, "buckets": buckets,
                "notify_label": notify_label, "issues": issues, "task_rows": tasks, "env": "real" if order["pay_mode"] == "real" else "sim",
                "confirming": order["id"] in confirming or self._awaiting_real(order), "recon": recon,
                "updated_at": max(times + [order["created_at"]])}

    def admin_orders(self):
        confirming = {w["order_id"] for w in self.pending_webhooks}
        rows = [dict(r) for r in self.db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()]
        return {"orders": [self._admin_row(r, confirming) for r in rows], "integrations": self.integrations()}

    def admin_order(self, order_id):
        order = self.order(order_id)
        if not order:
            return {"ok": False, "message": "Không tìm thấy đơn."}
        row = self._admin_row(order, {w["order_id"] for w in self.pending_webhooks})
        row["lines"] = [{**line, "name": BY_ID[line["id"]]["name"]} for line in json.loads(order["items"])]
        row["events"] = [e for e in self.events if e["order_id"] == order_id]   # bước xác minh được gắn mã đơn ngay khi tạo đơn
        row["risk_rules"] = [r for r in (order["risk_reasons"] or "").split("; ") if r]
        row["pay_action"] = self.pay_action(order)
        row["workflow"] = self._workflow(order, row["events"], row["task_rows"])
        row["attempts_pay"] = [dict(r) for r in self.db.execute("SELECT * FROM payment_attempts WHERE order_id=? ORDER BY created_at", (order_id,))]
        row["webhooks"] = [dict(r) for r in self.db.execute("SELECT * FROM processed_events WHERE order_id=? ORDER BY at", (order_id,))]
        row["holds"] = [{**dict(r), "name": self.sku_name(r["sku"])} for r in self.db.execute("SELECT * FROM stock_holds WHERE order_id=? ORDER BY id", (order_id,))]
        sent = {m["template"]: m for m in self.brevo.sent.values() if m["params"]["order_id"] == order_id}
        money = lambda n: format(n, ",").replace(",", ".") + " ₫"
        row["email_previews"] = [
            {"template": "Xác nhận thanh toán", "to": order["email"],
             "subject": f"Góc Tech đã nhận thanh toán cho đơn {order_id}",
             "body": [f"Chào {order['name']},", f"Góc Tech đã nhận thanh toán cho đơn {order_id}.",
                      f"Mã đơn: {order_id}", f"Tổng tiền: {money(order['amount'])}",
                      "Chúng tôi sẽ gửi email tiếp theo khi đơn có mã vận đơn."],
             "sent_at": sent.get("Xác nhận thanh toán", {}).get("time"),
             "condition": "Tạo khi đơn chuyển PAID"},
            {"template": "Mã vận đơn", "to": order["email"],
             "subject": f"Đơn {order_id} đã có mã vận đơn",
             "body": [f"Chào {order['name']},", f"Đơn {order_id} đã được bàn giao cho Giao Hàng Nhanh.",
                      f"Mã vận đơn: {order['tracking_code'] or '(chưa có, email này chưa được tạo)'}",
                      "Bạn có thể theo dõi hành trình ở trang chi tiết đơn."],
             "sent_at": sent.get("Mã vận đơn", {}).get("time"),
             "condition": "Chỉ tạo sau khi GHN trả mã vận đơn"},
        ]
        return {"ok": True, "order": row}

    TASK_NAMES = {"create_shipment": "Tạo vận đơn", "email_paid": "Email xác nhận thanh toán", "email_tracking": "Email mã vận đơn",
                  "email_failed": "Email báo thanh toán lỗi", "email_rejected": "Email báo từ chối", "refund": "Hoàn tiền"}
    TASK_STATES = {"PENDING": "Đang chờ", "DONE": "Thành công", "REVIEW": "Cần xử lý"}

    def _workflow(self, order, events, tasks):
        """Dòng thời gian cho tab Workflow: thời gian, dịch vụ, bước, kết quả, mã tham chiếu và NHÃN MÔI TRƯỜNG từng bước."""
        real_pay = order["pay_mode"] == "real"

        def env(e):
            s = e["service"]
            if s.startswith("Stripe"):
                return "Stripe Sandbox" if real_pay else "Stripe giả lập"
            if s == "Cloudflare Turnstile":
                return "Turnstile thật" if "Siteverify thật" in e["detail"] or "Siteverify" in e["detail"] and self.real_turnstile and real_pay else "Turnstile giả lập"
            if s == "OpenAI":
                return "OpenAI thật" if "API thật" in e["detail"] else "OpenAI giả lập"
            return {"Firebase Authentication": "Firebase giả lập", "GHN": "GHN giả lập", "Brevo": "Brevo giả lập"}.get(s, "Nội bộ")

        rows = []
        for e in sorted(events, key=lambda x: x["time"]):
            dup = "đã xử lý: bỏ qua" in e["detail"]
            rows.append({"time": e["time"], "service": e["service"], "step": re.sub(r"^\d+\.\s*", "", e["step"]), "detail": e["detail"],
                         "status": e["status"], "result": "Trùng, không tạo tác vụ mới" if dup else {"OK": "Thành công", "FAIL": "Lỗi / dừng",
                                                                                                   "WARN": "Cảnh báo", "INFO": "Ghi nhận"}.get(e["status"], e["status"]),
                         "ref": e.get("ref") or "", "env": env(e), "stuck": False})
        stuck = None                              # bước đang kẹt: để dashboard dẫn thẳng tới
        if order["order_status"] == "ON_HOLD":
            stuck = {"label": "Chờ admin duyệt đơn HIGH", "action": "review"}
        for t in tasks:
            if t["status"] == "REVIEW":
                ship = t["task"] == "create_shipment"
                stuck = {"label": f"{self.TASK_NAMES.get(t['task'], t['task'])}: {t['note']}", "task_key": t["task_key"],
                         "action": ("retry-shipment" if order["ship_check"] == "confirmed_none" else "reconcile") if ship else "reconcile-email"}
        if stuck:
            marker = "Chấm rủi ro" if stuck["action"] == "review" else ("Giao hàng" if "shipment" in stuck["action"] or stuck["action"] == "reconcile" else "Thông báo")
            for row in reversed(rows):
                if row["step"] == marker and row["status"] in ("WARN", "FAIL"):
                    row["stuck"] = True
                    break
        pending = [{"task": self.TASK_NAMES.get(t["task"], t["task"]), "state": self.TASK_STATES.get(t["status"], t["status"]), "status": t["status"],
                    "attempts": t["attempts"], "note": t["note"] or "", "next_attempt_at": t["next_attempt_at"], "updated_at": t["updated_at"],
                    "task_key": t["task_key"], "kind": t["task"]} for t in tasks]
        return {"rows": rows, "stuck": stuck, "tasks": pending,
                "risk": {"level": order["risk"], "score": order["risk_score"] or 0, "max": 3, "rules": [r for r in (order["risk_reasons"] or "").split("; ") if r],
                         "decision": order["review_result"], "by": order["reviewed_by"], "at": order["reviewed_at"], "reject_reason": order["reject_reason"]}}

    # ----------------------------------------------------------- Dashboard vận hành
    def dashboard(self, date_from="", date_to="", env="all", now=None):
        """Số liệu tổng hợp cho trang Tổng quan vận hành. Ngày tính theo giờ Việt Nam (Asia/Ho_Chi_Minh, UTC+7)."""
        now = time.time() if now is None else now
        day = lambda ts: datetime.fromtimestamp(ts, VN_TZ).strftime("%Y-%m-%d")
        ok_date = lambda s: bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s or ""))
        date_to = date_to if ok_date(date_to) else day(now)
        date_from = date_from if ok_date(date_from) else day(now - 6 * 86400)
        if date_from > date_to:
            date_from, date_to = date_to, date_from
        start = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=VN_TZ)
        days = min(92, (datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=VN_TZ) - start).days + 1)
        date_to = (start + timedelta(days=days - 1)).strftime("%Y-%m-%d")
        env = env if env in ("real", "sim") else "all"
        confirming = {w["order_id"] for w in self.pending_webhooks}
        rows = [self._admin_row(dict(r), confirming) for r in self.db.execute("SELECT * FROM orders ORDER BY created_at")]
        rows = [o for o in rows if env == "all" or o["env"] == env]
        inr = lambda ts: ts is not None and date_from <= day(ts) <= date_to
        created = [o for o in rows if inr(o["created_at"])]
        paid = [o for o in rows if o["paid_at"] and inr(o["paid_at"])]        # paid_at chỉ được ghi một lần, lúc Webhook hợp lệ ghi PAID
        value = lambda group: [{"currency": c.upper(), "amount": sum(o["amount"] for o in group if o["currency"] == c)}
                               for c in sorted({o["currency"] for o in group})]
        series = []
        for i in range(days):
            d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
            series.append({"date": d, "created": sum(1 for o in created if day(o["created_at"]) == d),
                           "paid": sum(1 for o in paid if day(o["paid_at"]) == d)})
        risk = {"LOW": 0, "HIGH": 0, "none": 0}
        for o in created:
            risk[o["risk"] if o["risk"] in ("LOW", "HIGH") else "none"] += 1
        ids = {o["id"] for o in rows}
        tasks = {k: {"pending": 0, "done": 0, "review": 0} for k in ("create_shipment", "email_paid", "email_tracking")}
        for t in self.db.execute("SELECT * FROM outbox"):                      # mỗi tác vụ là một dòng duy nhất; thử lại chỉ tăng attempts
            if t["task"] in tasks and t["order_id"] in ids and inr(t["created_at"]):
                tasks[t["task"]][{"PENDING": "pending", "DONE": "done", "REVIEW": "review"}.get(t["status"], "pending")] += 1
        queue = sorted(({"id": o["id"], "issues": o["issues"], "since": min(i["since"] for i in o["issues"]), "pay_label": o["pay_label"],
                         "env": o["env"]} for o in rows if o["issues"]), key=lambda q: q["since"])
        split = {e: {"count": sum(1 for o in paid if o["env"] == e), "value": value([o for o in paid if o["env"] == e])} for e in ("real", "sim")}
        return {"ok": True, "from": date_from, "to": date_to, "env": env, "generated_at": now, "timezone": "Asia/Ho_Chi_Minh",
                "created": len(created), "paid": len(paid), "paid_value": value(paid), "paid_split": split, "needs": len(queue),
                "series": series, "risk": risk,
                "tasks": [{"key": k, "name": self.TASK_NAMES[k], **v} for k, v in tasks.items()], "queue": queue,
                "webhook_duplicates": self.db.execute("SELECT COALESCE(SUM(dup_count), 0) FROM processed_events").fetchone()[0]}

    SEED = "dash-seed"

    def seed_dashboard(self):
        """Bộ dữ liệu kiểm thử cho dashboard: 4 đơn A–D (giả lập). Gọi lại sẽ xóa bộ cũ và tạo bộ mới."""
        old = [r["id"] for r in self.db.execute("SELECT id FROM orders WHERE demo=?", (self.SEED,))]
        for oid in old:
            for table in ("orders", "outbox", "tickets", "payment_attempts", "processed_events"):
                self.db.execute(f"DELETE FROM {table} WHERE {'id' if table == 'orders' else 'order_id'}=?", (oid,))
            self.ghn.orders.pop(oid, None)
            self.last_event.pop(oid, None)
        self.brevo.sent = {k: m for k, m in self.brevo.sent.items() if m["params"]["order_id"] not in old}
        self.hub.faults.discard("ghn")
        self._demo = self.SEED
        try:
            users = [self.firebase.new_customer(name=f"Khách mẫu {c}") for c in "ACD"]
            fresh = self.firebase.new_customer(fresh=True, name="Khách mẫu B")
            a = self.checkout(users[0], _token(), {"GD01|Bạc": 1}, area="hcm")["order_id"]            # LOW, trả 444.300 ₫, có vận đơn
            self.simulate_payment(a, "success"); self.run_worker(); self.run_worker()
            b_ = self.checkout(fresh, _token(), {"SP03|Đen": 1}, area="hcm")["order_id"]              # HIGH, chờ duyệt
            c = self.checkout(users[1], _token(), {"GD02|Trắng": 1}, area="hcm")["order_id"]          # LOW, đã trả, GHN cần đối soát
            self.hub.faults.add("ghn")
            self.simulate_payment(c, "success"); self.run_worker(); self.run_worker()
            self.hub.faults.discard("ghn")
            d = self.checkout(users[2], _token(), {"GD07|Trắng": 1}, area="hcm")["order_id"]          # LOW, chờ khách thanh toán
        finally:
            self._demo = None
        self.db.commit()
        return {"ok": True, "orders": {"A": a, "B": b_, "C": c, "D": d}}

    def resend_event(self, order_id):
        """Công cụ thử: cho cổng thanh toán (giả lập) gửi lại đúng sự kiện thanh toán đã xử lý của đơn."""
        if order_id not in self.last_event:
            return {"ok": False, "message": "Đơn này chưa có sự kiện thanh toán giả lập để gửi lại."}
        raw, _ = self.last_event[order_id]
        code, text = self.handle_webhook(raw, self.stripe.sign(raw))
        return {"ok": True, "http": code, "result": text}

    def whoami(self, id_token):
        user = self.firebase.users.get(id_token)
        return {"ok": True, "name": user["name"], "email": user["email"]} if user else {"ok": False}

    # ------------------------------------------------- Trang /demo: sáu tình huống
    DEMO = {
        "low_paid": {"title": "LOW Risk, thanh toán thành công", "sees": "Đã thanh toán, sau đó có mã vận đơn"},
        "high": {"title": "HIGH Risk", "sees": "“Đơn đang được kiểm tra”"},
        "security_fail": {"title": "Xác minh bảo mật lỗi", "sees": "“Chưa thể xác minh. Vui lòng thử lại.”"},
        "payment_unknown": {"title": "Thanh toán chưa rõ kết quả", "sees": "“Đang xác nhận thanh toán”"},
        "duplicate_event": {"title": "Sự kiện thanh toán đến hai lần", "sees": "Cùng một đơn đã thanh toán"},
        "ghn_down": {"title": "GHN tạm lỗi sau khi đã trả tiền", "sees": "“Đã thanh toán · Đang chuẩn bị giao”"},
    }
    DEMO_ADDRESS = "Khách demo, 0901234567, 12 Nguyễn Văn Linh, Quận 7, TP.HCM"

    def demo_run(self, name):
        if name not in self.DEMO:
            return {"ok": False, "message": "Không có tình huống này."}
        self.hub.faults_once.clear()
        self.hub.faults.discard("ghn")
        run_id = f"demo-{len(self.demo_runs) + 1}-{int(time.time() * 1000) % 100000}"
        token = self.firebase.new_customer(fresh=(name == "high"), name="Khách demo")
        uid = self.firebase.users[token]["uid"]
        started = time.time()
        order_id, view = None, ""
        self._demo = run_id
        try:
            if name == "security_fail":
                self.hub.faults_once.add("turnstile")           # lần kiểm tra bot kế tiếp sẽ timeout
                view = "?demo_cart=" + "SP01|Đen:1" + "&demo_autoplace=1#/checkout"
            else:
                items = {"SP03|Đen": 1} if name == "high" else {"SP01|Đen": 1}
                if name == "ghn_down":
                    self.hub.faults.add("ghn")
                result = self.checkout(token, _token(), items, address=self.DEMO_ADDRESS)
                order_id = result["order_id"]
                view = f"#/order/{order_id}"
                if name in ("low_paid", "ghn_down"):
                    self.simulate_payment(order_id, "success")
                    self.run_worker(); self.run_worker()
                    self.hub.faults.discard("ghn")      # lỗi GHN chỉ ảnh hưởng đơn của tình huống này
                elif name == "duplicate_event":
                    self.simulate_payment(order_id, "duplicate")
                    self.run_worker(); self.run_worker()
                elif name == "payment_unknown":
                    self.schedule_payment(order_id, "success", delay=3600)   # kết quả chưa về
                    view = f"#/result/{order_id}"
        finally:
            self._demo = None
        self.demo_runs[run_id] = {"name": name, "token": token, "uid": uid, "order_id": order_id,
                                  "view": view, "started": started}
        return {"ok": True, "run_id": run_id, "token": token, "view": view, "order_id": order_id}

    def demo_status(self, run_id):
        run = self.demo_runs.get(run_id)
        if not run:
            return {"ok": False, "message": "Phiên demo đã được đặt lại."}
        uid, name = run["uid"], run["name"]
        row = self.db.execute("SELECT * FROM orders WHERE uid=? ORDER BY created_at LIMIT 1", (uid,)).fetchone()
        order = dict(row) if row else None
        oid = order["id"] if order else None
        events = [e for e in self.events if e["uid"] == uid or (oid and e["order_id"] == oid)]
        emails = sorted(m["template"] for m in self.brevo.sent.values() if m["params"]["order_id"] == oid) if oid else []
        shipments = 1 if oid in self.ghn.orders else 0
        paid_writes = sum(1 for e in events if e["detail"].startswith("Chữ ký hợp lệ"))
        duplicates = sum(1 for e in events if "đã xử lý: bỏ qua" in e["detail"])
        confirming = any(w["order_id"] == oid for w in self.pending_webhooks)
        pay = order["payment_status"] if order else None
        tracking = order["tracking_code"] if order else None
        status = order["order_status"] if order else None
        c = []          # (nội dung, True đạt / False chưa đạt / None chờ thao tác)
        if name == "low_paid":
            c = [("Đơn chuyển PAID đúng một lần", pay == "SUCCEEDED" and paid_writes == 1),
                 ("Brevo tạo email xác nhận thanh toán", True if "Xác nhận thanh toán" in emails else None),
                 ("GHN tạo vận đơn", True if tracking else None),
                 ("Có mã GHN rồi Brevo mới tạo email thứ hai", True if tracking and "Mã vận đơn" in emails
                  else (False if "Mã vận đơn" in emails else None))]
        elif name == "high":
            decided = status != "ON_HOLD"
            c = [("Đơn bị giữ, chưa tạo phiên Stripe khi chờ duyệt", (order["session_id"] is None) if not decided else True),
                 ("Admin duyệt hoặc từ chối", True if decided else None),
                 ("Nếu duyệt: khách tiếp tục thanh toán được",
                  True if status in ("PAYMENT_PENDING", "PAID", "SHIPPING", "DELIVERED") and order["session_id"]
                  else None)]
        elif name == "security_fail":
            tried = any(e["status"] == "FAIL" and e["step"].startswith("2.") for e in events)
            stripe_calls = sum(1 for call in self.hub.calls if call["service"] == "Stripe" and call["time"] >= run["started"])
            fail_at = next((e["time"] for e in events if e["status"] == "FAIL" and e["step"].startswith("2.")), time.time())
            stripe_calls = sum(1 for call in self.hub.calls
                               if call["service"] == "Stripe" and run["started"] <= call["time"] <= fail_at)
            c = [("Khách thấy “Chưa thể xác minh. Vui lòng thử lại.”", True if tried else None),
                 ("Lần xác minh lỗi không tạo đơn", order is None or order["created_at"] > fail_at),
                 ("Lần xác minh lỗi không tạo phiên Stripe", stripe_calls == 0),
                 ("Giữ giỏ hàng và thông tin đã nhập (xem ở khung khách)", True if tried else None)]
        elif name == "payment_unknown":
            waiting = pay == "PENDING"
            c = [("Giữ đơn ở trạng thái chờ khi chưa có kết quả", True if waiting and confirming else (pay == "SUCCEEDED")),
                 ("Chưa ghi PAID, chưa tạo vận đơn, chưa gửi email khi đang chờ",
                  (paid_writes == 0 and not shipments and not emails) if waiting else True),
                 ("Khi kết quả về: đơn chuyển PAID đúng một lần", True if pay == "SUCCEEDED" and paid_writes == 1 else None)]
        elif name == "duplicate_event":
            c = [("Nhật ký đánh dấu lần thứ hai là trùng", duplicates >= 1),
                 ("Đơn chuyển PAID đúng một lần", pay == "SUCCEEDED" and paid_writes == 1),
                 ("Tổng số vận đơn vẫn là 1", True if shipments == 1 and tracking else None),
                 ("Tổng số email vẫn là 2", True if emails == ["Mã vận đơn", "Xác nhận thanh toán"] else (None if len(emails) < 2 else False))]
        elif name == "ghn_down":
            c = [("Đơn vẫn PAID", pay == "SUCCEEDED"),
                 ("Email xác nhận thanh toán đã được tạo", True if "Xác nhận thanh toán" in emails else None),
                 ("Email mã vận đơn chỉ xuất hiện sau khi có mã GHN", ("Mã vận đơn" in emails) == bool(tracking)),
                 ("Sau khi GHN hoạt động lại và đối soát: có mã vận đơn", True if tracking else None)]
        actions = []
        if order:
            if status == "ON_HOLD":
                actions += [["approve", "Admin: Duyệt đơn"], ["reject", "Admin: Từ chối đơn"]]
            if confirming:
                actions.append(["deliver_now", "Cho kết quả thanh toán về ngay"])
            if pay == "SUCCEEDED" and oid in self.last_event:
                actions.append(["resend_event", "Gửi lại sự kiện thanh toán"])
            if order["ship_check"]:
                actions.append(["restore_ghn", "Khôi phục GHN và đối soát vận đơn"])
        return {"ok": True, "run": {k: run[k] for k in ("name", "view", "token", "order_id")}, "info": self.DEMO[name],
                "order": self._admin_row(order, {w["order_id"] for w in self.pending_webhooks}) if order else None,
                "events": events, "checks": [{"text": t, "ok": ok} for t, ok in c], "actions": actions,
                "counts": {"shipments": shipments, "emails": emails, "paid_writes": paid_writes, "duplicates": duplicates},
                "faults": sorted(self.hub.faults)}

    def demo_action(self, run_id, action, actor=""):
        run = self.demo_runs.get(run_id)
        row = run and self.db.execute("SELECT id FROM orders WHERE uid=? ORDER BY created_at LIMIT 1", (run["uid"],)).fetchone()
        if not row:
            return {"ok": False, "message": "Tình huống này chưa có đơn."}
        oid = row["id"]
        if action == "approve":
            return self.review(oid, True, actor=actor)
        if action == "reject":
            return self.review(oid, False, "Từ chối trong buổi demo", actor=actor)
        if action == "deliver_now":
            for webhook in self.pending_webhooks:
                if webhook["order_id"] == oid:
                    webhook["due"] = 0
            self.tick()
            return {"ok": True}
        if action == "resend_event" and oid in self.last_event:
            raw, _ = self.last_event[oid]
            code, text = self.handle_webhook(raw, self.stripe.sign(raw))      # Stripe gửi lại cùng event id
            return {"ok": True, "http": code, "result": text}
        if action == "restore_ghn":
            self.hub.faults.discard("ghn")
            result = self.reconcile_shipment(oid)
            if result.get("ok") and not result.get("found"):
                result = self.retry_shipment(oid)
            self.tick()
            return result
        return {"ok": False, "message": "Thao tác không hợp lệ."}

    def reset_demo(self):
        """Xóa riêng dữ liệu của các phiên demo; đơn của khách thật và dữ liệu khác được giữ nguyên."""
        ids = {r["id"] for r in self.db.execute("SELECT id FROM orders WHERE demo IS NOT NULL").fetchall()}
        uids = {run["uid"] for run in self.demo_runs.values()}
        tokens = {run["token"] for run in self.demo_runs.values()}
        for oid in ids:
            self.db.execute("DELETE FROM orders WHERE id=?", (oid,))
            self.db.execute("DELETE FROM outbox WHERE order_id=?", (oid,))
            self.db.execute("DELETE FROM tickets WHERE order_id=?", (oid,))
            self.ghn.orders.pop(oid, None)
            self.last_event.pop(oid, None)
        self.db.commit()
        self.events = [e for e in self.events if e["order_id"] not in ids and e["uid"] not in uids]
        self.db.execute("DELETE FROM events")
        self.db.executemany("INSERT INTO events (time, order_id, uid, step, service, status, detail, ref) VALUES (?,?,?,?,?,?,?,?)",
                            [tuple(e[k] for k in ("time", "order_id", "uid", "step", "service", "status", "detail", "ref")) for e in self.events])
        for oid in ids:
            self.db.execute("DELETE FROM payment_attempts WHERE order_id=?", (oid,))
            self.db.execute("DELETE FROM processed_events WHERE order_id=?", (oid,))
        self.db.commit()
        self.alerts = [a for a in self.alerts if a["order_id"] not in ids]
        self.pending_webhooks = [w for w in self.pending_webhooks if w["order_id"] not in ids]
        self.brevo.sent = {k: m for k, m in self.brevo.sent.items() if m["params"]["order_id"] not in ids}
        for token in tokens:
            self.firebase.users.pop(token, None)
        self.demo_runs = {}
        self.hub.faults_once.clear()
        self.hub.faults.discard("ghn")
        return {"ok": True, "removed_orders": len(ids)}

    # ------------------------------------ Giả lập phía khách và phía Stripe cho demo
    def simulate_payment(self, order_id, outcome):
        """outcome: success | fail | fake | duplicate | tamper | expired"""
        order = self.order(order_id)
        if not order or not order["session_id"] or order["pay_mode"] == "real":
            return {"ok": False, "message": "Đơn chưa có phiên thanh toán giả lập."}
        session = {"payment_intent": order["payment_intent"], "amount": order["amount"],
                   "currency": order["currency"], "metadata": {"order_id": order_id}}
        kind = "payment_intent.payment_failed" if outcome == "fail" else "payment_intent.succeeded"
        event = self.stripe.build_event(kind, session, amount=1000 if outcome == "tamper" else None)
        raw = dumps(event)
        if outcome == "fake":
            header = self.stripe.sign(raw, secret="whsec_attacker")           # kẻ gian không có Signing Secret
        elif outcome == "expired":
            header = self.stripe.sign(raw, timestamp=time.time() - 3600)      # phát lại sự kiện cũ
        else:
            header = self.stripe.sign(raw)
        if outcome in ("success", "fail", "duplicate"):
            self.last_event[order_id] = (raw, header)
        results = [self.handle_webhook(raw, header)]
        if outcome == "duplicate":
            results.append(self.handle_webhook(raw, header))                  # Stripe gửi lại cùng event id
        return {"ok": True, "results": [{"http": code, "result": text} for code, text in results]}

    # ------------------------------------------------------- Dữ liệu cho giao diện
    def state(self):
        rows = lambda sql: [dict(r) for r in self.db.execute(sql).fetchall()]
        return {
            "catalog": CATALOG,
            "orders": rows("SELECT * FROM orders ORDER BY created_at DESC"),
            "outbox": rows("SELECT * FROM outbox ORDER BY id DESC"),
            "events": self.events[-200:],
            "alerts": self.alerts,
            "emails": list(self.brevo.sent.values()),
            "shipments": list(self.ghn.orders.values()),
            "calls": self.hub.calls[-60:],
            "faults": sorted(self.hub.faults),
            "processed_events": self.db.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0],
            "pending_webhooks": len(self.pending_webhooks),
        }


# ============================================================ Kịch bản demo dựng sẵn
def _token():
    import secrets
    return "ts-ok-" + secrets.token_hex(4)


def scenario(backend, name):
    """Chạy một kịch bản từ đầu đến cuối. Trả về mã đơn (nếu có)."""
    b = backend
    b._force_sim = True                           # kịch bản dựng sẵn luôn chạy bằng dịch vụ giả lập
    try:
        return _scenario(b, name)
    finally:
        b._force_sim = False


def _scenario(b, name):
    b.hub.faults.clear()
    alice = b.firebase.new_customer()             # mỗi lần chạy dùng một khách riêng (tài khoản lâu năm)
    bob = b.firebase.new_customer(fresh=True)     # và một tài khoản vừa đăng ký
    if name == "happy":                       # đơn LOW, mọi dịch vụ hoạt động
        b.ask_ai("Tìm giúp mình tai nghe chống ồn dưới 2 triệu, email mình là an@example.com")
        r = b.checkout(alice, _token(), {"SP01": 1})
        b.simulate_payment(r["order_id"], "success")
        b.run_worker(); b.run_worker()
        b.view_order(bob, r["order_id"])
    elif name == "high_risk":                 # tài khoản mới + đơn lớn -> HIGH -> duyệt
        r = b.checkout(bob, _token(), {"SP03": 1})
        b.review(r["order_id"], approve=True)
        b.simulate_payment(r["order_id"], "success")
        b.run_worker(); b.run_worker()
    elif name == "high_risk_reject":          # HIGH -> từ chối
        r = b.checkout(bob, _token(), {"SP03": 1})
        b.review(r["order_id"], approve=False)
        b.run_worker()
    elif name == "security_down":             # Security API lỗi -> fail closed
        b.hub.faults.add("turnstile")
        r = b.checkout(alice, _token(), {"SP02": 1})
        b.hub.faults.clear()
        b.checkout(alice, "ts-bot", {"SP02": 1})
        b.checkout("idtoken-khong-hop-le", _token(), {"SP02": 1})
    elif name == "payment_down":              # Payment API lỗi -> thử lại, giữ đơn, sau đó phục hồi
        b.hub.faults.add("stripe")
        r = b.checkout(alice, _token(), {"SP02": 1})
        b.hub.faults.clear()
        b.start_payment(r["order_id"])        # cùng Idempotency-Key với lần trước
        b.simulate_payment(r["order_id"], "success")
        b.run_worker(); b.run_worker()
    elif name == "fake_webhook":              # Webhook giả, phát lại, sửa số tiền
        r = b.checkout(alice, _token(), {"SP04": 2})
        b.simulate_payment(r["order_id"], "fake")
        b.simulate_payment(r["order_id"], "expired")
        b.simulate_payment(r["order_id"], "tamper")
    elif name == "duplicate_webhook":         # Stripe gửi lại cùng một sự kiện
        r = b.checkout(alice, _token(), {"SP01": 1})
        b.simulate_payment(r["order_id"], "duplicate")
        b.run_worker(); b.run_worker()
    elif name == "payment_failed":            # thất bại -> thử lại lần 2 thành công
        r = b.checkout(alice, _token(), {"SP02": 1})
        b.simulate_payment(r["order_id"], "fail")
        b.run_worker()
        b.start_payment(r["order_id"], new_attempt=True)
        b.simulate_payment(r["order_id"], "success")
        b.run_worker(); b.run_worker()
    elif name == "ghn_lost":                  # GHN đã tạo đơn nhưng rớt phản hồi -> đối soát
        r = b.checkout(alice, _token(), {"SP04": 1})
        b.simulate_payment(r["order_id"], "success")
        b.hub.faults.add("ghn_lost_response")
        b.run_worker()                        # GHN đã tạo vận đơn nhưng phản hồi bị rớt: dừng tự động
        b.hub.faults.clear()
        b.reconcile_shipment(r["order_id"])   # người vận hành đối soát: thấy vận đơn đã có, dùng lại mã
        b.run_worker()
    elif name == "ai_down":                   # AI lỗi -> khách vẫn mua được
        b.hub.faults.add("ai")
        b.ask_ai("Tư vấn giúp mình chuột không dây")
        b.hub.faults.clear()
        r = b.checkout(alice, _token(), {"SP04": 1})
    else:
        raise ValueError(f"Không có kịch bản: {name}")
    b.hub.faults.clear()
    return r.get("order_id") if isinstance(r, dict) else None


SCENARIOS = {
    "happy": "Đơn LOW, luồng thành công đầy đủ",
    "high_risk": "Đơn HIGH: giữ đơn, ALERT, người vận hành duyệt",
    "high_risk_reject": "Đơn HIGH bị từ chối: hủy đơn, email báo khách",
    "security_down": "Security API lỗi / bot / token sai: dừng checkout",
    "payment_down": "Payment API lỗi: thử lại cùng Idempotency-Key, giữ đơn",
    "fake_webhook": "Webhook giả, phát lại, sửa số tiền: từ chối",
    "duplicate_webhook": "Webhook trùng: chỉ ghi PAID và giao hàng một lần",
    "payment_failed": "Thanh toán thất bại rồi thử lại thành công",
    "ghn_lost": "GHN rớt phản hồi: đối soát, dùng lại mã, không tạo hai vận đơn",
    "ai_down": "AI Service lỗi: khách vẫn mua được",
}
