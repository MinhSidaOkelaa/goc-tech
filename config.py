"""Đọc cấu hình từ biến môi trường và file .env (cùng thư mục). Khóa bí mật chỉ nằm ở backend."""
import os
from pathlib import Path


def load_env(path):
    """Nạp KEY=VALUE từ .env; biến đã có sẵn trong môi trường được ưu tiên."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


load_env(Path(__file__).with_name(".env"))


def get(name, default=""):
    return os.environ.get(name, default).strip()


def mask(value):
    """Che khóa/token trước khi ghi log: chỉ giữ vài ký tự đầu để nhận diện."""
    value = str(value or "")
    if not value:
        return "(trống)"
    return value[:6] + "…" + f"({len(value)} ký tự)" if len(value) > 10 else "…"
