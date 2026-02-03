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

PROVIDER_TOKEN = (os.getenv("PROVIDER_TOKEN") or "").strip()
PAY_MODE = (os.getenv("PAY_MODE") or "manual").strip().lower()  # manual | telegram

# реквизит карты для ручной оплаты
CARD_REQUISITES = (os.getenv("CARD_REQUISITES") or "4400430232294519").strip()

ADMIN_IDS_ENV = (os.getenv("ADMIN_IDS") or "").strip()
ADMIN_IDS: set[int] = set()
if ADMIN_IDS_ENV:
    for x in ADMIN_IDS_ENV.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))
if not ADMIN_IDS:
    ADMIN_IDS = {8311003582}

KZ_TZ = timezone(timedelta(hours=5))
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")


# =========================
# LIMITS
# =========================
FREE_DAILY_USES = 3
WEEK_DAILY_USES = 5
# month/day/two_month: unlimited


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
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            name TEXT,
            phone TEXT,
            created_at TEXT
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
# USERS (name + phone)
# =========================
def get_user_profile(chat_id: int) -> Tuple[Optional[str], Optional[str]]:
    with db_lock, db() as c:
        cur = c.cursor()
        cur.execute("SELECT name, phone FROM users WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()
        if not row:
            return (None, None)
        return (row[0], row[1])

def upsert_user_name(chat_id: int, name: str):
    name = (name or "").strip()
    with db_lock, db() as c:
        c.execute("""
            INSERT INTO users(chat_id, name, phone, created_at)
            VALUES(?,?,NULL,?)
            ON CONFLICT(chat_id) DO UPDATE SET name=excluded.name
        """, (chat_id, name, now_iso()))
        c.commit()

def upsert_user_phone(chat_id: int, phone: str):
    phone = (phone or "").strip()
    with db_lock, db() as c:
        c.execute("""
            INSERT INTO users(chat_id, name, phone, created_at)
            VALUES(?,NULL,?,?)
            ON CONFLICT(chat_id) DO UPDATE SET phone=excluded.phone
        """, (chat_id, phone, now_iso()))
        c.commit()


# =========================
# SUBSCRIPTIONS
# =========================
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

PLAN_PRICES_KZT = {
    "day": 299,
    "week": 399,
    "month": 1499,
    "two_month": 2299,
}

def get_sub(chat_id: int) -> Tuple[str, datetime]:
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
        return "two_month"
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
        "step": "idle",
        "energy_now": None,
        "energy_msg_id": None,
        "energy_locked": False,
        "actions": [],
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
MENU_TEXTS = {
    "🚀 Начать действие",
    "⭐ Premium",
    "👤 Профиль",
    "📊 Статистика",
    "❓ Как пользоваться",
    "💳 Оплатил / Отправить чек",
    "⬅️ Назад в меню",
}

def menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚀 Начать действие", "⭐ Premium")
    kb.row("📊 Статистика", "👤 Профиль")
    kb.row("❓ Как пользоваться")
    return kb

def payment_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("💳 Оплатил / Отправить чек", "⭐ Premium")
    kb.row("🚀 Начать действие")
    kb.row("📊 Статистика", "👤 Профиль")
    kb.row("❓ Как пользоваться")
    return kb

def contact_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("📱 Поделиться контактом", request_contact=True))
    kb.add(types.KeyboardButton("⬅️ Назад в меню"))
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
    kb.add(*[types.InlineKeyboardButton(str(i), callback_data=f"score:{i}") for i in range(1, 6)])
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

def premium_menu_kb():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🟢 Day (299₸)", callback_data="buy:day"))
    kb.add(types.InlineKeyboardButton("🟡 Week (399₸)", callback_data="buy:week"))
    kb.add(types.InlineKeyboardButton("🟠 Month (1499₸)", callback_data="buy:month"))
    kb.add(types.InlineKeyboardButton("🔴 2 Month (2299₸)", callback_data="buy:two_month"))
    return kb


# =========================
# MANUAL PAY (NO OCR) — чек → админу → approve/reject + 10–15 sec delay
# =========================
PENDING_PAYMENTS: Dict[int, Dict[str, Any]] = {}  # user_id -> {"plan":..., "ts":..., "receipt_ts":..., "review_delay":...}

def admin_review_kb(user_id: int, plan: str):
    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Подтвердить", callback_data=f"admin:approve:{user_id}:{plan}"),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f"admin:reject:{user_id}:{plan}")
    )
    return kb

def manual_payment_text(plan_code: str) -> str:
    price = PLAN_PRICES_KZT.get(plan_code, 0)
    plan_title = PLAN_TITLES.get(plan_code, plan_code)
    return (
        "💳 <b>Оплата по реквизиту</b>\n\n"
        f"План: <b>{plan_title}</b>\n"
        f"Сумма: <b>{price} ₸</b>\n\n"
        "📌 <b>Реквизит (карта):</b>\n"
        f"<code>{CARD_REQUISITES}</code>\n\n"
        "После оплаты нажми <b>💳 Оплатил / Отправить чек</b> и пришли чек (фото или PDF)."
    )


# =========================
# SCORING HELPERS (упрощенно, оставил твою логику)
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
        energy_bonus = 6 - s["energy"]
        total = (s["influence"] * 2 + s["urgency"] * 2 + s["meaning"] * 1 + energy_bonus * ew)
        if total > best_score:
            best_score = total
            best = a
    return best


# =========================
# START / MENU
# =========================
def send_welcome(chat_id: int):
    bot.send_message(
        chat_id,
        "Привет! 👋\n"
        "Я помогу <b>быстро выбрать одно главное действие</b> и аккуратно поддержу.\n\n"
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

    # onboarding: name -> contact -> energy
    name, phone = get_user_profile(chat_id)

    if not name:
        user_data[chat_id]["step"] = "ask_name"
        bot.send_message(chat_id, "Давай познакомимся 🙂\nКак тебя зовут?", reply_markup=types.ReplyKeyboardRemove())
        return

    if not phone:
        user_data[chat_id]["step"] = "ask_contact"
        bot.send_message(
            chat_id,
            f"Приятно, <b>{name}</b> 🤝\nТеперь поделись контактом кнопкой ниже:",
            reply_markup=contact_kb()
        )
        return

    # go to energy
    user_data[chat_id]["step"] = "energy"
    bot.send_message(
        chat_id,
        "Отлично 👍\nДавай определим энергию.",
        reply_markup=menu_kb()
    )
    msg = bot.send_message(chat_id, "Твоя энергия сейчас?", reply_markup=energy_kb())
    user_data[chat_id]["energy_msg_id"] = msg.message_id
    user_data[chat_id]["energy_locked"] = False

def show_profile(chat_id: int):
    name, phone = get_user_profile(chat_id)
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

    bot.send_message(
        chat_id,
        "👤 <b>Профиль</b>\n\n"
        f"Имя: <b>{name or '—'}</b>\n"
        f"Телефон: <b>{phone or '—'}</b>\n\n"
        f"План: <b>{plan_title}</b>\n"
        f"Активен до: <b>{exp_text}</b>\n"
        f"Лимит действий: <b>{limit_text}</b>\n",
        reply_markup=menu_kb()
    )

def show_premium(chat_id: int):
    plan = effective_plan(chat_id)
    p, exp = get_sub(chat_id)
    exp_text = exp.strftime("%Y-%m-%d %H:%M") if is_active(p, exp) else "—"
    bot.send_message(
        chat_id,
        "⭐ <b>Premium</b>\n\n"
        f"Текущий план: <b>{PLAN_TITLES.get(plan, plan)}</b>\n"
        f"Активен до: <b>{exp_text}</b>\n\n"
        "Выбери план:",
        reply_markup=premium_menu_kb()
    )

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
    if txt == "⭐ Premium":
        show_premium(chat_id)
        return
    if txt == "⬅️ Назад в меню":
        bot.send_message(chat_id, "Ок 👌", reply_markup=menu_kb())
        return
    if txt == "💳 Оплатил / Отправить чек":
        if chat_id not in PENDING_PAYMENTS:
            bot.send_message(chat_id, "Сначала выбери план в ⭐ Premium.", reply_markup=menu_kb())
            return
        user_data.setdefault(chat_id, {})
        user_data[chat_id]["step"] = "wait_receipt"
        bot.send_message(chat_id, "Ок ✅ Пришли чек сюда (фото или PDF).")
        return


# =========================
# ONBOARDING: NAME
# =========================
@bot.message_handler(func=lambda m: m.chat.id in user_data and user_data[m.chat.id].get("step") == "ask_name")
def ask_name_handler(m):
    chat_id = m.chat.id
    txt = (m.text or "").strip()
    if not txt:
        bot.send_message(chat_id, "Напиши имя текстом 🙂")
        return
    if len(txt) < 2 or len(txt) > 30:
        bot.send_message(chat_id, "Имя слишком короткое/длинное. Напиши нормально 🙂")
        return

    upsert_user_name(chat_id, txt)
    user_data[chat_id]["step"] = "ask_contact"
    bot.send_message(chat_id, f"Отлично, <b>{txt}</b> ✅\nПоделись контактом:", reply_markup=contact_kb())

# =========================
# ONBOARDING: CONTACT
# =========================
@bot.message_handler(content_types=["contact"])
def contact_handler(m):
    chat_id = m.chat.id
    data = user_data.get(chat_id, {})
    if data.get("step") != "ask_contact":
        return

    phone = (m.contact.phone_number or "").strip()
    if not phone:
        bot.send_message(chat_id, "Не смог прочитать номер. Попробуй ещё раз.", reply_markup=contact_kb())
        return

    upsert_user_phone(chat_id, phone)
    bot.send_message(chat_id, "✅ Контакт сохранён! Поехали 🚀", reply_markup=menu_kb())
    start_energy_flow(chat_id)


# =========================
# ENERGY / ACTIONS / SCORING (оставлено как у тебя, сокращено)
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
    try:
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.answer_callback_query(call.id, "Ок ✅")
    bot.send_message(chat_id, "✍️ Напиши <b>минимум 3</b> действия (каждое с новой строки):", reply_markup=menu_kb())

@bot.message_handler(func=lambda m: m.chat.id in user_data and user_data[m.chat.id].get("step") == "actions")
def actions_input(m):
    chat_id = m.chat.id
    lines = [x.strip() for x in (m.text or "").split("\n") if x.strip()]
    if len(lines) < 3 or len(lines) > 7:
        bot.send_message(chat_id, "Нужно <b>3–7</b> действий. Каждое с новой строки.", reply_markup=menu_kb())
        return

    data = user_data[chat_id]
    data["actions"] = [{"name": a, "type": None, "scores": {}} for a in lines]
    data["cur_action"] = 0
    data["cur_crit"] = 0
    data["step"] = "typing"
    data["answered_type_msgs"].clear()
    ask_action_type(chat_id)

def ask_action_type(chat_id: int):
    data = user_data[chat_id]
    a = data["actions"][data["cur_action"]]
    msg = bot.send_message(chat_id, f"Выбери тип для:\n<b>{a['name']}</b>", reply_markup=type_kb())
    data["expected_type_msg_id"] = msg.message_id

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

    t = call.data.split(":", 1)[1]
    a = data["actions"][data["cur_action"]]
    a["type"] = t

    data["cur_action"] += 1
    if data["cur_action"] >= len(data["actions"]):
        data["cur_action"] = 0
        data["cur_crit"] = 0
        data["step"] = "scoring"
        ask_next_score(chat_id)
    else:
        ask_action_type(chat_id)

def ask_next_score(chat_id: int):
    data = user_data[chat_id]
    a = data["actions"][data["cur_action"]]
    key, title = CRITERIA[data["cur_crit"]]
    hint = HINTS.get(key, "")
    msg = bot.send_message(
        chat_id,
        f"Действие: <b>{a['name']}</b>\n"
        f"Тип: <b>{type_label(a.get('type'))}</b>\n\n"
        f"Оцени: <b>{title}</b>\n<i>{hint}</i>",
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

    score = int(call.data.split(":", 1)[1])
    a = data["actions"][data["cur_action"]]
    key, _ = CRITERIA[data["cur_crit"]]
    a["scores"][key] = score

    data["cur_crit"] += 1
    if data["cur_crit"] >= len(CRITERIA):
        data["cur_crit"] = 0
        data["cur_action"] += 1
        if data["cur_action"] >= len(data["actions"]):
            best = pick_best_local(data)
            bot.send_message(chat_id, f"🔥 Главное действие:\n<b>{best['name']}</b>", reply_markup=menu_kb())
            data["step"] = "idle"
            return

    ask_next_score(chat_id)


# =========================
# BUY PREMIUM (manual)
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("buy:"))
def buy_handler(call):
    chat_id = call.message.chat.id
    plan = call.data.split(":", 1)[1]
    if plan not in PLAN_DAYS:
        bot.answer_callback_query(call.id, "Ошибка")
        return

    if PAY_MODE == "telegram":
        bot.answer_callback_query(call.id, "Сейчас включен telegram, не manual")
        return

    PENDING_PAYMENTS[chat_id] = {
        "plan": plan,
        "ts": time.time(),
        "receipt_ts": None,
        "review_delay": None,
    }
    bot.answer_callback_query(call.id, "Ок ✅")
    bot.send_message(chat_id, manual_payment_text(plan), reply_markup=payment_kb())


# =========================
# RECEIPT HANDLER (photo/pdf)
# =========================
@bot.message_handler(content_types=["photo", "document"])
def receipt_handler(m):
    chat_id = m.chat.id

    if chat_id not in user_data or user_data[chat_id].get("step") != "wait_receipt":
        return

    pending = PENDING_PAYMENTS.get(chat_id)
    if not pending:
        bot.send_message(chat_id, "Сначала выбери план в ⭐ Premium.", reply_markup=menu_kb())
        user_data[chat_id]["step"] = "idle"
        return

    plan = pending["plan"]

    # фиксируем задержку 10–15 сек
    pending["receipt_ts"] = time.time()
    pending["review_delay"] = random.randint(10, 15)

    bot.send_message(chat_id, "✅ Чек получен. Проверяю…")
    log(chat_id, "manual_receipt_received", plan)

    name, phone = get_user_profile(chat_id)
    caption = (
        "🧾 <b>Новый чек</b>\n"
        f"User ID: <code>{chat_id}</code>\n"
        f"Имя: <b>{name or '—'}</b>\n"
        f"Телефон: <b>{phone or '—'}</b>\n"
        f"План: <b>{PLAN_TITLES[plan]}</b>\n"
        f"Сумма: <b>{PLAN_PRICES_KZT[plan]} ₸</b>\n\n"
        "Нажми кнопку ниже:"
    )

    for admin_id in ADMIN_IDS:
        try:
            if m.content_type == "photo":
                bot.send_photo(admin_id, m.photo[-1].file_id, caption=caption, reply_markup=admin_review_kb(chat_id, plan))
            else:
                bot.send_document(admin_id, m.document.file_id, caption=caption, reply_markup=admin_review_kb(chat_id, plan))
        except Exception:
            pass

    user_data[chat_id]["step"] = "idle"
    log(chat_id, "manual_receipt_sent_to_admin", plan)


# =========================
# ADMIN DECISION (approve/reject) with min 10–15 sec
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("admin:"))
def admin_decision(call):
    admin_id = call.message.chat.id
    if admin_id not in ADMIN_IDS:
        bot.answer_callback_query(call.id, "Нет доступа")
        return

    parts = call.data.split(":")
    if len(parts) < 4:
        bot.answer_callback_query(call.id, "Ошибка данных")
        return

    action = parts[1].strip()
    user_id = int(parts[2].strip())
    plan = parts[3].strip()

    try:
        bot.edit_message_reply_markup(admin_id, call.message.message_id, reply_markup=None)
    except Exception:
        pass

    pending = PENDING_PAYMENTS.get(user_id)
    if not pending:
        bot.answer_callback_query(call.id, "Заявка уже обработана / не найдена")
        return

    if plan not in PLAN_DAYS:
        bot.answer_callback_query(call.id, "Неизвестный план")
        return

    if action == "reject":
        PENDING_PAYMENTS.pop(user_id, None)
        bot.send_message(admin_id, f"❌ Отклонено. Пользователь <code>{user_id}</code>.")
        bot.send_message(user_id, "❌ Не удалось подтвердить оплату.\nПроверь чек и попробуй снова.", reply_markup=menu_kb())
        log(user_id, "manual_pay_rejected", plan)
        bot.answer_callback_query(call.id, "Ок ❌")
        return

    if action == "approve":
        receipt_ts = pending.get("receipt_ts") or time.time()
        review_delay = pending.get("review_delay") or random.randint(10, 15)

        elapsed = time.time() - receipt_ts
        remain = review_delay - elapsed

        def activate_subscription():
            set_sub(user_id, plan, PLAN_DAYS[plan])
            PENDING_PAYMENTS.pop(user_id, None)
            bot.send_message(admin_id, f"✅ Подтверждено. Подписка активирована пользователю <code>{user_id}</code>.")
            bot.send_message(user_id, f"✅ Оплата подтверждена!\nPremium активирован: <b>{PLAN_TITLES[plan]}</b>", reply_markup=menu_kb())
            log(user_id, "manual_pay_approved", plan)

        if remain > 0:
            bot.send_message(admin_id, f"⏳ Проверка… (подтверждение через ~{int(remain)} сек)")
            threading.Timer(remain, activate_subscription).start()
        else:
            activate_subscription()

        bot.answer_callback_query(call.id, "Ок ✅")
        return

    bot.answer_callback_query(call.id, "Неизвестная команда")


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
