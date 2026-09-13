# 🌐 راهنمای آنلاین‌کردن Volexturn

سه مسیر برای عمومی‌کردن پلتفرم وجود دارد. همه فایل‌های لازم آماده است.

---

## مسیر ۱ — هاست رایگان Render (دائمی، بدون روشن‌بودن سیستم تو)

بهترین گزینه اگر می‌خواهی لینک همیشه فعال باشد و سیستمت خاموش باشد.

### مراحل (۵ دقیقه):

1. پروژه را روی **GitHub** پوش کن (همه فایل‌ها از جمله `render.yaml` و `Procfile`)
2. برو به [render.com](https://render.com) و با گیت‌هابت ثبت‌نام کن
3. **New + → Blueprint** → مخزن `Local_Network` را انتخاب کن
   - (تنظیمات از `render.yaml` خودکار خوانده می‌شود)
4. **Apply** بزن — تمام! ✅

لینک می‌شود: `https://volexturn-xxxx.onrender.com`

### ⚠️ نکات مهم Render رایگان:
- بعد از **۱۵ دقیقه بی‌استفادگی** خاموش می‌شود و بازدید بعدی ~۳۰ ثانیه طول می‌کشد بالا بیاید (Wake-up)
- **دیتابیس و فایل‌های آپلودی در ری‌استارت/خواب پاک می‌شوند** (فضای موقت است). برای دائمی‌شدن داده‌ها: دیسک دائمی پولی Render، یا مسیر ۲/۳
- HTTPS خودکار دارد → **میکروفون روی گوشی هم کار می‌کند** 🎤

---

## مسیر ۲ — Cloudflare Tunnel روی دستگاه خودت (داده پیش خودت می‌ماند)

مخصوصاً برای Termux/گوشی یا PC‌ای که روشن است. رایگان + HTTPS خودکار + بدون بازکردن پورت روتر!

### روی PC (ویندوز/لینوکس/مک):
1. `cloudflared` را دانلود و نصب کن: [developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
2. سرور را اجرا کن:
   ```bash
   python server.py
   ```
3. در یک ترمینال دیگر:
   ```bash
   cloudflared tunnel --url http://localhost:5000
   ```
4. لینکی مثل `https://xxxx-yyyy.trycloudflare.com` می‌دهد → به بقیه بده! ✅

### روی Termux (اندروید):
```bash
pkg install cloudflared -y
python server.py &          # داخل پوشه پروژه
cloudflared tunnel --url http://localhost:5000
```

> لینک trycloudflare موقتی است (تا وقتی tunnel باز است). با حساب رایگان Cloudflare + دامنه شخصی می‌توانی **لینک دائمی با اسم دلخواه** بسازی:
> ```bash
> cloudflared tunnel login
> cloudflared tunnel create volexturn
> cloudflared tunnel route dns volexturn chat.yourdomain.com
> cloudflared tunnel run volexturn
> ```

---

## مسیر ۳ — VPS (حرفه‌ای‌ترین)

یک سرور مجازی ارزان بگیر (ابط، آروان، لیارا، Hetzner...) و:

```bash
git clone https://github.com/javadxpro/Local_Network.git
cd Local_Network
pip install -r requirements.txt
gunicorn -k gevent -w 1 -b 0.0.0.0:8000 server:app
```

- دیتای SQLite دائمی می‌ماند ✅
- برای HTTPS پشت Nginx + Certbot بگذار (یا خود Cloudflare Tunnel روی VPS)
- با systemd سرویسش کن تا همیشه بالا باشد

---

## 🔒 چک‌لیست امنیتی قبل از آنلاین‌شدن (مهم!)

1. ✅ **رمز ادمین را فوراً عوض کن** — وارد شو → پروفایل → «تغییر رمز عبور» (قابلیت جدید v3.1)
2. ✅ ثبت‌نام آزاد است؛ اگر فقط برای دوستانت است، ثبت‌نام‌ها را کنترل کن
3. ✅ پلتفرم توکن نشست + هش PBKDF2 دارد، ولی **HTTPS همیشه روشن باشد** (مسیر ۱ و ۲ خودکار دارند)
4. ✅ فایروال: پورت 5000 را مستقیم به اینترنت باز نکن — از تونل/پروکسی استفاده کن

---

## اجرای پروداکشن (همه مسیرها)

سرور با gunicorn + gevent اجرا می‌شود (WebSocket کامل + پایدار):

```bash
gunicorn -k gevent -w 1 -b 0.0.0.0:$PORT server:app
```

> حتماً `-w 1` (یک ورکر) بماند — Socket.IO با چند ورکر نیاز به sticky session دارد.

پورت روی هاست‌های ابری خودکار از `PORT` خوانده می‌شود.
