# Volexturn — Pre-Upgrade Audit

Baseline commit: `9cdceb3` (extraction of `Volexturn_Local_Network.zip`).

This document records what the codebase **already does**, so that the upgrade can preserve it.
Nothing here is aspirational — every line was read from the source.

## 1. Application shape

| Aspect | Finding |
|---|---|
| Backend | `server.py`, 1454 lines, single module, Flask + Flask-SocketIO |
| Frontend | `index.html`, 2702 lines: inline CSS (8–457), markup (458–1023), inline JS (1024–2700) |
| WSGI callable | `server:app` — referenced by `Procfile`, `Dockerfile`, `render.yaml`, `run.sh` |
| Entry point | `python3 server.py` → `socketio.run(app, host=0.0.0.0, port=$PORT\|5000)` |
| Runtime deps | flask, flask-socketio, werkzeug, simple-websocket, gunicorn, gevent |
| Database | SQLite `database.db`, created at import time by `init_db()` |
| Uploads | `uploads/{profiles,chat,stories,posts}/`, created at import |
| Tests | **none** |
| Frontend framework | none — vanilla JS, global functions, `document.getElementById` |

## 2. Existing routes (49) — the compatibility contract

The SPA calls these at the **root path** (not under `/api`). Only admin/meta live under `/api`.

```
GET    /                                index.html
GET    /api                             dev terminal (DEV_MODE only; api_terminal.html absent)
GET    /files/<category>/<filename>      uploaded media
POST   /login  /register                auth
GET    /users                            all users
GET    /messages/<u1>/<u2>               private thread
POST   /send_message  /edit_message      send / edit
DELETE /delete_message/<mid>
POST   /seen_messages/<partner>          mark thread read
GET    /unread_counts/<me>
POST   /pin_message/<mid>
POST   /forward_message
POST   /create_group   GET /my_groups   GET /group_info/<gid>
POST   /group_add_member/<gid>  /group_remove_member/<gid>
DELETE /group_delete/<gid>               GET /group_messages/<gid>
GET    /stories        POST /create_story
GET    /posts          POST /create_post
POST   /like_post/<id>  /comment_post/<id>  GET /post_comments/<id>
POST   /follow/<target_id>
GET    /user_profile/<me>/<target>
POST   /view_post/<id>  /view_story/<id>  GET /story_views/<id>
GET    /history/<me>
GET    /notifications/<me>               POST /notifications_read/<me>
GET    /lan_hosts       POST /create_lan_host   DELETE /delete_lan_host/<id>
POST   /update_profile  /change_password
GET    /api/app_info
GET    /api/admin/stats  /api/admin/users
DELETE /api/admin/delete_user/<id>  /api/admin/delete_post/<id>  /api/admin/clear_all_messages
POST   /api/admin/promote_user/<id>  /api/admin/demote_user/<id>
```

## 3. Socket.IO events (7) — current behaviour

| Event | Auth | Notes |
|---|---|---|
| `connect` | none | no-op |
| `join` | **none** | trusts `data['user_id']` from client for presence |
| `disconnect` | — | pops `socket_to_user` |
| `typing` | **none** | `broadcast=True` — leaks to every client |
| `join_voice` / `leave_voice` | **none** | joins arbitrary room name |
| `voice_signal` | **none** | relays WebRTC SDP/ICE to any room → signaling hijack |

Server→client broadcasts: `new_message`, `message_updated`, `message_deleted`,
`messages_seen`, `messages_pin`, `user_status`, `groups_changed`, `notify`,
`post_like`, `post_comment`, `post_view`, `follow_update`, `typing`.

## 4. Database schema (16 tables)

`users`, `sessions`, `messages`, `stories`, `posts`, `lan_hosts`, `post_likes`,
`post_comments`, `post_views`, `follows`, `story_views`, `notifications`,
`groups`, `group_members`, `group_seen`.

Legacy "light migrations" are ad-hoc `ALTER TABLE ... ADD COLUMN` wrapped in
`try/except OperationalError` (messages: `reply_to_id`, `edited_at`, `seen`,
`pinned`, `group_id`, `forwarded`). No index exists on **any** column.

`lan_hosts(user_id, game_name, ip_address, port, description, timestamp)` — no
status, no heartbeat, no player limit, no password, no game FK.

## 5. Auth / session logic

- `hash_password` → `pbkdf2$<iters>$<salt>$<sha256hex>`, 100 000 iters.
- `verify_password` supports the legacy plaintext format and triggers
  rehash-on-login. **Must be preserved** for existing DBs.
- Token: `secrets.token_hex(32)` in `sessions(token, user_id, created_at)`.
  `Authorization: Bearer <token>`. Max 5 live sessions per user.
- `require_auth` sets `g.user`; `require_admin` additionally needs `role='admin'`.
- `change_password` revokes other sessions but never expires anything.

## 6. What already works well (keep)

- Server-side ownership checks in `delete_message`, `pin_message`,
  `group_*`, `unread_counts`, `notifications`, `history`, `story_views`.
- `send_message` already rejects a spoofed `sender_id`.
- Output escaping helper `esc()` used throughout the SPA; `linkifyHashtags`
  and `avatarHTML` route user data through it.
- Security headers incl. a real CSP, `nosniff`, `referrer-policy`, `base-uri`.
- `secure_filename()` + per-type size limits; `UPLOAD_FOLDER` category allowlist.
- Persian/RTL dark+light glass UI with local Vazirmatn fonts (no CDN).

## 7. Defects the upgrade must fix

| # | Severity | Issue | Location |
|---|---|---|---|
| S1 | **High** | `socketio.emit('new_message', msg)` broadcasts every **private** message to all clients | `send_message`, `forward_message` |
| S2 | **High** | `notify` broadcast leaks other users' notifications to everyone | `add_notification` |
| S3 | **High** | Default admin `admin/admin123` auto-created at startup | `init_db` L211 |
| S4 | **High** | Unauthenticated WebRTC signaling relay; room name is client-supplied | `voice_signal` |
| S5 | **Med** | Presence spoofing — `join` trusts `user_id` | `handle_join` |
| S6 | **Med** | Sessions never expire (no `expires_at`) | `sessions` |
| S7 | **Med** | File type is extension-only; no MIME/magic sniffing. `.svg` allowed → stored XSS | `get_file_type` |
| S8 | **Med** | `create_lan_host` accepts arbitrary `ip_address`/`port` strings → SSRF once probing is added | `create_lan_host` |
| S9 | **Low** | `_RATE` dict grows unbounded (memory leak); limiter in-process only | `rate_ok` |
| S10 | **Low** | Password minimum is 4 characters | `register` |
| S11 | **Low** | No `Content-Security-Policy` nonce; `'unsafe-inline'` for scripts | CSP |

### Performance defects

| # | Issue |
|---|---|
| P1 | `/posts`, `/users`, `/messages/<u1>/<u2>`, `/stories`, `/history` are **unbounded** `SELECT *` — no pagination, no limits |
| P2 | Per-row correlated subqueries + N+1 loop for `liked_by_me`/`followed_by_me` on every post |
| P3 | No indexes at all |
| P4 | `admin_stats` issues 9 sequential `COUNT(*)` per request |
| P5 | `init_db()` runs at import time (import side effects, breaks gunicorn worker safety) |

## 8. Backward-compatibility constraints for the upgrade

1. `server:app` must remain importable (deployment files depend on it).
2. Every root-level route above must keep working — the SPA and any third-party
   clients use them. New `/api/*` paths are additive.
3. Existing `database.db` files must migrate in place, non-destructively.
4. `pbkdf2$` hashes must keep verifying; plaintext legacy hashes must still work.
5. Existing socket event names must keep their meaning (client is not rewritten atomically).
6. `uploads/` layout (`/files/<category>/<filename>`) must not change.
7. The visual identity (dark/light, RTL, glass, Vazirmatn, `--grad`) is preserved.
