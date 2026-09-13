#!/usr/bin/env bash
# ============================================
#  Volexturn — اجرای سریع (مخصوص GitHub Codespace)
#  دستور:  bash run.sh
# ============================================
set -e
cd "$(dirname "$0")"

echo "📦 در حال نصب وابستگی‌ها..."
pip install -q -r requirements.txt 2>/dev/null || pip3 install -q -r requirements.txt

echo ""
echo "🚀 سرور در حال اجرا..."
echo "   1) از پنل پایین VS Code، تب Ports را باز کن"
echo "   2) روی پورت 5000 → راست‌کلیک → Port Visibility → Public"
echo "   3) لینک https://...app.github.dev را به دوستانت بده!"
echo ""
python3 server.py
