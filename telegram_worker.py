"""Run ONE process alongside server.py, using the same DB_PATH."""
import json
import getpass
import os
import re
import secrets
import sys
import time
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import telegram_support as tg


class TelegramError(Exception):
    def __init__(self, code=0, retry_after=0):
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Telegram API error {code}")  # Never expose the token URL.


class TelegramAPI:
    def __init__(self, token):
        self.token = token

    def call(self, method, **payload):
        req = Request(f"https://api.telegram.org/bot{self.token}/{method}",
                      data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=20) as response:
                body = json.load(response)
        except HTTPError as error:
            try:
                body = json.load(error)
            except (ValueError, OSError):
                raise TelegramError(error.code) from None
        except (URLError, OSError, ValueError):
            raise TelegramError() from None
        if not body.get("ok"):
            raise TelegramError(body.get("error_code", 0), body.get("parameters", {}).get("retry_after", 0))
        return body["result"]


class Worker:
    def __init__(self, conn, api):
        self.conn, self.api = conn, api
        self.owner = secrets.token_hex(16)

    def lease(self):
        stamp = time.time()
        with self.conn:
            self.conn.execute("""INSERT INTO telegram_worker_lock VALUES(1,?,?)
                ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,until_at=excluded.until_at
                WHERE telegram_worker_lock.until_at < ? OR telegram_worker_lock.owner=?""",
                (self.owner, stamp+60, stamp, self.owner))
            owner = self.conn.execute("SELECT owner FROM telegram_worker_lock WHERE id=1").fetchone()[0]
            if owner != self.owner:
                raise RuntimeError("Другой процесс Telegram уже работает с этой базой.")
            tg.set_state(self.conn, "heartbeat", stamp)

    def deliver(self):
        if float(tg.state(self.conn, "send_after", "0")) > time.time():
            return
        jobs = self.conn.execute("""SELECT * FROM telegram_outbox q WHERE status='pending' AND next_attempt<=?
            AND NOT EXISTS(SELECT 1 FROM telegram_outbox s WHERE s.chat_id=q.chat_id AND s.sent_at>?)
            ORDER BY id LIMIT 20""", (time.time(), time.time()-1.1)).fetchall()
        sent_chats = set()
        for job in jobs:
            if job["chat_id"] in sent_chats:
                continue
            self.lease()
            group = self.conn.execute("SELECT * FROM classes WHERE id=?", (job["class_id"],)).fetchone() if job["class_id"] else None
            valid = True
            if job["class_id"]:
                valid = bool(group and group["chat_id"] == job["chat_id"] and group["recipient_version"] == job["recipient_version"])
                if valid and job["kind"] in ("IN", "OUT"):
                    valid = bool(group["notify_in" if job["kind"] == "IN" else "notify_out"])
                    person = self.conn.execute("SELECT * FROM people WHERE person_id=?", (job["person_id"],)).fetchone()
                    valid = valid and bool(person and person["enrolled_at"] == job["enrolled_at"] and person["group_name"] == group["group_name"])
            if not valid:
                with self.conn:
                    self.conn.execute("UPDATE telegram_outbox SET status='cancelled' WHERE id=?", (job["id"],))
                continue
            # A disconnect made before this recheck must not deliver an already cancelled job.
            if self.conn.execute("SELECT status FROM telegram_outbox WHERE id=?", (job["id"],)).fetchone()[0] != "pending":
                continue
            try:
                self.api.call("sendMessage", chat_id=job["chat_id"], text=job["text"], protect_content=True)
            except TelegramError as error:
                permanent = error.code in (400, 401, 403)
                delay = max(error.retry_after, min(3600, 5 * 2 ** min(job["attempts"], 9)))
                message = f"Telegram {error.code or 'network'}"
                with self.conn:
                    self.conn.execute("""UPDATE telegram_outbox SET attempts=attempts+1,status=?,
                        next_attempt=?,last_error=? WHERE id=?""",
                        ("failed" if permanent else "pending", time.time()+delay, message, job["id"]))
                if error.code == 429:
                    with self.conn:
                        tg.set_state(self.conn, "send_after", time.time()+max(1, error.retry_after))
                    break
            else:
                with self.conn:
                    self.conn.execute("UPDATE telegram_outbox SET status='sent',sent_at=?,last_error=NULL WHERE id=?", (time.time(), job["id"]))
            sent_chats.add(job["chat_id"])

    def schedule_reports(self):
        moment = datetime.now(tg.TZ)
        day = moment.date().isoformat()
        for group in self.conn.execute("SELECT * FROM classes WHERE chat_id IS NOT NULL AND report_time != ''").fetchall():
            if group["report_time"] > moment.strftime("%H:%M"):
                continue
            if group["report_days"] == "weekdays" and moment.weekday() >= 5:
                continue
            if group["report_days"] == "six_days" and moment.weekday() >= 6:
                continue
            key = f"scheduled:{group['id']}:{day}:{group['recipient_version']}"
            if not self.conn.execute("SELECT 1 FROM telegram_outbox WHERE dedupe_key=?", (key+":0",)).fetchone():
                with self.conn:
                    tg.enqueue(self.conn, key, group["chat_id"], tg.report_text(self.conn, group, day), group=group, kind="report")

    def poll(self):
        self.lease()
        updates = self.api.call("getUpdates", offset=int(tg.state(self.conn, "offset", "0")),
                                timeout=5, limit=100, allowed_updates=["message"])
        for update in updates:
            with self.conn:
                tg.process_update(self.conn, update)

    def run(self):
        self.lease()
        me = self.api.call("getMe")
        hook = self.api.call("getWebhookInfo")
        if hook.get("url"):
            raise RuntimeError("У бота включён webhook. Создайте отдельного бота или отключите его webhook перед запуском.")
        with self.conn:
            tg.set_state(self.conn, "username", me["username"])
        print(f"Telegram worker: @{me['username']}. Для остановки нажмите Ctrl+C.", flush=True)
        while True:
            try:
                self.lease()
                self.schedule_reports()
                self.deliver()
                self.poll()
                with self.conn:
                    tg.set_state(self.conn, "last_error", "")
            except TelegramError as error:
                with self.conn:
                    tg.set_state(self.conn, "last_error", f"Telegram {error.code or 'network'}")
                if error.code in (401, 409):
                    raise RuntimeError("Проверьте токен и убедитесь, что запущен только один Telegram worker.") from None
                time.sleep(min(30, max(3, error.retry_after)))


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token and "--prompt" in sys.argv:
        token = getpass.getpass("Telegram bot token (BotFather): ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        sys.exit("Укажите TELEGRAM_BOT_TOKEN от @BotFather. См. TELEGRAM_SETUP_RU.md")
    conn = tg.connect()
    conn.executescript(tg.SCHEMA)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='people'").fetchone():
        sys.exit("Сначала запустите сайт server.py с тем же DB_PATH.")
    worker = Worker(conn, TelegramAPI(token))
    try:
        worker.run()
    except KeyboardInterrupt:
        pass
    except (RuntimeError, TelegramError) as error:
        print(str(error), file=sys.stderr)
    finally:
        with conn:
            conn.execute("DELETE FROM telegram_worker_lock WHERE owner=?", (worker.owner,))
            # Do not change another worker's heartbeat if this process failed to acquire its lease.
            if not conn.execute("SELECT 1 FROM telegram_worker_lock").fetchone():
                tg.set_state(conn, "heartbeat", 0)
        conn.close()


if __name__ == "__main__":
    main()
