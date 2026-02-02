import os
import time
import threading
import sqlite3
from datetime import datetime, timedelta, timezone

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

# ================= CONFIG =================
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
KZ_TZ = timezone(timedelta(hours=5))

ADMIN_IDS = {8311003582}   # твой chat_id
MAX_DAILY_USES = 3         # для обычных пользователей

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

def log(chat_id: int, event: str, value: str | None = None):
    with db_lock, db() as c:
        c.execute(
            "INSERT INTO logs(chat_id,event,value,created_at) VALUES(?,?,?,?)",
            (chat_id, event, value, datetime.now(KZ_TZ).isoformat())
        )
        c.commit()

def count_today(chat_id: int, event: str) -> int:
    today = datetime.now(KZ_TZ).date().isoformat()
    with db_lock, db() as c:
        cur = c.cursor()
        cur.execute("""
            SELECT COUNT(*)
            FROM logs
            WHERE chat_id=? AND event=? AND substr(created_at,1,10)=?
        """, (chat_id, event, today))
        return int(cur.fetchone()[0])

def can_use_bot(chat_id: int) -> bool:
    if chat_id in ADMIN_IDS:
        return True
    uses = count_today(chat_id, "focus")  # считаем сколько раз был результат
    return uses < MAX_DAILY_USES

# ================= STATE =================
sessions = {}  # chat_id -> dict
timers = {}    # chat_id -> {"check": Timer, "remind": Timer}

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

def reset_session(chat_id: int):
    sessions[chat_id] = {
        # flow: energy -> actions -> typing -> scoring -> result / started / idle
        "step": "energy",

        "energy": None,          # low/mid/high
        "energy_msg_id": None,   # чтобы принимать только актуальные клики энергии
        "energy_locked": False,

        "actions": [],           # [{"name":..., "type":..., "scores":{...}}]
        "cur_action": 0,
        "cur_crit": 0,

        "expected_type_msg_id": None,
        "expected_score_msg_id": None,

        "focus": None,
        "result_msg_id": None,     # id сообщения результата (где 4 кнопки)
        "result_locked": False,    # чтобы не нажимали 2 раза

        "last_prompt": None,
    }

# ================= UI =================
MENU_TEXTS = {"🚀 Начать", "📊 Статистика", "❓ Как пользоваться"}

def menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚀 Начать")
    kb.row("📊 Статистика", "❓ Как пользоваться")
    return kb

def energy_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🔋 Высокая", callback_data="energy:high"),
        types.InlineKeyboardButton("😐 Средняя", callback_data="energy:mid"),
        types.InlineKeyboardButton("🪫 Низкая", callback_data="energy:low"),
    )
    return kb

def energy_label(code: str) -> str:
    return {"high": "🔋 Высокая", "mid": "😐 Средняя", "low": "🪫 Низкая"}.get(code, code)

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

def type_label(t: str | None) -> str:
    return {
        "mental": "🧠 Умственное",
        "physical": "💪 Физическое",
        "routine": "🗂 Рутинное",
        "social": "💬 Общение",
    }.get(t or "", "—")

def score_kb():
    kb = types.InlineKeyboardMarkup(row_width=5)
    kb.add(*[
        types.InlineKeyboardButton(str(i), callback_data=f"score:{i}")
        for i in range(1, 6)
    ])
    return kb

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

# ================= CRITERIA + HINTS =================
CRITERIA = [
    ("influence", "Влияние (польза для результата)"),
    ("urgency",   "Срочность (насколько важно сейчас)"),
    ("energy",    "Затраты сил (насколько тяжело сделать)"),
    ("meaning",   "Смысл (важно лично тебе)"),
]

HINTS = {
    "influence": "1 = почти не поможет, 5 = сильно продвинет",
    "urgency":   "1 = можно позже, 5 = нужно сейчас/сегодня",
    "energy":    "1 = легко, 5 = очень тяжело по силам",
    "meaning":   "1 = не важно, 5 = очень важно для тебя",
}

# ================= MOTIVATION =================
MOTIVATION_START = {
    "mental":   "Спокойно.\nНе нужно делать идеально.\nПросто подумай над первым шагом.",
    "physical": "Начни медленно.\nГлавное — движение, не скорость.\nТело включится по ходу.",
    "routine":  "Сделай самый неприятный кусочек первым.\nПотом станет легче.",
    "social":   "Не нужно идеально говорить.\nДостаточно начать разговор.",
}

MOTIVATION_OK = "Отлично.\nПродолжай в том же ритме.\nДаже если медленно — это работает."

MOTIVATION_HARD_BASE = "Ок, давай проще.\nСделай версию в 2 раза легче.\nДаже 1 маленький шаг считается."

MOTIVATION_HARD_BY_TYPE = {
    "mental":   "Можно просто набросать идеи, не решать.",
    "physical": "Сделай половину. Этого достаточно.",
    "routine":  "Остановись после одного пункта.",
    "social":   "Достаточно одного сообщения.",
}

# ================= PICK BEST =================
def pick_best(actions: list[dict], energy_code: str) -> dict:
    # energy_code: low/mid/high
    weight = {"low": 2.0, "mid": 1.0, "high": 0.6}.get(energy_code, 1.0)
    best = None
    best_score = -10**9

    for a in actions:
        s = a["scores"]  # dict: influence/urgency/energy/meaning
        score = (
            s["influence"] * 2 +
            s["urgency"] * 2 +
            s["meaning"] * 1 +
            (6 - s["energy"]) * weight
        )
        if score > best_score:
            best_score = score
            best = a

    return best

# ================= FLOWS =================
def start_flow(chat_id: int):
    if not can_use_bot(chat_id):
        bot.send_message(
            chat_id,
            "⛔ Лимит на сегодня исчерпан.\n\n"
            f"Можно использовать бота <b>{MAX_DAILY_USES} раза в день</b>.\n"
            "Попробуй завтра 🙌",
            reply_markup=menu_kb()
        )
        return

    cancel_all(chat_id)
    reset_session(chat_id)

    bot.send_message(chat_id, "Меню:", reply_markup=menu_kb())
    msg = bot.send_message(chat_id, "Твоя энергия сейчас?", reply_markup=energy_kb())
    sessions[chat_id]["energy_msg_id"] = msg.message_id
    log(chat_id, "start_flow", "ok")

def show_help(chat_id: int):
    bot.send_message(
        chat_id,
        "Я помогу выбрать одно главное действие.\n\n"
        "1) Выбираешь энергию\n"
        "2) Напиши как минимум 3 действия, которые ты можешь сделать сейчас (каждое с новой строки)\n"
        "3) Для каждого выбираешь тип\n"
        "4) Оцениваешь по 4 критериям\n"
        "5) Я выдаю главное действие + мотивация + чек через 10 минут 🙂",
        reply_markup=menu_kb()
    )

def show_stats(chat_id: int):
    focus_today = count_today(chat_id, "focus")
    started_today = count_today(chat_id, "started")
    progress_today = count_today(chat_id, "progress")
    bot.send_message(
        chat_id,
        "📊 Статистика за сегодня:\n"
        f"• Выборов (focus): <b>{focus_today}</b>\n"
        f"• Нажал 'Я начал': <b>{started_today}</b>\n"
        f"• Ответов 'как идёт': <b>{progress_today}</b>",
        reply_markup=menu_kb()
    )

def ask_type(chat_id: int):
    s = sessions[chat_id]
    a = s["actions"][s["cur_action"]]
    msg = bot.send_message(chat_id, f"Тип действия:\n<b>{a['name']}</b>", reply_markup=type_kb())
    s["expected_type_msg_id"] = msg.message_id

def ask_score(chat_id: int):
    s = sessions[chat_id]
    a = s["actions"][s["cur_action"]]
    key, title = CRITERIA[s["cur_crit"]]
    hint = HINTS.get(key, "")

    msg = bot.send_message(
        chat_id,
        f"Действие: <b>{a['name']}</b>\n"
        f"Тип: <b>{type_label(a.get('type'))}</b>\n\n"
        f"Оцени: <b>{title}</b>\n"
        f"<i>{hint}</i>",
        reply_markup=score_kb()
    )
    s["expected_score_msg_id"] = msg.message_id

def show_result(chat_id: int):
    s = sessions[chat_id]
    best = pick_best(s["actions"], s["energy"])
    s["focus"] = best["name"]
    s["step"] = "result"
    s["result_locked"] = False

    msg = bot.send_message(
        chat_id,
        f"🔥 <b>Главное действие сейчас:</b>\n\n"
        f"<b>{best['name']}</b>\n"
        f"Тип: <b>{type_label(best.get('type'))}</b>",
        reply_markup=result_kb()
    )
    s["result_msg_id"] = msg.message_id
    log(chat_id, "focus", best["name"])

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

# ================= COMMANDS =================
@bot.message_handler(commands=["start"])
def cmd_start(m):
    start_flow(m.chat.id)

@bot.message_handler(commands=["help"])
def cmd_help(m):
    show_help(m.chat.id)

@bot.message_handler(commands=["stats"])
def cmd_stats(m):
    show_stats(m.chat.id)

# ================= MENU HANDLER (ВАЖНО: ВЫШЕ step-хэндлеров) =================
@bot.message_handler(func=lambda m: (m.text or "").strip() in MENU_TEXTS)
def menu_handler(m):
    txt = (m.text or "").strip()
    chat_id = m.chat.id

    if txt == "🚀 Начать":
        start_flow(chat_id)
        return
    if txt == "❓ Как пользоваться":
        show_help(chat_id)
        return
    if txt == "📊 Статистика":
        show_stats(chat_id)
        return

# ================= ENERGY =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("energy:"))
def energy_pick(c):
    chat_id = c.message.chat.id
    s = sessions.get(chat_id)

    if not s:
        bot.answer_callback_query(c.id, "Нажми 🚀 Начать")
        return

    # только актуальное сообщение энергии
    if s.get("energy_msg_id") and c.message.message_id != s["energy_msg_id"]:
        bot.answer_callback_query(c.id, "Это старое сообщение")
        return

    if s.get("energy_locked"):
        bot.answer_callback_query(c.id, "Энергия уже выбрана ✅")
        return

    code = c.data.split(":", 1)[1]  # low/mid/high
    s["energy"] = code
    s["energy_locked"] = True
    log(chat_id, "energy", code)

    try:
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    try:
        bot.edit_message_text(
            f"✅ Энергия выбрана: <b>{energy_label(code)}</b>",
            chat_id, c.message.message_id
        )
    except Exception:
        pass

    bot.answer_callback_query(c.id, "Ок ✅")

    s["step"] = "actions"
    bot.send_message(chat_id, "✍️ Напиши как минимум 3 действия, которые ты можешь сделать сейчас (каждое с новой строки):", reply_markup=menu_kb())

# ================= ACTIONS INPUT =================
@bot.message_handler(func=lambda m: m.chat.id in sessions and sessions[m.chat.id].get("step") == "actions")
def actions_input(m):
    # если прилетело меню — не считаем это действиями
    if (m.text or "").strip() in MENU_TEXTS:
        return

    chat_id = m.chat.id
    s = sessions[chat_id]

    lines = [l.strip() for l in (m.text or "").split("\n") if l.strip()]
    if not (3 <= len(lines) <= 7):
        bot.send_message(chat_id, "❌ Нужно 3–7 действий. Каждое с новой строки.", reply_markup=menu_kb())
        return

    s["actions"] = [{"name": l, "type": None, "scores": {}} for l in lines]
    s["cur_action"] = 0
    s["cur_crit"] = 0
    s["step"] = "typing"
    log(chat_id, "actions_count", str(len(lines)))

    ask_type(chat_id)

# ================= TYPE PICK =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("type:"))
def type_pick(c):
    chat_id = c.message.chat.id
    s = sessions.get(chat_id)

    if not s or s.get("step") != "typing":
        bot.answer_callback_query(c.id, "Сейчас не время выбирать тип 🙂")
        return

    # только актуальное сообщение типа
    if s.get("expected_type_msg_id") and c.message.message_id != s["expected_type_msg_id"]:
        bot.answer_callback_query(c.id, "Это старое сообщение")
        return

    t = c.data.split(":", 1)[1]
    a = s["actions"][s["cur_action"]]
    a["type"] = t
    log(chat_id, "type", t)

    try:
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    try:
        bot.edit_message_text(
            f"✅ <b>{a['name']}</b> — {type_label(t)}",
            chat_id, c.message.message_id
        )
    except Exception:
        pass

    bot.answer_callback_query(c.id, "Ок ✅")

    # после типа — начинаем оценки по этому действию
    s["cur_crit"] = 0
    s["step"] = "scoring"
    ask_score(chat_id)

# ================= SCORE PICK =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("score:"))
def score_pick(c):
    chat_id = c.message.chat.id
    s = sessions.get(chat_id)

    if not s or s.get("step") != "scoring":
        bot.answer_callback_query(c.id, "Сейчас не время ставить оценку 🙂")
        return

    # только актуальное сообщение оценки
    if s.get("expected_score_msg_id") and c.message.message_id != s["expected_score_msg_id"]:
        bot.answer_callback_query(c.id, "Это старое сообщение")
        return

    score = int(c.data.split(":", 1)[1])
    a = s["actions"][s["cur_action"]]
    key, title = CRITERIA[s["cur_crit"]]
    a["scores"][key] = score
    log(chat_id, "score", f"{key}={score}")

    try:
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    try:
        bot.edit_message_text(
            f"✅ {title}: <b>{score}</b>",
            chat_id, c.message.message_id
        )
    except Exception:
        pass

    bot.answer_callback_query(c.id, "Ок ✅")

    s["cur_crit"] += 1

    if s["cur_crit"] >= len(CRITERIA):
        # следующее действие
        s["cur_action"] += 1

        if s["cur_action"] >= len(s["actions"]):
            show_result(chat_id)
            return

        # снова выбор типа для следующего действия
        s["step"] = "typing"
        ask_type(chat_id)
        return

    # следующий критерий для этого же действия
    ask_score(chat_id)

# ================= RESULT BUTTONS =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("act:"))
def act_handler(c):
    chat_id = c.message.chat.id
    s = sessions.get(chat_id)
    if not s or s.get("step") != "result" or not s.get("focus"):
        bot.answer_callback_query(c.id, "Нажми 🚀 Начать")
        return

    # только актуальное сообщение результата
    if s.get("result_msg_id") and c.message.message_id != s["result_msg_id"]:
        bot.answer_callback_query(c.id, "Это старое сообщение")
        return

    if s.get("result_locked"):
        bot.answer_callback_query(c.id, "Уже принято ✅")
        return

    cmd = c.data.split(":", 1)[1]
    focus = s["focus"]

    best = None
    for x in s["actions"]:
        if x["name"] == focus:
            best = x
            break
    action_type = (best or {}).get("type")

    # блокируем повторные нажатия + убираем клаву
    s["result_locked"] = True
    try:
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    if cmd == "start":
        log(chat_id, "started", focus)
        cancel_all(chat_id)

        text = (
            f"🚀 Ты начал: <b>{focus}</b>\n\n"
            f"{MOTIVATION_START.get(action_type, '')}\n\n"
            "Я не буду отвлекать.\n"
            "Через 10 минут спрошу, как идёт."
        )

        try:
            bot.edit_message_text(text, chat_id, c.message.message_id)
        except Exception:
            bot.send_message(chat_id, text, reply_markup=menu_kb())

        s["step"] = "started"
        schedule_check_in_10(chat_id)
        bot.answer_callback_query(c.id, "Погнали 🔥")
        return

    if cmd == "delay10":
        log(chat_id, "delayed", "10m")
        bot.send_message(chat_id, "Ок.\nЯ напомню через 10 минут.", reply_markup=menu_kb())
        s["step"] = "idle"
        schedule_remind(chat_id, 10)
        bot.answer_callback_query(c.id, "Ок ⏸")
        return

    if cmd == "delay30":
        log(chat_id, "delayed", "30m")
        bot.send_message(chat_id, "Ок.\nЯ напомню через 30 минут.", reply_markup=menu_kb())
        s["step"] = "idle"
        schedule_remind(chat_id, 30)
        bot.answer_callback_query(c.id, "Ок 🕒")
        return

    if cmd == "skip":
        log(chat_id, "skip", focus)
        bot.send_message(chat_id, "Ок.\nИногда лучше не давить на себя.", reply_markup=menu_kb())
        s["step"] = "idle"
        bot.answer_callback_query(c.id, "Ок")
        return

# ================= PROGRESS =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("prog:"))
def progress_handler(c):
    chat_id = c.message.chat.id
    s = sessions.get(chat_id)
    if not s or not s.get("focus"):
        bot.answer_callback_query(c.id, "Нажми 🚀 Начать")
        return

    val = c.data.split(":", 1)[1]
    log(chat_id, "progress", val)

    # определим тип выбранного действия
    focus = s["focus"]
    t = None
    for x in s["actions"]:
        if x["name"] == focus:
            t = x.get("type")
            break

    if val == "ok":
        try:
            bot.edit_message_text(MOTIVATION_OK, chat_id, c.message.message_id)
        except Exception:
            bot.send_message(chat_id, MOTIVATION_OK, reply_markup=menu_kb())
        bot.answer_callback_query(c.id, "✅")
        return

    if val == "hard":
        msg = MOTIVATION_HARD_BASE + "\n\n" + MOTIVATION_HARD_BY_TYPE.get(t, "")
        try:
            bot.edit_message_text(msg, chat_id, c.message.message_id)
        except Exception:
            bot.send_message(chat_id, msg, reply_markup=menu_kb())
        bot.answer_callback_query(c.id, "Ок")
        return

    if val == "quit":
        text = "Это нормально.\nТы попробовал — это уже шаг."
        try:
            bot.edit_message_text(text, chat_id, c.message.message_id, reply_markup=quit_kb())
        except Exception:
            bot.send_message(chat_id, text, reply_markup=quit_kb())
        bot.answer_callback_query(c.id, "Ок")
        return

# ================= QUIT ACTIONS =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("quit:"))
def quit_handler(c):
    chat_id = c.message.chat.id
    cmd = c.data.split(":", 1)[1]
    log(chat_id, "quit_action", cmd)

    if cmd == "retry":
        bot.send_message(chat_id, "Ок. Давай начнём заново и выберем действие поменьше 🙂", reply_markup=menu_kb())
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

    # устойчивый polling (если сеть глючит)
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

