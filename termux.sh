#!/usr/bin/env bash
#
# Volexturn روی Termux (اندروید) — نصب، اجرا، بیدار نگه‌داشتن گوشی.
# توضیح کامل و عیب‌یابی: docs/TERMUX.md
#
# این اسکریپت عمداً هیچ چیز رازآلودی انجام نمی‌دهد: همان چند خطی است که در
# docs/TERMUX.md دست‌ی نوشته شده، با دو تفاوت که روی گوشی مهم‌اند —
#  * `requirements-core.txt` را نصب می‌کند (gevent روی اندروید build نمی‌شود)
#  * کلید نشست را یک‌بار در .env می‌گذارد تا restartها نشست‌ها را باطل نکنند
#
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
PORT="${PORT:-5000}"
ENV_FILE=".env"

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }

need_python() {
  command -v "$PY" >/dev/null 2>&1 || die "python3 پیدا نشد —  pkg install -y python"
  "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "پایتون ۳.۱۱+ لازم است (نسخهٔ فعلی: $("$PY" -V 2>&1))"
}

# .env را طوری می‌سازد که VOLEXTURN_SECRET_KEY داشته باشد؛ اگر باشد دست نمی‌زند.
ensure_key() {
  # فقط اضافه می‌کند؛ اگر کلید تنظیم شده باشد دست‌نمی‌زند. اگر خط خالی
  # VOLEXTURN_SECRET_KEY= وجود داشته باشد، خط تازه‌ای که زیرش می‌آید برنده است
  # (python-dotenv آخرین مقدار هر کلید را نگه می‌دارد).
  if [ -f "$ENV_FILE" ] && grep -qs '^VOLEXTURN_SECRET_KEY=..*' "$ENV_FILE"; then
    return 0
  fi
  local key
  key="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  touch "$ENV_FILE" && chmod 600 "$ENV_FILE"
  printf 'VOLEXTURN_SECRET_KEY=%s\n' "$key" >> "$ENV_FILE"
  say "→ VOLEXTURN_SECRET_KEY در $ENV_FILE نوشته شد (این فایل در .gitignore است؛ commit نشود)"
}

install_deps() {
  say "→ pip install -r requirements-core.txt"
  if ! "$PY" -m pip install -r requirements-core.txt; then
    say "  خطا داد؛ دوباره با PIP_BREAK_SYSTEM_PACKAGES=1 امتحان می‌شود"
    PIP_BREAK_SYSTEM_PACKAGES=1 "$PY" -m pip install -r requirements-core.txt
  fi
}

lan_ip() {
  "$PY" - <<'EOS' 2>/dev/null || true
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("8.8.8.8", 80))
    print(s.getsockname()[0])
except OSError:
    pass
finally:
    s.close()
EOS
}

case "${1:-help}" in
  install)
    need_python
    if ! "$PY" -c 'import flask, socketio' >/dev/null 2>&1; then
      install_deps
    else
      say "→ وابستگی‌ها نصب‌اند"
    fi
    ensure_key
    say "→ migrate"
    "$PY" -m backend migrate
    say "→ doctor"
    "$PY" -m backend doctor || say "  (بعضی موارد ❌ است — متن بالا را بخوان)"
    say ""
    say "آماده. اگر هنوز ادمین نساختی:"
    say "  $PY -m backend create-admin -u root -p 'یک-رمز-بلند'"
    say "و برای اجرا:"
    say "  bash ./termux.sh run"
    ;;

  run)
    need_python
    if ! "$PY" -c 'import flask, socketio' >/dev/null 2>&1; then
      say "وابستگی‌ها نصب نشده — اول:  bash ./termux.sh install"
      exit 1
    fi
    ensure_key
    # wake-lock تا سرور زنده است (فقط روی Termux؛ جای دیگر این دستور نیست)
    if command -v termux-wake-lock >/dev/null 2>&1; then
      termux-wake-lock || say "  (termux-wake-lock کار نکرد — باتری Termux را روی Unrestricted بگذار)"
    fi
    export PORT
    ip="$(lan_ip || true)"
    say "↔️  این گوشی:      http://127.0.0.1:$PORT"
    [ -n "$ip" ] && say "↔️  شبکهٔ محلی:     http://$ip:$PORT"
    say "   (اگر بقیه وصل نشدند: همان WiFi باشند + AP/isolation مودم خاموش)"
    exec "$PY" -m backend run
    ;;

  bg)
    # همان run، ولی در پس‌زمینه با لاگ — برای وقتی که می‌خواهی ترمینال را ببندی
    need_python
    mkdir -p "$HOME"
    nohup bash ./termux.sh run >> "$HOME/volexturn.log" 2>&1 &
    say "سرور در پس‌زمینه شروع شد (pid=$!). لاگ: ~/volexturn.log"
    say "خاموش‌کردن:  pkill -f 'backend run'"
    ;;

  doctor)
    need_python; exec "$PY" -m backend doctor "${@:2}"
    ;;

  admin)
    need_python; exec "$PY" -m backend create-admin "${@:2}"
    ;;

  ip)
    need_python
    ip="$(lan_ip || true)"
    if [ -n "$ip" ]; then say "http://$ip:$PORT"; else
      say "IP پیدا نشد؛ دستی:  ip -4 addr show wlan0 | awk '/inet /{print \$2}' | cut -d/ -f1"
    fi
    ;;

  boot)
    # ~/.termux/boot/volexturn.sh — با اپ Termux:Boot (F-Droid) بعد از بوت گوشی اجرا می‌شود
    need_python
    mkdir -p "$HOME/.termux/boot"
    cat > "$HOME/.termux/boot/volexturn.sh" <<EOS
#!/usr/bin/env bash
termux-wake-lock 2>/dev/null || true
cd "$(pwd)"
exec bash ./termux.sh run >> "\$HOME/volexturn.log" 2>&1
EOS
    chmod +x "$HOME/.termux/boot/volexturn.sh"
    say "→ نوشت $HOME/.termux/boot/volexturn.sh"
    say "برای فعال‌شدن، اپ Termux:Boot (F-Droid) را نصب کن و گوشی را یک‌بار restart کن."
    ;;

  status)
    if pgrep -f "backend run" >/dev/null 2>&1; then
      say "در حال اجراست (pid=$(pgrep -f 'backend run' | tr '\n' ' '))"
    else
      say "در حال اجرا نیست"
    fi
    ;;

  stop)
    pkill -f "backend run" && say "متوقف شد" || say "چیزی در حال اجرا نبود"
    ;;

  *)
    cat <<'EOS'
Volexturn / Termux — کمک

  bash ./termux.sh install     وابستگی‌ها (requirements-core.txt) + migrate + doctor
  bash ./termux.sh run         اجرا در پیش‌زمینه (با wake-lock و چاپ آدرس LAN)
  bash ./termux.sh bg          همان run در پس‌زمینه، لاگ در ~/volexturn.log
  bash ./termux.sh status      وضعیت اجرا
  bash ./termux.sh stop        توقف
  bash ./termux.sh doctor      [ –json ] بررسی سلامت
  bash ./termux.sh admin -u root -p '…'    اولین ادمین
  bash ./termux.sh ip          آدرس LAN برای دادن به بقیه
  bash ./termux.sh boot        ساخت اسکریپت Termux:Boot

تنظیمات: PORT=8080 (پیش‌فرض 5000) · فایل .env در کنار همین اسکریپت
راهنمای کامل و عیب‌یابی: docs/TERMUX.md
EOS
    ;;
esac
