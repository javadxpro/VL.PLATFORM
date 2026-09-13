#!/usr/bin/env python3
"""
Generate `docs/API.md` and `docs/LEGACY_MAP.md` from the running app.

Hand-maintained route tables are wrong within a release: paths drift, and the
auth/rate columns drift silently because nothing fails when they do. So this
script reads both halves of the truth — the rules Flask actually registered
(`app.url_map`) and the decorator arguments they were built from (`Module.route`
calls parsed out of `backend/api/*.py`) — and prints the tables.

    python3 tools/gen_api_docs.py            # rewrite both docs
    python3 tools/gen_api_docs.py --check    # fail if the committed docs are stale

The docstrings of the view functions become the description column, which is the
cheapest way to keep the two in step: write the sentence once, next to the code.
"""

from __future__ import annotations

import argparse
import ast
import collections
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# Documenting the surface must not touch anyone's real database: the generator
# boots the app against a throwaway schema in /tmp.
os.environ.setdefault("VOLEXTURN_DB_PATH", "/tmp/volexturn-docs.sqlite")
os.environ.setdefault("VOLEXTURN_UPLOAD_FOLDER", "/tmp/volexturn-docs-uploads")
os.environ.setdefault("VOLEXTURN_LOG_LEVEL", "CRITICAL")


# --------------------------------------------------------------------- collect
def route_meta() -> tuple[dict, dict]:
    """(module -> {rule: [entry]}, (module, fn) -> docstring first line)."""
    meta: dict = collections.defaultdict(dict)
    docs: dict = {}
    for path in sorted((ROOT / "backend" / "api").glob("*.py")):
        module = path.stem
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.FunctionDef):
                continue
            head = ast.get_docstring(node)
            if head:
                docs[(module, node.name)] = " ".join(head.split())[:160]
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                if dec.func.attr not in ("route", "legacy"):
                    continue
                kw = {k.arg: k.value for k in dec.keywords}

                def lit(name, default=None):
                    node_ = kw.get(name)
                    if node_ is None:
                        return default
                    try:
                        return ast.literal_eval(node_)
                    except ValueError:
                        return default

                # rules are constants in practice; an f-string rule (a couple of
                # legacy aliases build them from a prefix) is skipped rather than
                # guessed at — the runtime table still lists it.
                rule = lit("rule")
                if rule is None and dec.args:
                    try:
                        rule = ast.literal_eval(dec.args[0])
                    except ValueError:
                        rule = None
                if not isinstance(rule, str):
                    continue
                meta[module].setdefault(rule, []).append({
                    "kind": dec.func.attr,
                    "auth": lit("auth", "user"),
                    "rate": lit("rate", "default"),
                    "csrf": lit("csrf", True),
                    "methods": tuple(lit("methods") or ("GET",)),
                    "legacy": lit("legacy"),
                    "fn": node.name,
                })
    return meta, docs


def build() -> tuple[str, str]:
    from backend.api import LEGACY_ALIASES, MODULE_ORDER, PREFIXES
    from backend.app import create_app

    app = create_app(skip_migrations=False)
    meta, docs = route_meta()

    runtime: dict = collections.defaultdict(list)
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r.rule)):
        if rule.endpoint == "static":
            continue
        methods = sorted((rule.methods or []) - {"HEAD", "OPTIONS"})
        module = rule.endpoint.split(".")[0] if "." in rule.endpoint else "(root)"
        # A legacy handler lives on the shared root blueprint (`legacy.…`), but it
        # belongs to the module whose data it serves: `/users` is a users endpoint
        # even though Flask files it at the root. Its endpoint name carries the
        # owner — `legacy.<blueprint>legacy_<module>_<fn>`.
        ep = str(rule.endpoint)
        for stem in ("legacy.legacy_", "legacy_"):
            if module == ("legacy" if stem == "legacy.legacy_" else "(root)") and ep.startswith(stem):
                module = ep[len(stem):].partition("_")[0]
                break
        runtime[module].append((str(rule.rule), methods, rule.endpoint))

    # real response bodies, so the "contracts" section quotes the program
    client = app.test_client()
    probe_unauth = client.get("/api/posts").get_json()
    probe_health = client.get("/healthz").get_json()
    probe_invalid = client.post("/api/auth/register", json={}).get_json()
    # a generated doc must be byte-stable, or `--check` fails for no reason:
    # request ids (and anything else per-response) are replaced by a placeholder
    for body in (probe_unauth, probe_invalid, probe_health):
        if isinstance(body, dict):
            for key in ("request_id", "ts", "now", "uptime_seconds"):
                if key in body:
                    body[key] = "<per-response>"

    label = {
        "auth": "احراز هویت", "users": "کاربران", "posts": "پست‌ها", "stories": "استوری‌ها",
        "messages": "پیام‌ها", "groups": "گروه‌ها", "gaming": "گیمینگ", "servers": "سرورها",
        "rooms": "اتاق‌ها", "notifications": "اعلان‌ها", "search": "جستجو",
        "reports": "گزارش‌ها", "admin": "مدیریت", "meta": "اطلاعات برنامه و فایل‌ها",
    }
    auth_txt = {"none": "عمومی", "optional": "اختیاری", "user": "نشست لازم", "admin": "مدیر لازم"}

    def rate_txt(v):
        if v == "default":
            return "پیش‌فرض"
        if v is None:
            return "ندارد"
        if isinstance(v, (list, tuple)) and len(v) == 2:
            return f"{v[0]} / {v[1]} ثانیه"
        return str(v)

    documented: set[str] = set()
    L: list[str] = []
    A = L.append
    total = sum(len(v) for v in runtime.values())
    A("# مرجع API")
    A("")
    A("> این دو فایل را `python3 tools/gen_api_docs.py` از خودِ برنامه تولید می‌کند:")
    A("> مسیرها از `app.url_map`، و ستون‌های دسترسی/Rate/CSRF از دکوریتورهای")
    A("> `backend/api/*.py`. چیزی که اینجا نیست در کد هم نیست؛ اگر جدولی قدیمی به")
    A("> نظر رسید، اسکریپت را دوباره اجرا کنید نه دست‌تیپ را.")
    A("")
    A(f"مجموع قواعد ثبت‌شده: **{total}** (شامل {len([1 for v in runtime.values() for r in v if str(r[2]).startswith('legacy:')])} "
      "میان‌بر قدیمی روی ریشه).")
    A("")
    A("## قراردادها")
    A("")
    A("| موضوع | قرارداد |")
    A("|---|---|")
    A("| پیشوند ماژول | " + " · ".join(f"`{m}` → `{PREFIXES[m]}`" for m in sorted(PREFIXES)) + " |")
    A("| هویت | `Authorization: Bearer <token>` یا کوکی نشست. هیچ `user_id` ارسالی کلاینت معتبر نیست. |")
    A("| شناسهٔ درخواست | هر پاسخ `X-Request-Id` برمی‌گرداند و همان کلید در لاگ JSON است. |")
    A("| اسلش | `strict_slashes=False`؛ `/api/posts/` و `/api/posts` یکی‌اند. |")
    A("| خطا | `success: false` و کلیدهای `error`/`message` — همان شکل نسخهٔ قدیمی. |")
    A("| فهرست | مسیرهای canonical `items` + صفحه‌بندی می‌دهند؛ مسیرهای قدیمی آرایهٔ برهنه. |")
    A("")
    A("سه پاسخ واقعی از همین build:")
    A("")
    A("```json")
    A("// GET /api/posts — بدون نشست")
    A(json.dumps(probe_unauth, ensure_ascii=False, indent=2))
    A("```")
    A("")
    A("```json")
    A("// POST /api/auth/register — بدنامعتبر")
    A(json.dumps(probe_invalid, ensure_ascii=False, indent=2))
    A("```")
    A("")
    A("```json")
    A("// GET /healthz")
    A(json.dumps(probe_health, ensure_ascii=False, indent=2))
    A("```")
    A("")
    A("## فهرست ماژول‌ها")
    A("")
    A("| ماژول | base | تعداد |")
    A("|---|---|---|")
    for m in MODULE_ORDER:
        A(f"| {label.get(m, m)} (`{m}`) | `{PREFIXES.get(m, '/api/' + m)}` | {len(runtime.get(m, []))} |")
    A(f"| ریشه و فایل‌ها | — | {len(runtime.get('(root)', []))} |")
    A("")
    A("### ورود/خروج چه شکلی است؟")
    A("")
    A("امضای هر endpoint (نام فیلدها، انواع، کدهای خطا) در خود `backend/api/<module>.py`")
    A("نزدیک‌ترین توضیح به کد است و ستون «توضیح» جدول‌های پایین از docstring همان تابع")
    A("می‌آید. برای نمونهٔ فراخوانی، `tests/` را ببینید — ۱۵۹ تست، همان‌ها که در CI اجرا")
    A("می‌شوند، تنها منبع معتبر shape هستند چون واقعاً اجرا می‌شوند.")
    A("")
    A("---")
    # A `Module.route` rule is relative to its blueprint prefix, while a
    # `Module.legacy` rule is absolute and lands on the root blueprint, so every
    # row is matched against one global index of absolute rules that also keeps
    # the owning module — that is where the docstring lives.
    index = {}
    for owner, rules in meta.items():
        for rule, entries in rules.items():
            for e in entries:
                key = rule if e["kind"] == "legacy" else PREFIXES.get(owner, "") + rule
                index.setdefault((key, tuple(e["methods"])), []).append((owner, e))

    def lookup(module: str, rule: str, methods, endpoint: str):
        found = index.get((rule, tuple(methods)))
        if found:
            return list(found)
        found = [pair for (key, _m), v in index.items() if key == rule for pair in v]
        if found:
            return found
        return []

    for m in list(MODULE_ORDER) + ["(root)"]:
        rows = runtime.get(m, [])
        if not rows:
            continue
        A("")
        A(f"## {label.get(m, m) if m != '(root)' else 'ریشه: فایل‌ها، سلامت و میان‌برهای قدیمی'}")
        A("")
        if m in PREFIXES:
            A(f"base: `{PREFIXES[m]}`")
            A("")
        A("| متد | مسیر | دسترسی | Rate limit | CSRF | توضیح |")
        A("|---|---|---|---|---|---|")
        shown = set()
        for rule, methods, endpoint in rows:
            shown.add(rule)          # every registered rule must end up printed
            if str(endpoint).startswith("legacy:"):
                target = str(endpoint)[len("legacy:"):].rsplit(":", 1)[0]
                A(f"| {','.join(methods)} | `{rule}` | میان‌بر قدیمی | پیش‌فرض | بله | aliasِ `{target}` |")
                continue
            # only entries whose declared methods belong to this row, so a GET row
            # never inherits the POST decorator's rate limit (HEAD rides along with
            # GET, which is how Flask registers it too)
            allowed = set(methods) | ({"HEAD"} if "GET" in methods else set())
            entries = [pair for pair in lookup(m, rule, methods, endpoint)
                       if set(pair[1]["methods"]) <= allowed]
            if entries:
                for owner, e in entries:
                    # the wrapper carries the *registered* auth level, which is the
                    # ground truth; the decorator parse only fills in rate/CSRF
                    real = getattr(app.view_functions.get(endpoint), "_vx_auth", None)
                    if real:
                        e = dict(e, auth=real)
                    note = docs.get((owner, e["fn"]), "")
                    if e["legacy"]:
                        note = (note + " · " if note else "") + f"میان‌بر قدیمی: `{e['legacy']}`"
                    if e["kind"] == "legacy":
                        note = (note + " · " if note else "") + "پاسخ با شکل قدیمی"
                    A(f"| {','.join(methods)} | `{rule}` | {auth_txt.get(e['auth'], e['auth'])} | "
                      f"{rate_txt(e['rate'])} | {'بله' if e['csrf'] else 'نه'} | {note} |")
            else:
                A(f"| {','.join(methods)} | `{rule}` | — | — | — | مسیر سرویس/داخلی (`{endpoint}`) |")
        documented.update(shown)
        for (rule, _meths), pairs in sorted(index.items()):
            if rule in shown or rule in documented or not any(o == m for o, _e in pairs):
                continue
            for owner, e in pairs:
                A(f"| {','.join(e['methods'])} | `{rule}` | {auth_txt.get(e['auth'], e['auth'])} | "
                  f"{rate_txt(e['rate'])} | {'بله' if e['csrf'] else 'نه'} | "
                  f"{docs.get((owner, e['fn']), '')} *(در url_map با این شکل پیدا نشد)* |")
                shown.add(rule)

    # Anything registered but not printed above would be a hole in the doc, so it
    # gets its own section instead of disappearing.
    leftovers = [(r, mth, ep) for mod, rows in runtime.items()
                  for r, mth, ep in rows if r not in documented and not str(ep).startswith("legacy:")]
    if leftovers:
        A("")
        A("## مسیرهای ثبت‌شدهٔ بدون دکوریتور ماژول")
        A("")
        A("این قاعده‌ها در `url_map` هستند اما هیچ `Module.route`/`Module.legacy` با همان")
        A("آدرس در `backend/api/*.py` پیدا نشد (یا مسیرشان از رشتهٔ non-literal ساخته شده،")
        A("یا به دست `add_url_rule` مستقیم ثبت شده‌اند).")
        A("")
        A("| متد | مسیر | endpoint |")
        A("|---|---|---|")
        for r, mth, ep in leftovers:
            A(f"| {','.join(mth)} | `{r}` | `{ep}` |")
        documented.update(r for r, _m, _e in leftovers)

    missing = [r for mod, rows in runtime.items() for r, _m, _e in rows if r not in documented]
    if missing:
        raise SystemExit(f"gen_api_docs: {len(missing)} route(s) not documented, e.g. {missing[:3]}")

    api_md = "\n".join(L) + "\n"

    # ------------------------------------------------------------- LEGACY_MAP.md
    M: list[str] = []
    B = M.append
    B("# نقشهٔ مسیرهای قدیمی (Legacy Map)")
    B("")
    B("سرور canonical دو نوع سازگاری با کلاینت‌های نسخهٔ ۳ دارد. این فایل هر دو را از")
    B("خودِ کد فهرست می‌کند، چون «یک روت را عوض نکردیم» چیزی است که با چشم قابل")
    B("بررسی نیست و با تست قابل اثبات است.")
    B("")
    B("## ۱) alias: مسیر قدیمی روی همان هندلر جدید")
    B("")
    B("در `Module.route(..., legacy=\"/x\")` ثبت می‌شود و در `LEGACY_BP` به همان view")
    B("می‌چسبد؛ یعنی **شکل پاسخ، اعتبارسنجی و همهٔ فیلترهای دسترسی نسخهٔ جدید** را")
    B("می‌گیرد. این مسیرها برای کلاینت‌هایی است که فقط آدرس عوض شده‌اند.")
    B("")
    B(f"تعداد: {len(LEGACY_ALIASES)}")
    B("")
    B("| مسیر قدیمی | متد | endpoint جدید |")
    B("|---|---|---|")
    for rule, methods, endpoint in sorted(LEGACY_ALIASES, key=lambda t: (str(t[0]), str(t[2]))):
        B(f"| `{rule}` | {','.join(methods)} | `{endpoint}` |")
    B("")
    B("## ۲) هندلر قدیمی: مسیر قدیمی با شکل پاسخ قدیمی")
    B("")
    B("وقتی کلاینت فقط آدرس را عوض نکرده — مثلاً آرایهٔ برهنه انتظار دارد، نه `items` —")
    B("از `@mod.legacy(rule)` استفاده می‌شود: تابعی جدا که همان کوئری را صدا می‌زند و")
    B("بدن را به شکل قدیمی درمی‌آورد. اینها در `LEGACY_BP` با نام `legacy_*` ثبت‌اند.")
    B("")
    B("| مسیر قدیمی | متد | تابع |")
    B("|---|---|---|")
    n = 0
    for rule, methods, endpoint in sorted(sum(runtime.values(), [])):
        if str(endpoint).startswith("legacy_"):
            n += 1
            B(f"| `{rule}` | {','.join(methods)} | `{endpoint}` |")
    B(f"| — | — | مجموع: {n} |")
    B("")
    B("## ۳) تفاوت شکل پاسخ (چیزی که alias حل نمی‌کند)")
    B("")
    B("این مسیرها در نسخهٔ ۳ بدنهٔ برهنه برمی‌گرداندند و همچنان برهنه می‌مانند:")
    B("")
    B("- `GET /users`، `GET /posts`، `GET /messages/<u1>/<u2>`، `GET /group_messages/<gid>`،")
    B("  `GET /unread_counts/<me>`، `GET /post_comments/<id>`، `GET /stories`،")
    B("  `GET /lan_hosts`، `GET /my_groups` → **آرایهٔ برهنه**")
    B("- `GET /notifications/<me>` → `{\"unread\": n, \"items\": [...]}`")
    B("- canonical‌ها (`/api/…`) همیشه `{\"success\": true, …}` با `items` و صفحه‌بندی")
    B("")
    B("## ۴) دیتابیس")
    B("")
    B("مهاجرت `0001:baseline_legacy_schema` (در `backend/migrations.py`) نام‌ها و")
    B("ستون‌های نسخهٔ ۳ را به اسکیمای canonical می‌رساند؛ همان‌جا ببینید کدام ستون")
    B("مهاجرت کرده و کدام حذف شده — `doctor` هم اگر دیتابیس مهاجرت‌نکرده باشد همان را")
    B("به‌عنوان problem گزارش می‌کند.")
    B("")
    B("## ۵) چه‌کار کنیم")
    B("")
    B("کلاینت‌های موجود می‌توانند بدون تغییر کار کنند، اما مسیرهای قدیمی new feature")
    B("نمی‌گیرند. برای مهاجرت: از `python3 -m backend routes --filter <substring>` برای")
    B("دیدن هر دو سطح استفاده کنید، و از تست‌های `tests/` به‌عنوان نمونهٔ درخواست/پاسخ.")
    legacy_md = "\n".join(M) + "\n"
    return api_md, legacy_md


def main() -> int:
    ap = argparse.ArgumentParser(prog="gen_api_docs")
    ap.add_argument("--check", action="store_true", help="fail if the committed files differ")
    args = ap.parse_args()
    api_md, legacy_md = build()
    targets = {ROOT / "docs" / "API.md": api_md, ROOT / "docs" / "LEGACY_MAP.md": legacy_md}
    stale = []
    for path, text in targets.items():
        old = path.read_text(encoding="utf-8") if path.exists() else None
        if args.check:
            if old != text:
                stale.append(path.name)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)} ({len(text.splitlines())} lines)")
    if args.check and stale:
        print("stale: " + ", ".join(stale) + " — run tools/gen_api_docs.py", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
