#!/usr/bin/env python3
"""
Volexturn — نقطه ورود (thin entrypoint)

=============================================
این فایل عمداً کوچک است. تمام منطق به بستهٔ `backend/` منتقل شده:

    backend/app.py        کارخانهٔ برنامه (Flask + SocketIO + config + migrations)
    backend/api/*.py      سطح HTTP (هم مسیرهای جدید /api/… و هم مسیرهای قدیمی)
    backend/realtime.py   رویدادهای socket.io
    backend/cli.py        ابزارهای خط فرمان (migrate / create-admin / doctor …)

`server:app` همان‌طور که در Procfile / Dockerfile / render.yaml استفاده می‌شود
باقی می‌ماند، پس هیچ چیز در دیپلوی عوض نمی‌شود.

اجرای مستقیم:
    python server.py                 # http://0.0.0.0:$PORT (پیش‌فرض 5000)
    python -m backend migrate        # فقط دیتابیس را مهاجرت بده
    python -m backend create-admin    # ساخت حساب مدیر اولیه
"""

from __future__ import annotations

import os
import socket
import sys

# اطمینان از اینکه بستهٔ backend از کنار همین فایل import می‌شود (مثلاً وقتی
# gunicorn را از دایرکتوری دیگری اجرا می‌کنید).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.app import create_app, socketio              # noqa: E402
from backend.config import get_config                     # noqa: E402
from backend.log import get_logger                        # noqa: E402

APP_NAME = "Volexturn"

# برنامه در زمان import ساخته می‌شود، دقیقاً مثل نسخهٔ قدیمی: gunicorn با
# `server:app` یک WSGI callable می‌خواهد، نه یک factory.
app = create_app()
log = get_logger("server")


def _lan_ip() -> str:
    """Best-effort LAN address for the startup banner; never raises."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _has_admin() -> bool:
    try:
        db = app.extensions["volexturn_db"]
        conn = db.connect()
        try:
            # role, not the legacy `is_admin` column: that no longer exists, and a
            # query that raises here used to make every boot claim "no admin exists".
            if not db.has_table(conn, "users"):
                return False
            return bool(db.scalar(conn, "SELECT COUNT(*) FROM users WHERE role = 'admin'"))
        finally:
            db.close(conn)
    except Exception:                 # no table yet / broken file → treat as "not ready"
        return False


def _banner(port: int) -> str:
    cfg = get_config()
    engine = cfg.db_engine
    lines = [
        "=" * 62,
        f"🚀 {APP_NAME} Super-App v{get_config().app_version}",
        f"🏠 Local:    http://localhost:{port}",
        f"🌐 Network:  http://{_lan_ip()}:{port}",
        f"🗄  Engine:  {engine} · {cfg.db_file if engine == 'sqlite' else 'PostgreSQL (DATABASE_URL)'}",
        "🔌 Sockets:  join / subscribe / typing / join_voice · leave_voice برای خروج",
    ]
    if not _has_admin():
        lines.append("👑 هیچ مدیری وجود ندارد — بسازید:  python -m backend create-admin")
    lines.append("🛠  عیب‌یابی:  python -m backend doctor")
    lines.append("=" * 62)
    return "\n".join(lines)


if __name__ == "__main__":
    _cfg = get_config()
    _port = int(os.environ.get("PORT") or _cfg.port)
    print(_banner(_port), flush=True)
    try:
        socketio.run(app, host=_cfg.host, port=_port, debug=False,
                     use_reloader=_cfg.dev_mode, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:                       # خاموش کردن تمیز
        print("\n👋 متوقف شد", flush=True)
        raise SystemExit(0) from None
