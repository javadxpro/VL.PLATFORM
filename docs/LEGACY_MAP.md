# نقشهٔ مسیرهای قدیمی (Legacy Map)

سرور canonical دو نوع سازگاری با کلاینت‌های نسخهٔ ۳ دارد. این فایل هر دو را از
خودِ کد فهرست می‌کند، چون «یک روت را عوض نکردیم» چیزی است که با چشم قابل
بررسی نیست و با تست قابل اثبات است.

## ۱) alias: مسیر قدیمی روی همان هندلر جدید

در `Module.route(..., legacy="/x")` ثبت می‌شود و در `LEGACY_BP` به همان view
می‌چسبد؛ یعنی **شکل پاسخ، اعتبارسنجی و همهٔ فیلترهای دسترسی نسخهٔ جدید** را
می‌گیرد. این مسیرها برای کلاینت‌هایی است که فقط آدرس عوض شده‌اند.

تعداد: 22

| مسیر قدیمی | متد | endpoint جدید |
|---|---|---|
| `/api/app_info` | GET | `meta.app_info` |
| `/api/me` | GET | `auth.me_endpoint` |
| `/change_password` | POST | `auth.change_password` |
| `/create_group` | POST | `groups.create_group` |
| `/create_lan_host` | POST | `servers.create` |
| `/create_post` | POST | `posts.create_post` |
| `/create_story` | POST | `stories.create_story` |
| `/delete_lan_host/<int:host_id>` | DELETE | `servers.delete` |
| `/delete_message/<int:mid>` | DELETE | `messages.delete_message` |
| `/edit_message` | POST | `messages.edit_message` |
| `/forward_message` | POST | `messages.forward_message` |
| `/group_add_member/<int:gid>` | POST | `groups.group_add_member` |
| `/group_delete/<int:gid>` | DELETE | `groups.group_delete` |
| `/login` | POST | `auth.login` |
| `/pin_message/<int:mid>` | POST | `messages.pin_message` |
| `/register` | POST | `auth.register` |
| `/seen_messages/<int:partner>` | POST | `messages.seen_messages` |
| `/send_message` | POST | `messages.send_message` |
| `/unread_counts/<int:me_id>` | GET | `messages.unread_counts` |
| `/update_profile` | POST | `users.update_profile` |
| `/view_post/<int:post_id>` | POST | `posts.view_post` |
| `/view_story/<int:story_id>` | POST | `stories.view_story` |

## ۲) هندلر قدیمی: مسیر قدیمی با شکل پاسخ قدیمی

وقتی کلاینت فقط آدرس را عوض نکرده — مثلاً آرایهٔ برهنه انتظار دارد، نه `items` —
از `@mod.legacy(rule)` استفاده می‌شود: تابعی جدا که همان کوئری را صدا می‌زند و
بدن را به شکل قدیمی درمی‌آورد. اینها در `LEGACY_BP` با نام `legacy_*` ثبت‌اند.

| مسیر قدیمی | متد | تابع |
|---|---|---|
| — | — | مجموع: 0 |

## ۳) تفاوت شکل پاسخ (چیزی که alias حل نمی‌کند)

این مسیرها در نسخهٔ ۳ بدنهٔ برهنه برمی‌گرداندند و همچنان برهنه می‌مانند:

- `GET /users`، `GET /posts`، `GET /messages/<u1>/<u2>`، `GET /group_messages/<gid>`،
  `GET /unread_counts/<me>`، `GET /post_comments/<id>`، `GET /stories`،
  `GET /lan_hosts`، `GET /my_groups` → **آرایهٔ برهنه**
- `GET /notifications/<me>` → `{"unread": n, "items": [...]}`
- canonical‌ها (`/api/…`) همیشه `{"success": true, …}` با `items` و صفحه‌بندی

## ۴) دیتابیس

مهاجرت `0001:baseline_legacy_schema` (در `backend/migrations.py`) نام‌ها و
ستون‌های نسخهٔ ۳ را به اسکیمای canonical می‌رساند؛ همان‌جا ببینید کدام ستون
مهاجرت کرده و کدام حذف شده — `doctor` هم اگر دیتابیس مهاجرت‌نکرده باشد همان را
به‌عنوان problem گزارش می‌کند.

## ۵) چه‌کار کنیم

کلاینت‌های موجود می‌توانند بدون تغییر کار کنند، اما مسیرهای قدیمی new feature
نمی‌گیرند. برای مهاجرت: از `python3 -m backend routes --filter <substring>` برای
دیدن هر دو سطح استفاده کنید، و از تست‌های `tests/` به‌عنوان نمونهٔ درخواست/پاسخ.
