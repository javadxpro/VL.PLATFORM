# اجرا روی Termux (اندروید)

سرور روی گوشی، روی شبکهٔ محلی، بدون سرور ابری؛ دیتابیس و آپلودها هم روی همان
گوشی می‌مانند و بقیهٔ دستگاه‌ها با IP گوشی وصل می‌شوند.

دو تفاوت اصلی با دسکتاپ دارد، وگرنه همان `backend/`، همان CLI و همان SQLite است:

1. `gevent` روی اندروید build نمی‌شود و `gunicorn` قابل اتکا نیست →
   `requirements.txt` کامل را نصب نکن.
2. اندروید هر پروسهٔ پس‌زمینه را بعد از چند دقیقه می‌کُشد → wake-lock و تنظیم
   باتری الزوری است، نه اختیاری.

Termux را از **F-Droid** (یا GitHub releases) بگیر؛ نسخهٔ Play Store قدیمی و
رهاشده است.

---

## ۱) نصب یک‌باره

```bash
pkg update -y && pkg upgrade -y
pkg install -y python git
python3 -V                 # باید 3.11 یا بالاتر باشد
```

```bash
git clone <آدرسِ مخزن> volexturn
cd volexturn
pip install -r requirements-core.txt
```

چرا `requirements-core.txt` و نه `requirements.txt`؟ چون `gevent` به C toolchain
و پچ‌های مخصوص اندروید نیاز دارد و نصب را نیمه‌کاره می‌کند. core دقیقاً همان چیزی
است که برنامه برای اجرا لازم دارد؛ در این حالت سرور threading mode است و
WebSocket هم با `simple-websocket` کار می‌کند.

اگر pip خطای `externally-managed-environment` داد:

```bash
PIP_BREAK_SYSTEM_PACKAGES=1 pip install -r requirements-core.txt
```

همین‌ها را `bash ./termux.sh install` یک‌جا انجام می‌دهد (به‌علاوهٔ preflight).

## ۲) کلید نشست، و یک preflight

بدون `VOLEXTURN_SECRET_KEY` برنامه بالا می‌آید، ولی هر restart نشست‌ها و
توکن‌های Socket.IO را باطل می‌کند. پس یک‌بار بسازش و در `.env` بگذار
(`python-dotenv` در core است، پس `.env` واقعاً خوانده می‌شود):

```bash
printf 'VOLEXTURN_SECRET_KEY=%s\n' "$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" > .env
python3 -m backend doctor
```

`doctor` اسکیمای مهاجرت‌شده، سقف آپلودها، driverها و تعداد ادمین‌ها را چک می‌کند.
روی دیتابیس تازه ممکن است ❌ «جدول نیست» بدهد — خودِ اجرا (و `create_app()`)
مهاجرت را می‌زند؛ یا دستی: `python3 -m backend migrate`.

## ۳) اولین ادمین

حساب پیش‌فرض وجود ندارد:

```bash
python3 -m backend create-admin -u root -p 'یک-رمز-بلند-و-منحصربه‌فرد'
```

## ۴) اجرا

```bash
bash ./termux.sh run                 # یا: python3 -m backend run
PORT=8080 bash ./termux.sh run       # پورت دیگر
```

پیش‌فرض `0.0.0.0:5000` است. سرور را در session دومِ Termux اجرا کن
(کشوی اعلان → اعلان Termux → ➕ New session) تا ترمینال اول آزاد بماند.

## ۵) زنده نگه‌داشتن روی اندروید

```bash
termux-wake-lock
```

و در تنظیمات اندروید: Apps → Termux → Battery → **Unrestricted** (روی بعضی ROMها
« بدون محدودیت»/«Allow background activity» و یک کلید جدا به اسم **Autostart**
هم دارد). وگرنه Doze بعد از قفل‌شدن صفحه سوکت‌ها را می‌بندد.

- صفحهٔ خاموش شود مشکلی نیست؛ wake-lock برای همین است.
- Battery saver روشن = سرور مرده، فارغ از بقیهٔ تنظیمات.
- با adb و روت: `adb shell dumpsys deviceidle whitelist +com.termux`

## ۶) بقیهٔ دستگاه‌ها چطور وصل شوند

```bash
bash ./termux.sh ip
# یا دستی:
ip -4 addr show wlan0 | awk '/inet /{print $2}' | cut -d/ -f1
```

روی دستگاه دیگر (همان WiFi): `http://<IP-گوشی>:5000`. از خود گوشی هم
`http://127.0.0.1:5000`.

اگر وصل نشد، به همین ترتیب چک کن (تقریباً همیشه یکی‌شان است):

1. گوشی و آن دستگاه روی **همان** شبکه باشند (نه یکی دیتای موبایل، یکی WiFi).
2. مودم client/AP isolation روشن نداشته باشد — بعضی مودم‌ها اسمش را
   «Privacy Separation» یا «Guest network» گذاشته‌اند؛ روی hotspot اپراتور هم
   معمولاً همین‌طور است.
3. سرور روی `0.0.0.0` باشد (پیش‌فرض هست) — در لاگ باید `Running on http://0.0.0.0:5000`
   را ببینی. فایروال اندروید جلوی LAN را نمی‌گیرد.

## ۷) HTTPS و رسیدن از بیرون خانه

```bash
pkg install cloudflared
cloudflared tunnel --url http://localhost:5000
```

ادامه (تانل دائمی، دامنه شخصی، VPS) در `DEPLOY.md`. برای شبکهٔ دوستان Tailscale
تمیزتر است. وقتی از بیرون وصل می‌شوی `VOLEXTURN_IS_SECURE=1` را هم در `.env`
بگذار تا کوکی `Secure` شود.

## ۸) دیتابیس و فایل‌ها کجا — و یک تلهٔ واقعی

پیش‌فرض داخل پوشهٔ پروژه است: `database.db` و `uploads/`، یعنی حافظهٔ داخلی
Termux. این درست‌ترین جا برای SQLite است.

❌ اینها را به `/sdcard/…` منتقل نکن: فایل‌سیستم اشتراکی (FUSE/sdcardfs) قفل‌های
POSIX را که SQLite لازم دارد پیاده نمی‌کند و نتیجه‌اش یا «database is locked» یا
فایل خراب است. برای بکاپ، کپی کن:

```bash
mkdir -p ~/backups
cp database.db ~/backups/volexturn-$(date +%F).db
tar czf ~/backups/uploads-$(date +%F).tgz uploads
```

## ۹) بالا آمدن خودکار بعد از روشن‌شدن گوشی

اپ **Termux:Boot** را از F-Droid نصب کن (هم‌امضای Termux؛ اگر از منابع مختلف
نصب شده باشی کار نمی‌کند)، بعد:

```bash
bash ./termux.sh boot        # ~/.termux/boot/volexturn.sh را می‌سازد
```

اسکریپت، wake-lock می‌گیرد و سرور را با لاگ در `~/volexturn.log` بالا می‌آورد.
یک‌بار گوشی را restart کن و تست بگیر.

## ۱۰) آپدیت

```bash
git pull
bash ./termux.sh doctor
```

مهاجرت‌ها idempotent‌اند و در boot خودکار اجرا می‌شوند؛ `python3 -m backend migrate`
هم دستی کار می‌کند.

## ۱۱) عیب‌یابی

| نشانه | کار |
|---|---|
| pip موقع build `gevent` می‌سوزد | `requirements-core.txt` را نصب کن، نه `requirements.txt` |
| `Address already in use` | `PORT=8080 bash ./termux.sh run` |
| با قفل‌شدن صفحه، Socket.IO می‌پرد | wake-lock / باتری Termux روی Unrestricted / Autostart |
| `.env` بی‌اثر است | `pip install python-dotenv` |
| لاگ JSON شلوغ است | در `.env`: `VOLEXTURN_LOG_LEVEL=WARNING` |
| صفحه می‌آید ولی فونت/JS نیست | `static/` کامل clone نشده (`git status` را ببین) |
| `database is locked` | DB روی `/sdcard` رفته؛ برگردانش داخل `$HOME` |
| `VOLEXTURN_SECRET_KEY not set` در production | کلید را در `.env` بگذار؛ در production برنامه بدون آن بالا نمی‌آید |

## ۱۲) چیزهایی که روی گوشی نمی‌شود حل کرد

- **threading mode، یک پروسه**: برای چند کاربر روی LAN کافی است؛ برای صدها
  اتصال هم‌زمان گوشی جای مناسبی نیست (برای آن کار، Docker/Render در `DEPLOY.md`).
- باکت‌های rate-limit و وضعیت presence در حافظه‌اند؛ با restart صفر می‌شوند.
- پورت‌های زیر ۱۰۲۴ بدون root باز نمی‌شوند.
- آپلود ویدیوی سنگین روی گوشی توصیه نمی‌شود؛ `VOLEXTURN_MAX_VIDEO_MB` را بالا نبر.
- **مجوز**: این پروژه متن‌باز نیست. `LICENSE` هیچ حقی — حتی اجرا روی دستگاه خودت
  اگر آن را با کس دیگری به اشتراک بگذاری — نمی‌دهد. شرایطش کوتاه است: اجازهٔ کتبی.
