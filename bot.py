"""News bot for Bothost: Telegram webhook and secure Gmail ingest."""
import hmac
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import requests
from flask import Flask, jsonify, request

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("news-bot")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
INGEST_SECRET = os.getenv("INGEST_SECRET", "").strip()
ALLOWED_IDS = {
    int(part.strip()) for part in os.getenv("ALLOWED_IDS", "").split(",")
    if part.strip().isdigit()
}
DB_PATH = Path(os.getenv("DB_PATH", "/app/data/news_bot.sqlite3"))
BASE = "https://api.telegram.org/bot" + BOT_TOKEN + "/"

if not BOT_TOKEN or not WEBHOOK_SECRET or not INGEST_SECRET:
    raise RuntimeError("Задайте BOT_TOKEN, WEBHOOK_SECRET, INGEST_SECRET в Bothost")

app = Flask(__name__)


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=15)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with closing(db()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            chem INTEGER NOT NULL DEFAULT 1,
            news INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT UNIQUE NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            kind TEXT NOT NULL,
            source_date TEXT NOT NULL,
            imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS deliveries (
            news_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            PRIMARY KEY (news_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS updates_seen (
            update_id INTEGER PRIMARY KEY
        );
        CREATE TABLE IF NOT EXISTS ui_messages (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (chat_id, message_id)
        );
        """)
        con.commit()


def api(method, **data):
    try:
        response = requests.post(BASE + method, json=data, timeout=18)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise RuntimeError("Ошибка связи с Telegram: " + str(exc)) from exc
    if not payload.get("ok"):
        raise RuntimeError("Telegram " + method + ": " + str(payload.get("description")))
    return payload.get("result")


def btn(title, command, style=None):
    button = {"text": title, "callback_data": command}
    if style:
        button["style"] = style
    return button


def keyboard(rows):
    return {"inline_keyboard": rows}


def main_keys(chat_id):
    with closing(db()) as con:
        subscriber = con.execute(
            "SELECT * FROM subscribers WHERE chat_id=?", (chat_id,)
        ).fetchone()
    if not subscriber:
        return keyboard([[btn("🏠 Меню", "home", "primary")]])
    state = "🔔 Вкл" if subscriber["enabled"] else "🔕 Выкл"
    return keyboard([
        [btn("📰 Последний обзор", "last", "primary")],
        [btn("📚 Архив", "arc:0"), btn("🔄 Обновить", "last", "success")],
        [btn(state, "notify", "success" if subscriber["enabled"] else "danger"),
         btn("⚙️ Темы", "topics")],
        [btn("ℹ️ Статус", "status")],
    ])


def remember_ui(chat_id, sent):
    """Remember only interactive messages. Scheduled news remain untouched."""
    if not isinstance(sent, dict) or not sent.get("message_id"):
        return
    with closing(db()) as con:
        con.execute(
            "INSERT OR IGNORE INTO ui_messages (chat_id,message_id) VALUES (?,?)",
            (chat_id, int(sent["message_id"]))
        )
        con.commit()


def clear_ui(chat_id):
    """Clean old menu and manually viewed reports on every navigation.

    Telegram can refuse deleting messages older than 48 hours. In that case
    try to remove inline buttons. Never delete scheduled news notifications.
    """
    with closing(db()) as con:
        rows = con.execute(
            "SELECT message_id FROM ui_messages WHERE chat_id=? ORDER BY message_id",
            (chat_id,)
        ).fetchall()
    for row in rows:
        message_id = row["message_id"]
        try:
            api("deleteMessage", chat_id=chat_id, message_id=message_id)
        except Exception as exc:
            log.info("Cannot delete old menu %s/%s: %s", chat_id, message_id, exc)
            try:
                api("editMessageReplyMarkup", chat_id=chat_id,
                    message_id=message_id, reply_markup={"inline_keyboard": []})
            except Exception:
                pass
        finally:
            with closing(db()) as con:
                con.execute("DELETE FROM ui_messages WHERE chat_id=? AND message_id=?",
                            (chat_id, message_id))
                con.commit()


def send(chat_id, text, keys=None, track=False):
    payload = {
        "chat_id": chat_id, "text": text, "disable_web_page_preview": True
    }
    if keys is not None:
        payload["reply_markup"] = keys
    result = api("sendMessage", **payload)
    if track:
        remember_ui(chat_id, result)
    return result


def split_text(text, max_size=3400):
    remaining = str(text).strip()
    while remaining:
        if len(remaining) <= max_size:
            yield remaining
            break
        cut = remaining.rfind("\n", 1000, max_size)
        if cut == -1:
            cut = max_size
        yield remaining[:cut].rstrip()
        remaining = remaining[cut:].lstrip("\n")


def send_long(chat_id, text, track=False):
    chunks = list(split_text(text)) or ["(Пустое сообщение)"]
    for index, chunk in enumerate(chunks):
        send(chat_id, chunk,
             main_keys(chat_id) if index == len(chunks) - 1 else None,
             track=track)


def message_text(row):
    return f"📰 {row['subject']}\n📅 {row['source_date']}\n\n{row['body']}"


def authorized(user_id):
    return user_id in ALLOWED_IDS


def register_user(chat_id, user_id):
    if chat_id != user_id or not authorized(user_id):
        return False
    with closing(db()) as con:
        con.execute(
            "INSERT OR IGNORE INTO subscribers (chat_id,user_id) VALUES (?,?)",
            (chat_id, user_id)
        )
        con.commit()
    return True


def answer_panel(chat_id, text, keys=None, callback=None):
    # Previous interactive messages are removed in process_update().
    # Always send a new, tracked panel to keep the chat free of stale menus.
    send(chat_id, text, keys or main_keys(chat_id), track=True)


def latest(chat_id):
    with closing(db()) as con:
        row = con.execute("SELECT * FROM news ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        send_long(chat_id, message_text(row), track=True)
    else:
        send(chat_id, "Обзоров пока нет. Проверьте настройки Gmail.",
             main_keys(chat_id), track=True)


def archive(chat_id, page, callback):
    page = max(0, min(page, 10000))
    with closing(db()) as con:
        count = con.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        last_page = max(0, (count - 1) // 5)
        page = min(page, last_page)
        rows = con.execute(
            "SELECT id,subject,source_date FROM news ORDER BY id DESC LIMIT 5 OFFSET ?",
            (page * 5,)
        ).fetchall()
    if not rows:
        return answer_panel(chat_id, "📚 Архив пуст.", callback=callback)
    buttons = [
        [btn(row["source_date"][:10] + " · " + row["subject"][:32],
             "read:" + str(row["id"]))]
        for row in rows
    ]
    navigation = []
    if page:
        navigation.append(btn("◀ Назад", f"arc:{page-1}"))
    if page < last_page:
        navigation.append(btn("Вперёд ▶", f"arc:{page+1}"))
    if navigation:
        buttons.append(navigation)
    buttons.append([btn("🏠 Меню", "home", "primary")])
    answer_panel(
        chat_id, f"📚 Архив\nСтраница {page+1} из {last_page+1}",
        keyboard(buttons), callback
    )


def topics(chat_id, callback):
    with closing(db()) as con:
        sub = con.execute(
            "SELECT * FROM subscribers WHERE chat_id=?", (chat_id,)
        ).fetchone()
    keys = keyboard([
        [btn(("✅ " if sub["chem"] else "⬜ ") + "Рынок бытовой химии", "chem")],
        [btn(("✅ " if sub["news"] else "⬜ ") + "Новости", "news")],
        [btn("🏠 Меню", "home", "primary")],
    ])
    answer_panel(chat_id, "⚙️ Выберите темы уведомлений", keys, callback)


def status(chat_id, callback):
    with closing(db()) as con:
        total = con.execute("SELECT COUNT(*) FROM news").fetchone()[0]
        recent = con.execute(
            "SELECT imported_at FROM news ORDER BY id DESC LIMIT 1"
        ).fetchone()
    last = recent[0] if recent else "пока нет писем"
    answer_panel(
        chat_id,
        f"ℹ️ Статус\nОбзоров в архиве: {total}\n"
        f"Последнее поступление: {last}\n"
        "Почта проверяется раз в 5 минут (Google Apps Script).",
        callback=callback
    )


def process_update(update):
    callback = update.get("callback_query")
    message = (callback or {}).get("message") or update.get("message") or {}
    user = (callback or {}).get("from") or message.get("from") or {}
    user_id = user.get("id")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not chat_id or not user_id or chat.get("type") != "private":
        return
    if not authorized(user_id):
        if not callback:
            send(chat_id, "Доступ закрыт. Ваш Telegram ID: " + str(user_id) +
                 "\nПередайте его администратору бота.")
        else:
            api("answerCallbackQuery", callback_query_id=callback["id"],
                text="Нет доступа", show_alert=True)
        return
    if not register_user(chat_id, user_id):
        return
    if callback:
        api("answerCallbackQuery", callback_query_id=callback["id"])
        clear_ui(chat_id)
        action = callback.get("data") or ""
        if action == "home":
            answer_panel(chat_id, "🧭 Новости и аналитика\nВыберите действие:", callback=callback)
        elif action == "last":
            latest(chat_id)
        elif action == "status":
            status(chat_id, callback)
        elif action == "topics":
            topics(chat_id, callback)
        elif action == "notify":
            with closing(db()) as con:
                con.execute(
                    "UPDATE subscribers SET enabled=1-enabled WHERE chat_id=?",
                    (chat_id,)
                )
                con.commit()
            answer_panel(chat_id, "🔔 Настройка уведомлений обновлена.", callback=callback)
        elif action in ("chem", "news"):
            with closing(db()) as con:
                con.execute(
                    f"UPDATE subscribers SET {action}=1-{action} WHERE chat_id=?",
                    (chat_id,)
                )
                con.commit()
            topics(chat_id, callback)
        elif action.startswith("arc:"):
            try:
                page = int(action[4:])
            except ValueError:
                page = 0
            archive(chat_id, page, callback)
        elif action.startswith("read:"):
            try:
                news_id = int(action[5:])
            except ValueError:
                return
            with closing(db()) as con:
                item = con.execute(
                    "SELECT * FROM news WHERE id=?", (news_id,)
                ).fetchone()
            if item:
                send_long(chat_id, message_text(item), track=True)
        return

    clear_ui(chat_id)
    if str(message.get("text", "")).startswith("/start"):
        send(chat_id, "✅ Бот подключён.\nВыберите действие:",
             main_keys(chat_id), track=True)
    else:
        send(chat_id, "Выберите действие:", main_keys(chat_id), track=True)


@app.get("/health")
def health():
    return jsonify({"status": "ok", "telegram_webhook": True})


@app.post("/webhook")
def telegram_webhook():
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(secret, WEBHOOK_SECRET):
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("update_id"), int):
        return jsonify({"error": "bad update"}), 400
    update_id = data["update_id"]
    with closing(db()) as con:
        found = con.execute(
            "SELECT 1 FROM updates_seen WHERE update_id=?", (update_id,)
        ).fetchone()
        if found:
            return jsonify({"ok": True, "duplicate": True})
        con.execute(
            "INSERT INTO updates_seen (update_id) VALUES (?)", (update_id,)
        )
        con.commit()
    try:
        process_update(data)
    except Exception:
        log.exception("Ошибка Telegram update_id=%s", update_id)
        return jsonify({"ok": False, "error": "handler failed"}), 500
    return jsonify({"ok": True})


def kind_for(subject):
    lowered = str(subject).lower()
    if "рынок бытовой химии" in lowered:
        return "chem"
    if "новости" in lowered:
        return "news"
    return None


@app.post("/ingest")
def ingest():
    secret = request.headers.get("X-Ingest-Secret", "")
    if not hmac.compare_digest(secret, INGEST_SECRET):
        return jsonify({"error": "unauthorized"}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON expected"}), 400
    source_id = str(data.get("source_id", ""))[:256].strip()
    subject = str(data.get("subject", ""))[:300].strip()
    body = str(data.get("body", ""))[:120000].strip()
    source_date = str(data.get("source_date", ""))[:64].strip()
    links = data.get("links", [])
    is_test = bool(data.get("test", False))
    kind = kind_for(subject)
    if not source_id or not subject or not kind:
        return jsonify({"error": "source_id or recognized subject required"}), 400
    if isinstance(links, list) and links:
        urls = [
            str(x) for x in links[:25]
            if isinstance(x, str) and x.startswith(("https://", "http://"))
        ]
        urls = [x for x in urls if x not in body]
        if urls:
            body += "\n\n🔗 Ссылки из письма:\n" + "\n".join(urls)
    with closing(db()) as con:
        result = con.execute(
            "INSERT OR IGNORE INTO news (source_id,subject,body,kind,source_date)"
            " VALUES (?,?,?,?,?)",
            (source_id, subject, body or "(письмо без текста)", kind, source_date)
        )
        is_new = result.rowcount > 0
        item = con.execute(
            "SELECT * FROM news WHERE source_id=?", (source_id,)
        ).fetchone()
        if is_new:
            for subscriber in con.execute("SELECT * FROM subscribers").fetchall():
                status_value = (
                    "pending" if subscriber["enabled"] and subscriber[kind] else "skipped"
                )
                con.execute(
                    "INSERT OR IGNORE INTO deliveries (news_id,chat_id,status)"
                    " VALUES (?,?,?)",
                    (item["id"], subscriber["chat_id"], status_value)
                )
        con.commit()
        if is_test:
            recipients = con.execute("SELECT chat_id FROM subscribers").fetchall()
        else:
            recipients = con.execute(
                "SELECT chat_id FROM deliveries WHERE news_id=? AND status='pending'",
                (item["id"],)
            ).fetchall()
    sent = 0
    failures = []
    for recipient in recipients:
        chat_id = recipient["chat_id"]
        try:
            send_long(chat_id, ("🧪 Тестовая пересылка\n\n" if is_test else "") +
                      message_text(item))
            if not is_test:
                with closing(db()) as con:
                    con.execute(
                        "UPDATE deliveries SET status='delivered'"
                        " WHERE news_id=? AND chat_id=?",
                        (item["id"], chat_id)
                    )
                    con.commit()
            sent += 1
        except Exception:
            log.exception("Не удалось доставить новость пользователю %s", chat_id)
            failures.append(str(chat_id))
    if failures:
        return jsonify({"ok": False, "sent": sent, "failed_chat_ids": failures}), 503
    return jsonify({"ok": True, "sent": sent, "news_id": item["id"],
                    "duplicate": not is_new})


def webhook_address():
    url = os.getenv("WEBHOOK_URL", "").strip() or os.getenv("PUBLIC_URL", "").strip()
    if not url:
        return ""
    if not url.startswith("https://"):
        raise RuntimeError("WEBHOOK_URL / PUBLIC_URL должен начинаться с https://")
    base = url.rstrip("/")
    return base if base.endswith("/webhook") else base + "/webhook"


def setup_webhook():
    address = webhook_address()
    if not address:
        log.warning("Нет WEBHOOK_URL или PUBLIC_URL: Telegram webhook не включён")
        return
    result = api("setWebhook", url=address, secret_token=WEBHOOK_SECRET,
                 allowed_updates=["message", "callback_query"],
                 drop_pending_updates=False)
    log.info("Telegram webhook установлен: %s (%s)", address, result)


if __name__ == "__main__":
    init_db()
    try:
        setup_webhook()
    except Exception:
        log.exception("Не удалось установить Telegram webhook")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "3000")), threaded=True)
