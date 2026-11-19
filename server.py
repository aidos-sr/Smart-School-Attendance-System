"""School Face · сайт присутствия. Принимает отметки от программы Face и показывает их онлайн."""
import hmac
import os
import secrets
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, g, jsonify, redirect, request, send_from_directory, session
import telegram_support as telegram

TZ = timezone(timedelta(hours=5), name="UTC+05:00")
BASE = Path(__file__).resolve().parent
API_KEY = os.environ.get("API_KEY", "")
VIEW_PASSWORD = os.environ.get("VIEW_PASSWORD", "")
SECRET_KEY = os.environ.get("SECRET_KEY", "")
DB_PATH = os.environ.get("DB_PATH", str(BASE / "web.sqlite3"))
ONLINE_SECONDS = 90

for name, value in (("API_KEY", API_KEY), ("VIEW_PASSWORD", VIEW_PASSWORD), ("SECRET_KEY", SECRET_KEY)):
    if len(value) < 8:
        sys.exit(f"Задайте переменную окружения {name} (минимум 8 символов). См. README_SITE_RU.md")

app = Flask(__name__, static_folder=None)
app.secret_key = SECRET_KEY
app.config.update(MAX_CONTENT_LENGTH=2 * 1024 * 1024, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "1") == "1",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=12))

SCHEMA = """
CREATE TABLE IF NOT EXISTS people(
  person_id INTEGER PRIMARY KEY, enrolled_at TEXT NOT NULL, name TEXT NOT NULL, group_name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, uid TEXT NOT NULL UNIQUE, person_id INTEGER NOT NULL,
  at TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('IN','OUT')), source TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_day ON events(at);
CREATE INDEX IF NOT EXISTS events_person ON events(person_id, at);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS class_checks(
  day TEXT NOT NULL, group_name TEXT NOT NULL,
  uniform_score INTEGER NOT NULL CHECK(uniform_score BETWEEN 0 AND 10),
  cleanliness_score INTEGER NOT NULL CHECK(cleanliness_score BETWEEN 0 AND 5),
  student_count INTEGER NOT NULL CHECK(student_count BETWEEN 0 AND 1000),
  updated_at TEXT NOT NULL,
  PRIMARY KEY(day, group_name));
"""


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


with sqlite3.connect(DB_PATH) as boot:
    boot.execute("PRAGMA journal_mode=WAL")
    boot.executescript(SCHEMA)
    boot.executescript(telegram.SCHEMA)


def now():
    return datetime.now(TZ)


def logged_in():
    return session.get("ok") is True


# ---------- приём данных от Face ----------
def parse_time(text):
    moment = datetime.fromisoformat(str(text))
    if moment.tzinfo is None:
        raise ValueError("нет часового пояса")
    return moment.astimezone(TZ).isoformat(timespec="seconds")


@app.post("/api/sync")
def api_sync():
    sent = request.headers.get("Authorization", "")
    if not hmac.compare_digest(sent, "Bearer " + API_KEY):
        return jsonify(error="unauthorized"), 401
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="bad json"), 400
    conn = db()
    accepted = skipped = 0
    try:
        with conn:
            people = body.get("people")
            if people is not None:
                if not isinstance(people, list) or len(people) > 3000:
                    raise ValueError("bad people")
                incoming = {}
                for p in people:
                    pid = int(p["id"])
                    name, group = str(p["name"]).strip()[:120], str(p["group"]).strip()[:60]
                    if not name:
                        raise ValueError("empty name")
                    incoming[pid] = (str(p["enrolled_at"])[:40], name, group)
                for row in conn.execute("SELECT person_id, enrolled_at FROM people").fetchall():
                    got = incoming.get(row["person_id"])
                    if got is None or got[0] != row["enrolled_at"]:
                        conn.execute("DELETE FROM events WHERE person_id=?", (row["person_id"],))
                        conn.execute("DELETE FROM people WHERE person_id=?", (row["person_id"],))
                for pid, (enrolled, name, group) in incoming.items():
                    conn.execute("""INSERT INTO people(person_id,enrolled_at,name,group_name) VALUES (?,?,?,?)
                                    ON CONFLICT(person_id) DO UPDATE SET name=excluded.name, group_name=excluded.group_name""",
                                 (pid, enrolled, name, group))
                    if group:
                        conn.execute("INSERT OR IGNORE INTO classes(group_name) VALUES(?)", (group,))
            events = body.get("events") or []
            if not isinstance(events, list) or len(events) > 500:
                raise ValueError("bad events")
            known = {r[0] for r in conn.execute("SELECT person_id FROM people")}
            for e in events:
                pid, kind = int(e["person_id"]), e["kind"]
                if pid not in known or kind not in ("IN", "OUT"):
                    skipped += 1
                    continue
                at = parse_time(e["at"])
                cur = conn.execute("INSERT OR IGNORE INTO events(uid,person_id,at,kind,source) VALUES (?,?,?,?,?)",
                                   (f"{pid}|{at}|{kind}", pid, at, kind, str(e.get("source", ""))[:20]))
                accepted += cur.rowcount
                if cur.rowcount:
                    telegram.queue_event(conn, pid, cur.lastrowid, at, kind, str(e.get("source", "")))
            conn.execute("INSERT OR REPLACE INTO meta VALUES('last_sync', ?)", (str(time.time()),))
    except (KeyError, TypeError, ValueError):
        return jsonify(error="bad payload"), 400
    return jsonify(ok=True, accepted=accepted, skipped=skipped)


# ---------- просмотр ----------
@app.get("/api/day")
def api_day():
    if not logged_in():
        return jsonify(error="login"), 401
    day = request.args.get("date") or now().date().isoformat()
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return jsonify(error="bad date"), 400
    conn = db()
    people = conn.execute("SELECT * FROM people ORDER BY group_name, name").fetchall()
    by_person = {}
    for e in conn.execute("SELECT person_id, at, kind, source FROM events WHERE substr(at,1,10)=? ORDER BY at, id", (day,)):
        by_person.setdefault(e["person_id"], []).append({"at": e["at"][11:19], "kind": e["kind"], "source": e["source"]})
    rows, classes = [], {}
    for p in people:
        ev = by_person.get(p["person_id"], [])
        status = "absent" if not ev else ("inside" if ev[-1]["kind"] == "IN" else "left")
        first_in = next((x["at"] for x in ev if x["kind"] == "IN"), None)
        rows.append({"id": p["person_id"], "name": p["name"], "group": p["group_name"], "status": status,
                     "first_in": first_in, "last_at": ev[-1]["at"] if ev else None, "events": ev})
        c = classes.setdefault(p["group_name"], {"group": p["group_name"], "total": 0, "present": 0, "inside": 0, "left": 0})
        c["total"] += 1
        if status != "absent":
            c["present"] += 1
            c[status] += 1
    meta = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
    last = float(meta[0]) if meta else None
    return jsonify(date=day, people=rows, classes=list(classes.values()),
                   face_online=bool(last and time.time() - last < ONLINE_SECONDS),
                   last_sync=datetime.fromtimestamp(last, TZ).strftime("%d.%m %H:%M:%S") if last else None,
                   server_time=now().strftime("%H:%M:%S"))


# ---------- оценки классов ----------
def check_day(value):
    if not isinstance(value, str):
        raise ValueError("bad date")
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    if parsed.isoformat() != value:
        raise ValueError("bad date")
    return value


@app.get("/api/class-checks")
def api_class_checks():
    if not logged_in():
        return jsonify(error="login"), 401
    try:
        day = check_day(request.args.get("date", now().date().isoformat()))
    except ValueError:
        return jsonify(error="bad date"), 400
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    conn = db()
    rows = conn.execute("""SELECT *, uniform_score + cleanliness_score + student_count AS total_score
                           FROM class_checks WHERE day=?
                           ORDER BY total_score DESC, group_name""", (day,)).fetchall()
    groups = conn.execute("""SELECT group_name FROM classes
                             UNION SELECT group_name FROM people WHERE trim(group_name) != ''
                             UNION SELECT group_name FROM class_checks ORDER BY group_name""").fetchall()
    return jsonify(date=day, checks=[dict(row) for row in rows],
                   groups=[row[0] for row in groups], csrf_token=session["csrf"])


@app.post("/api/class-checks")
def save_class_check():
    if not logged_in():
        return jsonify(error="login"), 401
    token = session.get("csrf")
    if not token or not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), token):
        return jsonify(error="csrf"), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="bad payload"), 400
    try:
        day = check_day(body.get("date"))
        group = body.get("group")
        if not isinstance(group, str) or not 1 <= len(group.strip()) <= 60:
            raise ValueError("bad group")
        group = group.strip()
        scores = []
        for key, maximum in (("uniform_score", 10), ("cleanliness_score", 5), ("student_count", 1000)):
            value = body.get(key)
            if type(value) is not int or not 0 <= value <= maximum:
                raise ValueError("bad score")
            scores.append(value)
    except (TypeError, ValueError):
        return jsonify(error="bad payload"), 400
    conn = db()
    with conn:
        conn.execute("""INSERT INTO class_checks
                        (day,group_name,uniform_score,cleanliness_score,student_count,updated_at)
                        VALUES (?,?,?,?,?,?) ON CONFLICT(day,group_name) DO UPDATE SET
                        uniform_score=excluded.uniform_score,
                        cleanliness_score=excluded.cleanliness_score,
                        student_count=excluded.student_count, updated_at=excluded.updated_at""",
                     (day, group, *scores, now().isoformat(timespec="seconds")))
    return jsonify(ok=True, total_score=sum(scores))


# ---------- классы и Telegram ----------
def staff_write_error():
    if not logged_in():
        return jsonify(error="login"), 401
    token = session.get("csrf", "")
    sent = request.headers.get("X-CSRF-Token", "")
    if not token or not hmac.compare_digest(sent.encode(), token.encode()):
        return jsonify(error="csrf"), 403
    return None


@app.get("/api/classes")
def api_classes():
    if not logged_in():
        return jsonify(error="login"), 401
    conn = db()
    with conn:
        telegram.import_classes(conn)
    session.setdefault("csrf", secrets.token_urlsafe(32))
    rows = conn.execute("""SELECT c.id,c.group_name,c.teacher_name,c.language,c.notify_in,c.notify_out,c.report_time,c.report_days,
        c.chat_id IS NOT NULL AS telegram_connected,
        (SELECT count(*) FROM people p WHERE p.group_name=c.group_name) AS student_count,
        (SELECT count(*) FROM telegram_outbox q WHERE q.class_id=c.id AND q.status='pending') AS pending,
        (SELECT count(*) FROM telegram_outbox q WHERE q.class_id=c.id AND q.recipient_version=c.recipient_version AND q.status='failed') AS failed
        FROM classes c ORDER BY c.group_name""").fetchall()
    heartbeat = float(telegram.state(conn, "heartbeat", "0"))
    return jsonify(classes=[dict(r) for r in rows], csrf_token=session["csrf"], telegram={
        "username": telegram.state(conn, "username", ""),
        "online": time.time()-heartbeat < 60,
        "last_error": telegram.state(conn, "last_error", "")})


@app.post("/api/classes")
def save_class():
    error = staff_write_error()
    if error:
        return error
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="bad payload"), 400
    group, teacher = body.get("group"), body.get("teacher", "")
    report_time, report_days = body.get("report_time", ""), body.get("report_days", "weekdays")
    if report_days not in ("weekdays", "six_days", "daily") or not isinstance(report_time, str):
        return jsonify(error="bad schedule"), 400
    if report_time:
        try:
            if datetime.strptime(report_time, "%H:%M").strftime("%H:%M") != report_time:
                raise ValueError()
        except ValueError:
            return jsonify(error="bad schedule"), 400
    if (not isinstance(group, str) or not 1 <= len(group.strip()) <= 60
            or not isinstance(teacher, str) or len(teacher.strip()) > 120
            or body.get("language") not in ("ru", "kk")
            or type(body.get("notify_in")) is not bool or type(body.get("notify_out")) is not bool):
        return jsonify(error="bad payload"), 400
    conn = db()
    class_id = body.get("id")
    with conn:
        if class_id is not None:
            if type(class_id) is not int:
                return jsonify(error="bad id"), 400
            old = conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
            if not old:
                return jsonify(error="not found"), 404
            if old["group_name"] != group.strip():
                return jsonify(error="rename in Face"), 400
            if old["teacher_name"] != teacher.strip():
                telegram.disconnect(conn, class_id)
            conn.execute("""UPDATE classes SET teacher_name=?,language=?,notify_in=?,notify_out=?,report_time=?,report_days=? WHERE id=?""",
                         (teacher.strip(), body["language"], body["notify_in"], body["notify_out"], report_time, report_days, class_id))
        else:
            try:
                cur = conn.execute("""INSERT INTO classes(group_name,teacher_name,language,notify_in,notify_out,report_time,report_days)
                                      VALUES(?,?,?,?,?,?,?)""", (group.strip(), teacher.strip(), body["language"], body["notify_in"], body["notify_out"], report_time, report_days))
            except sqlite3.IntegrityError:
                return jsonify(error="class exists"), 409
            class_id = cur.lastrowid
    return jsonify(ok=True, id=class_id)


@app.post("/api/classes/<int:class_id>/telegram/<action>")
def class_telegram_action(class_id, action):
    error = staff_write_error()
    if error:
        return error
    conn = db()
    group = conn.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
    if not group:
        return jsonify(error="not found"), 404
    with conn:
        if action == "link":
            username = telegram.state(conn, "username", "")
            if not username:
                return jsonify(error="telegram unconfigured"), 409
            token = telegram.issue_link(conn, class_id)
            return jsonify(ok=True, url=f"https://t.me/{username}?start={token}", expires_in=3600)
        if action == "disconnect":
            telegram.disconnect(conn, class_id)
            return jsonify(ok=True)
        if action == "report":
            if not group["chat_id"]:
                return jsonify(error="not connected"), 409
            day = now().date().isoformat()
            telegram.enqueue(conn, "manual:"+secrets.token_hex(16), group["chat_id"],
                             telegram.report_text(conn, group, day), group=group, kind="report")
            return jsonify(ok=True)
    return jsonify(error="not found"), 404


LOGIN = (BASE / "static" / "login.html").read_text(encoding="utf-8")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), VIEW_PASSWORD):
            session.permanent = True
            session["ok"] = True
            session["csrf"] = secrets.token_urlsafe(32)
            return redirect("/")
        time.sleep(1.5)
        return LOGIN.replace("{{ERR}}", "<div class=e role=alert data-ui=loginError>Неверный пароль</div>"), 401
    return LOGIN.replace("{{ERR}}", "")


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.get("/")
def index():
    if not logged_in():
        return redirect("/login")
    return send_from_directory(BASE / "static", "index.html")


@app.get("/class-checks")
def class_checks_page():
    if not logged_in():
        return redirect("/login")
    return send_from_directory(BASE / "static", "class-checks.html")


@app.get("/classes")
def classes_page():
    if not logged_in():
        return redirect("/login")
    return send_from_directory(BASE / "static", "classes.html")


@app.get("/assets/<path:filename>")
def assets(filename):
    return send_from_directory(BASE / "static" / "assets", filename)


@app.after_request
def headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
