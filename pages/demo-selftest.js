/*
 * Self-test for the demo backend — run with:  node pages/demo-selftest.js
 *
 * The demo replaces `fetch`, so it can be driven end to end in Node with a three
 * line DOM stub: no browser, no test framework, nothing to install. pytest runs this
 * file too (tests/test_pages_demo.py) and skips it when Node is not on the machine.
 *
 * What it pins: the response *shapes* the UI reads (bare arrays for lists,
 * `success` + fields for mutations), that state survives a reload, that seeded
 * accounts carry a well-formed VL ID, and that an uncovered path answers with a
 * valid body instead of a 500 or a hang.
 */

'use strict';

const path = require('path');

/* ───────── the minimum browser surface vl-demo.js touches ───────── */
const store = new Map();
globalThis.self = globalThis;
globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
};
globalThis.location = {
    pathname: '/VL.PLATFORM/index.html',
    href: 'https://example.invalid/VL.PLATFORM/index.html',
    origin: 'https://example.invalid',
};
const el = () => ({ style: {}, setAttribute() {}, appendChild() {}, addEventListener() {}, onclick: null });
globalThis.document = {
    readyState: 'complete',
    getElementById: () => null,
    querySelectorAll: () => [],
    createElement: el,
    documentElement: el(),
    addEventListener() {},
    body: el(),
};
if (!URL.createObjectURL) URL.createObjectURL = () => 'blob:demo';

/* ───────── assertions ───────── */
let checks = 0, failed = 0;
function is(label, cond, extra) {
    checks++;
    if (cond) { console.log('  ok   ' + label); return; }
    failed++;
    console.log('  FAIL ' + label + (extra === undefined ? '' : '  → ' + JSON.stringify(extra)));
}
function eq(label, got, want) { is(label + ' = ' + JSON.stringify(want), got === want, { got, want }); }

async function call(method, url, opts) {
    const r = await fetch(url, Object.assign({ method }, opts || {}));
    let body = null;
    try { body = await r.json(); } catch (e) { body = { _unparsable: true }; }
    return { status: r.status, body };
}
function json(method, url, payload) {
    return call(method, url, { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
}
function form(method, url, fields) {
    const fd = new FormData();
    Object.keys(fields).forEach((k) => fd.append(k, fields[k]));
    return call(method, url, { body: fd });
}

(async function main() {
    require(path.join(__dirname, 'vl-demo.js'));
    const VL_ID = /^VL-[0-9A-Z]{4}-[0-9A-Z]{4}$/;

    console.log('\nauth');
    let r = await json('POST', '/login', { username: 'sara', password: 'Passw0rd!x' });
    is('login returns a token', typeof r.body.token === 'string' && r.body.token.length > 6, r.body);
    is('login user carries a VL ID', VL_ID.test(r.body.user.vl_id), r.body.user.vl_id);
    eq('login echoes the api version', r.body.version, '4.0.0');
    r = await json('POST', '/login', { username: 'sara', password: 'short' });
    eq('a too-short password is refused', r.status, 401);

    r = await json('POST', '/register', { username: 'omid', password: 'Passw0rd!x', full_name: 'امید' });
    eq('register works', r.status, 201);
    const newId = r.body.user_id;
    r = await json('POST', '/register', { username: 'omid', password: 'Passw0rd!x' });
    eq('duplicate username refused', r.status, 409);
    r = await json('POST', '/register', { username: 'a!b', password: 'Passw0rd!x' });
    is('bad username refused', r.body.error && r.body.error.code === 'INVALID_USERNAME', r.body);

    console.log('\npeople');
    r = await call('GET', '/users');
    is('/users is a bare array (legacy contract)', Array.isArray(r.body) && r.body.length === 4,
       Array.isArray(r.body) ? r.body.length : r.body);
    is('every user has a VL ID', r.body.every((u) => VL_ID.test(u.vl_id || '')), r.body.map((u) => u.vl_id));
    is('seeded ids are unique', new Set(r.body.map((u) => u.vl_id)).size === r.body.length);
    is('no password in a projection', r.body.every((u) => !('password' in u)));
    r = await call('GET', '/api/users');
    is('canonical /api/users wraps in an envelope', Array.isArray(r.body.users) && r.body.users.length === 4, r.body);
    r = await call('GET', '/user_profile/1/2');
    is('profile carries counts', typeof r.body.posts_count === 'number' && 'vl_id' in r.body, r.body);

    console.log('\nfeed');
    r = await call('GET', '/posts');
    is('feed is newest first', Array.isArray(r.body) && r.body[0].id === 3, r.body.map((p) => p.id));
    is('feed rows carry the denormalised fields the UI reads',
       r.body.every((p) => ['username', 'full_name', 'avatar', 'likes_count', 'comments_count',
                            'views', 'liked_by_me', 'followed_by_me'].every((k) => k in p)),
       Object.keys(r.body[0]));
    const before = r.body.length;
    r = await json('POST', '/create_post', { content: 'تست دمو #دمو', visibility: 'public' });
    eq('create_post is created', r.status, 201);
    const postId = r.body.id;
    r = await call('GET', '/posts');
    eq('the new post shows in the feed', r.body.length, before + 1);
    is('hashtags parsed from content', r.body[0].hashtags === '#دمو', r.body[0].hashtags);
    r = await call('GET', '/posts?tag=دمو');
    is('tag filter finds it', r.body.length === 1, r.body.map((p) => p.content));
    r = await json('POST', '/like_post/' + postId, { user_id: 1 });
    is('like returns liked:true + count', r.body.liked === true && r.body.likes_count === 1, r.body);
    r = await json('POST', '/comment_post/' + postId, { user_id: 2, content: 'دمو 💪' });
    eq('comment count reported', r.body.comments_count, 1);
    r = await call('GET', '/post_comments/' + postId);
    is('comments carry author fields', r.body.length === 1 && !!r.body[0].username, r.body);
    is('identity comes from the session, not the body (as on the real server)',
       r.body[0].username === 'sara' && r.body[0].user_id === 1, r.body[0]);
    r = await json('POST', '/follow/1', { user_id: 2 });
    eq('follow toggles', r.body.following, true);

    console.log('\nstories');
    r = await call('GET', '/stories');
    is('stories is a bare array with viewer counts',
       Array.isArray(r.body) && 'viewers_count' in r.body[0], r.body);
    r = await json('POST', '/view_story/1', { user_id: 1 });
    eq('viewing a story succeeds', r.body.success, true);
    r = await form('POST', '/create_story', {});
    eq('a story without a file is refused', r.status, 400);
    is('refusal uses the server code', r.body.error && r.body.error.code === 'FILE_MISSING', r.body);

    console.log('\nmessages');
    r = await call('GET', '/messages/1/2');
    is('history with a peer is a bare array', Array.isArray(r.body) && r.body.length === 2, r.body);
    is('messages carry sender_name', 'sender_name' in r.body[0], Object.keys(r.body[0]).slice(0, 8));
    r = await form('POST', '/send_message', { sender_id: '1', receiver_id: '2', content: 'سلام دمو' });
    eq('sent', r.status, 201);
    r = await call('GET', '/messages/1/2');
    eq('and it is in the thread', r.body.length, 3);
    r = await call('GET', '/unread_counts/1');
    is('unread counts keyed by peer id', typeof r.body === 'object' && !('success' in r.body), r.body);
    r = await json('POST', '/pin_message/1', { user_id: 1 });
    is('pin toggles', r.body.pinned === true, r.body);

    console.log('\ngroups & servers');
    r = await json('POST', '/create_group', { name: 'گروه دمو', members: [2, 3] });
    eq('group created', r.status, 201);
    const gid = r.body.group_id;
    is('group id echoed', gid > 0, r.body);
    r = await call('GET', '/my_groups');
    is('my_groups lists it', r.body.some((g) => g.id === gid), r.body.map((g) => g.name));
    r = await call('GET', '/group_info/' + gid);
    is('group info has members + role', r.body.members.length === 3 && r.body.my_role === 'owner', r.body);
    r = await json('POST', '/group_add_member/' + gid, { user_id: 1 });
    eq('adding an existing member is idempotent', r.body.member_count, 3);
    r = await call('GET', '/lan_hosts');
    is('a seeded game server is listed', Array.isArray(r.body) && r.body[0].game_name.includes('فوتبال'), r.body);
    r = await json('POST', '/create_lan_host', { name: 'سرور من', game_name: 'زمین چمن',
                                                 ip_address: '192.168.1.5', port: 7777 });
    eq('server published', r.status, 201);
    r = await json('POST', '/create_lan_host', { name: 'بدون ip', game_name: 'x', ip_address: 'not-an-ip' });
    is('bad ip refused with the server code', r.body.error && r.body.error.code === 'BAD_IP', r.body);

    console.log('\nnotifications & admin');
    r = await call('GET', '/notifications/1');
    is('notifications are an envelope', typeof r.body.unread === 'number' && Array.isArray(r.body.items), r.body);
    const unread = r.body.unread;
    r = await json('POST', '/notifications_read/1', {});
    is('marking read returns a count', r.body.marked === unread, r.body);
    r = await call('GET', '/notifications/1');
    eq('and they are unread-free now', r.body.unread, 0);
    r = await call('GET', '/api/admin/stats');
    is('admin stats shaped like the server', r.body.stats && typeof r.body.total_posts === 'number', r.body);
    await json('POST', '/register', { username: 'siamak', password: 'Passw0rd!x' });
    r = await json('POST', '/login', { username: 'siamak', password: 'Passw0rd!x' });
    const token = r.body.token;
    r = await call('GET', '/api/admin/stats');
    eq('a non-admin cannot read admin stats', r.status, 403);

    console.log('\nplatform & fallback');
    r = await call('GET', '/api/app_info');
    eq('app_info version matches APP_VERSION', r.body.version, '4.0.0');
    is('app_info advertises the demo engine', r.body.db_engine === 'demo-local', r.body.db_engine);
    r = await call('GET', '/api/something/nobody/built');
    eq('an unknown GET does not 500', r.status, 200);
    is('and says it is a demo', r.body.success === false && r.body.error.code === 'DEMO_UNAVAILABLE', r.body);

    console.log('\npersistence');
    delete require.cache[path.join(__dirname, 'vl-demo.js')];
    require(path.join(__dirname, 'vl-demo.js'));
    r = await call('GET', '/posts');
    is('the post created before the reload is still there',
       r.body.some((p) => p.content === 'تست دمو #دمو'), r.body.map((p) => p.id));
    r = await call('GET', '/users');
    is('a user registered before the reload survives', r.body.some((u) => u.username === 'omid'), newId);
    is('nothing but JSON is stored', typeof store.get('volexturn_demo_v1') === 'string');

    console.log(`\n${checks - failed}/${checks} demo checks passed`);
    if (failed) { console.log(failed + ' FAILED'); process.exit(1); }
})().catch((e) => { console.error('\ndemo self-test crashed:', e); process.exit(1); });
