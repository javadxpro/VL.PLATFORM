import os
import sqlite3
import datetime
import socket
import json
import sys
import time
import hashlib
import secrets
from flask import Flask, request, jsonify, send_from_directory, g
from flask_socketio import SocketIO, emit, join_room, leave_room
from werkzeug.utils import secure_filename

# ============================================================
#  Volexturn Super-App — نسخه Public v3.0.0
#  چت (تلگرامی) + شبکه اجتماعی (اینستاگرامی) + ویدیو (یوتیوبی)
# ============================================================

APP_NAME = 'Volexturn'
APP_VERSION = '3.1.0'
DEV_MODE = False  # حالت توسعه: False برای نسخه publish (ترمینال API غیرفعال)

# محدودیت‌های آپلود (مگابایت)
MAX_IMAGE_MB = 5
MAX_VIDEO_MB = 10
MAX_FILE_MB = 50

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = (MAX_FILE_MB + 50) * 1024 * 1024  # پاکت کلی
# حالت async خودکار: اجرای محلی → threading، دیپلوی با gunicorn+gevent → gevent (پشتیبانی WebSocket)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode=None)

UPLOAD_FOLDER = 'uploads'
FOLDERS = ['profiles', 'chat', 'stories', 'posts']
for f in FOLDERS:
    os.makedirs(os.path.join(UPLOAD_FOLDER, f), exist_ok=True)

DB_FILE = 'database.db'

# ---------- هدرهای امنیتی ----------
@app.after_request
def security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    resp.headers['Permissions-Policy'] = 'geolocation=(), camera=(), microphone=(self)'
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "font-src 'self'; "
        "connect-src 'self' ws: wss:; "
        "object-src 'none'; base-uri 'self'"
    )
    return resp

# ---------- محدودیت نرخ درخواست (Rate-limit) ----------
_RATE = {}
def rate_ok(key, max_n, window_s):
    now = time.time()
    arr = [t for t in _RATE.get(key, []) if now - t < window_s]
    if len(arr) >= max_n:
        return False
    arr.append(now)
    _RATE[key] = arr
    return True

# ---------- دیتابیس ----------
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        password TEXT,
        full_name TEXT,
        bio TEXT,
        avatar TEXT,
        role TEXT DEFAULT 'user',
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER,
        receiver_id INTEGER,
        group_id INTEGER,
        content TEXT,
        file_path TEXT,
        file_type TEXT,
        file_name TEXT,
        reply_to_id INTEGER,
        edited_at DATETIME,
        seen INTEGER DEFAULT 0,
        pinned INTEGER DEFAULT 0,
        forwarded INTEGER DEFAULT 0,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS stories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        file_path TEXT,
        file_type TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        expires_at DATETIME DEFAULT (datetime('now', '+24 hours'))
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        content TEXT,
        file_path TEXT,
        file_type TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS lan_hosts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        game_name TEXT,
        ip_address TEXT,
        port INTEGER,
        description TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS post_likes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER, user_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(post_id, user_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS post_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER, user_id INTEGER, content TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS follows (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        follower_id INTEGER, following_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(follower_id, following_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS story_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        story_id INTEGER, user_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(story_id, user_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS post_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER, user_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(post_id, user_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, actor_id INTEGER, type TEXT, target_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        is_read INTEGER DEFAULT 0
    )''')
    # ---- گروه‌ها ----
    c.execute('''CREATE TABLE IF NOT EXISTS groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        creator_id INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS group_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER, user_id INTEGER,
        role TEXT DEFAULT 'member',
        joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(group_id, user_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS group_seen (
        group_id INTEGER, user_id INTEGER,
        last_seen_id INTEGER DEFAULT 0,
        UNIQUE(group_id, user_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT UNIQUE,
        user_id INTEGER,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    conn.commit()

    # مهاجرت‌های سبک (دیتابیس‌های قدیمی)
    for col_def in [
        'ALTER TABLE messages ADD COLUMN reply_to_id INTEGER',
        'ALTER TABLE messages ADD COLUMN edited_at DATETIME',
        'ALTER TABLE messages ADD COLUMN seen INTEGER DEFAULT 0',
        'ALTER TABLE messages ADD COLUMN pinned INTEGER DEFAULT 0',
        'ALTER TABLE messages ADD COLUMN group_id INTEGER',
        'ALTER TABLE messages ADD COLUMN forwarded INTEGER DEFAULT 0',
    ]:
        try:
            c.execute(col_def)
        except sqlite3.OperationalError:
            pass
    conn.commit()

    # ادمین پیش‌فرض
    c.execute("SELECT * FROM users WHERE username = 'admin'")
    if not c.fetchone():
        c.execute('''INSERT INTO users (username, password, full_name, bio, role)
                     VALUES (?, ?, ?, ?, ?)''',
                  ('admin', hash_password('admin123'), 'مدیر سیستم', 'مدیر ارشد Volexturn', 'admin'))
        conn.commit()
        print("[OK] ادمین پیش‌فرض ساخته شد: admin / admin123")

    c.execute("DELETE FROM stories WHERE expires_at < datetime('now')")
    conn.commit()
    conn.close()

# ---------- امنیت: هش رمز + توکن ----------
def hash_password(pw):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 100_000).hex()
    return f"pbkdf2$100000${salt}${h}"

def verify_password(pw, stored):
    try:
        if stored and stored.startswith('pbkdf2$'):
            _, iters, salt, h = stored.split('$', 3)
            calc = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), int(iters)).hex()
            return secrets.compare_digest(calc, h)
        return stored == pw  # فرمت قدیمی (مهاجرت خودکار بعد از ورود)
    except Exception:
        return False

def current_user():
    tok = request.headers.get('Authorization', '').replace('Bearer ', '').strip()
    if not tok:
        return None
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT u.* FROM sessions s JOIN users u ON s.user_id = u.id WHERE s.token = ?''', (tok,))
    row = c.fetchone()
    conn.close()
    return row

def require_auth(fn):
    def wrapper(*a, **k):
        u = current_user()
        if not u:
            return jsonify({'success': False, 'message': 'نشست نامعتبر — دوباره وارد شو'}), 401
        g.user = u
        return fn(*a, **k)
    wrapper.__name__ = fn.__name__
    return wrapper

def require_admin(fn):
    def wrapper(*a, **k):
        u = current_user()
        if not u:
            return jsonify({'success': False, 'message': 'نشست نامعتبر — دوباره وارد شو'}), 401
        if u['role'] != 'admin':
            return jsonify({'success': False, 'message': 'دسترسی مدیریتی لازم است'}), 403
        g.user = u
        return fn(*a, **k)
    wrapper.__name__ = fn.__name__
    return wrapper

def pub_user(row):
    d = dict(row)
    d.pop('password', None)
    return d

# آنلاین‌ها
online_users = set()
socket_to_user = {}

def get_file_type(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext in ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg']: return 'image'
    if ext in ['.mp4', '.mkv', '.mov', '.webm', '.avi', '.flv', '.3gp', '.m4v']: return 'video'
    if ext in ['.mp3', '.wav', '.ogg', '.m4a', '.flac', '.weba']: return 'audio'
    return 'file'

def upload_error(file, force_type=None):
    """بررسی حجم بر اساس نوع فایل؛ None یعنی مجاز"""
    if not file or not file.filename:
        return None
    t = force_type or get_file_type(file.filename)
    limits = {'image': MAX_IMAGE_MB, 'video': MAX_VIDEO_MB, 'audio': MAX_FILE_MB, 'file': MAX_FILE_MB}
    try:
        file.seek(0, 2)
        size = file.tell()
        file.seek(0)
    except Exception:
        size = file.content_length or 0
    if size > limits.get(t, MAX_FILE_MB) * 1024 * 1024:
        return f"حجم مجاز {t}: {limits.get(t)} مگابایت — فایل شما بزرگ‌تر است"
    return None

# ============================================================
#  مسیرهای عمومی
# ============================================================

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api')
def api_terminal():
    """ترمینال API فقط در حالت توسعه"""
    if not DEV_MODE:
        return jsonify({'message': 'در نسخه publish غیرفعال است'}), 404
    return send_from_directory('.', 'api_terminal.html')

@app.route('/files/<category>/<filename>')
def serve_file(category, filename):
    if category in FOLDERS:
        return send_from_directory(os.path.join(UPLOAD_FOLDER, category), filename)
    return jsonify({'message': 'Category invalid'}), 400

# ---------- ورود / ثبت‌نام ----------
@app.route('/login', methods=['POST'])
def login():
    ip = request.remote_addr or '?'
    if not rate_ok('login:' + ip, 10, 300):
        return jsonify({'success': False, 'message': 'تلاش زیاد — ۵ دقیقه صبر کن'}), 429
    data = request.json or {}
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    if not username or not password:
        return jsonify({'success': False, 'message': 'اطلاعات پر نشده است'})

    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE username = ?', (username,))
    user = c.fetchone()
    if not user or not verify_password(password, user['password']):
        conn.close()
        return jsonify({'success': False, 'message': 'نام کاربری یا رمز عبور اشتباه است'})

    # مهاجرت رمز قدیمی به هش
    if not (user['password'] or '').startswith('pbkdf2$'):
        c.execute('UPDATE users SET password = ? WHERE id = ?', (hash_password(password), user['id']))
        conn.commit()

    # توکن نشست (حداکثر ۵ نشست فعال برای هر کاربر)
    token = secrets.token_hex(32)
    c.execute('INSERT INTO sessions (token, user_id) VALUES (?, ?)', (token, user['id']))
    c.execute('''DELETE FROM sessions WHERE user_id = ? AND id NOT IN
                 (SELECT id FROM sessions WHERE user_id = ? ORDER BY id DESC LIMIT 5)''', (user['id'], user['id']))
    conn.commit()
    conn.close()

    u = pub_user(user)
    u['is_online'] = u['id'] in online_users
    return jsonify({'success': True, 'user': u, 'token': token, 'version': APP_VERSION})

@app.route('/register', methods=['POST'])
def register():
    ip = request.remote_addr or '?'
    if not rate_ok('reg:' + ip, 10, 300):
        return jsonify({'success': False, 'message': 'تلاش زیاد — ۵ دقیقه صبر کن'}), 429
    data = request.json or {}
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    if not username or not password:
        return jsonify({'success': False, 'message': 'اطلاعات پر نشده است'})
    if len(username) < 3:
        return jsonify({'success': False, 'message': 'نام کاربری حداقل ۳ کاراکتر باشد'})
    if len(password) < 4:
        return jsonify({'success': False, 'message': 'رمز عبور حداقل ۴ کاراکتر باشد'})
    if len(username) > 32 or len(password) > 128:
        return jsonify({'success': False, 'message': 'نام کاربری/رمز بیش از حد طولانی است'})
    if not all(ch.isalnum() or ch in '._-' for ch in username):
        return jsonify({'success': False, 'message': 'نام کاربری فقط حرف، عدد و . _ - مجاز است'})

    conn = get_db()
    c = conn.cursor()
    try:
        c.execute('INSERT INTO users (username, password, full_name, bio, avatar, role) VALUES (?, ?, ?, ?, ?, ?)',
                  (username, hash_password(password), username, 'کاربر جدید Volexturn', '', 'user'))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'ثبت‌نام انجام شد! اکنون وارد شوید.'})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'success': False, 'message': 'این نام کاربری قبلاً استفاده شده است'})

# ============================================================
#  کاربران و چت خصوصی (نیازمند ورود)
# ============================================================

@app.route('/users', methods=['GET'])
@require_auth
def get_users():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id, username, full_name, bio, avatar, role, created_at FROM users')
    users = [dict(row) for row in c.fetchall()]
    conn.close()
    for u in users:
        u['is_online'] = u['id'] in online_users
    return jsonify(users)

@app.route('/messages/<int:u1>/<int:u2>', methods=['GET'])
@require_auth
def get_messages(u1, u2):
    me = g.user['id']
    if me not in (u1, u2):
        return jsonify({'success': False, 'message': 'دسترسی غیرمجاز'}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT messages.*,
                        u.full_name AS sender_name,
                        r.content AS reply_content,
                        r.file_name AS reply_file_name,
                        r.file_type AS reply_file_type,
                        ru.full_name AS reply_sender_name
                 FROM messages
                 JOIN users u ON messages.sender_id = u.id
                 LEFT JOIN messages r ON messages.reply_to_id = r.id
                 LEFT JOIN users ru ON r.sender_id = ru.id
                 WHERE messages.group_id IS NULL
                   AND ((messages.sender_id = ? AND messages.receiver_id = ?)
                     OR (messages.sender_id = ? AND messages.receiver_id = ?))
                 ORDER BY messages.id ASC''', (u1, u2, u2, u1))
    msgs = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(msgs)

@app.route('/send_message', methods=['POST'])
@require_auth
def send_message():
    me = g.user['id']
    sender_id = request.form.get('sender_id') or me
    try:
        sender_id = int(sender_id)
    except Exception:
        sender_id = me
    if sender_id != me:
        return jsonify({'success': False, 'message': 'هویت ارسالکننده معتبر نیست'}), 403
    content = request.form.get('content', '')
    reply_to_id = request.form.get('reply_to_id') or None
    group_id = request.form.get('group_id') or None

    conn = get_db()
    c = conn.cursor()
    receiver_id = None
    if group_id:
        c.execute('SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?', (group_id, me))
        if not c.fetchone():
            conn.close()
            return jsonify({'success': False, 'message': 'عضو این گروه نیستی'}), 403
    else:
        receiver_id = request.form.get('receiver_id')

    file_path, file_type, file_name = None, None, None
    if 'file' in request.files:
        file = request.files['file']
        err = upload_error(file)
        if err:
            conn.close()
            return jsonify({'success': False, 'message': err}), 400
        if file and file.filename:
            file_name = secure_filename(file.filename)
            timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S%f_')
            file_path = timestamp + file_name
            file.save(os.path.join(UPLOAD_FOLDER, 'chat', file_path))
            file_type = get_file_type(file_name)

    c.execute('''INSERT INTO messages (sender_id, receiver_id, group_id, content, file_path, file_type, file_name, reply_to_id)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
              (me, receiver_id, group_id, content, file_path, file_type, file_name, reply_to_id))
    msg_id = c.lastrowid
    conn.commit()

    c.execute('''SELECT messages.*, r.content AS reply_content, r.file_name AS reply_file_name,
                        r.file_type AS reply_file_type, ru.full_name AS reply_sender_name
                 FROM messages
                 LEFT JOIN messages r ON messages.reply_to_id = r.id
                 LEFT JOIN users ru ON r.sender_id = ru.id
                 WHERE messages.id = ?''', (msg_id,))
    msg = dict(c.fetchone())
    conn.close()
    socketio.emit('new_message', msg)
    return jsonify({'success': True, 'message': msg})

@app.route('/edit_message', methods=['POST'])
@require_auth
def edit_message():
    data = request.json or {}
    mid = data.get('message_id')
    content = (data.get('content') or '').strip()
    if not mid or not content:
        return jsonify({'success': False, 'message': 'متن پیام خالی است'})
    if len(content) > 4000:
        return jsonify({'success': False, 'message': 'متن بیش از حد طولانی است'})
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM messages WHERE id = ? AND sender_id = ?', (mid, g.user['id']))
    if not c.fetchone():
        conn.close()
        return jsonify({'success': False, 'message': 'فقط فرستنده می‌تواند پیام را ویرایش کند'})
    c.execute('UPDATE messages SET content = ?, edited_at = CURRENT_TIMESTAMP WHERE id = ?', (content, mid))
    conn.commit()
    c.execute('''SELECT messages.*, r.content AS reply_content, ru.full_name AS reply_sender_name
                 FROM messages
                 LEFT JOIN messages r ON messages.reply_to_id = r.id
                 LEFT JOIN users ru ON r.sender_id = ru.id
                 WHERE messages.id = ?''', (mid,))
    msg = dict(c.fetchone())
    conn.close()
    socketio.emit('message_updated', msg)
    return jsonify({'success': True, 'message': msg})

@app.route('/delete_message/<int:mid>', methods=['DELETE'])
@require_auth
def delete_message(mid):
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM messages WHERE id = ?', (mid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'message': 'پیام پیدا نشد'})
    allowed = (row['sender_id'] == me)
    if row['receiver_id'] == me:
        allowed = True
    if row['group_id']:
        c.execute('SELECT role FROM group_members WHERE group_id = ? AND user_id = ?', (row['group_id'], me))
        gm = c.fetchone()
        if gm and gm['role'] == 'owner':
            allowed = True
    if g.user['role'] == 'admin':
        allowed = True
    if not allowed:
        conn.close()
        return jsonify({'success': False, 'message': 'مجاز به حذف این پیام نیستید'}), 403
    c.execute('DELETE FROM messages WHERE id = ?', (mid,))
    conn.commit()
    conn.close()
    socketio.emit('message_deleted', {'id': mid})
    return jsonify({'success': True})

@app.route('/seen_messages/<int:partner>', methods=['POST'])
@require_auth
def seen_messages(partner):
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE messages SET seen = 1 WHERE sender_id = ? AND receiver_id = ? AND seen = 0', (partner, me))
    n = c.rowcount
    conn.commit()
    conn.close()
    if n:
        socketio.emit('messages_seen', {'by': me, 'partner': partner})
    return jsonify({'success': True, 'seen': n})

@app.route('/unread_counts/<int:me>', methods=['GET'])
@require_auth
def unread_counts(me):
    if me != g.user['id']:
        return jsonify({'success': False, 'message': 'دسترسی غیرمجاز'}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT sender_id, COUNT(*) AS n FROM messages WHERE receiver_id = ? AND seen = 0 AND group_id IS NULL GROUP BY sender_id', (me,))
    res = {str(r['sender_id']): r['n'] for r in c.fetchall()}
    conn.close()
    return jsonify(res)

@app.route('/pin_message/<int:mid>', methods=['POST'])
@require_auth
def pin_message(mid):
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM messages WHERE id = ?', (mid,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'message': 'پیام پیدا نشد'})
    # عضویت: طرفین گفتگو یا عضو گروه
    ok = me in (row['sender_id'], row['receiver_id'])
    if row['group_id']:
        c.execute('SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?', (row['group_id'], me))
        ok = bool(c.fetchone())
    if not ok:
        conn.close()
        return jsonify({'success': False, 'message': 'مجاز نیستید'}), 403

    new_state = 0 if row['pinned'] else 1
    if row['group_id']:
        c.execute('UPDATE messages SET pinned = 0 WHERE group_id = ?', (row['group_id'],))
    else:
        pair = (row['sender_id'], row['receiver_id'])
        c.execute('''UPDATE messages SET pinned = 0 WHERE group_id IS NULL AND
                     ((sender_id = ? AND receiver_id = ?) OR (sender_id = ? AND receiver_id = ?))''',
                  (pair[0], pair[1], pair[1], pair[0]))
    if new_state:
        c.execute('UPDATE messages SET pinned = 1 WHERE id = ?', (mid,))
    conn.commit()
    conn.close()
    socketio.emit('messages_pin', {'id': mid})
    return jsonify({'success': True, 'pinned': bool(new_state)})

@app.route('/forward_message', methods=['POST'])
@require_auth
def forward_message():
    """هدایت پیام به یک یا چند مخاطب/گروه"""
    me = g.user['id']
    data = request.json or {}
    mid = data.get('message_id')
    targets = data.get('targets') or []
    if not mid or not targets:
        return jsonify({'success': False, 'message': 'مقصدی انتخاب نشده'})
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM messages WHERE id = ?', (mid,))
    orig = c.fetchone()
    if not orig:
        conn.close()
        return jsonify({'success': False, 'message': 'پیام پیدا نشد'})
    # دسترسی به پیام اصلی
    ok = me in (orig['sender_id'], orig['receiver_id'])
    if orig['group_id']:
        c.execute('SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?', (orig['group_id'], me))
        ok = bool(c.fetchone())
    if not ok:
        conn.close()
        return jsonify({'success': False, 'message': 'به این پیام دسترسی نداری'}), 403

    sent = 0
    for t in targets[:10]:
        ttype = t.get('type')
        tid = t.get('id')
        if ttype == 'user' and tid:
            c.execute('''INSERT INTO messages (sender_id, receiver_id, content, file_path, file_type, file_name, forwarded)
                         VALUES (?, ?, ?, ?, ?, ?, 1)''',
                      (me, tid, orig['content'], orig['file_path'], orig['file_type'], orig['file_name']))
            sent += 1
        elif ttype == 'group' and tid:
            c.execute('SELECT 1 FROM group_members WHERE group_id = ? AND user_id = ?', (tid, me))
            if c.fetchone():
                c.execute('''INSERT INTO messages (sender_id, group_id, content, file_path, file_type, file_name, forwarded)
                             VALUES (?, ?, ?, ?, ?, ?, 1)''',
                          (me, tid, orig['content'], orig['file_path'], orig['file_type'], orig['file_name']))
                sent += 1
        if sent > 0:
            new_id = c.lastrowid
            c.execute('''SELECT messages.*, r.content AS reply_content, ru.full_name AS reply_sender_name
                         FROM messages
                         LEFT JOIN messages r ON messages.reply_to_id = r.id
                         LEFT JOIN users ru ON r.sender_id = ru.id
                         WHERE messages.id = ?''', (new_id,))
            socketio.emit('new_message', dict(c.fetchone()))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'sent': sent})

# ============================================================
#  گروه‌ها
# ============================================================

def is_group_member(gid, uid, conn):
    c = conn.cursor()
    c.execute('SELECT role FROM group_members WHERE group_id = ? AND user_id = ?', (gid, uid))
    return c.fetchone()

@app.route('/create_group', methods=['POST'])
@require_auth
def create_group():
    me = g.user['id']
    data = request.json or {}
    name = (data.get('name') or '').strip()
    members = data.get('members') or []
    if not name:
        return jsonify({'success': False, 'message': 'نام گروه لازم است'})
    if len(name) > 64:
        return jsonify({'success': False, 'message': 'نام گروه بیش از حد طولانی است'})
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO groups (name, creator_id) VALUES (?, ?)', (name, me))
    gid = c.lastrowid
    c.execute('INSERT INTO group_members (group_id, user_id, role) VALUES (?, ?, ?)', (gid, me, 'owner'))
    for m in members[:100]:
        try:
            mid = int(m)
        except Exception:
            continue
        if mid == me:
            continue
        c.execute('INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES (?, ?, ?)', (gid, mid, 'member'))
    conn.commit()
    conn.close()
    socketio.emit('groups_changed', {})
    return jsonify({'success': True, 'group_id': gid, 'message': f'گروه «{name}» ساخته شد'})

@app.route('/my_groups', methods=['GET'])
@require_auth
def my_groups():
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT g.id, g.name, g.creator_id, g.created_at,
                        (SELECT COUNT(*) FROM group_members gm2 WHERE gm2.group_id = g.id) AS members_count,
                        (SELECT content FROM messages m WHERE m.group_id = g.id ORDER BY m.id DESC LIMIT 1) AS last_content,
                        (SELECT file_name FROM messages m WHERE m.group_id = g.id ORDER BY m.id DESC LIMIT 1) AS last_file,
                        (SELECT timestamp FROM messages m WHERE m.group_id = g.id ORDER BY m.id DESC LIMIT 1) AS last_time,
                        (SELECT COALESCE(MAX(id),0) FROM messages m WHERE m.group_id = g.id) AS last_msg_id,
                        COALESCE((SELECT last_seen_id FROM group_seen gs WHERE gs.group_id = g.id AND gs.user_id = ?), 0) AS last_seen
                 FROM groups g
                 JOIN group_members gm ON gm.group_id = g.id AND gm.user_id = ?
                 ORDER BY last_msg_id DESC''', (me, me))
    groups = []
    for r in c.fetchall():
        d = dict(r)
        d['unread'] = 0
        if d['last_msg_id'] and d['last_msg_id'] > d['last_seen']:
            c.execute('SELECT COUNT(*) FROM messages WHERE group_id = ? AND id > ? AND sender_id != ?',
                      (d['id'], d['last_seen'], me))
            d['unread'] = c.fetchone()[0]
        groups.append(d)
    conn.close()
    return jsonify(groups)

@app.route('/group_info/<int:gid>', methods=['GET'])
@require_auth
def group_info(gid):
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    if not is_group_member(gid, me, conn):
        conn.close()
        return jsonify({'success': False, 'message': 'عضو این گروه نیستی'}), 403
    c.execute('SELECT id, name, creator_id, created_at FROM groups WHERE id = ?', (gid,))
    grp = dict(c.fetchone())
    c.execute('''SELECT u.id, u.username, u.full_name, u.avatar, gm.role
                 FROM group_members gm JOIN users u ON gm.user_id = u.id
                 WHERE gm.group_id = ? ORDER BY gm.role = 'owner' DESC, u.full_name''', (gid,))
    members = [dict(r) for r in c.fetchall()]
    for m in members:
        m['is_online'] = m['id'] in online_users
    conn.close()
    return jsonify({'success': True, 'group': grp, 'members': members})

@app.route('/group_add_member/<int:gid>', methods=['POST'])
@require_auth
def group_add_member(gid):
    me = g.user['id']
    uid = (request.json or {}).get('user_id')
    conn = get_db()
    c = conn.cursor()
    gm = is_group_member(gid, me, conn)
    if not gm:
        conn.close()
        return jsonify({'success': False, 'message': 'عضو این گروه نیستی'}), 403
    c.execute('SELECT creator_id FROM groups WHERE id = ?', (gid,))
    creator = c.fetchone()['creator_id']
    if gm['role'] != 'owner' and creator != me and g.user['role'] != 'admin':
        conn.close()
        return jsonify({'success': False, 'message': 'فقط مدیر گروه می‌تواند عضو اضافه کند'}), 403
    c.execute('INSERT OR IGNORE INTO group_members (group_id, user_id, role) VALUES (?, ?, ?)', (gid, uid, 'member'))
    conn.commit()
    conn.close()
    socketio.emit('groups_changed', {})
    return jsonify({'success': True, 'message': 'عضو اضافه شد'})

@app.route('/group_remove_member/<int:gid>', methods=['POST'])
@require_auth
def group_remove_member(gid):
    me = g.user['id']
    uid = (request.json or {}).get('user_id')
    conn = get_db()
    c = conn.cursor()
    gm = is_group_member(gid, me, conn)
    if not gm:
        conn.close()
        return jsonify({'success': False, 'message': 'عضو این گروه نیستی'}), 403
    c.execute('SELECT creator_id FROM groups WHERE id = ?', (gid,))
    creator = c.fetchone()['creator_id']
    # خروج خودی یا حذف توسط مالک/ادمین سایت
    if uid != me and gm['role'] != 'owner' and creator != me and g.user['role'] != 'admin':
        conn.close()
        return jsonify({'success': False, 'message': 'دسترسی نداری'}), 403
    c.execute('DELETE FROM group_members WHERE group_id = ? AND user_id = ? AND role != "owner"', (gid, uid))
    conn.commit()
    conn.close()
    socketio.emit('groups_changed', {})
    return jsonify({'success': True, 'message': 'انجام شد'})

@app.route('/group_delete/<int:gid>', methods=['DELETE'])
@require_auth
def group_delete(gid):
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT creator_id FROM groups WHERE id = ?', (gid,))
    grp = c.fetchone()
    if not grp:
        conn.close()
        return jsonify({'success': False, 'message': 'گروه پیدا نشد'})
    if grp['creator_id'] != me and g.user['role'] != 'admin':
        conn.close()
        return jsonify({'success': False, 'message': 'فقط سازنده گروه یا ادمین سایت'}), 403
    for tbl in ['group_members', 'group_seen', 'messages']:
        c.execute(f'DELETE FROM {tbl} WHERE group_id = ?', (gid,))
    c.execute('DELETE FROM groups WHERE id = ?', (gid,))
    conn.commit()
    conn.close()
    socketio.emit('groups_changed', {})
    return jsonify({'success': True, 'message': 'گروه حذف شد'})

@app.route('/group_messages/<int:gid>', methods=['GET'])
@require_auth
def group_messages(gid):
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    if not is_group_member(gid, me, conn):
        conn.close()
        return jsonify({'success': False, 'message': 'عضو این گروه نیستی'}), 403
    c.execute('''SELECT messages.*, u.full_name AS sender_name, u.avatar AS sender_avatar,
                        r.content AS reply_content, r.file_name AS reply_file_name,
                        r.file_type AS reply_file_type, ru.full_name AS reply_sender_name
                 FROM messages
                 JOIN users u ON messages.sender_id = u.id
                 LEFT JOIN messages r ON messages.reply_to_id = r.id
                 LEFT JOIN users ru ON r.sender_id = ru.id
                 WHERE messages.group_id = ?
                 ORDER BY messages.id ASC''', (gid,))
    msgs = [dict(row) for row in c.fetchall()]
    max_id = msgs[-1]['id'] if msgs else 0
    c.execute('''INSERT INTO group_seen (group_id, user_id, last_seen_id) VALUES (?, ?, ?)
                 ON CONFLICT(group_id, user_id) DO UPDATE SET last_seen_id = excluded.last_seen_id''', (gid, me, max_id))
    conn.commit()
    conn.close()
    return jsonify(msgs)

# ============================================================
#  استوری / پست (نیازمند ورود)
# ============================================================

@app.route('/stories', methods=['GET'])
@require_auth
def get_stories():
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT stories.*, users.full_name, users.avatar
                 FROM stories
                 JOIN users ON stories.user_id = users.id
                 WHERE stories.expires_at > datetime('now')
                 ORDER BY stories.id DESC''')
    stories = [dict(row) for row in c.fetchall()]
    for s in stories:
        c.execute('SELECT 1 FROM story_views WHERE story_id = ? AND user_id = ?', (s['id'], me))
        s['viewed_by_me'] = bool(c.fetchone())
    conn.close()
    return jsonify(stories)

@app.route('/create_story', methods=['POST'])
@require_auth
def create_story():
    me = g.user['id']
    file = request.files.get('file')
    err = upload_error(file)
    if err:
        return jsonify({'success': False, 'message': err}), 400
    if file and file.filename:
        file_name = secure_filename(file.filename)
        timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S%f_')
        file_path = timestamp + file_name
        file.save(os.path.join(UPLOAD_FOLDER, 'stories', file_path))
        file_type = get_file_type(file_name)
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO stories (user_id, file_path, file_type, expires_at)
                     VALUES (?, ?, ?, datetime('now', '+24 hours'))''', (me, file_path, file_type))
        conn.commit()
        conn.close()
        return jsonify({'success': True, 'message': 'استوری با موفقیت قرار گرفت'})
    return jsonify({'success': False, 'message': 'فایلی ارسال نشده است'})

@app.route('/posts', methods=['GET'])
@require_auth
def get_posts():
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT posts.*, users.full_name, users.avatar, users.username,
                        (SELECT COUNT(*) FROM post_likes pl WHERE pl.post_id = posts.id) AS likes_count,
                        (SELECT COUNT(*) FROM post_comments pc WHERE pc.post_id = posts.id) AS comments_count,
                        (SELECT COUNT(*) FROM post_views pv WHERE pv.post_id = posts.id) AS views
                 FROM posts JOIN users ON posts.user_id = users.id ORDER BY posts.id DESC''')
    posts = [dict(row) for row in c.fetchall()]
    for p in posts:
        c.execute('SELECT 1 FROM post_likes WHERE post_id = ? AND user_id = ?', (p['id'], me))
        p['liked_by_me'] = bool(c.fetchone())
        c.execute('SELECT 1 FROM follows WHERE follower_id = ? AND following_id = ?', (me, p['user_id']))
        p['followed_by_me'] = bool(c.fetchone())
    conn.close()
    return jsonify(posts)

@app.route('/create_post', methods=['POST'])
@require_auth
def create_post():
    me = g.user['id']
    content = request.form.get('content', '')[:4000]
    file_path, file_type = None, None
    if 'file' in request.files:
        file = request.files['file']
        err = upload_error(file)
        if err:
            return jsonify({'success': False, 'message': err}), 400
        if file and file.filename:
            file_name = secure_filename(file.filename)
            timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S%f_')
            file_path = timestamp + file_name
            file.save(os.path.join(UPLOAD_FOLDER, 'posts', file_path))
            file_type = get_file_type(file_name)
    if not content and not file_path:
        return jsonify({'success': False, 'message': 'متن یا فایل لازم است'})
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO posts (user_id, content, file_path, file_type) VALUES (?, ?, ?, ?)', (me, content, file_path, file_type))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'پست با موفقیت منتشر شد'})

# ============================================================
#  شبکه اجتماعی
# ============================================================

def add_notification(user_id, actor_id, ntype, target_id):
    if not user_id or user_id == actor_id:
        return
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO notifications (user_id, actor_id, type, target_id) VALUES (?, ?, ?, ?)',
              (user_id, actor_id, ntype, target_id))
    nid = c.lastrowid
    c.execute('SELECT u.full_name FROM users u WHERE u.id = ?', (actor_id,))
    actor = c.fetchone()
    conn.commit()
    conn.close()
    socketio.emit('notify', {
        'id': nid, 'user_id': user_id, 'actor_id': actor_id,
        'actor_name': actor['full_name'] if actor else '?', 'type': ntype, 'target_id': target_id
    })

@app.route('/like_post/<int:post_id>', methods=['POST'])
@require_auth
def like_post(post_id):
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT 1 FROM post_likes WHERE post_id = ? AND user_id = ?', (post_id, me))
    if c.fetchone():
        c.execute('DELETE FROM post_likes WHERE post_id = ? AND user_id = ?', (post_id, me))
        liked = False
    else:
        c.execute('INSERT INTO post_likes (post_id, user_id) VALUES (?, ?)', (post_id, me))
        liked = True
    c.execute('SELECT COUNT(*) FROM post_likes WHERE post_id = ?', (post_id,))
    n = c.fetchone()[0]
    c.execute('SELECT user_id FROM posts WHERE id = ?', (post_id,))
    owner = c.fetchone()
    conn.commit()
    conn.close()
    if liked and owner:
        add_notification(owner['user_id'], me, 'like', post_id)
    socketio.emit('post_like', {'post_id': post_id, 'likes_count': n})
    return jsonify({'success': True, 'liked': liked, 'likes_count': n})

@app.route('/comment_post/<int:post_id>', methods=['POST'])
@require_auth
def comment_post(post_id):
    me = g.user['id']
    data = request.json or {}
    content = (data.get('content') or '').strip()[:1000]
    if not content:
        return jsonify({'success': False, 'message': 'کامنت خالی است'})
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO post_comments (post_id, user_id, content) VALUES (?, ?, ?)', (post_id, me, content))
    cid = c.lastrowid
    c.execute('''SELECT pc.*, u.full_name, u.avatar, u.username FROM post_comments pc
                 JOIN users u ON pc.user_id = u.id WHERE pc.id = ?''', (cid,))
    comment = dict(c.fetchone())
    c.execute('SELECT COUNT(*) FROM post_comments WHERE post_id = ?', (post_id,))
    n = c.fetchone()[0]
    c.execute('SELECT user_id FROM posts WHERE id = ?', (post_id,))
    owner = c.fetchone()
    conn.commit()
    conn.close()
    if owner:
        add_notification(owner['user_id'], me, 'comment', post_id)
    socketio.emit('post_comment', {'post_id': post_id, 'comments_count': n})
    return jsonify({'success': True, 'comment': comment, 'comments_count': n})

@app.route('/post_comments/<int:post_id>', methods=['GET'])
@require_auth
def post_comments(post_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT pc.*, u.full_name, u.avatar, u.username FROM post_comments pc
                 JOIN users u ON pc.user_id = u.id WHERE pc.post_id = ? ORDER BY pc.id ASC''', (post_id,))
    comments = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(comments)

@app.route('/follow/<int:target_id>', methods=['POST'])
@require_auth
def follow(target_id):
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT 1 FROM follows WHERE follower_id = ? AND following_id = ?', (me, target_id))
    if c.fetchone():
        c.execute('DELETE FROM follows WHERE follower_id = ? AND following_id = ?', (me, target_id))
        following = False
    else:
        c.execute('INSERT INTO follows (follower_id, following_id) VALUES (?, ?)', (me, target_id))
        following = True
    c.execute('SELECT COUNT(*) FROM follows WHERE following_id = ?', (target_id,))
    followers = c.fetchone()[0]
    conn.commit()
    conn.close()
    if following:
        add_notification(target_id, me, 'follow', me)
    socketio.emit('follow_update', {'target_id': target_id, 'followers': followers})
    return jsonify({'success': True, 'following': following, 'followers': followers})

@app.route('/user_profile/<int:me>/<int:target>', methods=['GET'])
@require_auth
def user_profile(me, target):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id, username, full_name, bio, avatar, role, created_at FROM users WHERE id = ?', (target,))
    u = c.fetchone()
    if not u:
        conn.close()
        return jsonify({'success': False}), 404
    prof = dict(u)
    c.execute('SELECT COUNT(*) FROM follows WHERE following_id = ?', (target,))
    prof['followers'] = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM follows WHERE follower_id = ?', (target,))
    prof['following_count'] = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM posts WHERE user_id = ?', (target,))
    prof['posts_count'] = c.fetchone()[0]
    c.execute('SELECT 1 FROM follows WHERE follower_id = ? AND following_id = ?', (me, target))
    prof['is_following'] = bool(c.fetchone())
    conn.close()
    return jsonify(prof)

@app.route('/view_post/<int:post_id>', methods=['POST'])
@require_auth
def view_post(post_id):
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO post_views (post_id, user_id) VALUES (?, ?)', (post_id, me))
    is_new = c.rowcount > 0
    c.execute('SELECT COUNT(*) FROM post_views WHERE post_id = ?', (post_id,))
    n = c.fetchone()[0]
    conn.commit()
    conn.close()
    if is_new:
        socketio.emit('post_view', {'post_id': post_id, 'views': n})
    return jsonify({'success': True, 'views': n})

@app.route('/history/<int:me>', methods=['GET'])
@require_auth
def watch_history(me):
    if me != g.user['id']:
        return jsonify({'success': False, 'message': 'دسترسی غیرمجاز'}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT posts.*, users.full_name, users.avatar,
                        (SELECT COUNT(*) FROM post_likes pl WHERE pl.post_id = posts.id) AS likes_count,
                        (SELECT COUNT(*) FROM post_comments pc WHERE pc.post_id = posts.id) AS comments_count,
                        (SELECT COUNT(*) FROM post_views pv2 WHERE pv2.post_id = posts.id) AS views
                 FROM post_views pv
                 JOIN posts ON pv.post_id = posts.id
                 JOIN users ON posts.user_id = users.id
                 WHERE pv.user_id = ?
                 ORDER BY pv.timestamp DESC''', (me,))
    posts = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(posts)

@app.route('/view_story/<int:story_id>', methods=['POST'])
@require_auth
def view_story(story_id):
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO story_views (story_id, user_id) VALUES (?, ?)', (story_id, me))
    c.execute('SELECT COUNT(*) FROM story_views WHERE story_id = ?', (story_id,))
    n = c.fetchone()[0]
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'viewers': n})

@app.route('/story_views/<int:story_id>', methods=['GET'])
@require_auth
def story_views(story_id):
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT user_id FROM stories WHERE id = ?', (story_id,))
    row = c.fetchone()
    if not row or row['user_id'] != me:
        conn.close()
        return jsonify({'success': False, 'message': 'فقط صاحب استوری می‌تواند بیننده‌ها را ببیند'}), 403
    c.execute('''SELECT u.id, u.full_name, u.username, u.avatar FROM story_views sv
                 JOIN users u ON sv.user_id = u.id WHERE sv.story_id = ? ORDER BY sv.timestamp DESC''', (story_id,))
    viewers = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify({'success': True, 'viewers': viewers})

@app.route('/notifications/<int:me>', methods=['GET'])
@require_auth
def get_notifications(me):
    if me != g.user['id']:
        return jsonify({'success': False, 'message': 'دسترسی غیرمجاز'}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT n.*, u.full_name AS actor_name, u.avatar AS actor_avatar
                 FROM notifications n JOIN users u ON n.actor_id = u.id
                 WHERE n.user_id = ? ORDER BY n.id DESC LIMIT 40''', (me,))
    items = [dict(r) for r in c.fetchall()]
    c.execute('SELECT COUNT(*) FROM notifications WHERE user_id = ? AND is_read = 0', (me,))
    unread = c.fetchone()[0]
    conn.close()
    return jsonify({'unread': unread, 'items': items})

@app.route('/notifications_read/<int:me>', methods=['POST'])
@require_auth
def read_notifications(me):
    if me != g.user['id']:
        return jsonify({'success': False, 'message': 'دسترسی غیرمجاز'}), 403
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE notifications SET is_read = 1 WHERE user_id = ?', (me,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ============================================================
#  بازی LAN و پروفایل
# ============================================================

@app.route('/lan_hosts', methods=['GET'])
@require_auth
def get_lan_hosts():
    conn = get_db()
    c = conn.cursor()
    c.execute('''SELECT lan_hosts.*, users.full_name FROM lan_hosts JOIN users ON lan_hosts.user_id = users.id ORDER BY lan_hosts.id DESC''')
    hosts = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(hosts)

@app.route('/create_lan_host', methods=['POST'])
@require_auth
def create_lan_host():
    me = g.user['id']
    data = request.json or {}
    conn = get_db()
    c = conn.cursor()
    c.execute('''INSERT INTO lan_hosts (user_id, game_name, ip_address, port, description) VALUES (?, ?, ?, ?, ?)''',
              (me, (data.get('game_name') or '')[:64], (data.get('ip_address') or '')[:45], (data.get('port') or '')[:6], (data.get('description') or '')[:200]))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/delete_lan_host/<int:host_id>', methods=['DELETE'])
@require_auth
def delete_lan_host(host_id):
    me = g.user['id']
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT user_id FROM lan_hosts WHERE id = ?', (host_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'success': False, 'message': 'پیدا نشد'})
    if row['user_id'] != me and g.user['role'] != 'admin':
        conn.close()
        return jsonify({'success': False, 'message': 'فقط سازنده یا ادمین'}), 403
    c.execute('DELETE FROM lan_hosts WHERE id = ?', (host_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@app.route('/update_profile', methods=['POST'])
@require_auth
def update_profile():
    me = g.user['id']
    full_name = (request.form.get('full_name') or '')[:64]
    bio = (request.form.get('bio') or '')[:200]
    conn = get_db()
    c = conn.cursor()
    if 'avatar' in request.files:
        file = request.files['avatar']
        err = upload_error(file, force_type='image')
        if err:
            conn.close()
            return jsonify({'success': False, 'message': err}), 400
        if file and file.filename:
            file_name = secure_filename(file.filename)
            timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S%f_')
            avatar_path = timestamp + file_name
            file.save(os.path.join(UPLOAD_FOLDER, 'profiles', avatar_path))
            c.execute('UPDATE users SET full_name = ?, bio = ?, avatar = ? WHERE id = ?', (full_name, bio, avatar_path, me))
        else:
            c.execute('UPDATE users SET full_name = ?, bio = ? WHERE id = ?', (full_name, bio, me))
    else:
        c.execute('UPDATE users SET full_name = ?, bio = ? WHERE id = ?', (full_name, bio, me))
    conn.commit()
    c.execute('SELECT * FROM users WHERE id = ?', (me,))
    updated_user = pub_user(c.fetchone())
    conn.close()
    return jsonify({'success': True, 'user': updated_user})

@app.route('/change_password', methods=['POST'])
@require_auth
def change_password():
    """تغییر رمز عبور + باطل‌کردن نشست‌های دیگر (مهم برای اجرای آنلاین)"""
    me = g.user
    current_token = request.headers.get('Authorization', '').replace('Bearer ', '').strip()
    data = request.json or {}
    old = (data.get('old_password') or '').strip()
    new = (data.get('new_password') or '').strip()
    if not old or not new:
        return jsonify({'success': False, 'message': 'هر دو رمز لازم است'})
    if not verify_password(old, me['password']):
        return jsonify({'success': False, 'message': 'رمز فعلی اشتباه است'})
    if len(new) < 4 or len(new) > 128:
        return jsonify({'success': False, 'message': 'رمز جدید باید حداقل ۴ کاراکتر باشد'})
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET password = ? WHERE id = ?', (hash_password(new), me['id']))
    # همه نشست‌های دیگر بی‌اعتبار می‌شوند
    c.execute('DELETE FROM sessions WHERE user_id = ? AND token != ?', (me['id'], current_token))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'رمز عوض شد و نشست‌های دیگر بی‌اعتبار شدند 🔒'})

# ============================================================
#  API های ادمین (محافظت‌شده با توکن)
# ============================================================

@app.route('/api/admin/stats', methods=['GET'])
@require_admin
def admin_stats():
    conn = get_db()
    c = conn.cursor()
    counts = {}
    for label, q in [('total_users', 'SELECT COUNT(*) FROM users'),
                     ('total_messages', 'SELECT COUNT(*) FROM messages'),
                     ('total_posts', 'SELECT COUNT(*) FROM posts'),
                     ('active_lan_hosts', 'SELECT COUNT(*) FROM lan_hosts'),
                     ('total_groups', 'SELECT COUNT(*) FROM groups'),
                     ('total_likes', 'SELECT COUNT(*) FROM post_likes'),
                     ('total_comments', 'SELECT COUNT(*) FROM post_comments'),
                     ('total_follows', 'SELECT COUNT(*) FROM follows')]:
        c.execute(q)
        counts[label] = c.fetchone()[0]
    c.execute('SELECT COUNT(*) FROM stories WHERE expires_at > datetime("now")')
    counts['active_stories'] = c.fetchone()[0]
    conn.close()
    counts.update({
        'status': 'فعال و آنلاین ⚡',
        'active_sockets': len(online_users),
        'max_file_size_mb': MAX_FILE_MB,
        'image_limit_mb': MAX_IMAGE_MB,
        'video_limit_mb': MAX_VIDEO_MB,
        'version': APP_VERSION,
        'mode': 'publish' if not DEV_MODE else 'dev',
        'server_time': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'python_version': sys.version.split()[0]
    })
    return jsonify(counts)

@app.route('/api/admin/users', methods=['GET'])
@require_admin
def admin_get_users():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id, username, full_name, bio, avatar, role, created_at FROM users ORDER BY id DESC')
    users = [dict(row) for row in c.fetchall()]
    conn.close()
    return jsonify(users)

@app.route('/api/admin/delete_user/<int:user_id>', methods=['DELETE'])
@require_admin
def admin_delete_user(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT id FROM users WHERE username = "admin"')
    admin_row = c.fetchone()
    if admin_row and user_id == admin_row['id']:
        conn.close()
        return jsonify({'success': False, 'message': 'ادمین اصلی قابل حذف نیست'}), 400
    # پاکسازی کامل داده‌های کاربر
    c.execute('SELECT id FROM posts WHERE user_id = ?', (user_id,))
    pids = [r['id'] for r in c.fetchall()]
    for pid in pids:
        for tbl in ['post_likes', 'post_comments', 'post_views']:
            c.execute(f'DELETE FROM {tbl} WHERE post_id = ?', (pid,))
    c.execute('DELETE FROM posts WHERE user_id = ?', (user_id,))
    c.execute('DELETE FROM stories WHERE user_id = ?', (user_id,))
    c.execute('DELETE FROM lan_hosts WHERE user_id = ?', (user_id,))
    c.execute('DELETE FROM messages WHERE sender_id = ?', (user_id,))
    c.execute('DELETE FROM group_members WHERE user_id = ?', (user_id,))
    c.execute('DELETE FROM group_seen WHERE user_id = ?', (user_id,))
    c.execute('DELETE FROM follows WHERE follower_id = ? OR following_id = ?', (user_id, user_id))
    c.execute('DELETE FROM notifications WHERE user_id = ? OR actor_id = ?', (user_id, user_id))
    c.execute('DELETE FROM sessions WHERE user_id = ?', (user_id,))
    # گروه‌هایی که ساخته: حذف کامل گروه
    c.execute('SELECT id FROM groups WHERE creator_id = ?', (user_id,))
    gids = [r['id'] for r in c.fetchall()]
    for gid in gids:
        c.execute('DELETE FROM messages WHERE group_id = ?', (gid,))
        c.execute('DELETE FROM group_members WHERE group_id = ?', (gid,))
        c.execute('DELETE FROM group_seen WHERE group_id = ?', (gid,))
        c.execute('DELETE FROM groups WHERE id = ?', (gid,))
    c.execute('DELETE FROM users WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()
    socketio.emit('groups_changed', {})
    return jsonify({'success': True, 'message': 'کاربر و همه داده‌هایش حذف شد'})

@app.route('/api/admin/promote_user/<int:user_id>', methods=['POST'])
@require_admin
def admin_promote_user(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET role = "admin" WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'کاربر به ادمین ارتقا یافت'})

@app.route('/api/admin/demote_user/<int:user_id>', methods=['POST'])
@require_admin
def admin_demote_user(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT username FROM users WHERE id = ?', (user_id,))
    u = c.fetchone()
    if u and u['username'] == 'admin':
        conn.close()
        return jsonify({'success': False, 'message': 'ادمین اصلی قابل تنزل نیست'}), 400
    c.execute('UPDATE users SET role = "user" WHERE id = ?', (user_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'ادمین به کاربر عادی تبدیل شد'})

@app.route('/api/admin/delete_post/<int:post_id>', methods=['DELETE'])
@require_admin
def admin_delete_post(post_id):
    conn = get_db()
    c = conn.cursor()
    for tbl in ['post_likes', 'post_comments', 'post_views']:
        c.execute(f'DELETE FROM {tbl} WHERE post_id = ?', (post_id,))
    c.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'پست با موفقیت حذف شد'})

@app.route('/api/admin/clear_all_messages', methods=['DELETE'])
@require_admin
def admin_clear_all_messages():
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM messages')
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': 'همه پیام‌ها پاک شدند'})

@app.route('/api/app_info', methods=['GET'])
def app_info():
    return jsonify({'name': APP_NAME, 'version': APP_VERSION, 'mode': 'publish' if not DEV_MODE else 'dev'})

# ============================================================
#  Socket.IO
# ============================================================

@socketio.on('connect')
def handle_connect():
    pass

@socketio.on('join')
def handle_join(data):
    user_id = data.get('user_id')
    if user_id:
        online_users.add(int(user_id))
        socket_to_user[request.sid] = int(user_id)
        emit('user_status', {}, broadcast=True)

@socketio.on('disconnect')
def handle_disconnect():
    user_id = socket_to_user.pop(request.sid, None)
    if user_id and user_id in online_users:
        online_users.remove(user_id)
        emit('user_status', {}, broadcast=True)

@socketio.on('typing')
def handle_typing(data):
    emit('typing', data, broadcast=True, include_self=False)

@socketio.on('join_voice')
def handle_join_voice(data):
    join_room(data.get('room', 'global_voice'))

@socketio.on('leave_voice')
def handle_leave_voice(data):
    leave_room(data.get('room', 'global_voice'))

@socketio.on('voice_signal')
def handle_voice_signal(data):
    emit('voice_signal', data, to=data.get('room', 'global_voice'), include_self=False)

# ============================================================
#  اجرا
# ============================================================

init_db()

if __name__ == '__main__':
    def get_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return '127.0.0.1'

    ip = get_ip()
    port = int(os.environ.get('PORT', 5000))  # روی هاست‌های ابری پورت از ENV خوانده می‌شود
    print("=" * 60)
    print(f"🚀 {APP_NAME} Super-App v{APP_VERSION} (Publish)")
    print(f"🏠 Local:   http://localhost:{port}")
    print(f"🌐 Network: http://{ip}:{port}")
    print(f"👑 Admin:   admin / admin123  (بعد از ورود رمز را عوض کن)")
    print(f"🔒 امنیت: هش PBKDF2 + توکن نشست + محافظت API ادمین")
    print(f"📦 محدودیت: عکس {MAX_IMAGE_MB}MB | فیلم {MAX_VIDEO_MB}MB | فایل {MAX_FILE_MB}MB")
    print("=" * 60)

    socketio.run(app, host='0.0.0.0', port=port, debug=False, use_reloader=False, allow_unsafe_werkzeug=True)
