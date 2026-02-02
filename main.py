import os
import time
import random
import threading
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

# ================= CONFIG =================
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
KZ_TZ = timezone(timedelta(hours=5))

# ================= DATABASE =================
DB = "data.sqlite3"
db_lock = threading.Lock()

def db():
    return sqlite3.connect(DB, check_same_thread=False)

def init_db():
    with db_lock, db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            event TEXT,
            value TEXT,
            created_at TEXT
        )
        """)
        c.commit()

def log(chat_id: int, event: str, value: Optional[str] = None):
    with db_lock, db() as c:
        c.execute(
            "INSERT INTO logs(chat_id,event,value,created_at) VALUES(?,?,?,?)",
            (chat_id, event, value, datetime.now(KZ_TZ).isoformat())
        )
        c.commit()

# ================= STATE =================
sessions: Dict[int, Dict[str, Any]] = {}  # chat_id -> dict
timers: Dict[int, Dict[str, Optional[threading.Timer]]] = {}  # chat_id -> {"check": Timer, "remind": Timer}

def cancel_timer(chat_id: int, key: str):
    t = timers.get(chat_id, {}).get(key)
    if t:
        try:
            t.cancel()
        except Exception:
            pass
    timers.setdefault(chat_id, {})[key] = None

def cancel_all(chat_id: int):
    cancel_timer(chat_id, "check")
    cancel_timer(chat_id, "remind")

def ensure_session(chat_id: int):
    if chat_id not in sessions:
        sessions[chat_id] = {
            "step": "idle",         # idle -> waiting_action -> waiting_type -> result -> started
            "action": None,         # str
            "type": None,           # mental/physical/routine/social
            "result_msg_id": None,  # int
            "locked_result": False, # bool
        }

# ================= UI =================
MENU_TEXTS = {"🚀 Начать", "📊 Статистика", "❓ Как пользоваться"}

def menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚀 Начать")
    kb.row("📊 Статистика", "❓ Как пользоваться")
    return kb

def type_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🧠 Умственное", callback_data="type:mental"),
        types.InlineKeyboardButton("💪 Физическое", callback_data="type:physical"),
    )
    kb.row(
        types.InlineKeyboardButton("🗂 Рутинное", callback_data="type:routine"),
        types.InlineKeyboardButton("💬 Общение", callback_data="type:social"),
    )
    return kb

def type_label(t: Optional[str]) -> str:
    return {
        "mental": "🧠 Умственное",
        "physical": "💪 Физическое",
        "routine": "🗂 Рутинное",
        "social": "💬 Общение",
    }.get(t or "", "—")

def result_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🚀 Я начал", callback_data="act:start"),
        types.InlineKeyboardButton("⏸ Отложить 10 минут", callback_data="act:delay10"),
    )
    kb.add(
        types.InlineKeyboardButton("🕒 Попозже (30 минут)", callback_data="act:delay30"),
        types.InlineKeyboardButton("❌ Не хочу сейчас", callback_data="act:skip"),
    )
    return kb

def progress_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("👍 Норм", callback_data="prog:ok"),
        types.InlineKeyboardButton("😵 Тяжело", callback_data="prog:hard"),
        types.InlineKeyboardButton("❌ Бросил", callback_data="prog:quit"),
    )
    return kb

def quit_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🔁 Попробовать снова (меньше)", callback_data="quit:retry"),
        types.InlineKeyboardButton("🕒 Вернуться позже", callback_data="quit:later"),
    )
    kb.add(
        types.InlineKeyboardButton("🚀 Начать другое действие", callback_data="quit:new"),
    )
    return kb

# ================= MOTIVATION (полный набор из твоего текста) =================
MOTIVATION_START_BY_TYPE = {
    "mental": [
        "Сейчас цель — войти в поток, не решить всё. Начни с 1 простого шага.",
        "Сделай черновик/набросок. Потом улучшим.",
        "Только 10 минут фокуса. Без оценки результата.",
    ],
    "physical": [
        "Начни мягко. Первые минуты — разогрев, дальше само пойдёт.",
        "Сейчас важна регулярность, а не интенсивность.",
        "Сделай 1 подход/1 круг. Потом решишь, продолжать ли.",
    ],
    "routine": [
        "Сделай один конкретный кусок и закрой тему.",
        "Начни с самого мелкого шага — он разгонит.",
        "Сейчас не “идеально”, сейчас — “закончено”.",
    ],
    "social": [
        "Твоя цель — начать, не быть идеальным.",
        "Одно короткое сообщение достаточно. Дальше легче.",
        "Скажи просто и по делу. Без лишних объяснений.",
    ],
}

MOTIVATION_OK_BY_TYPE = {
    "mental": [
        "Хорошо идёт. Не ускоряйся — просто держи темп ещё 10 минут.",
        "Продолжай. Главное — не переключаться.",
    ],
    "physical": [
        "Отлично. Держи ровный ритм, без рывков.",
        "Ещё 10 минут — и будет чувство “я сделал”.",
    ],
    "routine": [
        "Класс. Доведи до точки: “готово/отправлено/убрано”.",
        "Продолжай — рутина ломается только движением.",
    ],
    "social": [
        "Отлично. Держи простоту и ясность — этого достаточно.",
        "Продолжай. Не усложняй формулировки.",
    ],
}

MOTIVATION_HARD_BASE = "Ок, давай проще. Сделай версию в 2 раза легче. Даже 1 маленький шаг считается."

MOTIVATION_HARD_BY_TYPE = {
    "mental": [
        "Сними сложность: сделай самую лёгкую часть или просто подготовь (открыть файл, план, 3 пункта).",
        "Разрешаю “плохой черновик”. Он лучше нуля.",
    ],
    "physical": [
        "Уменьши нагрузку в 2 раза: меньше повторов/темп ниже — но не останавливайся полностью.",
        "Сделай 2 минуты очень легко. Это сохраняет привычку.",
    ],
    "routine": [
        "Сузь задачу: один пункт, один документ, один угол, одно сообщение.",
        "Поставь таймер на 3 минуты и делай только это.",
    ],
    "social": [
        "Сократи: 1–2 предложения. Или задай один вопрос — этого хватит.",
        "Можно написать черновик и отправить через минуту.",
    ],
}

QUIT_TEXTS = [
    "Нормально. Ты не “провалился” — ты проверил состояние.",
    "Давай либо сделаем шаг в 10 раз меньше, либо вернёмся позже.",
]

def pick_motivation(pool: Dict[str, list], t: Optional[str]) -> str:
    arr = pool.get(t or "", [])
    if not arr:
        return "Сделай самый маленький шаг. Этого достаточно."
    return random.choice(arr)

# ================= FLOWS =================
def start_flow(chat_id: int):
    ensure_session(chat_id)
    cancel_all(chat_id)

    sessions[chat_id].update({
        "step": "waiting_action",
        "action": None,
        "type": None,
        "result_msg_id": None,
        "locked_result": False,
    })

    bot.send_message(
        chat_id,
        "✍️ Напиши <b>одно</b> действие, которое хочешь сделать сейчас (одной строкой):",
        reply_markup=menu_kb()
    )
    log(chat_id, "start_flow", "ok")

def show_result(chat_id: int):
    s = sessions[chat_id]
    action = s["action"]
    t = s["type"]

    s["step"] = "result"
    s["locked_result"] = False

    msg = bot.send_message(
        chat_id,
        f"🔥 <b>Главное действие сейчас:</b>\n\n<b>{action}</b>\nТип: <b>{type_label(t)}</b>",
        reply_markup=result_kb()
    )
    s["result_msg_id"] = msg.message_id
    log(chat_id, "focus", action)

def schedule_check_in_10(chat_id: int):
    cancel_timer(chat_id, "check")

    def check():
        try:
            bot.send_message(chat_id, "Как идёт?", reply_markup=progress_kb())
            log(chat_id, "check_sent", "10m")
        except Exception:
            pass

    t = threading.Timer(10 * 60, check)
    timers.setdefault(chat_id, {})["check"] = t
    t.start()

def schedule_remind(chat_id: int, minutes: int):
    cancel_timer(chat_id, "remind")

    def remind():
        try:
            bot.send_message(chat_id, "Можешь начать с самого маленького шага.", reply_markup=menu_kb())
            log(chat_id, "reminder_sent", f"{minutes}m")
        except Exception:
            pass

    t = threading.Timer(minutes * 60, remind)
    timers.setdefault(chat_id, {})["remind"] = t
    t.start()

# ================= COMMANDS & MENU =================
@bot.message_handler(commands=["start"])
def cmd_start(m):
    start_flow(m.chat.id)

@bot.message_handler(func=lambda m: (m.text or "").strip() in MENU_TEXTS)
def menu_handler(m):
    chat_id = m.chat.id
    txt = (m.text or "").strip()

    if txt == "🚀 Начать":
        start_flow(chat_id)
        return

    if txt == "❓ Как пользоваться":
        bot.send_message(
            chat_id,
            "Как пользоваться:\n"
            "1) 🚀 Начать\n"
            "2) Напиши действие\n"
            "3) Выбери тип\n"
            "4) Нажми: Я начал / Отложить / Попозже / Не хочу\n"
            "5) Я не отвлекаю — чек через 10 минут 🙂",
            reply_markup=menu_kb()
        )
        return

    if txt == "📊 Статистика":
        bot.send_message(chat_id, "📊 Статистика: логируется в базе (logs).", reply_markup=menu_kb())
        return

# ================= TEXT ROUTER =================
@bot.message_handler(func=lambda m: True, content_types=["text"])
def text_router(m):
    chat_id = m.chat.id
    ensure_session(chat_id)

    # меню уже обработали
    if (m.text or "").strip() in MENU_TEXTS:
        return

    s = sessions[chat_id]
    step = s.get("step")

    if step == "waiting_action":
        action = (m.text or "").strip()
        if len(action) < 2:
            bot.send_message(chat_id, "Напиши нормальное действие одной строкой 🙂", reply_markup=menu_kb())
            return

        s["action"] = action
        s["step"] = "waiting_type"
        log(chat_id, "action_set", action)

        bot.send_message(chat_id, f"Выбери тип для:\n<b>{action}</b>", reply_markup=type_kb())
        return

    # остальной текст игнорируем
    return

# ================= TYPE PICK =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("type:"))
def type_pick(c):
    chat_id = c.message.chat.id
    ensure_session(chat_id)
    s = sessions[chat_id]

    if s.get("step") != "waiting_type":
        bot.answer_callback_query(c.id, "Сейчас это не нужно 🙂")
        return

    t = c.data.split(":", 1)[1]
    s["type"] = t
    log(chat_id, "type", t)

    try:
        bot.edit_message_text(
            f"✅ Тип выбран: <b>{type_label(t)}</b>\n\nДействие:\n<b>{s['action']}</b>",
            chat_id, c.message.message_id
        )
    except Exception:
        pass

    bot.answer_callback_query(c.id, "Ок ✅")
    show_result(chat_id)

# ================= RESULT ACTIONS =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("act:"))
def act_handler(c):
    chat_id = c.message.chat.id
    ensure_session(chat_id)
    s = sessions[chat_id]

    if s.get("step") != "result" or not s.get("action"):
        bot.answer_callback_query(c.id, "Нажми 🚀 Начать")
        return

    # защита от двойных кликов
    if s.get("locked_result"):
        bot.answer_callback_query(c.id, "Уже принято ✅")
        return

    # только актуальное сообщение результата
    if s.get("result_msg_id") and c.message.message_id != s["result_msg_id"]:
        bot.answer_callback_query(c.id, "Это старое сообщение")
        return

    cmd = c.data.split(":", 1)[1]
    action = s["action"]
    t = s["type"]

    # блокируем повторные нажатия + убираем клавиатуру
    s["locked_result"] = True
    try:
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    if cmd == "start":
        log(chat_id, "started", action)
        cancel_all(chat_id)

        # 1) отдельно: Ты начал
        bot.send_message(chat_id, f"🚀 Ты начал: <b>{action}</b>")

        # 2) отдельно: мотивация
        motivation = pick_motivation(MOTIVATION_START_BY_TYPE, t)
        bot.send_message(chat_id, f"Мотивация: {motivation}")

        # 3) отдельно: не отвлекать + чек через 10 минут
        bot.send_message(chat_id, "Я не буду отвлекать.\nЧерез 10 минут спрошу, как идёт.")

        schedule_check_in_10(chat_id)

        bot.answer_callback_query(c.id, "Погнали 🔥")
        s["step"] = "started"
        return

    if cmd == "delay10":
        log(chat_id, "delayed", "10m")
        bot.send_message(chat_id, "Ок.\nЯ напомню через 10 минут.", reply_markup=menu_kb())
        schedule_remind(chat_id, 10)
        bot.answer_callback_query(c.id, "Ок ⏸")
        s["step"] = "idle"
        return

    if cmd == "delay30":
        log(chat_id, "delayed", "30m")
        bot.send_message(chat_id, "Ок.\nЯ напомню через 30 минут.", reply_markup=menu_kb())
        schedule_remind(chat_id, 30)
        bot.answer_callback_query(c.id, "Ок 🕒")
        s["step"] = "idle"
        return

    if cmd == "skip":
        log(chat_id, "skip", action)
        bot.send_message(chat_id, "Ок.\nИногда лучше не давить на себя.", reply_markup=menu_kb())
        bot.answer_callback_query(c.id, "Ок")
        s["step"] = "idle"
        return

# ================= PROGRESS =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("prog:"))
def progress_handler(c):
    chat_id = c.message.chat.id
    ensure_session(chat_id)
    s = sessions[chat_id]

    val = c.data.split(":", 1)[1]
    t = s.get("type")

    log(chat_id, "progress", val)

    # Чтобы кнопки не нажимали 2 раза — уберём клавиатуру
    try:
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    if val == "ok":
        # отдельно: коротко + отдельно мотивация
        bot.send_message(chat_id, "👍 Принято: Норм.")
        m = pick_motivation(MOTIVATION_OK_BY_TYPE, t)
        bot.send_message(chat_id, f"Мотивация: {m}")

        bot.answer_callback_query(c.id, "✅")
        return

    if val == "hard":
        bot.send_message(chat_id, "😵 Принято: Тяжело.")
        bot.send_message(chat_id, f"Мотивация: {MOTIVATION_HARD_BASE}")
        m = pick_motivation(MOTIVATION_HARD_BY_TYPE, t)
        bot.send_message(chat_id, f"Мотивация: {m}")

        bot.answer_callback_query(c.id, "Ок")
        return

    if val == "quit":
        bot.send_message(chat_id, "❌ Принято: Бросил.")
        bot.send_message(chat_id, f"Мотивация: {random.choice(QUIT_TEXTS)}", reply_markup=quit_kb())
        bot.answer_callback_query(c.id, "Ок")
        return

# ================= QUIT ACTIONS =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("quit:"))
def quit_handler(c):
    chat_id = c.message.chat.id
    ensure_session(chat_id)

    cmd = c.data.split(":", 1)[1]
    log(chat_id, "quit_action", cmd)

    # уберём кнопки
    try:
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    if cmd == "retry":
        bot.send_message(chat_id, "Ок. Выбери действие поменьше и начнём заново 🙂", reply_markup=menu_kb())
        start_flow(chat_id)
        bot.answer_callback_query(c.id, "Ок")
        return

    if cmd == "later":
        bot.send_message(chat_id, "Ок. Вернёшься позже — нажми 🚀 Начать.", reply_markup=menu_kb())
        bot.answer_callback_query(c.id, "Ок")
        return

    if cmd == "new":
        start_flow(chat_id)
        bot.answer_callback_query(c.id, "Ок")
        return

# ================= RUN =================
if __name__ == "__main__":
    init_db()
    print("Bot started")

    # устойчивый polling (на случай сетевых ошибок)
    while True:
        try:
            bot.infinity_polling(skip_pending=True, none_stop=True, timeout=60, long_polling_timeout=60)
        except ApiTelegramException as e:
            # 409 = запущен другой экземпляр
            if "409" in str(e):
                print("409 conflict: another instance is running. Stop the other instance. Retrying in 10s...")
                time.sleep(10)
            else:
                raise
        except Exception as e:
            print("Polling error:", e)
            time.sleep(5)
