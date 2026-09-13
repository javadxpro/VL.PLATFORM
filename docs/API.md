# مرجع API

> این دو فایل را `python3 tools/gen_api_docs.py` از خودِ برنامه تولید می‌کند:
> مسیرها از `app.url_map`، و ستون‌های دسترسی/Rate/CSRF از دکوریتورهای
> `backend/api/*.py`. چیزی که اینجا نیست در کد هم نیست؛ اگر جدولی قدیمی به
> نظر رسید، اسکریپت را دوباره اجرا کنید نه دست‌تیپ را.

مجموع قواعد ثبت‌شده: **212** (شامل 22 میان‌بر قدیمی روی ریشه).

## قراردادها

| موضوع | قرارداد |
|---|---|
| پیشوند ماژول | `admin` → `/api/admin` · `auth` → `/api/auth` · `gaming` → `/api/games` · `groups` → `/api/groups` · `messages` → `/api/messages` · `meta` → `/api` · `notifications` → `/api/notifications` · `posts` → `/api/posts` · `reports` → `/api/reports` · `rooms` → `/api/rooms` · `search` → `/api/search` · `servers` → `/api/servers` · `stories` → `/api/stories` · `users` → `/api/users` |
| هویت | `Authorization: Bearer <token>` یا کوکی نشست. هیچ `user_id` ارسالی کلاینت معتبر نیست. |
| شناسهٔ درخواست | هر پاسخ `X-Request-Id` برمی‌گرداند و همان کلید در لاگ JSON است. |
| اسلش | `strict_slashes=False`؛ `/api/posts/` و `/api/posts` یکی‌اند. |
| خطا | `success: false` و کلیدهای `error`/`message` — همان شکل نسخهٔ قدیمی. |
| فهرست | مسیرهای canonical `items` + صفحه‌بندی می‌دهند؛ مسیرهای قدیمی آرایهٔ برهنه. |

سه پاسخ واقعی از همین build:

```json
// GET /api/posts — بدون نشست
{
  "success": false,
  "error": {
    "code": "UNAUTHORIZED",
    "message": "نیاز به ورود — دوباره وارد شوید"
  },
  "message": "نیاز به ورود — دوباره وارد شوید",
  "request_id": "<per-response>"
}
```

```json
// POST /api/auth/register — بدنامعتبر
{
  "success": false,
  "error": {
    "code": "FIELD_REQUIRED",
    "message": "«username» الزامی است",
    "details": {
      "field": "username"
    }
  },
  "message": "«username» الزامی است",
  "request_id": "<per-response>"
}
```

```json
// GET /healthz
{
  "success": true,
  "status": "ok",
  "janitor": {
    "running": true,
    "elected": true,
    "ticks": 1,
    "errors": 0,
    "interval_seconds": 60,
    "seconds_since_last_tick": 0.0,
    "last_report": {
      "stories_expired": 0,
      "servers_timed_out": 0,
      "servers_archived": 0,
      "rooms_closed": 0,
      "rooms_expired": 0,
      "invites_expired": 0,
      "sessions_purged": 0,
      "presence_reconciled": 0
    }
  }
}
```

## فهرست ماژول‌ها

| ماژول | base | تعداد |
|---|---|---|
| اطلاعات برنامه و فایل‌ها (`meta`) | `/api` | 7 |
| احراز هویت (`auth`) | `/api/auth` | 14 |
| کاربران (`users`) | `/api/users` | 15 |
| پست‌ها (`posts`) | `/api/posts` | 21 |
| استوری‌ها (`stories`) | `/api/stories` | 9 |
| پیام‌ها (`messages`) | `/api/messages` | 15 |
| گروه‌ها (`groups`) | `/api/groups` | 22 |
| گیمینگ (`gaming`) | `/api/games` | 12 |
| سرورها (`servers`) | `/api/servers` | 15 |
| اتاق‌ها (`rooms`) | `/api/rooms` | 14 |
| اعلان‌ها (`notifications`) | `/api/notifications` | 9 |
| جستجو (`search`) | `/api/search` | 3 |
| گزارش‌ها (`reports`) | `/api/reports` | 9 |
| مدیریت (`admin`) | `/api/admin` | 22 |
| ریشه و فایل‌ها | — | 25 |

### ورود/خروج چه شکلی است؟

امضای هر endpoint (نام فیلدها، انواع، کدهای خطا) در خود `backend/api/<module>.py`
نزدیک‌ترین توضیح به کد است و ستون «توضیح» جدول‌های پایین از docstring همان تابع
می‌آید. برای نمونهٔ فراخوانی، `tests/` را ببینید — ۱۵۹ تست، همان‌ها که در CI اجرا
می‌شوند، تنها منبع معتبر shape هستند چون واقعاً اجرا می‌شوند.

---

## اطلاعات برنامه و فایل‌ها

base: `/api`

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| GET | `/api` | عمومی | ندارد | بله | Surface summary for humans at a browser. The legacy build served `api_terminal.html` here in DEV_MODE only and 404'd otherwise; that file never shipped in the a |
| GET | `/api/app_info` | عمومی | ندارد | بله | Public capability banner. Extended without changing any existing key. · میان‌بر قدیمی: `/api/app_info` |
| GET | `/api/config` | عمومی | ندارد | بله | Only what the SPA legitimately needs. Never secrets, never the allowlist. |
| POST | `/api/discovery/selftest` | مدیر لازم | 10 / 60 ثانیه | بله | Lets an admin verify the *policy* before trusting statuses. Never probes: it reports whether a given target would be allowed, and why not. Actual probing stays  |
| GET | `/api/health` | عمومی | ندارد | بله | Cheap readiness probe: DB round-trip + janitor + socket counts. |
| GET | `/api/routes` | مدیر لازم | ندارد | بله | Introspection for admins/docs tooling. |
| GET | `/api/whoami` | اختیاری | ندارد | بله |  |

## احراز هویت

base: `/api/auth`

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| POST | `/api/auth/change_password` | نشست لازم | 5 / 300 ثانیه | بله | میان‌بر قدیمی: `/change_password` |
| POST | `/api/auth/login` | عمومی | ندارد | بله | Credentials + a hard lockout, then a fresh expiring session. Throttling is deliberately two-layer: by IP *and* by username, so neither a spray across accounts n · میان‌بر قدیمی: `/login` |
| POST | `/api/auth/logout` | نشست لازم | 30 / 60 ثانیه | بله | Revoke just this device — other sessions are untouched. |
| POST | `/api/auth/logout-all` | نشست لازم | 5 / 120 ثانیه | بله | Panic button: every session except the caller's own. |
| GET | `/api/auth/me` | نشست لازم | ندارد | بله | میان‌بر قدیمی: `/api/me` |
| GET | `/api/auth/online` | نشست لازم | ندارد | بله |  |
| POST | `/api/auth/password/reset` | عمومی | ندارد | بله | Honest 501. A reset flow needs a delivery channel (SMTP/SMTP relay). Volexturn ships without one, so rather than pretending to send mail we say so and point at  |
| POST | `/api/auth/password/strength` | عمومی | 20 / 300 ثانیه | بله | Client-side meter helper — no state change, no password stored. |
| POST | `/api/auth/register` | عمومی | 5 / 600 ثانیه | بله | میان‌بر قدیمی: `/register` |
| GET | `/api/auth/sessions` | نشست لازم | ندارد | بله |  |
| POST | `/api/auth/sessions/<int:sid>/revoke` | نشست لازم | 30 / 60 ثانیه | بله |  |
| GET | `/api/auth/setup` | عمومی | 10 / 300 ثانیه | بله | Public: is this instance still unclaimed? Drives the SPA's banner. |
| POST | `/api/auth/setup` | عمومی | 5 / 900 ثانیه | بله | Create the first administrator. Refused the moment an admin exists. This is the whole answer to "remove insecure default administrator credentials": nothing is  |
| GET | `/api/auth/verify` | عمومی | ندارد | بله | Used by the SPA on boot to decide whether a stored token is still good. |

## کاربران

base: `/api/users`

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| GET | `/api/users` | نشست لازم | ندارد | بله | Canonical listing: paginated, searchable. `/users` stays array-shaped. |
| GET | `/api/users/<int:target>` | نشست لازم | ندارد | بله |  |
| POST | `/api/users/<int:target>/follow` | نشست لازم | 60 / 60 ثانیه | بله |  |
| GET | `/api/users/<int:target>/gaming` | نشست لازم | ندارد | بله |  |
| POST | `/api/users/block` | نشست لازم | 20 / 60 ثانیه | بله |  |
| GET | `/api/users/friends` | نشست لازم | ندارد | بله |  |
| POST | `/api/users/friends/<int:target>/remove` | نشست لازم | 20 / 60 ثانیه | بله |  |
| POST | `/api/users/friends/request` | نشست لازم | 20 / 60 ثانیه | بله |  |
| POST | `/api/users/friends/respond` | نشست لازم | 30 / 60 ثانیه | بله |  |
| POST | `/api/users/me/profile` | نشست لازم | 20 / 60 ثانیه | بله | Accepts multipart (the legacy SPA) and JSON alike. Avatar upload goes through the storage provider with content sniffing, so a renamed `.exe` can never become a · میان‌بر قدیمی: `/update_profile` |
| GET | `/api/users/online` | نشست لازم | ندارد | بله |  |
| POST | `/api/users/unblock` | نشست لازم | 20 / 60 ثانیه | بله |  |
| POST | `/follow/<int:target_id>` | نشست لازم | 60 / 60 ثانیه | بله | Exact legacy response: {success, following, followers}. · پاسخ با شکل قدیمی |
| GET | `/user_profile/<int:me_id>/<int:target>` | نشست لازم | ندارد | بله | Old shape: the bare profile dict, no envelope, `me` cross-checked. · پاسخ با شکل قدیمی |
| GET | `/users` | نشست لازم | ندارد | بله | Pre-upgrade contract: a bare array, every user, `is_online` overlaid. · پاسخ با شکل قدیمی |

## پست‌ها

base: `/api/posts`

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| GET | `/api/posts` | نشست لازم | ندارد | بله | Canonical feed page: `mode` (recent/popular/…) + limit/offset paging, with visibility and blocking pushed into the SQL, never filtered after the fact. |
| POST | `/api/posts` | نشست لازم | 20 / 300 ثانیه | بله | میان‌بر قدیمی: `/create_post` |
| GET | `/api/posts/<int:post_id>` | اختیاری | ندارد | بله | One post by id — the permalink/refresh path the SPA has no endpoint for. Visibility is enforced here too: a private post answers 404 for everyone but its author |
| DELETE | `/api/posts/<int:post_id>` | نشست لازم | 30 / 60 ثانیه | بله |  |
| GET | `/api/posts/<int:post_id>/comments` | نشست لازم | ندارد | بله |  |
| POST | `/api/posts/<int:post_id>/comments` | نشست لازم | 40 / 60 ثانیه | بله |  |
| POST | `/api/posts/<int:post_id>/like` | نشست لازم | 90 / 60 ثانیه | بله |  |
| POST | `/api/posts/<int:post_id>/pin` | نشست لازم | 10 / 300 ثانیه | بله |  |
| POST | `/api/posts/<int:post_id>/react` | نشست لازم | 90 / 60 ثانیه | بله |  |
| POST | `/api/posts/<int:post_id>/save` | نشست لازم | 60 / 60 ثانیه | بله |  |
| POST | `/api/posts/<int:post_id>/view` | نشست لازم | 240 / 60 ثانیه | بله | Idempotent per viewer (UNIQUE(post_id,user_id)), so refresh-spam cannot inflate a view count — which is what makes the trending score honest. · میان‌بر قدیمی: `/view_post/<int:post_id>` |
| GET | `/api/posts/by/<int:user_id>` | نشست لازم | ندارد | بله |  |
| DELETE | `/api/posts/comments/<int:comment_id>` | نشست لازم | 40 / 60 ثانیه | بله |  |
| GET | `/api/posts/feed` | نشست لازم | ندارد | بله |  |
| GET | `/api/posts/history` | نشست لازم | ندارد | بله |  |
| GET | `/api/posts/saved` | نشست لازم | ندارد | بله |  |
| POST | `/comment_post/<int:post_id>` | نشست لازم | 40 / 60 ثانیه | بله | Legacy shape: {success, comment, comments_count}. · پاسخ با شکل قدیمی |
| GET | `/history/<int:me_id>` | نشست لازم | ندارد | بله | پاسخ با شکل قدیمی |
| POST | `/like_post/<int:post_id>` | نشست لازم | 90 / 60 ثانیه | بله | Legacy shape: {success, liked, likes_count}. · پاسخ با شکل قدیمی |
| GET | `/post_comments/<int:post_id>` | نشست لازم | ندارد | بله | Legacy shape: bare array of comment rows. · پاسخ با شکل قدیمی |
| GET | `/posts` | نشست لازم | ندارد | بله | Pre-upgrade contract: a bare array of posts. Capped at one generous page instead of the old unbounded `SELECT *`; the SPA only ever renders the first screenful  · پاسخ با شکل قدیمی |

## استوری‌ها

base: `/api/stories`

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| GET | `/api/stories` | نشست لازم | ندارد | بله |  |
| POST | `/api/stories` | نشست لازم | 20 / 300 ثانیه | بله | میان‌بر قدیمی: `/create_story` |
| DELETE | `/api/stories/<int:story_id>` | نشست لازم | 30 / 60 ثانیه | بله |  |
| POST | `/api/stories/<int:story_id>/archive` | نشست لازم | 20 / 120 ثانیه | بله |  |
| POST | `/api/stories/<int:story_id>/view` | نشست لازم | 600 / 60 ثانیه | بله | Idempotent per viewer via UNIQUE(story_id,user_id); the count is derived, never incremented blind, so replays cannot inflate it. · میان‌بر قدیمی: `/view_story/<int:story_id>` |
| GET | `/api/stories/<int:story_id>/viewers` | نشست لازم | ندارد | بله | Owner-only list — the pre-upgrade rule, kept exactly. |
| GET | `/api/stories/me` | نشست لازم | ندارد | بله |  |
| GET | `/stories` | نشست لازم | ندارد | بله | Legacy contract: bare array, newest first, viewed_by_me included. · پاسخ با شکل قدیمی |
| GET | `/story_views/<int:story_id>` | نشست لازم | ندارد | بله | Legacy: same rows, `{success, viewers}` only (the old SPA ignores counts). · پاسخ با شکل قدیمی |

## پیام‌ها

base: `/api/messages`

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| POST | `/api/messages` | نشست لازم | 90 / 60 ثانیه | بله | Send to a user or a group. Identity is `g.user` — the legacy `sender_id` form field is accepted and *rejected* if it disagrees, which is what the old endpoint s · میان‌بر قدیمی: `/send_message` |
| PATCH,POST | `/api/messages/<int:mid>` | نشست لازم | 60 / 60 ثانیه | بله | میان‌بر قدیمی: `/edit_message` |
| DELETE | `/api/messages/<int:mid>` | نشست لازم | 60 / 60 ثانیه | بله | Delete for everyone (sender, admins, group owner) or just for me. The pre-upgrade rule is kept: sender, the receiving party, the group owner and admins may remo · میان‌بر قدیمی: `/delete_message/<int:mid>` |
| POST | `/api/messages/<int:mid>/pin` | نشست لازم | 30 / 60 ثانیه | بله | میان‌بر قدیمی: `/pin_message/<int:mid>` |
| POST | `/api/messages/<int:mid>/react` | نشست لازم | 120 / 60 ثانیه | بله |  |
| GET | `/api/messages/<int:mid>/seen-by` | نشست لازم | ندارد | بله |  |
| POST | `/api/messages/forward` | نشست لازم | 40 / 60 ثانیه | بله | میان‌بر قدیمی: `/forward_message` |
| GET | `/api/messages/partners` | نشست لازم | ندارد | بله | Conversation list with last message + unread, in one query. |
| GET | `/api/messages/pinned` | نشست لازم | ندارد | بله |  |
| GET | `/api/messages/search` | نشست لازم | ندارد | بله | Full-text-ish search confined to the requester's own conversations. Uses the denormalised `search_text` column so LIKE has something indexed to work with, and n |
| POST | `/api/messages/seen/<int:partner>` | نشست لازم | 120 / 60 ثانیه | بله | Marks *incoming* messages from `partner` as read — never another pair's. · میان‌بر قدیمی: `/seen_messages/<int:partner>` |
| GET | `/api/messages/thread/<int:partner>` | نشست لازم | ندارد | بله |  |
| GET | `/api/messages/unread` | نشست لازم | ندارد | بله | Legacy contract: a bare map of `sender_id -> count`. `me_id` from the URL is cross-checked against the token, so guessing another user's id returns 403 instead  · میان‌بر قدیمی: `/unread_counts/<int:me_id>` |
| GET | `/api/messages/unread/groups` | نشست لازم | ندارد | بله |  |
| GET | `/messages/<int:u1>/<int:u2>` | نشست لازم | ندارد | بله | Legacy contract: bare array, ascending id, both members of the pair enforced server-side. Capped to a page instead of the whole table. · پاسخ با شکل قدیمی |

## گروه‌ها

base: `/api/groups`

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| GET | `/api/groups` | نشست لازم | ندارد | بله |  |
| POST | `/api/groups` | نشست لازم | 15 / 600 ثانیه | بله | میان‌بر قدیمی: `/create_group` |
| GET | `/api/groups/<int:gid>` | نشست لازم | ندارد | بله |  |
| DELETE | `/api/groups/<int:gid>` | نشست لازم | 10 / 300 ثانیه | بله | میان‌بر قدیمی: `/group_delete/<int:gid>` |
| PATCH,POST | `/api/groups/<int:gid>` | نشست لازم | 20 / 300 ثانیه | بله |  |
| GET | `/api/groups/<int:gid>/bans` | نشست لازم | ندارد | بله |  |
| POST | `/api/groups/<int:gid>/invite` | نشست لازم | 20 / 300 ثانیه | بله |  |
| POST | `/api/groups/<int:gid>/leave` | نشست لازم | 20 / 300 ثانیه | بله |  |
| POST | `/api/groups/<int:gid>/members` | نشست لازم | 60 / 300 ثانیه | بله | میان‌بر قدیمی: `/group_add_member/<int:gid>` |
| POST | `/api/groups/<int:gid>/members/<int:target>/ban` | نشست لازم | 30 / 300 ثانیه | بله |  |
| POST | `/api/groups/<int:gid>/members/<int:target>/mute` | نشست لازم | 40 / 300 ثانیه | بله |  |
| DELETE,POST | `/api/groups/<int:gid>/members/<int:target>/remove` | نشست لازم | 60 / 300 ثانیه | بله |  |
| POST | `/api/groups/<int:gid>/members/<int:target>/role` | نشست لازم | 30 / 300 ثانیه | بله |  |
| GET | `/api/groups/<int:gid>/messages` | نشست لازم | ندارد | بله |  |
| GET | `/api/groups/<int:gid>/search` | نشست لازم | ندارد | بله |  |
| POST | `/api/groups/<int:gid>/seen` | نشست لازم | 120 / 60 ثانیه | بله |  |
| POST | `/api/groups/<int:gid>/unban/<int:target>` | نشست لازم | 30 / 300 ثانیه | بله |  |
| GET,POST | `/api/groups/join/<code>` | نشست لازم | 20 / 300 ثانیه | بله |  |
| GET | `/group_info/<int:gid>` | نشست لازم | ندارد | بله | Legacy returned the bare info dict. · پاسخ با شکل قدیمی |
| GET | `/group_messages/<int:gid>` | نشست لازم | ندارد | بله | Legacy contract: bare array ascending by id, capped at one page. · پاسخ با شکل قدیمی |
| POST | `/group_remove_member/<int:gid>` | نشست لازم | 60 / 300 ثانیه | بله | Legacy took `user_id` in the body. · پاسخ با شکل قدیمی |
| GET | `/my_groups` | نشست لازم | ندارد | بله | Legacy contract: bare array with `unread` per group. · پاسخ با شکل قدیمی |

## گیمینگ

base: `/api/games`

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| GET | `/api/games` | نشست لازم | ندارد | بله | Browse/search the catalog. `mine=1` limits it to the viewer's library. |
| POST | `/api/games` | مدیر لازم | 20 / 600 ثانیه | بله |  |
| GET | `/api/games/<int:game_id>` | نشست لازم | ندارد | بله |  |
| PATCH,POST | `/api/games/<int:game_id>` | مدیر لازم | 30 / 600 ثانیه | بله |  |
| DELETE | `/api/games/<int:game_id>` | مدیر لازم | 10 / 600 ثانیه | بله | Games with any history are archived rather than deleted. A hard delete would cascade into `lan_hosts.game_id`, `user_games` and `game_rooms`; losing a catalog e |
| GET | `/api/games/library` | نشست لازم | ندارد | بله |  |
| POST | `/api/games/library/<int:game_id>` | نشست لازم | 60 / 300 ثانیه | بله | Mark a game owned / playing / favorite / wishlist, or remove it. |
| POST | `/api/games/library/<int:game_id>/hours` | نشست لازم | 20 / 600 ثانیه | بله | Manual play-time entry. There is no trusted source of play time for arbitrary LAN games, so this is an explicit user action rather than something we invent or i |
| GET | `/api/games/meta` | نشست لازم | ندارد | بله | Everything the create-server form needs, in one call. |
| GET,POST | `/api/games/profile` | نشست لازم | 30 / 600 ثانیه | بله |  |
| GET | `/api/games/recent` | نشست لازم | ندارد | بله | Recently played, for the sidebar (spec §18). |
| GET | `/api/games/slug/<slug>` | نشست لازم | ندارد | بله |  |

## سرورها

base: `/api/servers`

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| GET | `/api/servers` | نشست لازم | ندارد | بله |  |
| POST | `/api/servers` | نشست لازم | 10 / 600 ثانیه | بله | Publish a game room/server (spec §14). · میان‌بر قدیمی: `/create_lan_host` |
| GET | `/api/servers/<int:sid>` | نشست لازم | ندارد | بله |  |
| PATCH,POST | `/api/servers/<int:sid>` | نشست لازم | 20 / 600 ثانیه | بله |  |
| DELETE | `/api/servers/<int:sid>` | نشست لازم | 10 / 600 ثانیه | بله | Owner/admin removal. A server with participants or followers is archived rather than deleted so the history stays auditable; `?purge=1` (admin) removes it outri · میان‌بر قدیمی: `/delete_lan_host/<int:host_id>` |
| POST | `/api/servers/<int:sid>/follow` | نشست لازم | 30 / 300 ثانیه | بله |  |
| POST | `/api/servers/<int:sid>/heartbeat` | نشست لازم | 120 / 60 ثانیه | بله | REST twin of the `heartbeat` socket event, for hosts that prefer HTTP. Only the owner may report. The heartbeat is what turns `unknown`/`offline` into `online`; |
| POST | `/api/servers/<int:sid>/join` | نشست لازم | 20 / 300 ثانیه | بله | Record intent to play, i.e. a join request the host can see. This is *not* a claim that the game server accepted anyone — Volexturn has no authority over the ga |
| POST | `/api/servers/<int:sid>/leave` | نشست لازم | 20 / 300 ثانیه | بله |  |
| POST | `/api/servers/<int:sid>/offline` | نشست لازم | 20 / 300 ثانیه | بله |  |
| POST | `/api/servers/<int:sid>/probe` | نشست لازم | 6 / 120 ثانیه | بله | On-demand probe of your own server. Runs the same policy-gated provider as the janitor, so pressing this cannot turn the app into a scanner; disabled discovery  |
| POST | `/api/servers/<int:sid>/unlock` | نشست لازم | 10 / 300 ثانیه | بله | Reveal connection details for a password-protected server. The supplied password is verified against the stored hash; on success the client gets ip/port/motd. A |
| GET | `/api/servers/discovery` | نشست لازم | ندارد | بله |  |
| GET | `/api/servers/stats` | نشست لازم | ندارد | بله |  |
| GET | `/lan_hosts` | نشست لازم | ندارد | بله | Pre-upgrade contract: bare array with exactly the old keys. Extra keys ride along harmlessly; the ones the old SPA reads (`id, user_id, game_name, ip_address, p · پاسخ با شکل قدیمی |

## اتاق‌ها

base: `/api/rooms`

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| GET | `/api/rooms` | نشست لازم | ندارد | بله |  |
| POST | `/api/rooms` | نشست لازم | 10 / 600 ثانیه | بله | Create Game Room → select game → (optionally) bind a server → publish. Every field the spec lists is stored; a room may legally have no server, in which case `c |
| GET | `/api/rooms/<int:rid>` | نشست لازم | ندارد | بله |  |
| POST | `/api/rooms/<int:rid>/close` | نشست لازم | 20 / 300 ثانیه | بله |  |
| POST | `/api/rooms/<int:rid>/invite` | نشست لازم | 30 / 300 ثانیه | بله | Invite friends (spec §18: “Invite to game”). |
| POST | `/api/rooms/<int:rid>/join` | نشست لازم | 30 / 300 ثانیه | بله |  |
| POST | `/api/rooms/<int:rid>/kick` | نشست لازم | 30 / 300 ثانیه | بله |  |
| POST | `/api/rooms/<int:rid>/leave` | نشست لازم | 30 / 300 ثانیه | بله |  |
| GET,POST | `/api/rooms/<int:rid>/messages` | نشست لازم | ندارد | بله | Lobby chat. Reads paginated; writes are membership-checked (see realtime.py). |
| POST | `/api/rooms/<int:rid>/ready` | نشست لازم | 60 / 60 ثانیه | بله |  |
| POST | `/api/rooms/<int:rid>/start` | نشست لازم | 20 / 300 ثانیه | بله | 'Start game' does exactly three things: flips status, stamps the time, tells members. It cannot launch a process and does not pretend to. |
| POST | `/api/rooms/<int:rid>/stop` | نشست لازم | 20 / 300 ثانیه | بله |  |
| GET,POST | `/api/rooms/invites` | نشست لازم | ندارد | بله |  |
| GET | `/api/rooms/mine` | نشست لازم | ندارد | بله |  |

## اعلان‌ها

base: `/api/notifications`

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| GET | `/api/notifications` | نشست لازم | ندارد | بله |  |
| DELETE | `/api/notifications/<int:nid>` | نشست لازم | 60 / 60 ثانیه | بله |  |
| POST | `/api/notifications/<int:nid>/read` | نشست لازم | 200 / 60 ثانیه | بله |  |
| GET,POST | `/api/notifications/preferences` | نشست لازم | ندارد | بله |  |
| POST | `/api/notifications/read` | نشست لازم | 120 / 60 ثانیه | بله | Mark all, or up to a given id (the SPA's 'seen this far' pattern). |
| POST | `/api/notifications/test` | نشست لازم | 5 / 300 ثانیه | بله | Explicit self-test for the realtime path. Deliberately notifies *yourself only*, so an admin can verify websocket delivery without spamming another account or p |
| GET | `/api/notifications/unread` | نشست لازم | ندارد | بله |  |
| GET | `/notifications/<int:me_id>` | نشست لازم | ندارد | بله | Legacy: {unread, items} for *your own* id only. · پاسخ با شکل قدیمی |
| POST | `/notifications_read/<int:me_id>` | نشست لازم | 120 / 60 ثانیه | بله | پاسخ با شکل قدیمی |

## جستجو

base: `/api/search`

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| GET,POST | `/api/search` | نشست لازم | 40 / 60 ثانیه | بله |  |
| GET | `/api/search/suggestions` | نشست لازم | 60 / 60 ثانیه | بله | Typeahead: names + games only, 8 items, no bodies. Separate from /search so a keystroke-per-request pattern does not run eight COUNT(*) queries per character. |
| GET | `/api/search/trending` | نشست لازم | ندارد | بله | Trending hashtags over the last 24h. Purely deterministic: frequency of a tag in recent posts, no engagement modelling, no external signal. |

## گزارش‌ها

base: `/api/reports`

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| POST | `/api/reports` | نشست لازم | 10 / 600 ثانیه | بله |  |
| GET | `/api/reports/<int:rid>` | مدیر لازم | ندارد | بله |  |
| POST | `/api/reports/<int:rid>/claim` | مدیر لازم | 60 / 60 ثانیه | بله |  |
| GET | `/api/reports/<int:rid>/dupes` | مدیر لازم | ندارد | بله |  |
| POST | `/api/reports/<int:rid>/handle` | مدیر لازم | 60 / 300 ثانیه | بله | One endpoint for every admin decision. `delete_content` refuses to touch user rows it does not own the semantics of (e.g. a `message` is soft-deleted, a `server |
| GET | `/api/reports/mine` | نشست لازم | ندارد | بله |  |
| GET | `/api/reports/queue` | مدیر لازم | ندارد | بله |  |
| GET | `/api/reports/reasons` | عمومی | ندارد | بله |  |
| GET | `/api/reports/target` | نشست لازم | ندارد | بله | What the "report" dialog needs to know: does this target exist, and who owns it. Returns minimal data only — no private message bodies for a target the requeste |

## مدیریت

base: `/api/admin`

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| GET | `/api/admin/audit` | مدیر لازم | ندارد | بله |  |
| DELETE,POST | `/api/admin/clear_all_messages` | — | — | — | مسیر سرویس/داخلی (`legacy.legacy_admin_clear_all_messages`) |
| GET | `/api/admin/dashboard` | مدیر لازم | ندارد | بله |  |
| DELETE,POST | `/api/admin/delete_post/<int:pid>` | — | — | — | مسیر سرویس/داخلی (`legacy.legacy_admin_legacy_delete_post`) |
| DELETE,POST | `/api/admin/delete_user/<int:uid>` | — | — | — | مسیر سرویس/داخلی (`legacy.legacy_admin_legacy_delete_user`) |
| POST | `/api/admin/demote_user/<int:uid>` | — | — | — | مسیر سرویس/داخلی (`legacy.legacy_admin_legacy_demote`) |
| GET | `/api/admin/health` | مدیر لازم | ندارد | بله |  |
| GET | `/api/admin/posts` | مدیر لازم | ندارد | بله |  |
| DELETE | `/api/admin/posts/<int:pid>` | مدیر لازم | 30 / 600 ثانیه | بله |  |
| POST | `/api/admin/promote_user/<int:uid>` | — | — | — | مسیر سرویس/داخلی (`legacy.legacy_admin_legacy_promote`) |
| GET | `/api/admin/servers` | مدیر لازم | ندارد | بله |  |
| DELETE | `/api/admin/servers/<int:sid>` | مدیر لازم | 10 / 600 ثانیه | بله |  |
| POST | `/api/admin/servers/<int:sid>/disable` | مدیر لازم | 30 / 600 ثانیه | بله |  |
| GET | `/api/admin/stats` | — | — | — | مسیر سرویس/داخلی (`legacy.legacy_admin_stats`) |
| GET | `/api/admin/users` | مدیر لازم | ندارد | بله |  |
| GET | `/api/admin/users` | مدیر لازم | ندارد | بله |  |
| GET | `/api/admin/users/<int:uid>` | مدیر لازم | ندارد | بله |  |
| DELETE | `/api/admin/users/<int:uid>` | مدیر لازم | 10 / 600 ثانیه | بله |  |
| POST | `/api/admin/users/<int:uid>/admin` | مدیر لازم | 10 / 600 ثانیه | بله |  |
| POST | `/api/admin/users/<int:uid>/ban` | مدیر لازم | 30 / 600 ثانیه | بله |  |
| POST | `/api/admin/users/<int:uid>/password` | مدیر لازم | 10 / 600 ثانیه | بله | Admin password reset. Sets `must_change_password` so the user is forced to pick their own on next login; the temporary value itself is returned once and never s |
| POST | `/api/admin/users/<int:uid>/unban` | مدیر لازم | 30 / 600 ثانیه | بله |  |

## ریشه: فایل‌ها، سلامت و میان‌برهای قدیمی

| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |
|---|---|---|---|---|---|
| GET | `/` | — | — | — | مسیر سرویس/داخلی (`index`) |
| GET | `/api/app_info` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/api/app_info` |
| GET | `/api/me` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/api/me` |
| POST | `/change_password` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/change_password` |
| POST | `/create_group` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/create_group` |
| POST | `/create_lan_host` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/create_lan_host` |
| POST | `/create_post` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/create_post` |
| POST | `/create_story` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/create_story` |
| DELETE | `/delete_lan_host/<int:host_id>` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/delete_lan_host/<int:host_id>` |
| DELETE | `/delete_message/<int:mid>` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/delete_message/<int:mid>` |
| POST | `/edit_message` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/edit_message` |
| GET | `/files/<category>/<path:filename>` | — | — | — | مسیر سرویس/داخلی (`serve_file`) |
| POST | `/forward_message` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/forward_message` |
| POST | `/group_add_member/<int:gid>` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/group_add_member/<int:gid>` |
| DELETE | `/group_delete/<int:gid>` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/group_delete/<int:gid>` |
| GET | `/healthz` | — | — | — | مسیر سرویس/داخلی (`healthz`) |
| POST | `/login` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/login` |
| POST | `/pin_message/<int:mid>` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/pin_message/<int:mid>` |
| POST | `/register` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/register` |
| POST | `/seen_messages/<int:partner>` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/seen_messages/<int:partner>` |
| POST | `/send_message` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/send_message` |
| GET | `/unread_counts/<int:me_id>` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/unread_counts/<int:me_id>` |
| POST | `/update_profile` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/update_profile` |
| POST | `/view_post/<int:post_id>` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/view_post/<int:post_id>` |
| POST | `/view_story/<int:story_id>` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `/view_story/<int:story_id>` |
