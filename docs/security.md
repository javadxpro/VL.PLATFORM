# امنیت

هرچه در این فایل است از خودِ کد گرفته شده و کنار هر بند، فایل مرجش آمده تا
هنگام تغییر کد، جای اشتباه معلوم باشد. بخش‌های ۱ تا ۹ چیزی را که *هست* توصیف
می‌کند؛ بخش ۱۰ عمداً می‌گوید چه چیزی *نیست*.

---

## ۱. رمز عبور  · `backend/security.py`

- `PBKDF2-HMAC-SHA256`، salt مستقل برای هر کاربر،
  `VOLEXTURN_PBKDF2_ITERATIONS` = **۲۴۰٬۰۰۰** پیش‌فرض. قالب ذخیره
  `pbkdf2$<iters>$<salt>$<digest>` — تعداد دور داخل خود رشته است، پس افزایشش
  بعداً حساب‌های قبلی را نمی‌شکند.
- رمزهای نسخهٔ ۳ (متن ساده یا PBKDF2 با دور کم) در اولین ورود موفق بی‌صدا
  بازنویسی می‌شوند (`needs_rehash` → `hash_password`).
- `check_password_strength` هم در ثبت‌نام، هم `POST /api/auth/password/strength`،
  هم `python -m backend create-admin` اجرا می‌شود — پس اعتبارسنجی سمت کلاینت نیست.
- هیچ حساب پیش‌فرضی ساخته نمی‌شود؛ اگر دیتابیس قدیمی `admin123` را داشته باشد
  `python -m backend doctor` آن را ❌ می‌کند.

## ۲. نشست  · `backend/auth.py`

- کوکی `vx_session` با `HttpOnly`، `SameSite=Lax`، و `Secure` وقتی
  `VOLEXTURN_ENV=production`. مسیر API با `Authorization: Bearer` هم کار می‌کند.
- توکن تصادفی است و در جدول نشست‌ها با انقضا ثبت می‌شود. تغییر رمز،
  `logout-all` و `revoke` همان کاربر، نشست‌های قبلی‌اش را باطل می‌کند
  (تست: `tests/test_api_auth.py`، `tests/test_cli.py`).
- در production نبود `VOLEXTURN_SECRET_KEY` باعث می‌شود برنامه اصلاً بالا نیاید؛
  در development فقط هشدار می‌دهد و نشست‌ها با restart می‌میرند.

## ۳. CSRF  · `backend/security.py: same_origin_ok`

برای متدهای غیرایمن روی هر مسیری با `csrf=True` (پیش‌فرض همه‌جا جز موارد صریح):

- درخواستی که با هدر `Authorization` می‌آید از قبل cross-site قابل جعل نیست
  (مرورگر آن هدر را بدون CORS اجازه‌داده‌شده نمی‌فرستد و CORS در این برنامه به
  میزبان قفل است)؛ پذیرفته می‌شود.
- درخواست کوکی‌محور باید مبدأش ثابت باشد: `Origin` یا `Referer` با میزبان
  می‌خواند. **هم‌سایت** کافی است نه هم‌ریشهٔ سخت، چون کاربر LAN ممکن است با IP
  یا نام میزبانِ olmayanِ `Host` وارد شود.
- نبودِ هر دو هدر فقط وقتی مجاز است که درخواست شبیه مرورگر نباشد (یعنی
  `Content-Type: application/json` نداشته باشد)؛ کلاینت‌های غیرمرورگری همین‌طور
  کار می‌کنند.

## ۴. Rate limit  · `backend/api/__init__.py: _guarded`

- پیش‌فرض هر endpoint: ۱۲۰ درخواست / ۶۰ ثانیه
  (`VOLEXTURN_RATE_CAPACITY`, `VOLEXTURN_RATE_WINDOW`).
- کلید باکت: `u<user_id>` با نشست معتبر، وگرنه `ip:<client_ip>` — پس یک کاربر
  با IP مشترک بقیه را خفه نمی‌کند.
- ورود دو لایه محدود می‌شود: `auth:login_ip` با ۸ تلاش
  (`VOLEXTURN_LOGIN_MAX_ATTEMPTS`) و `auth:login_user` با ۵ تلاش برای همان نام
  کاربری (بی‌توجه به بزرگی حرف)، هر دو در پنجرهٔ ۳۰۰ ثانیه
  (`VOLEXTURN_LOGIN_WINDOW_SECONDS`).
- ثبت‌نام ۵/۶۰۰ ثانیه، تغییر رمز ۵/۳۰۰، `setup` ۵/۹۰۰. لیست کامل در
  `docs/API.md`، ستون «Rate limit».

## ۵. آپلود  · `backend/uploads.py` + `backend/security.py: validate_upload`

سه بررسی مستقل باید هم‌راستا باشند: allowlist پسوند، نوع اعلامی مرورگر، و
magic bytes محتوا. دلیل کلاسیک دورزدنِ «فقط پسوند» همین است.

| چه چیزی | مقدار |
|---|---|
| سقف اندازه | image ۵MB · video ۱۰MB · file ۵۰MB · avatar ۳MB (`VOLEXTURN_MAX_*_MB`) |
| کدهای خطا | `FILE_MISSING` · `DENIED_EXTENSION` · `FILE_TOO_LARGE` · `SAVE_FAILED` |
| دلایل رد (از `validate_upload`) | `extension_<ext>` · `unrecognised_content` · `content_type_mismatch` · `declared_type_mismatch` |
| نام روی دیسک | تولیدی؛ فایل هرگز با نام کاربر ذخیره نمی‌شود |
| مسیر ذخیره | `VOLEXTURN_UPLOAD_FOLDER/<category>/…` |
| اثر انگشت | SHA-256 هر فایل ثبت می‌شود (برای ژانیتور/sweep، نه آنتی‌ویروس) |

سرو فایل از `GET /files/<category>/<path:filename>` است؛ هر نام غیرمجاز
۴۰۰ با `INVALID_KEY` می‌گیرد (نه ۴۰۴) تا حدس‌زدن traversal بی‌فایده باشد و
هیچ‌وقت مسیر بیرون از پوشهٔ آپلود serve نمی‌شود.

## ۶. دسترسی به داده  · `backend/visibility.py`

`auth="user"` فقط هویت را ثابت می‌کند؛ «این ردیف را ببینم یا نه» جای دیگری است:

- خصوصی‌بودن پست، audience استوری، دوستی و بلاک **داخل SQL** اعمال می‌شوند، نه
  بعد از گرفتن ردیف‌ها — وگرنه `total` و صفحه‌بندی دروغ می‌گفتند.
- `user_id`/`me_id` در path یا body هرگز منبع هویت نیستند؛ فقط با نشست
  مقایسه می‌شوند و اختلافشان ۴۰۳ می‌دهد.
- مسیرهای `auth="admin"` علاوه بر نشست، `role = 'admin'` را هم می‌خواهند؛ هیچ
  endpoint عمومی برای ارتقای نقش وجود ندارد.

## ۷. SQL و ورودی  · `backend/db.py`

- همه‌چیز پارامتری؛ هیچ `WHERE`ای با الحاق رشته ساخته نمی‌شود (برای الگوهای
  LIKE از `db.ilike` استفاده می‌شود که کاراکترهای ویژه را escape می‌کند).
- `sanitize_text` طول و newline را می‌بَرَد؛ `pagination_args` سقف `max_size`
  دارد تا `?limit=1000000` به ابزار تبدیل‌شدن نباشد.
- DDL فقط از `backend/migrations.py` می‌آید و مهاجرت‌ها خطی‌اند (v1…v10).

## ۸. هدرهای امنیتی  · `backend/app.py: _csp`

```
default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';
img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self';
connect-src 'self' ws: wss:; object-src 'none'; base-uri 'self';
form-action 'self'; frame-ancestors 'self'
```

به‌علاوهٔ `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`,
`Referrer-Policy: no-referrer` روی پاسخ‌ها، و CSP جداگانه و سخت‌گیرانه‌تر برای
خودِ فایل‌ها (`default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'`).

### ریسک باقی‌مانده و راه حذف `'unsafe-inline'`

`index.html` یک SPA تک‌فایلی است: ~۱۰۲ تا `onclick="…"` و بلوک‌های
`<script>` inline دارد. تا وقتی این‌طور است، `script-src 'unsafe-inline'`
لازم است و یعنی هر XSS که از فیلترهای `sanitize_text`/visibility رد شود *قابل
اجرا* است؛ `style-src 'unsafe-inline'` هم برای رنگ‌های تم inline لازم است.
برای بستن این دریچ، به ترتیب:

1. هندلرها را از attribute به `addEventListener` در یک فایل `.js` خارجی منتقل کنید
   (همان `index.html`، فقط بدون اسکریپت inline).
2. رنگ/استایل inline را به class تبدیل کنید و متغیرها را در یک stylesheet بگذارید.
3. بعد `'unsafe-inline'` را از هر دو directive حذف کنید؛ `/` و صفحهٔ SPA در
   `tests/test_compat_routes.py` پوشش داده شده‌اند و باید سبز بمانند (هر کلیک
   شکسته‌ای آنجا معلوم می‌شود، چون تست‌ها روی DOM واقعی هندلر صدا می‌زنند نه
   attribute).
4. اگر لازم شد policy را شل‌تر نکنید: `script-src 'self' 'sha256-…'` برای هر
   اسکریپت استثنا.

## ۹. راه‌اندازی اولیهٔ ادمین  · `backend/api/auth.py`

تا وقتی هیچ ادمینی نیست، `POST /api/auth/setup` باز است و دو قفل دارد: فقط از
`127.0.0.1`/آدرس private (یا با «کد راه‌اندازی» یک‌بارمصرف که در لاگ چاپ شده)
و به‌محض وجود اولین ادمین بسته می‌شود. مسیر جایگزین و توصیه‌شدهٔ روی سرور:
`python -m backend create-admin`.

## ۱۰. عمداً انجام **نشده**

- **TLS:** این برنامه تمام‌کنندهٔ TLS نیست. پشت reverse proxy / Render بگذارید و
  `VOLEXTURN_IS_SECURE=1` را تنظیم کنید تا کوکی `Secure` و scheme درست شود.
- **۲FA / SSO / رمزنگاری در حال استراحت / بازرسی بدافزار یا OCR روی مدیا: نیست.**
- **تأیید ایمیل:** حساب‌ها بدون تأیید ایمیل ساخته می‌شوند؛ هیچ سرویس ایمیلی در
  این پروژه config نشده است.
- **مقیاس افقی:** باکت‌های rate-limit، وضعیت presence و اتاق‌های Socket.IO در
  حافظهٔ همان پروسه‌اند. چند worker/instance یعنی اتاق‌های جدا؛ یا یک instance
  با `gevent` (همان چیزی که `Procfile`/`Dockerfile` می‌گذارند) یا اضافه‌کردن
  Redis manager.
- **SQLite** برای تک‌نویسنده خوب است؛ برای نوشتن‌های هم‌زمان `DATABASE_URL`
  (PostgreSQL) را ست کنید.

## ۱۱. گزارش آسیب‌پذیری

مشکل را **خصوصی** گزارش کنید (Security Advisory همان مخزن)، نه با Issue عمومی.
همراهش بفرستید: نسخهٔ برنامه و خروجی کامل

```bash
python3 -m backend doctor --json
```

این مستند JSON تنها چیزی است که schema، سقف آپلودها، driverها و مشکلات امنیتی
شناخته‌شده (مثل رمز پیش‌فرض قدیمی) را یک‌جا توصیف می‌کند، پس برای بازتولید کافی است.
