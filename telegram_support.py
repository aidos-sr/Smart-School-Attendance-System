"""Shared SQLite storage for class settings and a durable Telegram delivery queue."""
import hashlib
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=5))
DB_PATH = os.environ.get("DB_PATH", str(Path(__file__).resolve().parent / "web.sqlite3"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS classes(
 id INTEGER PRIMARY KEY, group_name TEXT NOT NULL UNIQUE, teacher_name TEXT NOT NULL DEFAULT '',
 language TEXT NOT NULL DEFAULT 'ru', report_time TEXT NOT NULL DEFAULT '',
 report_days TEXT NOT NULL DEFAULT 'weekdays', notify_in INTEGER NOT NULL DEFAULT 1,
 notify_out INTEGER NOT NULL DEFAULT 0, chat_id TEXT, recipient_version INTEGER NOT NULL DEFAULT 0,
 notify_since REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS telegram_links(
 token_hash TEXT PRIMARY KEY, class_id INTEGER NOT NULL REFERENCES classes(id), expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS telegram_outbox(
 id INTEGER PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE, class_id INTEGER REFERENCES classes(id),
 recipient_version INTEGER, chat_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
 person_id INTEGER, enrolled_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
 last_error TEXT, created_at REAL NOT NULL, sent_at REAL);
CREATE INDEX IF NOT EXISTS telegram_queue ON telegram_outbox(status,next_attempt);
CREATE INDEX IF NOT EXISTS telegram_chat_sent ON telegram_outbox(chat_id,sent_at);
CREATE TABLE IF NOT EXISTS telegram_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS telegram_worker_lock(id INTEGER PRIMARY KEY,owner TEXT NOT NULL,until_at REAL NOT NULL);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def state(conn, key, default=None):
    row = conn.execute("SELECT value FROM telegram_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO telegram_state VALUES(?,?)", (key, str(value)))


def import_classes(conn):
    conn.execute("""INSERT OR IGNORE INTO classes(group_name)
                    SELECT group_name FROM people WHERE trim(group_name) != ''
                    UNION SELECT group_name FROM class_checks WHERE trim(group_name) != ''""")


def disconnect(conn, class_id):
    conn.execute("""UPDATE classes SET chat_id=NULL,recipient_version=recipient_version+1,
                    notify_since=0 WHERE id=?""", (class_id,))
    conn.execute("DELETE FROM telegram_links WHERE class_id=?", (class_id,))
    conn.execute("UPDATE telegram_outbox SET status='cancelled' WHERE class_id=? AND status='pending'", (class_id,))


def issue_link(conn, class_id):
    # Issuing a replacement link immediately revokes the previous recipient.
    disconnect(conn, class_id)
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO telegram_links VALUES(?,?,?)",
                 (hashlib.sha256(token.encode()).hexdigest(), class_id, time.time() + 3600))
    return token


def enqueue(conn, key, chat_id, text, *, group=None, kind="service", person=None):
    # Short plain-text chunks also stay below Telegram's UTF-16 text limit.
    for part, offset in enumerate(range(0, len(text), 1800)):
        conn.execute("""INSERT OR IGNORE INTO telegram_outbox
            (dedupe_key,class_id,recipient_version,chat_id,kind,text,person_id,enrolled_at,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (f"{key}:{part}", group["id"] if group else None,
             group["recipient_version"] if group else None, str(chat_id), kind, text[offset:offset+1800],
             person["person_id"] if person else None, person["enrolled_at"] if person else None, time.time()))


def queue_event(conn, person_id, event_id, at, kind, source):
    person = conn.execute("SELECT * FROM people WHERE person_id=?", (person_id,)).fetchone()
    group = conn.execute("SELECT * FROM classes WHERE group_name=?", (person["group_name"],)).fetchone()
    if not group or not group["chat_id"] or not group["notify_in" if kind == "IN" else "notify_out"]:
        return
    moment = datetime.fromisoformat(at)
    if moment.timestamp() < group["notify_since"]:
        return  # Do not send the old Face archive when a teacher first connects.
    stamp = moment.strftime("%d.%m.%Y %H:%M:%S")
    if group["language"] == "kk":
        action = "Мектепке келді" if kind == "IN" else "Мектептен шықты"
        text = f"{action}\n{person['name']} · {group['group_name']}\n{stamp} (UTC+5)\nКамера белгісі бойынша."
        if source == "manual":
            text += " Белгі қолмен қойылған."
    else:
        action = "Пришёл в школу" if kind == "IN" else "Ушёл из школы"
        text = f"{action}\n{person['name']} · {group['group_name']}\n{stamp} (UTC+5)\nПо отметке системы."
        if source == "manual":
            text += " Отметка внесена вручную."
    enqueue(conn, f"event:{event_id}:{person['enrolled_at']}:{at}:{kind}", group["chat_id"], text, group=group, kind=kind, person=person)


def report_text(conn, group, day):
    rows = conn.execute("""SELECT p.name,
        (SELECT min(e.at) FROM events e WHERE e.person_id=p.person_id AND e.kind='IN'
         AND substr(e.at,1,10)=?) AS first_in,
        (SELECT e.kind FROM events e WHERE e.person_id=p.person_id AND substr(e.at,1,10)=?
         ORDER BY e.at DESC,e.id DESC LIMIT 1) AS last_kind,
        (SELECT e.at FROM events e WHERE e.person_id=p.person_id AND substr(e.at,1,10)=?
         ORDER BY e.at DESC,e.id DESC LIMIT 1) AS last_at
        FROM people p WHERE p.group_name=? ORDER BY p.name""", (day, day, day, group["group_name"])).fetchall()
    kk = group["language"] == "kk"
    arrived = sum(bool(r["first_in"]) for r in rows)
    lines = [f"{group['group_name']} · {day}",
             (f"Келгені: {arrived}/{len(rows)}" if kk else f"Пришли: {arrived}/{len(rows)}")]
    lines.append(("Құрастырылды: " if kk else "Сформировано: ") + datetime.now(TZ).strftime("%H:%M:%S"))
    for r in rows:
        if r["first_in"]:
            status = ("Келді " if kk else "Пришёл ") + r["first_in"][11:19]
        else:
            status = "Кіру белгісі жоқ" if kk else "Нет отметки прихода"
        if r["last_kind"] == "OUT":
            status += ("; шықты " if kk else "; ушёл ") + r["last_at"][11:19]
        lines.append(f"{r['name']} — {status}")
    if not rows:
        lines.append("Face жүйесінен оқушыларды синхрондаңыз." if kk else "Синхронизируйте список учеников из Face.")
    last_sync = conn.execute("SELECT value FROM meta WHERE key='last_sync'").fetchone()
    if last_sync and time.time()-float(last_sync[0]) < 90:
        lines.append("Face байланыста." if kk else "Face на связи.")
    else:
        lines.append("Face байланыста емес: деректер кешігуі мүмкін." if kk else "Face не на связи: данные могут запаздывать.")
    lines.append("UTC+5. Кіру белгісінің болмауы оқушының келмегенін дәлелдемейді." if kk
                 else "UTC+5. Отсутствие отметки не подтверждает отсутствие ученика.")
    return "\n".join(lines)


def process_update(conn, update):
    """Apply a Telegram update and advance its cursor in the same transaction."""
    uid = update.get("update_id")
    if type(uid) is not int or uid < int(state(conn, "offset", "0")):
        return
    set_state(conn, "offset", uid + 1)
    message = update.get("message", {})
    chat = message.get("chat", {})
    if chat.get("type") != "private" or type(chat.get("id")) is not int:
        return  # No student information or pairing in group chats.
    chat_id = str(chat["id"])
    text = message.get("text", "")
    if not isinstance(text, str):
        return
    command, _, argument = text.partition(" ")
    command = command.split("@")[0].lower()
    key = f"update:{uid}"
    if command == "/start" and argument:
        digest = hashlib.sha256(argument.strip().encode()).hexdigest()
        link = conn.execute("SELECT * FROM telegram_links WHERE token_hash=? AND expires_at>?", (digest, time.time())).fetchone()
        if not link:
            enqueue(conn, key, chat_id, "Ссылка недействительна или истекла. Попросите администратора создать новую ссылку для вашего класса.")
            return
        conn.execute("""UPDATE classes SET chat_id=?,recipient_version=recipient_version+1,notify_since=? WHERE id=?""",
                     (chat_id, int(time.time()), link["class_id"]))
        conn.execute("DELETE FROM telegram_links WHERE class_id=?", (link["class_id"],))
        group = conn.execute("SELECT * FROM classes WHERE id=?", (link["class_id"],)).fetchone()
        reply = (f"{group['group_name']} сыныбы қосылды. /today — бүгінгі қатысу; /stop — ажырату."
                 if group["language"] == "kk" else f"Класс {group['group_name']} подключён. /today — сводка за сегодня; /stop — отключить.")
        enqueue(conn, key, chat_id, reply, group=group)
    elif command == "/today":
        groups = conn.execute("SELECT * FROM classes WHERE chat_id=?", (chat_id,)).fetchall()
        for group in groups:
            enqueue(conn, f"{key}:{group['id']}", chat_id,
                    report_text(conn, group, datetime.now(TZ).date().isoformat()), group=group, kind="report")
        if not groups:
            enqueue(conn, key, chat_id, "Класс не подключён. Получите личную ссылку подключения у администратора сайта.")
    elif command == "/stop":
        for group in conn.execute("SELECT id FROM classes WHERE chat_id=?", (chat_id,)).fetchall():
            disconnect(conn, group["id"])
        enqueue(conn, key, chat_id, "Уведомления отключены. Для нового подключения нужна новая ссылка администратора. / Хабарламалар өшірілді.")
    else:
        enqueue(conn, key, chat_id, "School Face\n/start — подключение по личной ссылке администратора\n/today — кто пришёл и во сколько / бүгінгі қатысу\n/stop — отключить уведомления / хабарламаларды өшіру\n/help — помощь")
