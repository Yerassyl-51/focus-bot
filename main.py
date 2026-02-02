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

ADMIN_IDS = {8311003582}  # твой chat_id
MAX_DAILY_USES = 3        # обычным пользователям

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

def count_today(chat_id: int, event: str) -> int:
    today = datetime.now(KZ_TZ).date().isoformat()
    with db_lock, db() as c:
        cur = c.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM logs
            WHERE chat_id=? AND event=? AND substr(created_at,1,10)=?
        """, (chat_id, event, today))
        return int(cur.fetchone()[0])

def can_use_bot(chat_id: int) -> bool:
    if chat_id in ADMIN_IDS:
        return True
    uses = count_today(chat_id, "focus")  # считаем "выборы главного действия"
    return uses < MAX_DAILY_USES

# ================= STATE =================
sessions: Dict[int, Dict[str, Any]] = {}
timers: Dict[int, Dict[str, Optional[threading.Timer]]] = {}

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
        sessions[chat_id] = {}

def reset_session(chat_id: int):
    sessions[chat_id] = {
        # flow
        "step": "energy",         # energy -> actions -> typing -> scoring -> result -> started/idle/delayed
        "energy": None,           # high/mid/low
        "energy_msg_id": None,
        "energy_locked": False,

        # actions list
        "actions": [],            # [{"name":..., "type":..., "scores":{...}}]
        "cur_action": 0,
        "cur_crit": 0,

        # locks for inline messages
        "expected_type_msg_id": None,
        "expected_score_msg_id": None,
        "answered_type_msgs": set(),
        "answered_score_msgs": set(),

        # result
        "focus": None,
        "focus_type": None,
        "result_msg_id": None,
        "result_locked": False,
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

def type_label(t: Optional[str]) -> str:
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
    kb.add(types.InlineKeyboardButton("🚀 Начать другое действие", callback_data="quit:new"))
    return kb

# ================= CRITERIA =================
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

def energy_weight(level: str) -> float:
    return {"low": 2.0, "mid": 1.0, "high": 0.6}.get(level, 1.0)

def pick_best_action(session: Dict[str, Any]) -> Dict[str, Any]:
    lvl = session.get("energy", "mid")
    ew = energy_weight(lvl)

    best = None
    best_score = -10**9

    for a in session["actions"]:
        s = a["scores"]
        energy_bonus = 6 - s["energy"]  # 1 легко -> бонус 5
        total = (
            s["influence"] * 2 +
            s["urgency"] * 2 +
            s["meaning"] * 1 +
            energy_bonus * ew
        )
        if total > best_score:
            best_score = total
            best = a

    return best

# ================= MOTIVATION (из твоего текста) =================
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

# ================= TIMERS =================
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

# ================= START FLOW =================
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

    ensure_session(chat_id)
    cancel_all(chat_id)
    reset_session(chat_id)

    msg = bot.send_message(chat_id, "Твоя энергия сейчас?", reply_markup=energy_kb())
    sessions[chat_id]["energy_msg_id"] = msg.message_id
    bot.send_message(chat_id, "Меню:", reply_markup=menu_kb())
    log(chat_id, "start_flow", "ok")

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
            "2) Выбери энергию\n"
            "3) Напиши минимум 3 действия (каждое с новой строки)\n"
            "4) Для каждого выбери тип\n"
            "5) Оцени по 4 критериям\n"
            "6) Получишь главное действие + кнопки\n"
            "7) После «Я начал» — я не отвлекаю, чек через 10 минут 🙂",
            reply_markup=menu_kb()
        )
        return

    if txt == "📊 Статистика":
        focus_today = count_today(chat_id, "focus")
        started_today = count_today(chat_id, "started")
        progress_today = count_today(chat_id, "progress")
        bot.send_message(
            chat_id,
            "📊 Статистика за сегодня:\n"
            f"• Выборов: <b>{focus_today}</b>\n"
            f"• Начал: <b>{started_today}</b>\n"
            f"• Ответов «как идёт»: <b>{progress_today}</b>",
            reply_markup=menu_kb()
        )
        return

# ================= ENERGY =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("energy:"))
def energy_pick(c):
    chat_id = c.message.chat.id
    ensure_session(chat_id)
    s = sessions[chat_id]

    if s.get("energy_locked"):
        bot.answer_callback_query(c.id, "Уже выбрано ✅")
        return

    # защита от старых сообщений энергии
    if s.get("energy_msg_id") and c.message.message_id != s["energy_msg_id"]:
        bot.answer_callback_query(c.id, "Это старое сообщение")
        return

    code = c.data.split(":", 1)[1]  # high/mid/low
    s["energy"] = code
    s["energy_locked"] = True
    s["step"] = "actions"

    log(chat_id, "energy", code)

    try:
        bot.edit_message_text(
            f"✅ Энергия: <b>{energy_label(code)}</b>",
            chat_id, c.message.message_id
        )
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    bot.answer_callback_query(c.id, "Ок ✅")
    bot.send_message(chat_id, "✍️ Напиши <b>минимум 3 действия</b>, которые ты можешь сделать сейчас (каждое с новой строки) :", reply_markup=menu_kb())

# ================= ACTIONS INPUT =================
@bot.message_handler(func=lambda m: True, content_types=["text"])
def actions_router(m):
    chat_id = m.chat.id
    ensure_session(chat_id)
    s = sessions[chat_id]

    # меню не воспринимать как действия
    if (m.text or "").strip() in MENU_TEXTS:
        return

    if s.get("step") != "actions":
        return

    lines = [x.strip() for x in (m.text or "").split("\n") if x.strip()]
    if len(lines) < 3:
        bot.send_message(chat_id, "❌ Нужно минимум 3 действия (каждое с новой строки).", reply_markup=menu_kb())
        return

    s["actions"] = [{"name": name, "type": None, "scores": {}} for name in lines]
    s["cur_action"] = 0
    s["cur_crit"] = 0
    s["step"] = "typing"
    s["answered_type_msgs"].clear()
    s["answered_score_msgs"].clear()

    log(chat_id, "actions_count", str(len(lines)))
    ask_type(chat_id)

def ask_type(chat_id: int):
    s = sessions[chat_id]
    a = s["actions"][s["cur_action"]]
    msg = bot.send_message(chat_id, f"Выбери тип для:\n<b>{a['name']}</b>", reply_markup=type_kb())
    s["expected_type_msg_id"] = msg.message_id

# ================= TYPE PICK =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("type:"))
def type_pick(c):
    chat_id = c.message.chat.id
    ensure_session(chat_id)
    s = sessions[chat_id]

    if s.get("step") != "typing":
        bot.answer_callback_query(c.id, "Сейчас не время выбирать тип 🙂")
        return

    if s.get("expected_type_msg_id") and c.message.message_id != s["expected_type_msg_id"]:
        bot.answer_callback_query(c.id, "Это старое сообщение")
        return

    if c.message.message_id in s["answered_type_msgs"]:
        bot.answer_callback_query(c.id, "Уже выбрано ✅")
        return

    t = c.data.split(":", 1)[1]
    a = s["actions"][s["cur_action"]]
    a["type"] = t
    log(chat_id, "type", t)

    s["answered_type_msgs"].add(c.message.message_id)

    try:
        bot.edit_message_text(
            f"✅ <b>{a['name']}</b> — {type_label(t)}",
            chat_id, c.message.message_id
        )
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    bot.answer_callback_query(c.id, "Ок ✅")

    s["cur_action"] += 1
    if s["cur_action"] >= len(s["actions"]):
        # переходим к оценкам
        s["cur_action"] = 0
        s["cur_crit"] = 0
        s["step"] = "scoring"
        ask_score(chat_id)
    else:
        ask_type(chat_id)

# ================= SCORE =================
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

@bot.callback_query_handler(func=lambda c: c.data.startswith("score:"))
def score_pick(c):
    chat_id = c.message.chat.id
    ensure_session(chat_id)
    s = sessions[chat_id]

    if s.get("step") != "scoring":
        bot.answer_callback_query(c.id, "Сейчас не время ставить оценку 🙂")
        return

    if s.get("expected_score_msg_id") and c.message.message_id != s["expected_score_msg_id"]:
        bot.answer_callback_query(c.id, "Это старое сообщение")
        return

    if c.message.message_id in s["answered_score_msgs"]:
        bot.answer_callback_query(c.id, "Уже выбрано ✅")
        return

    score = int(c.data.split(":", 1)[1])
    a = s["actions"][s["cur_action"]]
    key, title = CRITERIA[s["cur_crit"]]
    a["scores"][key] = score
    log(chat_id, "score", f"{key}={score}")

    s["answered_score_msgs"].add(c.message.message_id)

    try:
        bot.edit_message_text(
            f"✅ <b>{a['name']}</b>\n{title}: <b>{score}</b>",
            chat_id, c.message.message_id
        )
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    bot.answer_callback_query(c.id, "Ок ✅")

    s["cur_crit"] += 1
    if s["cur_crit"] >= len(CRITERIA):
        s["cur_crit"] = 0
        s["cur_action"] += 1

        if s["cur_action"] >= len(s["actions"]):
            show_result(chat_id)
            return

    ask_score(chat_id)

# ================= RESULT =================
def show_result(chat_id: int):
    s = sessions[chat_id]
    s["step"] = "result"
    s["result_locked"] = False

    best = pick_best_action(s)
    s["focus"] = best["name"]
    s["focus_type"] = best.get("type")

    log(chat_id, "focus", s["focus"])

    msg = bot.send_message(
        chat_id,
        f"🔥 <b>Главное действие сейчас:</b>\n\n"
        f"<b>{s['focus']}</b>\n"
        f"Тип: <b>{type_label(s['focus_type'])}</b>",
        reply_markup=result_kb()
    )
    s["result_msg_id"] = msg.message_id

# ================= RESULT BUTTONS =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("act:"))
def act_handler(c):
    chat_id = c.message.chat.id
    ensure_session(chat_id)
    s = sessions[chat_id]

    if s.get("step") != "result" or not s.get("focus"):
        bot.answer_callback_query(c.id, "Нажми 🚀 Начать")
        return

    if s.get("result_msg_id") and c.message.message_id != s["result_msg_id"]:
        bot.answer_callback_query(c.id, "Это старое сообщение")
        return

    if s.get("result_locked"):
        bot.answer_callback_query(c.id, "Уже принято ✅")
        return

    cmd = c.data.split(":", 1)[1]
    focus = s["focus"]
    t = s.get("focus_type")

    # блокируем двойной клик + убираем кнопки у результата
    s["result_locked"] = True
    try:
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    if cmd == "start":
        log(chat_id, "started", focus)
        cancel_all(chat_id)

        # 1) отдельно
        bot.send_message(chat_id, f"🚀 Ты начал: <b>{focus}</b>")

        # 2) отдельно
        motivation = pick_motivation(MOTIVATION_START_BY_TYPE, t)
        bot.send_message(chat_id, f"Мотивация: {motivation}")

        # 3) отдельно
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
        log(chat_id, "skip", focus)
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
    t = s.get("focus_type")

    log(chat_id, "progress", val)

    # убрать кнопки у "как идёт?"
    try:
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    if val == "ok":
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

    try:
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    if cmd == "retry":
        bot.send_message(chat_id, "Ок. Начнём заново, выбери действия поменьше 🙂", reply_markup=menu_kb())
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

    while True:
        try:
            bot.infinity_polling(skip_pending=True, none_stop=True, timeout=60, long_polling_timeout=60)
        except ApiTelegramException as e:
            if "409" in str(e):
                print("409 conflict: another instance is running. Stop the other instance. Retrying in 10s...")
                time.sleep(10)
            else:
                raise
        except Exception as e:
            print("Polling error:", e)
            time.sleep(5)
