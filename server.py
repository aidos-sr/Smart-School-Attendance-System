"""School Face · сайт присутствия. Принимает отметки от программы Face и показывает их онлайн."""
import hmac
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, g, jsonify, redirect, request, send_from_directory, session

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
                    return jsonify(error="bad people"), 400
                incoming = {}
                for p in people:
                    pid = int(p["id"])
                    name, group = str(p["name"]).strip()[:120], str(p["group"]).strip()[:60]
                    if not name:
                        return jsonify(error="empty name"), 400
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
            events = body.get("events") or []
            if not isinstance(events, list) or len(events) > 500:
                return jsonify(error="bad events"), 400
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


LOGIN = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Вход · School Face</title><style>
body{font-family:system-ui,Arial;background:#eaf2ff;display:grid;place-items:center;min-height:100vh;margin:0}
form{background:#fff;padding:32px;border-radius:16px;box-shadow:0 8px 30px #1b4f9c33;width:min(340px,90vw)}
h1{margin:0 0 4px;color:#123f8c;font-size:22px}p{color:#5a6b85;margin:0 0 18px;font-size:14px}
input,button{width:100%;box-sizing:border-box;padding:12px;font-size:16px;border-radius:10px;border:1px solid #b9c8e3}
button{background:#1d5fd0;color:#fff;border:0;margin-top:12px;cursor:pointer}.e{color:#b3261e;margin-top:10px;font-size:14px}
</style><form method=post><h1>School Face</h1><p>Присутствие учеников · доступ для сотрудников</p>
<input type=password name=password placeholder=Пароль autofocus required><button>Войти</button>{{ERR}}</form>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), VIEW_PASSWORD):
            session.permanent = True
            session["ok"] = True
            return redirect("/")
        time.sleep(1.5)
        return LOGIN.replace("{{ERR}}", "<div class=e>Неверный пароль</div>"), 401
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


@app.after_request
def headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    return resp


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
