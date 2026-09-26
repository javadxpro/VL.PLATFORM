/*
 * Volexturn — موتور نسخهٔ نمایشی (GitHub Pages)
 *
 * GitHub Pages فقط فایل ایستا سرو می‌کند: نه پایتون، نه SQLite، نه آپلود، نه
 * WebSocket. این فایل همان SPA موجود (index.html) را بدون هیچ تغییری زنده نگه
 * می‌دارد، چون تمام ترافیک شبکهٔ آن از یک گلوگاه می‌گذرد: `fetch`. پس اینجا
 * `window.fetch` را با یک سرور محلی جایگزین می‌کنیم که پاسخ‌هایش را از
 * localStorage می‌خواند و می‌نویسد.
 *
 * سه نکته که باید دانست:
 *  ۱) این «احراز هویت» نیست. هر رمزی که ۸ نویسه باشد پذیرفته می‌شود و هیچ
 *     رمزی ذخیره نمی‌شود. فقط برای نمایش رابط است.
 *  ۲) داده‌ها در همین مرورگر و همین دستگاه می‌مانند. دو نفر دو دنیای جدا
 *     می‌بینند؛ چیزی بین‌شان ردوبدل نمی‌شود.
 *  ۳) شکل پاسخ‌ها از سرور واقعی گرفته شده (نمونه‌های ضبط‌شده از
 *     backend/api/*)، نه از روی حدس — تا اگر روزی این دمو با سرور واقعی
 *     جابه‌جا شد، رابط نفرینش را عوض نکند. مسیرهای پوشش‌داده‌نشده پاسخ
 *     معتبرِ خالی می‌گیرند، نه ۵۰۰.
 */

(function () {
    'use strict';

    var STORE_KEY = 'volexturn_demo_v1';
    /* باید با APP_VERSION در index.html و Config.app_version یکی باشد.
       تست: tests/test_pages_demo.py */
    var API_VERSION = '4.0.0';
    var UNAVAILABLE = 'این بخش در نسخهٔ نمایشی فعال نیست (سرور واقعی لازم دارد)';

    /* ───────── VL ID — mirror of backend/vlid.py (Crockford base32, no I L O U) */
    var ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
    function newVlId() {
        var bytes = new Uint8Array(8);
        (self.crypto || {}).getRandomValues
            ? self.crypto.getRandomValues(bytes)
            : bytes.forEach(function (_, i) { bytes[i] = Math.floor(Math.random() * 256); });
        var body = '';
        for (var i = 0; i < 8; i++) body += ALPHABET[bytes[i] % 32];
        return 'VL-' + body.slice(0, 4) + '-' + body.slice(4);
    }

    /* ───────── helpers */
    function pad(n) { return (n < 10 ? '0' : '') + n; }
    function sqlNow(minutesAgo) {
        var d = new Date(Date.now() - (minutesAgo || 0) * 60000);
        return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
               pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
    }
    function clone(x) { return JSON.parse(JSON.stringify(x)); }
    function find(list, id) {
        id = Number(id);
        for (var i = 0; i < list.length; i++) if (Number(list[i].id) === id) return list[i];
        return null;
    }
    function nextId(list) {
        var max = 0;
        for (var i = 0; i < list.length; i++) max = Math.max(max, Number(list[i].id) || 0);
        return max + 1;
    }

    /* ───────── seed: the shapes the real server returns, nothing secret */
    function seed() {
        var st = {
            session: null,
            users: [
                user(1, 'sara', 'سارا محمدی', 'سازندهٔ لیگ محله 🏆', 'admin', 'VL-S4PB-0J92', 90),
                user(2, 'kim', 'کیم رضایی', 'مدیر سرور زمین چمن', 'user', 'VL-JNB1-8BSD', 40),
                user(3, 'negar', 'نگار احمدی', 'کلیپر و گیمر', 'user', 'VL-4T9K-7RQ2', 15)
            ],
            posts: [], stories: [], comments: [], messages: [],
            groups: [], hosts: [], notifications: [],
            follows: [{ id: 1, follower_id: 2, followee_id: 1, created_at: sqlNow(300) }],
            likes: [], views: [], storyViews: [], seen: [], read: [], pinned: []
        };
        st.posts.push(
            post(1, 1, 'فوتبال خیابونی فردا ساعت ۱۸ — کی میاد؟ #فوتبال', 55),
            post(2, 2, 'کلپ ۳۰ ثانیه‌ای از مسابقهٔ هفتهٔ قبل 🎮', 25),
            post(3, 3, 'دنبال تیم جدیدم 🏆 تمرین سه‌شنبه‌ها', 5)
        );
        st.comments.push(
            { id: 1, post_id: 1, user_id: 2, content: 'من هستم 💪', timestamp: sqlNow(50),
              parent_id: null, edited_at: null, reply_count: 0,
              full_name: 'کیم رضایی', username: 'kim', avatar: '',
              parent_content: null, parent_name: null },
            { id: 2, post_id: 1, user_id: 3, content: 'ساعت رو دقیق بگو', timestamp: sqlNow(45),
              parent_id: null, edited_at: null, reply_count: 0,
              full_name: 'نگار احمدی', username: 'negar', avatar: '',
              parent_content: null, parent_name: null }
        );
        st.stories.push({
            id: 1, user_id: 2, file_path: '', file_type: 'image', timestamp: sqlNow(35),
            expires_at: sqlNow(-400), caption: 'قبل از مسابقه 🔥', privacy: 'everyone',
            view_count: 0, bg: null, full_name: 'کیم رضایی', username: 'kim', avatar: '',
            accent: null, viewers_count: 0
        });
        st.messages.push(
            msg(1, 2, 1, 'برنامه فردا قطعی؟', 48),
            msg(2, 1, 2, 'آره، ۱۸ — زمین رزرو شد', 44),
            msg(3, 3, 1, 'من هم میام، تو گروه هم گفتم 🙂', 12)
        );
        st.groups.push({
            id: 1, name: 'تیم زمین چمن', description: 'لیگ محلی، سه‌شنبه‌ها', avatar: null,
            creator_id: 1, created_at: sqlNow(600), is_private: 0, slow_mode_seconds: 0,
            only_admins_post: 0, max_members: 200, role: 'owner', member_count: 3,
            message_count: 0, last_message_id: null, unread: 0,
            member_ids: [1, 2, 3]
        });
        st.hosts.push({
            id: 1, user_id: 1, game_id: null, game_name: 'فوتبال خیابونی ایران',
            name: 'سرور لیگ محله', ip_address: '192.168.1.20', port: 7777,
            description: 'سرور اختصاصی روزهای جمعه — نسخهٔ نمایشی، قابل اتصال نیست',
            region: 'تهران', max_players: 16, player_count: 7, status: 'unknown',
            visibility: 'public', is_password_protected: 0, manual_status: null,
            tags: 'community,competitive', timestamp: sqlNow(700), last_probe_at: null,
            online: false, owner_name: 'سارا محمدی', owner_username: 'sara',
            owner_avatar: '', following: false, followers_count: 0
        });
        st.notifications.push(
            note(1, 1, 2, 'message', 1, 48, 'پیام جدید برای شما فرستاد', 'send'),
            note(2, 1, 3, 'message', 3, 12, 'پیام جدید برای شما فرستاد', 'send'),
            note(3, 1, 2, 'comment', 1, 50, 'نظر شما را پاسخ داد', 'chat'),
            note(4, 2, 1, 'follow', 1, 300, 'شما را دنبال کرد', 'person'),
            note(5, 3, 1, 'like', 1, 55, 'پست شما را پسندید', 'heart')
        );
        st.likes.push({ id: 1, post_id: 1, user_id: 2, created_at: sqlNow(49) });
        st.views.push({ id: 1, post_id: 1, user_id: 2, viewed_at: sqlNow(49) });
        return st;

        function user(id, username, full_name, bio, role, vl_id, ago) {
            return {
                id: id, username: username, full_name: full_name, bio: bio, avatar: '',
                role: role, created_at: sqlNow(ago + 5000), last_seen_at: sqlNow(ago),
                presence: ago < 5 ? 'online' : 'offline', accent: null, location: '',
                website: null, vl_id: vl_id, is_online: ago < 5,
                is_following: false, is_friend: false, blocked_by_me: false,
                blocks_me: false, status: 'active', is_banned: 0, ban_reason: null,
                banned_at: null, gender: null, online_visible: 1, updated_at: null,
                password_changed_at: null
            };
        }
        function post(id, user_id, content, ago) {
            return {
                id: id, user_id: user_id, content: content, file_path: null, file_type: null,
                timestamp: sqlNow(ago), visibility: 'public', hashtags: null, edit_count: 0,
                edited_at: null, is_pinned: 0, like_count: 0, comment_count: 0, view_count: 0,
                deleted_at: null, likes_count: 0, comments_count: 0, views: 0,
                reactions_count: 0, liked_by_me: false, saved_by_me: false,
                viewed_by_me: false, followed_by_me: false, friend_with_me: false,
                media_url: null
            };
        }
        function msg(id, sender_id, receiver_id, content, ago) {
            return {
                id: id, sender_id: sender_id, receiver_id: receiver_id, group_id: null,
                content: content, file_path: null, file_type: null, file_name: null,
                reply_to_id: null, edited_at: null, seen: ago > 20 ? 1 : 0, pinned: 0,
                forwarded: 0, timestamp: sqlNow(ago), deleted_for_everyone: 0, deleted_by: null,
                reply_count: 0, thread_root_id: null, search_text: content, msg_type: 'text',
                duration_ms: null, waveform: null, delivered_at: null, reply_to_name: null,
                pinned_by: null, pinned_at: null, edited_by: null
            };
        }
        function note(id, user_id, actor_id, type, target_id, ago, label, icon) {
            return {
                id: id, user_id: user_id, actor_id: actor_id, type: type, target_id: target_id,
                timestamp: sqlNow(ago), is_read: 0, body: '', icon: icon, target_type: type,
                room_id: null, server_id: null, priority: 'normal', read_at: null,
                delivered_at: null, data: null, actor_name: '', actor_avatar: '',
                label: label
            };
        }
    }

    /* ───────── store */
    var S = null;
    var sessionMedia = {};   /* file_path -> object URL, only for this page load */

    function load() {
        var raw = null;
        try { raw = localStorage.getItem(STORE_KEY); } catch (e) { raw = null; }
        if (raw) {
            try {
                var parsed = JSON.parse(raw);
                if (parsed && parsed.users && parsed.users.length) {
                    /* فایل‌ها در دمو بین دو بارگذاری نگه داشته نمی‌شوند (بدون سرور
                       هیچ‌جا برای ذخیرهٔ بایت‌ها نیست) — متن‌ها می‌مانند. */
                    stripMedia(parsed);
                    return parsed;
                }
            } catch (e) { /* خراب/نسخهٔ قدیمی → از نو */ }
        }
        return seed();
    }
    function stripMedia(obj) {
        ['posts', 'stories', 'messages'].forEach(function (k) {
            (obj[k] || []).forEach(function (row) {
                if (!sessionMedia[row.file_path]) { row.file_path = null; row.file_type = null; }
            });
        });
    }
    function save() {
        try { localStorage.setItem(STORE_KEY, JSON.stringify(S)); }
        catch (e) {
            /* سهمیهٔ localStorage — دمو نباید به‌خاطر این بمیرد */
            console.warn('[vl-demo] ذخیرهٔ محلی پر شد؛ تغییرات این نشست نگه داشته نمی‌شود');
        }
    }
    function me() { return S.session ? find(S.users, S.session.user_id) : null; }
    function uid() { var u = me(); return u ? u.id : null; }

    /* ───────── view builders: the exact projections the real API returns */
    function authorOf(userId) {
        var u = find(S.users, userId) || {};
        return { username: u.username || 'unknown', full_name: u.full_name || 'کاربر',
                 avatar: u.avatar || '', accent: u.accent || null };
    }
    function visibleTo(row) {
        return row.visibility !== 'private' || Number(row.user_id) === uid();
    }
    function postView(p) {
        var o = clone(p);
        var a = authorOf(o.user_id);
        o.username = a.username; o.full_name = a.full_name; o.avatar = a.avatar; o.accent = a.accent;
        o.likes_count = count(S.likes, 'post_id', o.id);
        o.comments_count = count(S.comments, 'post_id', o.id);
        o.views = count(S.views, 'post_id', o.id);
        o.like_count = o.likes_count; o.comment_count = o.comments_count; o.view_count = o.views;
        o.reactions_count = 0;
        o.liked_by_me = !!S.likes.filter(function (x) {
            return Number(x.post_id) === Number(o.id) && Number(x.user_id) === uid(); })[0];
        o.saved_by_me = false;
        o.viewed_by_me = !!S.views.filter(function (x) {
            return Number(x.post_id) === Number(o.id) && Number(x.user_id) === uid(); })[0];
        o.followed_by_me = isFollowing(o.user_id);
        o.friend_with_me = false;
        o.deleted_at = o.deleted_at || null;
        o.is_pinned = o.is_pinned ? 1 : 0;
        if (o.file_path) o.media_url = mediaHref('posts', o.file_path);
        return o;
    }
    function userView(u, forId) {
        var o = clone(u);
        delete o.password;
        var meId = forId || uid();
        o.is_online = (Date.now() - Date.parse(o.last_seen_at.replace(' ', 'T'))) < 5 * 60000;
        o.is_following = false;
        o.is_friend = false;
        o.blocked_by_me = false;
        o.blocks_me = false;
        if (meId && Number(meId) !== Number(o.id)) {
            o.is_following = !!S.follows.filter(function (f) {
                return Number(f.follower_id) === Number(meId) &&
                       Number(f.followee_id) === Number(o.id); })[0];
        }
        return o;
    }
    function profileView(target, viewerId) {
        var o = userView(target, viewerId);
        o.followers = countWhere(S.follows, function (f) { return Number(f.followee_id) === Number(o.id); });
        o.following_count = countWhere(S.follows, function (f) { return Number(f.follower_id) === Number(o.id); });
        o.posts_count = countWhere(S.posts, function (p) { return Number(p.user_id) === Number(o.id); });
        o.friends_count = 0;
        o.follows_me = !!S.follows.filter(function (f) {
            return Number(f.follower_id) === Number(viewerId) && Number(f.followee_id) === Number(o.id); })[0]
            ? !!S.follows.filter(function (f) {
                return Number(f.follower_id) === Number(o.id) && Number(f.followee_id) === Number(viewerId); })[0]
            : false;
        o.friend_state = 'none';
        o.is_self = Number(o.id) === Number(viewerId);
        o.mutual_followers = 0;
        o.email_hint = null;
        return o;
    }
    function messageView(m) {
        var o = clone(m);
        var s = authorOf(o.sender_id);
        o.sender_name = s.full_name; o.sender_username = s.username; o.sender_avatar = s.avatar;
        var r = authorOf(o.receiver_id);
        o.receiver_name = r.full_name; o.receiver_username = r.username;
        o.seen = o.seen ? 1 : 0; o.pinned = o.pinned ? 1 : 0;
        o.forwarded = o.forwarded ? 1 : 0; o.deleted_for_everyone = 0;
        if (o.file_path) o.file_url = mediaHref('chat', o.file_path);
        return o;
    }
    function noteView(n) {
        var o = clone(n);
        var a = authorOf(o.actor_id);
        o.actor_name = a.full_name; o.actor_avatar = a.avatar;
        o.is_read = wasRead(o.id) ? 1 : 0;
        return o;
    }

    function count(list, key, value) {
        return countWhere(list, function (x) { return Number(x[key]) === Number(value); });
    }
    function countWhere(list, fn) {
        var n = 0;
        for (var i = 0; i < list.length; i++) if (fn(list[i])) n++;
        return n;
    }
    function isFollowing(otherId) {
        if (!uid() || Number(otherId) === uid()) return false;
        return !!S.follows.filter(function (f) {
            return Number(f.follower_id) === uid() && Number(f.followee_id) === Number(otherId); })[0];
    }
    function wasRead(noteId) { return S.read.indexOf(noteId) >= 0; }
    function mediaHref(category, name) {
        /* آدرس موقت همین نشست؛ بعد از رفرش حذف می‌شود (بخش‌های ۲ و ۳ از docs/PAGES.md) */
        return sessionMedia[name] ? sessionMedia[name] : '/files/' + category + '/' + encodeURIComponent(name || '');
    }

    /* ───────── the router */
    var routes = [];
    function on(method, pattern, handler) {
        var names = [], re = new RegExp('^' + pattern.replace(/:(\w+)/g, function (_, n) {
            names.push(n); return '([^/]+)';
        }) + '$');
        routes.push({ method: method, re: re, names: names, handler: handler });
    }

    /* auth */
    function doLogin(req, p, body) {
        var u = S.users.filter(function (x) {
            return String(x.username).toLowerCase() === String(body.username || '').toLowerCase(); })[0];
        if (!u) return err(401, 'USER_NOT_FOUND', 'چنین کاربری وجود ندارد');
        if (String(body.password || '').length < 8)
            return err(401, 'BAD_PASSWORD', 'رمز عبور اشتباه است (در دمو حداقل ۸ نویسه کافی است)');
        S.session = { user_id: u.id, token: 'demo-' + Math.random().toString(36).slice(2, 14) };
        save();
        return ok({ success: true, user: userView(u), token: S.session.token,
                    expires_at: sqlNow(-60 * 24 * 30), version: API_VERSION,
                    must_change_password: false });
    }
    function doRegister(req, p, body) {
        var name = String(body.username || '').trim();
        if (!/^[A-Za-z0-9._-]{3,32}$/.test(name))
            return err(400, 'INVALID_USERNAME', 'نام کاربری فقط حرف انگلیسی، عدد، نقطه، خط تیره و زیرخط (۳ تا ۳۲)');
        if (S.users.some(function (u) { return u.username.toLowerCase() === name.toLowerCase(); }))
            return err(409, 'USERNAME_TAKEN', 'این نام کاربری گرفته شده است');
        if (String(body.password || '').length < 8)
            return err(400, 'WEAK_PASSWORD', 'رمز عبور باید حداقل ۸ نویسه باشد');
        var u = { id: nextId(S.users), username: name, full_name: String(body.full_name || name).slice(0, 40),
                  bio: 'کاربر Volexturn', avatar: '', role: 'user', created_at: sqlNow(0),
                  last_seen_at: sqlNow(0), presence: 'offline', accent: null, location: '',
                  website: null, vl_id: newVlId(), is_online: true, is_following: false,
                  is_friend: false, blocked_by_me: false, blocks_me: false, status: 'active',
                  is_banned: 0, ban_reason: null, banned_at: null, gender: null,
                  online_visible: 1, updated_at: null, password_changed_at: null };
        S.users.push(u);
        save();
        return ok({ success: true, message: 'ثبت‌نام انجام شد! اکنون وارد شوید.', user_id: u.id }, 201);
    }
    on('POST', '/login', doLogin);
    on('POST', '/api/auth/login', doLogin);
    on('POST', '/register', doRegister);
    on('POST', '/api/auth/register', doRegister);
    on('GET', '/api/auth/me', function () {
        var u = me();
        if (!u) return err(401, 'UNAUTHORIZED', 'نشست تمام شده است');
        return ok({ success: true, user: userView(u), is_admin: u.role === 'admin',
                    counts: { posts: countWhere(S.posts, function (x) { return Number(x.user_id) === u.id; }),
                              followers: countWhere(S.follows, function (f) { return Number(f.followee_id) === u.id; }),
                              following: countWhere(S.follows, function (f) { return Number(f.follower_id) === u.id; }),
                              friends: 0,
                              notifications_unread: unreadNotes(),
                              servers: countWhere(S.hosts, function (h) { return Number(h.user_id) === u.id; }),
                              rooms_open: 0 } });
    });
    on('POST', '/api/auth/logout', function () { S.session = null; save(); return ok({ success: true }); });
    on('POST', '/change_password', function () {
        return ok({ success: true, message: 'در نسخهٔ نمایشی تغییر رمز معنا ندارد', changed: true });
    });

    /* people */
    on('GET', '/users', function () {
        return ok(S.users.map(function (u) { return userView(u); }));
    });
    on('GET', '/api/users', function () {
        return ok({ success: true, users: S.users.map(function (u) { return userView(u); }),
                    pagination: { page: 1, per_page: S.users.length, total: S.users.length } });
    });
    on('GET', '/user_profile/:me/:id', function (req, p) {
        var target = find(S.users, p.id);
        if (!target) return err(404, 'USER_NOT_FOUND', 'کاربر پیدا نشد');
        return ok(profileView(target, p.me));
    });
    on('GET', '/api/users/:id', function (req, p) {
        var target = find(S.users, p.id);
        if (!target) return err(404, 'USER_NOT_FOUND', 'کاربر پیدا نشد');
        return ok({ success: true, user: profileView(target, uid()) });
    });
    on('POST', '/update_profile', function (req, p, body, form) {
        var u = me();
        if (!u) return err(401, 'UNAUTHORIZED', 'اول وارد شو');
        if (form) {
            ['full_name', 'bio', 'location', 'website', 'accent', 'gender'].forEach(function (k) {
                if (form.has(k)) u[k] = form.get(k) || (k === 'bio' ? '' : null);
            });
            var file = form.get('avatar');
            if (file && file.name) u.avatar = keepMedia('profiles', file);
        } else {
            ['full_name', 'bio', 'location', 'website', 'accent'].forEach(function (k) {
                if (body && body[k] !== undefined) u[k] = body[k];
            });
        }
        /* vl_id عمداً قابل تغییر نیست — همان قاعدهٔ سرور واقعی */
        save();
        return ok({ success: true, message: 'پروفایل بروزرسانی شد', user: userView(u) });
    });
    on('POST', '/follow/:id', function (req, p) {
        if (!uid()) return err(401, 'UNAUTHORIZED', 'اول وارد شو');
        var existing = null;
        S.follows = S.follows.filter(function (f) {
            var hit = Number(f.follower_id) === uid() && Number(f.followee_id) === Number(p.id);
            if (hit) existing = f;
            return !hit;
        });
        if (!existing) S.follows.push({ id: nextId(S.follows), follower_id: uid(),
                                        followee_id: Number(p.id), created_at: sqlNow(0) });
        save();
        return ok({ success: true, following: !!existing === false,
                    followers: countWhere(S.follows, function (f) { return Number(f.followee_id) === Number(p.id); }) });
    });

    /* feed */
    on('GET', '/posts', function (req, p, body, form, url) {
        var tag = url.searchParams.get('tag') || '';
        var only = url.searchParams.get('user_id');
        var rows = S.posts.filter(visibleTo).map(postView).reverse();
        if (tag) rows = rows.filter(function (r) { return String(r.content).indexOf('#' + tag) >= 0; });
        if (only) rows = rows.filter(function (r) { return Number(r.user_id) === Number(only); });
        return ok(rows);
    });
    on('GET', '/api/posts/feed', function () {
        return ok({ success: true, posts: S.posts.filter(visibleTo).map(postView).reverse(),
                    has_more: false });
    });
    on('POST', '/create_post', function (req, p, body, form) {
        if (!uid()) return err(401, 'UNAUTHORIZED', 'اول وارد شو');
        var content = form ? (form.get('content') || '') : (body.content || '');
        var row = { id: nextId(S.posts), user_id: uid(), content: String(content),
                    file_path: null, file_type: null, timestamp: sqlNow(0),
                    visibility: (body.visibility || 'public'), hashtags: null, edit_count: 0,
                    edited_at: null, is_pinned: 0, deleted_at: null };
        var file = form && form.get('file');
        if (file && file.name) {
            row.file_path = keepMedia('posts', file);
            row.file_type = /^image\//.test(file.type) ? 'image'
                          : /^video\//.test(file.type) ? 'video' : 'file';
        }
        if (!row.content && !row.file_path) return err(400, 'EMPTY_POST', 'متن یا فایل لازم است');
        var tags = row.content.match(/#[\w\u0600-\u06FF\u200c]+/g);
        row.hashtags = tags ? tags.join(',') : null;
        S.posts.push(row);
        save();
        return ok({ success: true, message: 'منتشر شد', post: postView(row), id: row.id }, 201);
    });
    on('POST', '/like_post/:id', function (req, p) {
        var hit = S.likes.filter(function (x) {
            return Number(x.post_id) === Number(p.id) && Number(x.user_id) === uid(); })[0];
        if (hit) S.likes = S.likes.filter(function (x) { return x !== hit; });
        else S.likes.push({ id: nextId(S.likes), post_id: Number(p.id), user_id: uid(), created_at: sqlNow(0) });
        save();
        return ok({ success: true, liked: !hit,
                    likes_count: count(S.likes, 'post_id', p.id) });
    });
    on('POST', '/view_post/:id', function (req, p) {
        if (!S.views.some(function (x) { return Number(x.post_id) === Number(p.id) && Number(x.user_id) === uid(); }))
            S.views.push({ id: nextId(S.views), post_id: Number(p.id), user_id: uid(), viewed_at: sqlNow(0) });
        save();
        return ok({ success: true, views: count(S.views, 'post_id', p.id) });
    });
    on('GET', '/history/:id', function (req, p) {
        return ok(S.views.filter(function (v) { return Number(v.user_id) === Number(p.id); })
                        .map(function (v) { var r = find(S.posts, v.post_id); if (r) r = postView(r), r.viewed_at = v.viewed_at; return r; })
                        .filter(Boolean).reverse());
    });
    on('GET', '/post_comments/:id', function (req, p) {
        return ok(S.comments.filter(function (cm) { return Number(cm.post_id) === Number(p.id); })
                          .map(function (cm) {
                              var o = clone(cm);
                              var a = authorOf(o.user_id);
                              o.full_name = a.full_name; o.username = a.username; o.avatar = a.avatar;
                              return o;
                          }));
    });
    on('POST', '/comment_post/:id', function (req, p, body) {
        if (!uid()) return err(401, 'UNAUTHORIZED', 'اول وارد شو');
        var text = String(body.content || '').trim();
        if (!text) return err(400, 'EMPTY_COMMENT', 'نظر خالی است');
        S.comments.push({ id: nextId(S.comments), post_id: Number(p.id), user_id: uid(),
                          content: text, timestamp: sqlNow(0), parent_id: body.parent_id || null,
                          edited_at: null, reply_count: 0, parent_content: null, parent_name: null });
        save();
        return ok({ success: true, comments_count: count(S.comments, 'post_id', p.id) }, 201);
    });

    /* stories */
    on('GET', '/stories', function () {
        return ok(S.stories.filter(function (s) {
            var exp = Date.parse(String(s.expires_at).replace(' ', 'T'));
            return !(exp && exp < Date.now());
        }).map(function (s) {
            var o = clone(s);
            var a = authorOf(o.user_id);
            o.full_name = a.full_name; o.username = a.username; o.avatar = a.avatar; o.accent = a.accent;
            o.view_count = o.viewers_count = count(S.storyViews, 'story_id', o.id);
            o.viewed_by_me = S.storyViews.some(function (v) {
                return Number(v.story_id) === Number(o.id) && Number(v.user_id) === uid(); });
            if (o.file_path) o.media_url = mediaHref('stories', o.file_path);
            return o;
        }));
    });
    on('POST', '/create_story', function (req, p, body, form) {
        if (!uid()) return err(401, 'UNAUTHORIZED', 'اول وارد شو');
        var file = form && form.get('file');
        if (!file || !file.name) return err(400, 'FILE_MISSING', 'فایلی ارسال نشده است');
        var row = { id: nextId(S.stories), user_id: uid(), file_path: keepMedia('stories', file),
                    file_type: /^video\//.test(file.type) ? 'video' : 'image',
                    timestamp: sqlNow(0),
                    expires_at: new Date(Date.now() + 24 * 3600 * 1000).toISOString().slice(0, 19).replace('T', ' '),
                    caption: form.get('caption') || '', privacy: 'everyone', view_count: 0, bg: null };
        S.stories.push(row);
        save();
        return ok({ success: true, message: 'استوری منتشر شد', story: row }, 201);
    });
    on('POST', '/view_story/:id', function (req, p) {
        if (!S.storyViews.some(function (v) { return Number(v.story_id) === Number(p.id) && Number(v.user_id) === uid(); }))
            S.storyViews.push({ id: nextId(S.storyViews), story_id: Number(p.id), user_id: uid(),
                                user_name: me() ? me().full_name : '', viewed_at: sqlNow(0) });
        save();
        return ok({ success: true });
    });
    on('GET', '/story_views/:id', function (req, p) {
        var seen = S.storyViews.filter(function (v) { return Number(v.story_id) === Number(p.id); });
        var story = find(S.stories, p.id);
        if (!story) return err(404, 'STORY_NOT_FOUND', 'استوری پیدا نشد');
        if (Number(story.user_id) !== uid()) return err(403, 'FORBIDDEN', 'فقط صاحب استوری ببیننده‌ها را می‌بیند');
        return ok({ success: true, viewers: seen.map(function (v) {
            var u = find(S.users, v.user_id) || {};
            return { id: u.id, username: u.username, full_name: u.full_name, avatar: u.avatar || '',
                     accent: null, viewed_at: v.viewed_at };
        }) });
    });

    /* direct messages */
    on('GET', '/messages/:me/:peer', function (req, p) {
        return ok(S.messages.filter(function (m) { return !m.group_id; }).filter(function (m) {
            return (Number(m.sender_id) === Number(p.me) && Number(m.receiver_id) === Number(p.peer)) ||
                   (Number(m.sender_id) === Number(p.peer) && Number(m.receiver_id) === Number(p.me));
        }).map(messageView));
    });
    on('GET', '/group_messages/:gid', function (req, p) {
        return ok(S.messages.filter(function (m) { return Number(m.group_id) === Number(p.gid); })
                          .map(messageView));
    });
    on('POST', '/send_message', function (req, p, body, form) {
        var from = uid();
        if (!from) return err(401, 'UNAUTHORIZED', 'اول وارد شو');
        var get = function (k) { return form ? form.get(k) : (body ? body[k] : null); };
        var text = String(get('content') || '').trim();
        var file = form && form.get('file');
        var row = { id: nextId(S.messages), sender_id: from,
                    receiver_id: Number(get('receiver_id') || 0) || null,
                    group_id: Number(get('group_id') || 0) || null,
                    content: text, file_path: null, file_type: null, file_name: null,
                    reply_to_id: null, edited_at: null, seen: 0, pinned: 0, forwarded: 0,
                    timestamp: sqlNow(0), deleted_for_everyone: 0, deleted_by: null,
                    reply_count: 0, thread_root_id: null, search_text: text, msg_type: 'text',
                    duration_ms: null, waveform: null, delivered_at: null, reply_to_name: null,
                    pinned_by: null, pinned_at: null, edited_by: null };
        if (file && file.name) {
            row.file_path = keepMedia('chat', file);
            row.file_name = file.name;
            row.file_type = /^video\//.test(file.type) ? 'video'
                         : /^audio\//.test(file.type) ? 'voice'
                         : /^image\//.test(file.type) ? 'image' : 'file';
            row.msg_type = row.file_type === 'voice' ? 'voice' : 'file';
            row.duration_ms = row.msg_type === 'voice' ? 3000 : null;
        }
        if (!row.content && !row.file_path) return err(400, 'EMPTY_MESSAGE', 'پیام خالی است');
        S.messages.push(row);
        save();
        return ok({ success: true, message: row, id: row.id }, 201);
    });
    on('POST', '/edit_message', function (req, p, body) {
        var m = find(S.messages, body.message_id);
        if (!m || Number(m.sender_id) !== uid()) return err(403, 'FORBIDDEN', 'فقط پیام خودت');
        m.content = String(body.content || ''); m.edited_at = sqlNow(0); m.search_text = m.content;
        save();
        return ok({ success: true });
    });
    on('DELETE', '/delete_message/:id', function (req, p) {
        S.messages = S.messages.filter(function (m) { return Number(m.id) !== Number(p.id); });
        save();
        return ok({ success: true });
    });
    on('POST', '/pin_message/:id', function (req, p) {
        var m = find(S.messages, p.id);
        if (!m) return err(404, 'MESSAGE_NOT_FOUND', 'پیام پیدا نشد');
        m.pinned = m.pinned ? 0 : 1;
        save();
        return ok({ success: true, pinned: !!m.pinned });
    });
    on('POST', '/forward_message', function (req, p, body) {
        var src = find(S.messages, body.message_id);
        if (!src) return err(404, 'MESSAGE_NOT_FOUND', 'پیام پیدا نشد');
        var targets = body.targets || [];
        targets.forEach(function (t) {
            var copy = clone(src);
            copy.id = nextId(S.messages); copy.sender_id = uid(); copy.receiver_id = Number(t);
            copy.seen = 0; copy.forwarded = 1; copy.timestamp = sqlNow(0);
            S.messages.push(copy);
        });
        save();
        return ok({ success: true, sent: targets.length });
    });
    on('POST', '/seen_messages/:peer', function (req, p) {
        S.messages.forEach(function (m) {
            if (Number(m.receiver_id) === uid() && Number(m.sender_id) === Number(p.peer)) m.seen = 1;
        });
        save();
        return ok({ success: true, seen: countWhere(S.messages, function (m) { return Number(m.seen) === 1; }) });
    });
    on('GET', '/unread_counts/:me', function (req, p) {
        var out = {};
        S.messages.forEach(function (m) {
            if (Number(m.receiver_id) === Number(p.me) && !m.seen && !m.group_id) {
                var k = String(m.sender_id);
                out[k] = (out[k] || 0) + 1;
            }
        });
        return ok(out);
    });

    /* groups */
    on('GET', '/my_groups', function () {
        return ok(S.groups.filter(function (g) {
            return !g.member_ids || g.member_ids.indexOf(uid()) >= 0 || Number(g.creator_id) === uid();
        }).map(function (g) {
            var o = clone(g);
            o.message_count = count(S.messages, 'group_id', o.id);
            o.unread = 0;
            o.member_count = (o.member_ids || []).length || o.member_count;
            return o;
        }));
    });
    on('POST', '/create_group', function (req, p, body) {
        if (!uid()) return err(401, 'UNAUTHORIZED', 'اول وارد شو');
        var name = String(body.name || '').trim();
        if (!name) return err(400, 'EMPTY_NAME', 'نام گروه لازم است');
        var members = [uid()].concat((body.members || []).map(Number)).filter(function (x, i, a) {
            return x > 0 && a.indexOf(x) === i;
        });
        var g = { id: nextId(S.groups), name: name, description: String(body.description || ''),
                  avatar: null, creator_id: uid(), created_at: sqlNow(0), is_private: 0,
                  slow_mode_seconds: 0, only_admins_post: 0, max_members: 200, role: 'owner',
                  member_count: members.length, message_count: 0, last_message_id: null,
                  unread: 0, member_ids: members };
        S.groups.push(g);
        save();
        return ok({ success: true, group_id: g.id, id: g.id, name: g.name,
                    message: 'گروه ساخته شد' }, 201);
    });
    on('GET', '/group_info/:gid', function (req, p) {
        var g = find(S.groups, p.gid);
        if (!g) return err(404, 'GROUP_NOT_FOUND', 'گروه پیدا نشد');
        var members = (g.member_ids || []).map(function (id) { return userView(find(S.users, id) || { id: id }); });
        var out = clone(g); delete out.member_ids;
        return ok({ success: true, group: out, members: members, member_count: members.length,
                    my_role: Number(g.creator_id) === uid() ? 'owner' : 'member',
                    permissions: { post: true, invite: true, manage: Number(g.creator_id) === uid() },
                    can_manage: Number(g.creator_id) === uid() });
    });
    on('POST', '/group_add_member/:gid', function (req, p, body) {
        var g = find(S.groups, p.gid);
        if (!g) return err(404, 'GROUP_NOT_FOUND', 'گروه پیدا نشد');
        var id = Number(body.user_id);
        g.member_ids = g.member_ids || [g.creator_id];
        if (g.member_ids.indexOf(id) < 0) g.member_ids.push(id);
        g.member_count = g.member_ids.length;
        save();
        return ok({ success: true, member_count: g.member_count });
    });
    on('POST', '/group_remove_member/:gid', function (req, p, body) {
        var g = find(S.groups, p.gid);
        if (!g) return err(404, 'GROUP_NOT_FOUND', 'گروه پیدا نشد');
        g.member_ids = (g.member_ids || []).filter(function (id) { return Number(id) !== Number(body.user_id); });
        g.member_count = g.member_ids.length;
        save();
        return ok({ success: true, member_count: g.member_count });
    });
    on('DELETE', '/group_delete/:gid', function (req, p) {
        S.groups = S.groups.filter(function (g) { return Number(g.id) !== Number(p.gid); });
        S.messages = S.messages.filter(function (m) { return Number(m.group_id) !== Number(p.gid); });
        save();
        return ok({ success: true, message: 'گروه حذف شد' });
    });

    /* game servers — the reason VL exists (docs/ROADMAP.md) */
    on('GET', '/lan_hosts', function () {
        return ok(S.hosts.map(function (h) {
            var o = clone(h);
            var a = authorOf(o.user_id);
            o.owner_name = a.full_name; o.owner_username = a.username; o.owner_avatar = a.avatar;
            o.online = false;
            return o;
        }));
    });
    on('POST', '/create_lan_host', function (req, p, body) {
        if (!uid()) return err(401, 'UNAUTHORIZED', 'اول وارد شو');
        var ip = String(body.ip_address || '').trim();
        if (!/^(\d{1,3}\.){3}\d{1,3}$|^[0-9a-fA-F:]+$/.test(ip))
            return err(400, 'BAD_IP', 'آدرس IP معتبر نیست (فقط IPv4/IPv6 مستقیم)');
        var h = { id: nextId(S.hosts), user_id: uid(), game_id: null,
                  game_name: String(body.game_name || body.name || 'بازی'), name: String(body.name || ''),
                  ip_address: ip, port: Number(body.port || 0) || null,
                  description: String(body.description || ''), region: String(body.region || ''),
                  max_players: Number(body.max_players || 16), player_count: 0, status: 'unknown',
                  visibility: 'public', is_password_protected: 0, manual_status: null, tags: '',
                  timestamp: sqlNow(0), last_probe_at: null, online: false,
                  owner_name: me().full_name, owner_username: me().username, owner_avatar: '',
                  following: false, followers_count: 0 };
        S.hosts.unshift(h);
        save();
        return ok({ success: true, message: 'سرور ثبت شد', id: h.id, host: h }, 201);
    });
    on('DELETE', '/delete_lan_host/:id', function (req, p) {
        S.hosts = S.hosts.filter(function (h) { return Number(h.id) !== Number(p.id); });
        save();
        return ok({ success: true, message: 'حذف شد' });
    });

    /* notifications */
    on('GET', '/notifications/:me', function (req, p) {
        var rows = S.notifications.filter(function (n) { return Number(n.user_id) === Number(p.me); })
                                  .map(noteView).reverse();
        return ok({ unread: countWhere(rows, function (n) { return !n.is_read; }), items: rows });
    });
    on('POST', '/notifications_read/:me', function (req, p) {
        var mine = S.notifications.filter(function (n) {
            return Number(n.user_id) === Number(p.me) && !wasRead(n.id);
        });
        mine.forEach(function (n) { S.read.push(n.id); });
        save();
        return ok({ success: true, marked: mine.length });
    });
    function unreadNotes() {
        return countWhere(S.notifications, function (n) {
            return Number(n.user_id) === uid() && !wasRead(n.id);
        });
    }

    /* admin (demo numbers, no secrets) */
    on('GET', '/api/admin/stats', function () {
        if (!me() || me().role !== 'admin') return err(403, 'FORBIDDEN', 'دسترسی غیرمجاز');
        var stats = {
            users: S.users.length, posts: S.posts.length, messages: S.messages.length,
            stories: S.stories.length, groups: S.groups.length, servers: S.hosts.length,
            comments: S.comments.length, reports: 0, new_today: S.users.length
        };
        return ok({ success: true, stats: stats, total_users: S.users.length, online_users: 1,
                    total_posts: S.posts.length, total_messages: S.messages.length,
                    total_servers: S.hosts.length, new_today: S.users.length, open_reports: 0,
                    recent_users: S.users.slice(-5).reverse().map(function (u) {
                        return { id: u.id, username: u.username, full_name: u.full_name,
                                 created_at: u.created_at, vl_id: u.vl_id }; }),
                    top_games: [{ name: 'فوتبال خیابونی ایران', servers: 1 }],
                    presence: { online: 1, total: S.users.length, by_presence: {} },
                    discovery: { enabled: false, probed: 0, online: 0 } });
    });
    on('GET', '/api/admin/users', function () {
        if (!me() || me().role !== 'admin') return err(403, 'FORBIDDEN', 'دسترسی غیرمجاز');
        return ok({ success: true, users: S.users.map(function (u) { return userView(u); }),
                    pagination: { page: 1, per_page: 50, total: S.users.length, pages: 1 } });
    });
    function adminAction(what) {
        return function (req, p, body) {
            if (!me() || me().role !== 'admin') return err(403, 'FORBIDDEN', 'دسترسی غیرمجاز');
            if (what === 'promote_user' || what === 'demote_user') {
                var u = find(S.users, p.id);
                if (!u) return err(404, 'USER_NOT_FOUND', 'کاربر پیدا نشد');
                u.role = what === 'promote_user' ? 'admin' : 'user';
                save();
                return ok({ success: true, message: what === 'promote_user' ? 'مدیر شد' : 'از مدیریت خارج شد' });
            }
            if (what === 'delete_user') {
                S.users = S.users.filter(function (x) { return Number(x.id) !== Number(p.id); });
                save();
                return ok({ success: true, message: 'حذف شد' });
            }
            if (what === 'delete_post') {
                S.posts = S.posts.filter(function (x) { return Number(x.id) !== Number(p.id); });
                save();
                return ok({ success: true, message: 'حذف شد' });
            }
            S.messages = []; save();
            return ok({ success: true, message: 'پیام‌ها پاک شدند' });
        };
    }
    /* هر پنج مسیر با نام کامل ثبت می‌شوند، نه با رشتهٔ ساخته‌شده:
       tests/test_pages_demo.py جدول مسیرها را از همین فایل می‌خواند */
    on('POST', '/api/admin/promote_user/:id', adminAction('promote_user'));
    on('POST', '/api/admin/demote_user/:id', adminAction('demote_user'));
    on('POST', '/api/admin/delete_user/:id', adminAction('delete_user'));
    on('POST', '/api/admin/delete_post/:id', adminAction('delete_post'));
    on('POST', '/api/admin/clear_all_messages', adminAction('clear_all_messages'));

    /* platform meta */
    on('GET', '/api/app_info', function () {
        return ok({ name: 'Volexturn', version: API_VERSION, mode: 'publish', api_version: 2,
                    capabilities: { gaming: true, game_rooms: true, discovery: false, friends: true,
                                   search: true, reports: true, voice: false, file_search: true,
                                   infinite_feed: false, websocket: false },
                    limits: { image_mb: 5, video_mb: 10, file_mb: 50, avatar_mb: 3,
                              page_size: 20, max_page_size: 100 },
                    session: { ttl_days: 30, max_per_user: 5 },
                    db_engine: 'demo-local', server_time: sqlNow(0) });
    });
    on('GET', '/api/health', function () {
        return ok({ success: true, status: 'ok', engine: 'demo-local', version: API_VERSION });
    });

    /* anything else the UI asks for: a valid, empty answer — never a 500 */
    var emptyArrayFor = ['/saved_posts', '/search', '/games', '/game_rooms', '/reports',
                         '/api/reports', '/api/games', '/api/rooms', '/threads', '/api/threads'];

    function fallback(method, path) {
        if (method === 'GET' && emptyArrayFor.some(function (p) { return path.indexOf(p) === 0; }))
            return ok([]);
        return ok({ success: false, message: UNAVAILABLE,
                    error: { code: 'DEMO_UNAVAILABLE', message: UNAVAILABLE } });
    }

    /* ───────── request plumbing */
    function ok(body, status) { return { status: status || 200, body: body }; }
    function err(status, code, message) {
        return { status: status, body: { success: false, error: { code: code, message: message },
                                         message: message } };
    }
    function keepMedia(category, file) {
        var name = 'demo_' + Date.now().toString(36) + '_' + category;
        sessionMedia[name] = URL.createObjectURL(file);
        /* فایل فقط تا همین بارگذاری صفحه زنده است؛ docs/PAGES.md توضیح می‌دهد چرا */
        return name;
    }

    function readBody(req, init) {
        var inline = init && init.body;
        if (inline == null) {
            if (!req || !req.body) return Promise.resolve({});
            var ct = (req.headers && req.headers.get && req.headers.get('Content-Type')) || '';
            if (/json/.test(ct)) return req.text().then(parseJson, function () { return {}; });
            if (/multipart|form-data/.test(ct) && req.formData) {
                return req.formData().then(toForm, function () { return {}; });
            }
            return req.text().then(function (t) { return parseJson(t); }, function () { return {}; });
        }
        if (typeof FormData !== 'undefined' && inline instanceof FormData) return Promise.resolve({ form: inline });
        if (typeof URLSearchParams !== 'undefined' && inline instanceof URLSearchParams) {
            var out = {};
            inline.forEach(function (v, k) { out[k] = v; });
            return Promise.resolve(out);
        }
        if (typeof inline === 'string') return Promise.resolve(parseJson(inline));
        if (inline && typeof inline === 'object') return Promise.resolve(inline);
        return Promise.resolve({});
    }
    function parseJson(text) { try { return text ? JSON.parse(text) : {}; } catch (e) { return {}; } }
    function toForm(f) { return { form: f }; }

    function routeFor(method, path) {
        for (var i = 0; i < routes.length; i++) {
            var r = routes[i];
            if (r.method !== method) continue;
            var m = r.re.exec(path);
            if (!m) continue;
            var params = {};
            r.names.forEach(function (n, k) { params[n] = decodeURIComponent(m[k + 1]); });
            return { route: r, params: params };
        }
        return null;
    }

    S = load();

    var realFetch = self.fetch ? self.fetch.bind(self) : null;
    self.fetch = function (input, init) {
        var req = (typeof Request !== 'undefined' && input instanceof Request) ? input : null;
        var rawUrl = req ? req.url : (typeof input === 'string' ? input : (input && input.url) || '');
        var method;
        try { method = String((init && init.method) || (req && req.method) || 'GET').toUpperCase(); }
        catch (e) { method = 'GET'; }
        var url;
        try { url = new URL(rawUrl, self.location.href); }
        catch (e) { return realFetch ? realFetch(input, init) : Promise.reject(e); }
        if (url.origin !== self.location.origin) {
            /* آدرس بیرونی: دست نزن (مثلاً imageٔ blob: یا دادهٔ تحلیلی) */
            return realFetch ? realFetch(input, init) : Promise.reject(new Error('external url'));
        }
        var path = url.pathname.replace(/\/+$/, '');
        var base = self.location.pathname.replace(/\/[^/]*$/, '');
        if (base && path.indexOf(base) === 0) path = path.slice(base.length);
        if (path.indexOf('/api') === 0 && !routeFor(method, path)) path = path.slice(4);
        var hit = routeFor(method, path || '/');
        var start = Date.now();
        var bodyP = (method === 'GET' || method === 'HEAD') ? Promise.resolve({}) : readBody(req, init);
        return bodyP.then(function (payload) {
            var form = payload && payload.form ? payload.form : null;
            var body = form ? {} : (payload || {});
            var result = hit
                ? hit.route.handler({ method: method, headers: (init || {}).headers }, hit.params, body, form, url)
                : fallback(method, path);
            /* کمی تأخیر واقع‌نما، تا حالت‌های بارگذاری رابط دیده شوند */
            var wait = Math.max(0, 90 - (Date.now() - start));
            return new Promise(function (resolve) {
                setTimeout(function () {
                    resolve(new Response(JSON.stringify(result.body), {
                        status: result.status,
                        headers: { 'Content-Type': 'application/json; charset=utf-8',
                                   'X-Volexturn-Demo': '1' }
                    }));
                }, wait);
            });
        });
    };

    /* socket.io را حذف می‌کنیم، ولی کد موجود `io()` را صدا می‌زند */
    if (typeof self.io === 'undefined') {
        self.io = function () {
            return { on: function () {}, off: function () {}, emit: function () {},
                     close: function () {}, connected: false, disconnected: true,
                     io: { connected: false } };
        };
    }

    /* نوار اعلام «دمو» — فقط در این نسخه، نه در برنامهٔ واقعی */
    function installBanner() {
        if (document.getElementById('vlDemoBanner')) return;
        var box = document.createElement('div');
        box.id = 'vlDemoBanner';
        box.setAttribute('dir', 'rtl');
        box.style.cssText = 'position:fixed;left:14px;bottom:14px;z-index:9999;display:flex;gap:8px;' +
            'align-items:center;padding:7px 11px;border-radius:999px;font:500 11.5px Vazirmatn,sans-serif;' +
            'background:rgba(17,20,31,.94);color:#f4f6fb;border:1px solid rgba(255,255,255,.16);' +
            'box-shadow:0 8px 24px rgba(0,0,0,.35);backdrop-filter:blur(8px)';
        box.innerHTML = '<span style="width:7px;height:7px;border-radius:50%;background:#f59e0b;display:inline-block"></span>' +
            '<span>نسخهٔ نمایشی — داده‌ها فقط در همین مرورگر</span>';
        var btn = document.createElement('button');
        btn.textContent = 'بازنشانی';
        btn.style.cssText = 'border:0;background:rgba(255,255,255,.1);color:#f4f6fb;border-radius:999px;' +
            'padding:3px 9px;font:500 11px Vazirmatn,sans-serif;cursor:pointer';
        btn.onclick = function () {
            localStorage.removeItem(STORE_KEY);
            self.location.reload();
        };
        box.appendChild(btn);
        (document.body || document.documentElement).appendChild(box);
    }
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', installBanner);
    } else {
        installBanner();
    }
})();
