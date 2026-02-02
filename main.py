import os
import time
import random
import threading
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

import telebot
from telebot import types
from telebot.apihelper import ApiTelegramException

# =========================
# CONFIG
# =========================
TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

PROVIDER_TOKEN = (os.getenv("PROVIDER_TOKEN") or "").strip()  # optional (Telegram Payments)

ADMIN_IDS_ENV = (os.getenv("ADMIN_IDS") or "").strip()
ADMIN_IDS = set()
if ADMIN_IDS_ENV:
    for x in ADMIN_IDS_ENV.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

# fallback если не задано
if not ADMIN_IDS:
    ADMIN_IDS = {8311003582}

KZ_TZ = timezone(timedelta(hours=5))
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")

# =========================
# LIMITS
# =========================
FREE_DAILY_USES = 3          # free: 3 раза/день
WEEK_DAILY_USES = 5          # week: 5 раз/день (пример ограничения)
# month/day/2month: unlimited daily uses

# =========================
# DATABASE
# =========================
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
        c.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            chat_id INTEGER PRIMARY KEY,
            plan TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """)
        c.commit()

def now_iso() -> str:
    return datetime.now(KZ_TZ).isoformat()

def log(chat_id: int, event: str, value: Optional[str] = None):
    with db_lock, db() as c:
        c.execute(
            "INSERT INTO logs(chat_id,event,value,created_at) VALUES(?,?,?,?)",
            (chat_id, event, value, now_iso())
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

# =========================
# SUBSCRIPTIONS
# =========================
# plans: free, day, week, month, two_month
PLAN_TITLES = {
    "free": "Free",
    "day": "Day (пробная)",
    "week": "Week",
    "month": "Month",
    "two_month": "2 Month",
}

PLAN_DAYS = {
    "day": 1,
    "week": 7,
    "month": 30,
    "two_month": 60,
}

def get_sub(chat_id: int) -> Tuple[str, datetime]:
    """return (plan, expires_dt). If no sub -> free and expires in past."""
    with db_lock, db() as c:
        cur = c.cursor()
        cur.execute("SELECT plan, expires_at FROM subscriptions WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        if not row:
            return ("free", datetime(1970, 1, 1, tzinfo=KZ_TZ))
        plan, exp = row[0], row[1]
        try:
            exp_dt = datetime.fromisoformat(exp)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=KZ_TZ)
        except Exception:
            exp_dt = datetime(1970, 1, 1, tzinfo=KZ_TZ)
        return (plan, exp_dt)

def is_active(plan: str, exp: datetime) -> bool:
    if plan == "free":
        return False
    return exp > datetime.now(KZ_TZ)

def effective_plan(chat_id: int) -> str:
    if chat_id in ADMIN_IDS:
        return "two_month"  # админ как максимальный
    plan, exp = get_sub(chat_id)
    return plan if is_active(plan, exp) else "free"

def set_sub(chat_id: int, plan: str, days: int):
    exp = datetime.now(KZ_TZ) + timedelta(days=days)
    with db_lock, db() as c:
        c.execute("""
            INSERT INTO subscriptions(chat_id, plan, expires_at)
            VALUES(?,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET plan=excluded.plan, expires_at=excluded.expires_at
        """, (chat_id, plan, exp.isoformat()))
        c.commit()
    log(chat_id, "sub_set", f"{plan}|{exp.isoformat()}")

def can_use_today(chat_id: int) -> Tuple[bool, str]:
    """daily usage limit based on plan. usage counted by event 'focus'."""
    if chat_id in ADMIN_IDS:
        return True, ""

    plan = effective_plan(chat_id)
    used = count_today(chat_id, "focus")

    if plan in ("month", "two_month", "day"):
        return True, ""

    if plan == "week":
        if used < WEEK_DAILY_USES:
            return True, ""
        return False, (
            "⛔ Лимит на сегодня исчерпан.\n"
            f"План: <b>{PLAN_TITLES[plan]}</b>\n"
            f"Лимит: <b>{WEEK_DAILY_USES}</b> раз/день."
        )

    # free
    if used < FREE_DAILY_USES:
        return True, ""
    return False, (
        "⛔ Лимит на сегодня исчерпан.\n"
        f"План: <b>{PLAN_TITLES['free']}</b>\n"
        f"Лимит: <b>{FREE_DAILY_USES}</b> раза/день."
    )

# =========================
# SESSION MEMORY + TIMERS
# =========================
user_data: Dict[int, Dict[str, Any]] = {}
timers: Dict[int, Dict[str, Optional[threading.Timer]]] = {}

CRITERIA: List[Tuple[str, str]] = [
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

def reset_session(chat_id: int):
    user_data[chat_id] = {
        # flow: idle -> energy -> actions -> typing -> scoring -> result -> started/delayed/idle
        "step": "idle",

        "energy_now": None,
        "energy_msg_id": None,
        "energy_locked": False,

        "actions": [],  # [{"name":..., "type":..., "scores":{...}}]
        "cur_action": 0,
        "cur_crit": 0,

        "expected_type_msg_id": None,
        "answered_type_msgs": set(),

        "expected_score_msg_id": None,
        "answered_score_msgs": set(),

        "focus": None,
        "focus_type": None,
        "result_msg_id": None,
        "result_locked": False,

        # coaching
        "check_count": 0,  # reserved for future
    }

def cancel_timer(chat_id: int, key: str):
    t = timers.get(chat_id, {}).get(key)
    if t:
        try:
            t.cancel()
        except Exception:
            pass
    timers.setdefault(chat_id, {})[key] = None

def cancel_all_timers(chat_id: int):
    cancel_timer(chat_id, "check")
    cancel_timer(chat_id, "remind")
    cancel_timer(chat_id, "support")

# =========================
# UI
# =========================
MENU_TEXTS = {"🚀 Начать действие", "⭐ Premium", "👤 Профиль", "📊 Статистика", "❓ Как пользоваться"}

def menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚀 Начать действие", "⭐ Premium")
    kb.row("📊 Статистика", "👤 Профиль")
    kb.row("❓ Как пользоваться")
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
    return {"high":"🔋 Высокая", "mid":"😐 Средняя", "low":"🪫 Низкая"}.get(code, code)

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

def result_kb(plan: str):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🚀 Я начал", callback_data="res:start"),
        types.InlineKeyboardButton("⏸ Отложить 10 минут", callback_data="res:delay10"),
    )
    if plan in ("two_month", "month", "day"):
        kb.add(
            types.InlineKeyboardButton("🕒 Попозже (30 минут)", callback_data="res:delay30"),
            types.InlineKeyboardButton("❌ Не хочу сейчас", callback_data="res:skip"),
        )
    else:
        kb.add(types.InlineKeyboardButton("❌ Не хочу сейчас", callback_data="res:skip"))
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

def premium_menu_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🟢 Day (299₸)", callback_data="buy:day"))
    kb.add(types.InlineKeyboardButton("🟡 Week (399₸)", callback_data="buy:week"))
    kb.add(types.InlineKeyboardButton("🟠 Month (1499₸)", callback_data="buy:month"))
    kb.add(types.InlineKeyboardButton("🔴 2 Month (2299₸)", callback_data="buy:two_month"))
    return kb

# =========================
# MOTIVATION POOLS
# =========================
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

def pick(pool: Dict[str, List[str]], t: Optional[str]) -> str:
    arr = pool.get(t or "", [])
    if not arr:
        return "Сделай самый маленький шаг. Этого достаточно."
    return random.choice(arr)

# =========================
# SCORING
# =========================
def energy_weight(level: str) -> float:
    return {"low": 2.0, "mid": 1.0, "high": 0.6}.get(level, 1.0)

def pick_best_local(data: Dict[str, Any]) -> Dict[str, Any]:
    lvl = data.get("energy_now", "mid")
    ew = energy_weight(lvl)

    best = None
    best_score = -10**9
    for a in data["actions"]:
        s = a["scores"]
        energy_bonus = 6 - s["energy"]  # energy: 1 easy ... 5 hard
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

# =========================
# FLOWS: START / MENU ✅
# =========================
def send_welcome(chat_id: int):
    bot.send_message(
        chat_id,
        "Привет! 👋\n"
        "Я помогу <b>быстро выбрать одно главное действие</b> и аккуратно поддержу, чтобы ты не бросил.\n\n"
        "Нажми <b>🚀 Начать действие</b>.",
        reply_markup=menu_kb()
    )

def start_energy_flow(chat_id: int):
    ok, reason = can_use_today(chat_id)
    if not ok:
        bot.send_message(chat_id, reason, reply_markup=menu_kb())
        return

    cancel_all_timers(chat_id)
    reset_session(chat_id)
    user_data[chat_id]["step"] = "energy"

    bot.send_message(
        chat_id,
        "Отлично 👍\n"
        "Давай сначала определим твою энергию,\n"
        "чтобы выбрать подходящее действие.",
        reply_markup=menu_kb()
    )

    msg = bot.send_message(chat_id, "Твоя энергия сейчас?", reply_markup=energy_kb())
    user_data[chat_id]["energy_msg_id"] = msg.message_id
    user_data[chat_id]["energy_locked"] = False

    log(chat_id, "start_energy_flow", "ok")

def show_profile(chat_id: int):
    p, exp = get_sub(chat_id)
    eff = effective_plan(chat_id)
    plan_title = PLAN_TITLES.get(eff, eff)

    used_focus = count_today(chat_id, "focus")

    if eff == "free":
        limit_text = f"{used_focus}/{FREE_DAILY_USES} сегодня"
        exp_text = "—"
    elif eff == "week":
        limit_text = f"{used_focus}/{WEEK_DAILY_USES} сегодня"
        exp_text = exp.strftime("%Y-%m-%d %H:%M")
    else:
        limit_text = "без лимита"
        exp_text = exp.strftime("%Y-%m-%d %H:%M") if is_active(p, exp) else "—"

    is_admin = "✅" if chat_id in ADMIN_IDS else "—"

    bot.send_message(
        chat_id,
        "👤 <b>Профиль</b>\n\n"
        f"План: <b>{plan_title}</b>\n"
        f"Активен до: <b>{exp_text}</b>\n"
        f"Лимит действий: <b>{limit_text}</b>\n"
        f"Админ: <b>{is_admin}</b>",
        reply_markup=menu_kb()
    )

def show_stats(chat_id: int):
    focus_today = count_today(chat_id, "focus")
    started_today = count_today(chat_id, "started")
    progress_today = count_today(chat_id, "progress")

    bot.send_message(
        chat_id,
        "📊 <b>Статистика за сегодня</b>\n"
        f"• Выборов (главное действие): <b>{focus_today}</b>\n"
        f"• Нажал “Я начал”: <b>{started_today}</b>\n"
        f"• Ответов “как идёт”: <b>{progress_today}</b>",
        reply_markup=menu_kb()
    )

def show_help(chat_id: int):
    bot.send_message(
        chat_id,
        "❓ <b>Как пользоваться</b>\n\n"
        "1) 🚀 Начать действие\n"
        "2) Выбери энергию\n"
        "3) Напиши 3–7 действий (каждое с новой строки)\n"
        "4) Для каждого выбери тип\n"
        "5) Оцени по 4 критериям (кнопки 1–5)\n"
        "6) Получишь одно главное действие + кнопки управления\n\n"
        "Важно: после “🚀 Я начал” я <b>не отвлекаю</b> и спрашиваю через 10 минут 🙂",
        reply_markup=menu_kb()
    )

def show_premium(chat_id: int):
    plan = effective_plan(chat_id)
    p, exp = get_sub(chat_id)
    exp_text = exp.strftime("%Y-%m-%d %H:%M") if is_active(p, exp) else "—"

    # лимиты по планам (для отображения)
    if plan == "free":
        limits = f"{FREE_DAILY_USES} выбора/день"
    elif plan == "week":
        limits = f"{WEEK_DAILY_USES} выборов/день"
    else:
        limits = "без лимита"

    text = (
        "⭐ <b>Premium</b>\n\n"
        "<b>Текущий план:</b> "
        f"<b>{PLAN_TITLES.get(plan, plan)}</b>\n"
        f"<b>Активен до:</b> <b>{exp_text}</b>\n"
        f"<b>Лимит:</b> <b>{limits}</b>\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "<b>Планы:</b>\n\n"

        "🟢 <b>Day — 299₸</b>\n"
        "• Как <b>Month</b>, но на <b>1 день</b>\n"
        "• Без дневного лимита\n"
        "• Кнопка “🕒 30 минут” доступна\n\n"

        "🟡 <b>Week — 399₸</b>\n"
        f"• Лимит выше: <b>{WEEK_DAILY_USES}</b> выборов/день\n"
        "• Базовые напоминания\n"
        "• Кнопка “🕒 30 минут” <b>недоступна</b>\n\n"

        "🟠 <b>Month — 1499₸</b>\n"
        "• Без дневного лимита\n"
        "• “🕒 30 минут” доступно\n"
        "• 1 чек через 10 минут на действие (вопрос “Как идёт?”)\n\n"

        "🔴 <b>2 Month — 2299₸</b>\n"
        "• Без дневного лимита\n"
        "• “🕒 30 минут” доступно\n"
        "• Расширенный режим поддержки:\n"
        "  – чек через 10 минут (“Как идёт?”)\n"
        "  – если ответ “👍 Норм” → ещё поддержка через 10 минут (без вопроса)\n\n"

        "━━━━━━━━━━━━━━━━\n"
        "Выбери план:"
    )

    bot.send_message(chat_id, text, reply_markup=premium_menu_kb())


@bot.message_handler(commands=["start"])
def cmd_start(m):
    send_welcome(m.chat.id)

@bot.message_handler(func=lambda m: (m.text or "").strip() in MENU_TEXTS)
def menu_handler(m):
    chat_id = m.chat.id
    txt = (m.text or "").strip()

    if txt == "🚀 Начать действие":
        start_energy_flow(chat_id)
        return
    if txt == "👤 Профиль":
        show_profile(chat_id)
        return
    if txt == "📊 Статистика":
        show_stats(chat_id)
        return
    if txt == "❓ Как пользоваться":
        show_help(chat_id)
        return
    if txt == "⭐ Premium":
        show_premium(chat_id)
        return

# =========================
# ENERGY (LOCKED)
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("energy:"))
def energy_pick(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)

    if not data or data.get("step") != "energy":
        bot.answer_callback_query(call.id, "Нажми 🚀 Начать действие")
        return

    if data.get("energy_msg_id") and call.message.message_id != data["energy_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    if data.get("energy_locked"):
        bot.answer_callback_query(call.id, "✅ Энергия уже выбрана")
        return

    lvl = call.data.split(":", 1)[1]
    data["energy_now"] = lvl
    data["energy_locked"] = True
    data["step"] = "actions"

    log(chat_id, "energy", lvl)

    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"✅ Энергия: <b>{energy_label(lvl)}</b>"
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id, "Ок ✅")
    bot.send_message(
        chat_id,
        "✍️ Напиши <b>минимум 3</b> действия (каждое с новой строки):",
        reply_markup=menu_kb()
    )

# =========================
# ACTIONS INPUT
# =========================
@bot.message_handler(func=lambda m: m.chat.id in user_data and user_data[m.chat.id].get("step") == "actions")
def actions_input(m):
    chat_id = m.chat.id
    if (m.text or "").strip() in MENU_TEXTS:
        return

    data = user_data[chat_id]
    lines = [x.strip() for x in (m.text or "").split("\n") if x.strip()]
    if len(lines) < 3 or len(lines) > 7:
        bot.send_message(chat_id, "Нужно <b>3–7</b> действий. Каждое с новой строки.", reply_markup=menu_kb())
        return

    data["actions"] = [{"name": a, "type": None, "scores": {}} for a in lines]
    data["cur_action"] = 0
    data["cur_crit"] = 0
    data["step"] = "typing"
    data["answered_type_msgs"].clear()
    data["expected_type_msg_id"] = None

    log(chat_id, "actions_count", str(len(lines)))
    ask_action_type(chat_id)

def ask_action_type(chat_id: int):
    data = user_data[chat_id]
    a = data["actions"][data["cur_action"]]
    msg = bot.send_message(chat_id, f"Выбери тип для:\n<b>{a['name']}</b>", reply_markup=type_kb())
    data["expected_type_msg_id"] = msg.message_id

# =========================
# TYPE PICK
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("type:"))
def type_pick(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)

    if not data or data.get("step") != "typing":
        bot.answer_callback_query(call.id, "Нажми 🚀 Начать действие")
        return

    if data.get("expected_type_msg_id") and call.message.message_id != data["expected_type_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    if call.message.message_id in data["answered_type_msgs"]:
        bot.answer_callback_query(call.id, "✅ Уже выбрано")
        return

    t = call.data.split(":", 1)[1]
    a = data["actions"][data["cur_action"]]
    a["type"] = t
    data["answered_type_msgs"].add(call.message.message_id)
    log(chat_id, "type", t)

    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"✅ <b>{a['name']}</b> — {type_label(t)}"
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id, "Ок ✅")

    data["cur_action"] += 1
    if data["cur_action"] >= len(data["actions"]):
        data["cur_action"] = 0
        data["cur_crit"] = 0
        data["step"] = "scoring"
        data["answered_score_msgs"].clear()
        ask_next_score(chat_id)
    else:
        ask_action_type(chat_id)

# =========================
# SCORING
# =========================
def ask_next_score(chat_id: int):
    data = user_data[chat_id]
    a = data["actions"][data["cur_action"]]
    key, title = CRITERIA[data["cur_crit"]]
    hint = HINTS.get(key, "")

    msg = bot.send_message(
        chat_id,
        f"Действие: <b>{a['name']}</b>\n"
        f"Тип: <b>{type_label(a.get('type'))}</b>\n\n"
        f"Оцени: <b>{title}</b>\n"
        f"<i>{hint}</i>",
        reply_markup=score_kb()
    )
    data["expected_score_msg_id"] = msg.message_id

@bot.callback_query_handler(func=lambda c: c.data.startswith("score:"))
def score_pick(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)

    if not data or data.get("step") != "scoring":
        bot.answer_callback_query(call.id, "Сейчас не время 🙂")
        return

    if data.get("expected_score_msg_id") and call.message.message_id != data["expected_score_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    if call.message.message_id in data["answered_score_msgs"]:
        bot.answer_callback_query(call.id, "✅ Уже выбрано")
        return

    score = int(call.data.split(":", 1)[1])
    a = data["actions"][data["cur_action"]]
    key, title = CRITERIA[data["cur_crit"]]
    a["scores"][key] = score

    data["answered_score_msgs"].add(call.message.message_id)
    log(chat_id, "score", f"{key}={score}")

    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    try:
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"✅ <b>{a['name']}</b>\n{title}: <b>{score}</b>"
        )
    except Exception:
        pass

    bot.answer_callback_query(call.id, "Ок ✅")

    data["cur_crit"] += 1
    if data["cur_crit"] >= len(CRITERIA):
        data["cur_crit"] = 0
        data["cur_action"] += 1

        if data["cur_action"] >= len(data["actions"]):
            show_result(chat_id)
            return

    ask_next_score(chat_id)

# =========================
# RESULT
# =========================
def show_result(chat_id: int):
    data = user_data[chat_id]
    data["step"] = "result"
    data["result_locked"] = False

    best = pick_best_local(data)
    data["focus"] = best["name"]
    data["focus_type"] = best.get("type")

    log(chat_id, "focus", best["name"])  # daily limit

    plan = effective_plan(chat_id)

    msg = bot.send_message(
        chat_id,
        "🔥 <b>Главное действие сейчас:</b>\n\n"
        f"<b>{best['name']}</b>\n"
        f"Тип: <b>{type_label(best.get('type'))}</b>",
        reply_markup=result_kb(plan)
    )
    data["result_msg_id"] = msg.message_id

# =========================
# TIMERS
# =========================
def schedule_check(chat_id: int, minutes: int = 10):
    cancel_timer(chat_id, "check")

    def check():
        try:
            bot.send_message(chat_id, "Как идёт?", reply_markup=progress_kb())
            log(chat_id, "check_sent", f"{minutes}m")
        except Exception:
            pass

    t = threading.Timer(minutes * 60, check)
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

def schedule_support_after_ok_two_month(chat_id: int):
    cancel_timer(chat_id, "support")

    def support():
        try:
            plan = effective_plan(chat_id)
            if plan != "two_month":
                return
            data = user_data.get(chat_id)
            if not data:
                return
            t = data.get("focus_type")
            msg = pick(MOTIVATION_OK_BY_TYPE, t)
            bot.send_message(chat_id, f"Мотивация: {msg}")
            log(chat_id, "support_sent", "ok+10m")
        except Exception:
            pass

    tmr = threading.Timer(10 * 60, support)
    timers.setdefault(chat_id, {})["support"] = tmr
    tmr.start()

# =========================
# RESULT BUTTONS
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("res:"))
def result_actions(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)

    if not data or data.get("step") != "result":
        bot.answer_callback_query(call.id, "Нажми 🚀 Начать действие")
        return

    if data.get("result_msg_id") and call.message.message_id != data["result_msg_id"]:
        bot.answer_callback_query(call.id, "Это старое сообщение")
        return

    if data.get("result_locked"):
        bot.answer_callback_query(call.id, "Уже принято ✅")
        return

    cmd = call.data.split(":", 1)[1]
    focus = data.get("focus") or "это действие"
    t = data.get("focus_type")
    plan = effective_plan(chat_id)

    data["result_locked"] = True
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    if cmd == "start":
        cancel_all_timers(chat_id)
        log(chat_id, "started", focus)

        bot.send_message(chat_id, f"🚀 Ты начал: <b>{focus}</b>")
        bot.send_message(chat_id, f"Мотивация: {pick(MOTIVATION_START_BY_TYPE, t)}")
        bot.send_message(chat_id, "Я не буду отвлекать.\nЧерез 10 минут спрошу, как идёт.")

        schedule_check(chat_id, 10)

        data["step"] = "started"
        bot.answer_callback_query(call.id, "Погнали 🔥")
        return

    if cmd == "delay10":
        cancel_all_timers(chat_id)
        log(chat_id, "delayed", "10m")
        bot.send_message(chat_id, "Ок.\nЯ напомню через 10 минут.", reply_markup=menu_kb())
        schedule_remind(chat_id, 10)
        data["step"] = "idle"
        bot.answer_callback_query(call.id, "Ок ⏸")
        return

    if cmd == "delay30":
        if plan not in ("two_month", "month", "day"):
            bot.send_message(chat_id, "🕒 30 минут доступно в Premium.", reply_markup=menu_kb())
            data["step"] = "idle"
            bot.answer_callback_query(call.id, "Ок")
            return

        cancel_all_timers(chat_id)
        log(chat_id, "delayed", "30m")
        bot.send_message(chat_id, "Ок.\nЯ напомню через 30 минут.", reply_markup=menu_kb())
        schedule_remind(chat_id, 30)
        data["step"] = "idle"
        bot.answer_callback_query(call.id, "Ок 🕒")
        return

    if cmd == "skip":
        cancel_all_timers(chat_id)
        log(chat_id, "skip", focus)
        bot.send_message(chat_id, "Ок.\nИногда лучше не давить на себя.", reply_markup=menu_kb())
        data["step"] = "idle"
        bot.answer_callback_query(call.id, "Ок")
        return

# =========================
# PROGRESS (через 10 минут)
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("prog:"))
def progress_handler(call):
    chat_id = call.message.chat.id
    data = user_data.get(chat_id)

    if not data:
        bot.answer_callback_query(call.id, "Нажми 🚀 Начать действие")
        return

    val = call.data.split(":", 1)[1]
    t = data.get("focus_type")
    plan = effective_plan(chat_id)

    log(chat_id, "progress", val)

    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    if val == "ok":
        bot.send_message(chat_id, "👍 Принято: Норм.")
        bot.send_message(chat_id, f"Мотивация: {pick(MOTIVATION_OK_BY_TYPE, t)}")
        if plan == "two_month":
            schedule_support_after_ok_two_month(chat_id)
        bot.answer_callback_query(call.id, "✅")
        return

    if val == "hard":
        bot.send_message(chat_id, "😵 Принято: Тяжело.")
        bot.send_message(chat_id, f"Мотивация: {MOTIVATION_HARD_BASE}")
        bot.send_message(chat_id, f"Мотивация: {pick(MOTIVATION_HARD_BY_TYPE, t)}")
        bot.answer_callback_query(call.id, "Ок")
        return

    if val == "quit":
        bot.send_message(chat_id, "❌ Принято: Бросил.")
        bot.send_message(chat_id, f"Мотивация: {random.choice(QUIT_TEXTS)}", reply_markup=quit_kb())
        bot.answer_callback_query(call.id, "Ок")
        return

# =========================
# QUIT ACTIONS
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("quit:"))
def quit_handler(call):
    chat_id = call.message.chat.id
    cmd = call.data.split(":", 1)[1]
    log(chat_id, "quit_action", cmd)

    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    if cmd == "retry":
        bot.send_message(chat_id, "Ок. Начнём заново — выбери шаг поменьше 🙂", reply_markup=menu_kb())
        start_energy_flow(chat_id)
        bot.answer_callback_query(call.id, "Ок")
        return

    if cmd == "later":
        bot.send_message(chat_id, "Ок. Вернёшься позже — нажми 🚀 Начать действие.", reply_markup=menu_kb())
        bot.answer_callback_query(call.id, "Ок")
        return

    if cmd == "new":
        start_energy_flow(chat_id)
        bot.answer_callback_query(call.id, "Ок")
        return

# =========================
# PREMIUM BUY (Telegram Payments)
# =========================
PLAN_PRICES_KZT = {
    "day": 299,
    "week": 399,
    "month": 1499,
    "two_month": 2299,
}

@bot.callback_query_handler(func=lambda c: c.data.startswith("buy:"))
def buy_handler(call):
    chat_id = call.message.chat.id
    plan = call.data.split(":", 1)[1]

    if plan not in PLAN_DAYS:
        bot.answer_callback_query(call.id, "Ошибка")
        return

    if not PROVIDER_TOKEN:
        bot.answer_callback_query(call.id, "Оплата не настроена")
        bot.send_message(
            chat_id,
            "⚠️ Оплата пока не подключена (нет PROVIDER_TOKEN).\n"
            "Можно подключить Telegram Payments или включить вручную через админа.\n\n"
            "Если хочешь — я добавлю команду админа /grant.",
            reply_markup=menu_kb()
        )
        return

    price = PLAN_PRICES_KZT[plan]
    title = f"Premium {PLAN_TITLES[plan]}"
    desc = f"Доступ к Premium на {PLAN_DAYS[plan]} дней"
    payload = f"sub:{plan}:{chat_id}:{int(time.time())}"

    prices = [types.LabeledPrice(label=title, amount=price * 100)]

    bot.answer_callback_query(call.id, "Открываю оплату…")
    bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=desc,
        provider_token=PROVIDER_TOKEN,
        currency="KZT",
        prices=prices,
        start_parameter="premium",
        payload=payload
    )

@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(pre_checkout_q):
    bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)

@bot.message_handler(content_types=["successful_payment"])
def successful_payment(m):
    chat_id = m.chat.id
    payload = (m.successful_payment.invoice_payload or "")
    try:
        parts = payload.split(":")
        if len(parts) >= 2 and parts[0] == "sub":
            plan = parts[1]
            if plan in PLAN_DAYS:
                set_sub(chat_id, plan, PLAN_DAYS[plan])
                bot.send_message(chat_id, f"✅ Premium активирован: <b>{PLAN_TITLES[plan]}</b>", reply_markup=menu_kb())
                return
    except Exception:
        pass

    bot.send_message(chat_id, "✅ Оплата получена. Но я не смог распознать план. Напиши в поддержку/админу.", reply_markup=menu_kb())

# =========================
# ADMIN grant (ручная выдача)
# =========================
@bot.message_handler(commands=["grant"])
def grant_cmd(m):
    chat_id = m.chat.id
    if chat_id not in ADMIN_IDS:
        return

    parts = (m.text or "").split()
    if len(parts) < 3:
        bot.send_message(chat_id, "Формат: /grant <user_id> <day|week|month|two_month>", reply_markup=menu_kb())
        return

    uid = parts[1].strip()
    plan = parts[2].strip()
    if not uid.isdigit() or plan not in PLAN_DAYS:
        bot.send_message(chat_id, "Ошибка. Пример: /grant 123456789 month", reply_markup=menu_kb())
        return

    uid_i = int(uid)
    set_sub(uid_i, plan, PLAN_DAYS[plan])
    bot.send_message(chat_id, f"✅ Выдал {PLAN_TITLES[plan]} пользователю {uid_i}", reply_markup=menu_kb())

# =========================
# RUN
# =========================
if __name__ == "__main__":
    init_db()
    print("Bot started")

    try:
        bot.infinity_polling(skip_pending=True, none_stop=True, timeout=60, long_polling_timeout=60)
    except ApiTelegramException as e:
        if "409" in str(e):
            print("409 conflict: another instance is running. Stop the other instance and restart.")
            raise
        raise

