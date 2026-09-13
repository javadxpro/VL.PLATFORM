"""
User reporting + the moderation queue.

A report records a claim, never an action: nothing is deleted because someone
filed a report. Only an admin action (dismiss / delete / warn / ban) changes
state, and every such action is written to `moderation_actions` with the actor,
so the queue is auditable in both directions (spec §21).
"""

from __future__ import annotations

from flask import jsonify, request

from . import Module, conn, current_db, int_arg, my_id, payload, text_field
from ..errors import BadRequest, Conflict, Forbidden, NotFound
from ..log import get_logger
from ..notify import notify
from ..security import pagination_args, paginated, sanitize_text

mod = Module("reports")
log = get_logger("api.reports")

REASONS = {
    "spam": "هرزنامه",
    "harassment": "آزار و اذیت",
    "inappropriate": "محتوای نامناسب",
    "cheating": "تقلب در بازی",
    "malicious_server": "سرور مخرب",
    "impersonation": "جعل هویت",
    "nsfw": "محتوای بزرگسال",
    "other": "سایر",
}

#: which target kinds exist, and how to locate them
TARGETS: dict[str, tuple[str, str]] = {
    "user": ("users", "id"),
    "post": ("posts", "id"),
    "comment": ("post_comments", "id"),
    "message": ("messages", "id"),
    "group": ("groups", "id"),
    "room": ("game_rooms", "id"),
    "server": ("lan_hosts", "id"),
    "story": ("stories", "id"),
}

HANDLE_ACTIONS = ("dismiss", "delete_content", "warn", "mute", "ban", "unban", "restore")


def _target_exists(db, c, kind: str, target_id: int) -> dict | None:
    spec = TARGETS.get(kind)
    if not spec:
        return None
    table, pk = spec
    if not db.has_table(c, table):
        return None
    extra = " AND deleted_at IS NULL" if table in ("posts", "groups") else ""
    return db.query_one(c, f"SELECT * FROM {table} WHERE {pk} = ?{extra}", (target_id,))


@mod.route("/reasons", auth="none", rate=None)
def reasons():
    return jsonify({"success": True, "reasons": REASONS, "targets": sorted(TARGETS),
                    "status_flow": ["open", "reviewing", "actioned", "dismissed"]})


@mod.route("", methods=["POST"], auth="user", rate=(10, 600))
def create_report():
    me = my_id()
    body = payload()
    kind = sanitize_text(body.get("target_type"), max_len=16, strip_newlines=True)
    target_id = int_arg("target_id", src=body, lo=1)
    reason = sanitize_text(body.get("reason"), max_len=24, strip_newlines=True)
    details = text_field("details", body, max_len=1000, required=False, newlines=True)
    if kind not in TARGETS:
        raise BadRequest("نوع هدف نامعتبر است", code="BAD_TARGET_TYPE",
                         details={"allowed": sorted(TARGETS)})
    if reason not in REASONS:
        raise BadRequest("دلیل گزارش نامعتبر است", code="BAD_REASON",
                         details={"allowed": sorted(REASONS)})
    if not target_id:
        raise BadRequest("شناسه هدف لازم است", code="TARGET_ID_REQUIRED")

    db, c = current_db(), conn()
    row = _target_exists(db, c, kind, target_id)
    if row is None:
        raise NotFound("مورد قابل گزارش پیدا نشد", code="TARGET_NOT_FOUND")
    if kind == "user" and int(target_id) == me:
        raise BadRequest("نمی‌توانید از خودتان گزارش بدهید", code="SELF_REPORT")

    # A second identical open report escalates instead of duplicating.
    dup = db.query_one(c, """SELECT id, reporter_id FROM reports
                              WHERE target_type = ? AND target_id = ? AND status IN ('open','reviewing')
                                AND reporter_id = ?""", (kind, target_id, me))
    if dup:
        raise Conflict("شما همین مورد را اخیراً گزارش کرده‌اید", code="ALREADY_REPORTED",
                       details={"report_id": int(dup["id"])})

    same = int(db.scalar(c, """SELECT COUNT(*) FROM reports
                                WHERE target_type = ? AND target_id = ? AND status IN ('open','reviewing')""",
                          (kind, target_id)) or 0)
    rid = db.insert(c, """
        INSERT INTO reports (reporter_id, target_type, target_id, reason, details, status)
        VALUES (?, ?, ?, ?, ?, 'open')""", (me, kind, target_id, reason, details))
    c.commit()
    log.info("report_created", extra={"ctx": {"report_id": rid, "kind": kind,
                                              "target_id": target_id, "by": me}})
    return jsonify({"success": True, "report_id": rid,
                    "reports_on_target": same + 1,
                    "escalated": same >= 2,
                    "message": "گزارش شما ثبت شد و در صف بازبینی است"}), 201


@mod.route("/mine", auth="user", rate=None)
def my_reports():
    me = my_id()
    db, c = current_db(), conn()
    limit, offset, page = pagination_args(default_size=20)
    total = db.scalar(c, "SELECT COUNT(*) FROM reports WHERE reporter_id = ?", (me,))
    rows = db.query(c, f"""
        SELECT r.*, u.full_name AS target_owner_name FROM reports r
        LEFT JOIN users u ON u.id = r.reporter_id
        WHERE r.reporter_id = ? ORDER BY r.id DESC {db.limit_offset(limit, offset)}""", (me,))
    return jsonify({"success": True, "reports": [dict(r) for r in rows],
                    **paginated(total, limit, offset, page)})


@mod.route("/queue", auth="admin", rate=None)
def queue():
    db, c = current_db(), conn()
    limit, offset, page = pagination_args(default_size=25)
    where, params = ["1=1"], []
    status = sanitize_text(request.args.get("status"), max_len=16, strip_newlines=True)
    if status in {"open", "reviewing", "actioned", "dismissed"}:
        where.append("r.status = ?"); params.append(status)
    kind = sanitize_text(request.args.get("target_type"), max_len=16, strip_newlines=True)
    if kind in TARGETS:
        where.append("r.target_type = ?"); params.append(kind)
    clause = " AND ".join(where)
    total = db.scalar(c, f"SELECT COUNT(*) FROM reports r WHERE {clause}", params)
    rows = db.query(c, f"""
        SELECT r.*, rep.full_name AS reporter_name, rep.username AS reporter_username,
               (SELECT COUNT(*) FROM reports x WHERE x.target_type = r.target_type
                  AND x.target_id = r.target_id) AS duplicates
        FROM reports r JOIN users rep ON rep.id = r.reporter_id
        WHERE {clause}
        ORDER BY CASE r.status WHEN 'open' THEN 0 WHEN 'reviewing' THEN 1 ELSE 2 END,
                 duplicates DESC, r.id DESC {db.limit_offset(limit, offset)}""", params)
    items = []
    for r in rows:
        d = dict(r)
        d["reason_label"] = REASONS.get(d.get("reason") or "", d.get("reason"))
        d["target_label"] = _target_label(db, c, d.get("target_type"), d.get("target_id"))
        d["handled"] = bool(d.get("handled_at"))
        items.append(d)
    counts = {s: int(db.scalar(c, "SELECT COUNT(*) FROM reports WHERE status = ?", (s,)) or 0)
              for s in ("open", "reviewing", "actioned", "dismissed")}
    return jsonify({"success": True, "reports": items, "counts": counts,
                    **paginated(total, limit, offset, page)})


def _target_label(db, c, kind: str | None, target_id: int | None) -> dict | None:
    if not kind or not target_id:
        return None
    row = _target_exists(db, c, str(kind), int(target_id))
    if row is None:
        return {"kind": kind, "id": int(target_id), "deleted": True}
    label = None
    if kind == "user":
        label = f"@{row.get('username')}"
    elif kind in {"post", "comment", "message"}:
        label = str(row.get("content") or "")[:120] or "(فایل)"
    elif kind == "group":
        label = row.get("name")
    elif kind == "room":
        label = row.get("name")
    elif kind == "server":
        label = f"{row.get('name') or row.get('game_name')} · {row.get('ip_address')}:{row.get('port')}"
    elif kind == "story":
        label = f"استوری {row.get('file_type') or ''}".strip()
    return {"kind": kind, "id": int(target_id), "label": label,
            "owner_id": int(row.get("user_id") or row.get("creator_id") or row.get("host_id") or 0) or None}


@mod.route("/<int:rid>", auth="admin", rate=None)
def detail(rid: int):
    db, c = current_db(), conn()
    row = db.query_one(c, """SELECT r.*, rep.full_name AS reporter_name, rep.username AS reporter_username
                             FROM reports r JOIN users rep ON rep.id = r.reporter_id WHERE r.id = ?""", (rid,))
    if row is None:
        raise NotFound("گزارش پیدا نشد")
    d = dict(row)
    d["target"] = _target_label(db, c, d.get("target_type"), d.get("target_id"))
    if d.get("target_type") == "user" and d.get("target_id"):
        uid = int(d["target_id"])
        d["history"] = {
            "reports_against": int(db.scalar(c, """SELECT COUNT(*) FROM reports
                                                   WHERE target_type = 'user' AND target_id = ?""", (uid,)) or 0),
            "prior_actions": [dict(r) for r in db.query(c, """
                SELECT action, note, created_at FROM moderation_actions WHERE user_id = ?
                ORDER BY id DESC LIMIT 10""", (uid,))],
            "is_banned": bool(db.scalar(c, "SELECT COALESCE(is_banned,0) FROM users WHERE id = ?", (uid,))),
        }
    return jsonify({"success": True, "report": d})


@mod.route("/<int:rid>/claim", methods=["POST"], auth="admin", rate=(60, 60))
def claim(rid: int):
    db, c = current_db(), conn()
    if db.query_one(c, "SELECT id FROM reports WHERE id = ?", (rid,)) is None:
        raise NotFound("گزارش پیدا نشد")
    db.execute(c, """UPDATE reports SET status = 'reviewing', handled_by = ?
                     WHERE id = ? AND status = 'open'""", (my_id(), rid)).close()
    c.commit()
    return jsonify({"success": True, "status": "reviewing"})


@mod.route("/<int:rid>/handle", methods=["POST"], auth="admin", rate=(60, 300))
def handle(rid: int):
    """
    One endpoint for every admin decision.

    `delete_content` refuses to touch user rows it does not own the semantics of
    (e.g. a `message` is soft-deleted, a `server` is disabled not deleted) — the
    mapping is explicit below rather than a generic `DELETE FROM {table}`.
    """
    me = my_id()
    body = payload()
    action = sanitize_text(body.get("action"), max_len=24, strip_newlines=True)
    note = text_field("note", body, max_len=500, required=False, newlines=True)
    if action not in HANDLE_ACTIONS:
        raise BadRequest("اقدام نامعتبر است", code="BAD_ACTION", details={"allowed": list(HANDLE_ACTIONS)})
    db, c = current_db(), conn()
    report = db.query_one(c, "SELECT * FROM reports WHERE id = ?", (rid,))
    if report is None:
        raise NotFound("گزارش پیدا نشد")
    kind, target_id = report["target_type"], report.get("target_id")
    owner_id = _owner_of(db, c, kind, target_id)
    result: dict = {"action": action}

    if action == "dismiss":
        pass
    elif action == "delete_content":
        result["content"] = _delete_content(db, c, kind, int(target_id or 0), me)
    elif action == "warn":
        if not owner_id:
            raise BadRequest("مقصدی برای اخطار نیست", code="NO_TARGET_USER")
        notify(db, c, user_id=owner_id, actor_id=me, ntype="moderation", target_id=rid,
               target_type="report", body=f"اخطار مدیر: {note or 'رفتار تخلفی گزارش شد'}",
               socketio=_sio())
    elif action == "mute":
        # Muting is a *group* concept here: there is no platform-wide mute that
        # the client would honour, so pretending to apply one would be a lie.
        minutes = int_arg("minutes", 60, src=body, lo=1, hi=60 * 24 * 30)
        if kind != "group" or not target_id:
            raise BadRequest("بی‌صدا کردن فقط در گروه معنا دارد؛ برای هدف‌های دیگر اخطار یا بن را استفاده کنید",
                             code="MUTE_NEEDS_GROUP")
        if not owner_id:
            raise BadRequest("مقصدی برای بی‌صدا کردن نیست", code="NO_TARGET_USER")
        until = db.hours_ahead_sql(minutes / 60)
        cur = db.execute(c, f"""UPDATE group_members SET muted_until = {until}
                                WHERE group_id = ? AND user_id = ?""", (target_id, owner_id))
        if not cur.rowcount:
            cur.close()
            raise NotFound("عضو این گروه نیست", code="NOT_A_MEMBER")
        cur.close()
        result["muted_until_minutes"] = minutes
        notify(db, c, user_id=owner_id, actor_id=me, ntype="group_muted", target_id=int(target_id),
               target_type="group", body=f"در گروه تا {minutes} دقیقه بی‌صدا شدید", socketio=_sio())
    elif action in {"ban", "unban"}:
        if not owner_id:
            raise BadRequest("مقصدی برای بن نیست", code="NO_TARGET_USER")
        if owner_id == me:
            raise Forbidden("نمی‌توانید خودتان را بن کنید", code="SELF_BAN")
        from ..auth import revoke_user_sessions
        if action == "ban":
            db.execute(c, f"""UPDATE users SET is_banned = 1, status = 'banned',
                              ban_reason = ?, banned_at = {db.now_sql()} WHERE id = ?""",
                       (note or "تخلف گزارش‌شده", owner_id)).close()
            revoke_user_sessions(db, c, owner_id, reason="banned")
        else:
            db.execute(c, """UPDATE users SET is_banned = 0, status = 'active', ban_reason = NULL,
                              banned_at = NULL WHERE id = ?""", (owner_id,)).close()
    elif action == "restore":
        result["restored"] = _restore(db, c, kind, int(target_id or 0))

    db.execute(c, f"""UPDATE reports SET status = ?, handled_by = ?, handled_at = {db.now_sql()},
                      handle_action = ?, handle_note = ? WHERE id = ?""",
               ("actioned" if action != "dismiss" else "dismissed", me, action, note, rid)).close()
    db.insert(c, """INSERT INTO moderation_actions (admin_id, action, target_type, target_id, user_id, note)
                    VALUES (?, ?, ?, ?, ?, ?)""", (me, action[:24], kind, target_id, owner_id, note[:500]))
    c.commit()
    log.info("report_handled", extra={"ctx": {"report_id": rid, "action": action, "admin_id": me,
                                              "target_user_id": owner_id}})
    return jsonify({"success": True, "report_id": rid, "result": result, "action": action})


def _owner_of(db, c, kind: str | None, target_id) -> int | None:
    if not kind or not target_id:
        return None
    spec = TARGETS.get(str(kind))
    if not spec:
        return None
    table, pk = spec
    if kind == "user":
        return int(target_id)
    col = {"posts": "user_id", "post_comments": "user_id", "messages": "sender_id",
           "groups": "creator_id", "game_rooms": "host_id", "lan_hosts": "user_id",
           "stories": "user_id"}.get(table)
    if not col or not db.has_table(c, table):
        return None
    row = db.query_one(c, f"SELECT {col} AS owner FROM {table} WHERE {pk} = ?", (target_id,))
    return int(row["owner"]) if row and row.get("owner") is not None else None


def _delete_content(db, c, kind: str | None, target_id: int, admin_id: int) -> dict:
    """Soft-delete where the table supports it; hard delete only where it doesn't."""
    table = {"post": "posts", "comment": "post_comments", "message": "messages",
             "story": "stories", "room": "game_rooms", "server": "lan_hosts",
             "group": "groups"}.get(str(kind))
    if not table or not target_id:
        raise BadRequest("نوع هدف قابل حذف نیست", code="NOT_DELETABLE")
    if table in {"posts", "groups"}:
        db.execute(c, f"UPDATE {table} SET deleted_at = CURRENT_TIMESTAMP WHERE id = ?", (target_id,)).close()
        return {"table": table, "mode": "soft"}
    if table == "lan_hosts":
        db.execute(c, f"""UPDATE lan_hosts SET is_enabled = 0, status = 'offline',
                          archived_at = {db.now_sql()} WHERE id = ?""", (target_id,)).close()
        return {"table": table, "mode": "disabled"}
    if table == "game_rooms":
        db.execute(c, f"""UPDATE game_rooms SET status = 'closed', closed_at = {db.now_sql()}
                          WHERE id = ?""", (target_id,)).close()
        return {"table": table, "mode": "closed"}
    if table == "messages":
        db.execute(c, """UPDATE messages SET deleted_for_everyone = 1, deleted_by = ?, content = ''
                          WHERE id = ?""", (admin_id, target_id)).close()
        return {"table": table, "mode": "hidden"}
    if table == "stories":
        db.execute(c, "DELETE FROM stories WHERE id = ?", (target_id,)).close()
        return {"table": table, "mode": "deleted"}
    db.execute(c, f"DELETE FROM {table} WHERE id = ?", (target_id,)).close()
    return {"table": table, "mode": "deleted"}


def _restore(db, c, kind: str | None, target_id: int) -> dict:
    table = {"post": "posts", "group": "groups", "server": "lan_hosts",
             "room": "game_rooms"}.get(str(kind))
    if not table:
        return {"restored": False, "reason": "unsupported"}
    if table == "posts":
        db.execute(c, "UPDATE posts SET deleted_at = NULL WHERE id = ?", (target_id,)).close()
    elif table == "groups":
        db.execute(c, "UPDATE groups SET deleted_at = NULL WHERE id = ?", (target_id,)).close()
    elif table == "lan_hosts":
        db.execute(c, """UPDATE lan_hosts SET archived_at = NULL, is_enabled = 1,
                          status = 'unknown' WHERE id = ?""", (target_id,)).close()
    else:
        db.execute(c, "UPDATE game_rooms SET closed_at = NULL, status = 'open' WHERE id = ?",
                   (target_id,)).close()
    return {"restored": True, "table": table}


@mod.route("/<int:rid>/dupes", auth="admin", rate=None)
def duplicates(rid: int):
    db, c = current_db(), conn()
    r = db.query_one(c, "SELECT target_type, target_id FROM reports WHERE id = ?", (rid,))
    if r is None:
        raise NotFound("گزارش پیدا نشد")
    rows = db.query(c, """SELECT id, reporter_id, reason, status, created_at FROM reports
                          WHERE target_type = ? AND target_id = ? ORDER BY id DESC LIMIT 50""",
                    (r["target_type"], r["target_id"]))
    return jsonify({"success": True, "reports": [dict(x) for x in rows]})


@mod.route("/target", auth="user", rate=None)
def target_context():
    """
    What the "report" dialog needs to know: does this target exist, and who owns it.

    Returns minimal data only — no private message bodies for a target the
    requester cannot already see.
    """
    kind = sanitize_text(request.args.get("type"), max_len=16, strip_newlines=True)
    target_id = int_arg("id", src=request.args, lo=1)
    if kind not in TARGETS or not target_id:
        raise BadRequest("هدف نامعتبر است", code="BAD_TARGET")
    db, c = current_db(), conn()
    row = _target_exists(db, c, kind, target_id)
    if row is None:
        raise NotFound("موردی پیدا نشد")
    return jsonify({"success": True, "exists": True, "type": kind, "id": target_id,
                    "owner_id": _owner_of(db, c, kind, target_id),
                    "can_report": not (kind == "user" and _owner_of(db, c, kind, target_id) == my_id())})


def _sio():
    from flask import current_app
    return current_app.extensions["volexturn_socketio"]
