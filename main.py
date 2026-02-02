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

# Telegram Payments provider token (Stripe/YooKassa/etc.)
PROVIDER_TOKEN = (os.getenv("PAYMENT_PROVIDER_TOKEN") or "").strip()  # <-- add in env
CURRENCY = "KZT"  # Kazakhstan Tenge

bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
KZ_TZ = timezone(timedelta(hours=5))

ADMIN_IDS = {8311003582}  # твой chat_id (админ — без лимитов)

# =========================
# PRICING / PLANS
# =========================
# prices are in "minor units": KZT * 100 (tiyin)
PLAN_DAY = "day"
PLAN_WEEK = "week"
PLAN_MONTH = "month"
PLAN_2MONTH = "2month"
PLAN_FREE = "free"

PLAN_META = {
    PLAN_DAY:   {"title": "Premium 1 день",   "days": 1,  "price_kzt": 299},
    PLAN_WEEK:  {"title": "Premium 7 дней",   "days": 7,  "price_kzt": 399},
    PLAN_MONTH: {"title": "Premium 30 дней",  "days": 30, "price_kzt": 1490},
    PLAN_2MONTH:{"title": "Premium 60 дней",  "days": 60, "price_kzt": 2290},
}

# Feature rules per plan
PLAN_RULES = {
    PLAN_FREE: {
        "max_daily_focus": 3,                 # сколько "выборов" в день
        "allowed_delays": [10],               # кнопки отсрочки
        "checkins": 1,                        # сколько раз спрашивать "Как идёт?"
        "checkin_gap_min": 10,                # через сколько минут
        "extra_support_after_ok": 0,          # доп поддержка после "Норм"
    },
    PLAN_DAY: {
        "max_daily_focus": None,              # безлимит
        "allowed_delays": [10],               # только 10
        "checkins": 1,
        "checkin_gap_min": 10,
        "extra_support_after_ok": 0,
    },
    PLAN_WEEK: {
        "max_daily_focus": 10,                # до 10 выборов/день
        "allowed_delays": [10, 30],           # 10 и 30
        "checkins": 1,
        "checkin_gap_min": 10,
        "extra_support_after_ok": 0,
    },
    PLAN_MONTH: {
        "max_daily_focus": None,
        "allowed_delays": [10, 30],           # можно оставить 30
        "checkins": 1,                        # только 1 раз
        "checkin_gap_min": 10,
        "extra_support_after_ok": 0,
    },
    PLAN_2MONTH: {
        "max_daily_focus": None,
        "allowed_delays": [10, 20, 30],       # 10/20/30
        "checkins": 1,                        # "вопрос" 1 раз
        "checkin_gap_min": 10,
        "extra_support_after_ok": 1,          # потом ещё поддержка через 10 (без вопроса)
    },
}

# =========================
# DATABASE (SQLite)
# =========================
DB = "data.sqlite3"
db_lock = threading.Lock()

def db():
    return sqlite3.connect(DB, check_same_thread=False)

def init_db():
    with db_lock, db() as c:
        # logs
        c.execute("""
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            event TEXT,
            value TEXT,
            created_at TEXT
        )
        """)
        # subscriptions
        c.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            chat_id INTEGER PRIMARY KEY,
            plan TEXT NOT NULL,
            paid_until TEXT NOT NULL,
            updated_at TEXT NOT NULL
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

def get_subscription(chat_id: int) -> Tuple[str, Optional[datetime]]:
    """
    return (plan, paid_until_dt) or ('free', None)
    """
    with db_lock, db() as c:
        cur = c.cursor()
        cur.execute("SELECT plan, paid_until FROM subscriptions WHERE chat_id=?", (chat_id,))
        row = cur.fetchone()

    if not row:
        return PLAN_FREE, None

    plan, paid_until_s = row[0], row[1]
    try:
        paid_until = datetime.fromisoformat(paid_until_s)
    except Exception:
        return PLAN_FREE, None

    now = datetime.now(KZ_TZ)
    if paid_until > now:
        return plan, paid_until

    return PLAN_FREE, None

def set_subscription(chat_id: int, plan: str, days: int):
    now = datetime.now(KZ_TZ)
    cur_plan, cur_until = get_subscription(chat_id)

    # если подписка ещё активна — продлеваем от paid_until, иначе от now
    base = cur_until if cur_until else now
    new_until = base + timedelta(days=days)

    with db_lock, db() as c:
        c.execute("""
        INSERT INTO subscriptions(chat_id, plan, paid_until, updated_at)
        VALUES(?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET
            plan=excluded.plan,
            paid_until=excluded.paid_until,
            updated_at=excluded.updated_at
        """, (chat_id, plan, new_until.isoformat(), now.isoformat()))
        c.commit()

    log(chat_id, "sub_set", f"{plan}|until={new_until.isoformat()}")

def plan_rules(chat_id: int) -> Dict[str, Any]:
    if chat_id in ADMIN_IDS:
        return PLAN_RULES[PLAN_2MONTH]  # админ как максимальный
    plan, _ = get_subscription(chat_id)
    return PLAN_RULES.get(plan, PLAN_RULES[PLAN_FREE])

def plan_name(chat_id: int) -> str:
    if chat_id in ADMIN_IDS:
        return "ADMIN"
    plan, until = get_subscription(chat_id)
    if plan == PLAN_FREE:
        return "FREE"
    if until:
        return f"{plan.upper()} до {until.strftime('%Y-%m-%d %H:%M')}"
    return plan.upper()

def can_use_focus(chat_id: int) -> bool:
    if chat_id in ADMIN_IDS:
        return True
    rules = plan_rules(chat_id)
    limit = rules.get("max_daily_focus")
    if limit is None:
        return True
    used = count_today(chat_id, "focus")
    return used < int(limit)

# =========================
# SESSION STATE
# =========================
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
    cancel_timer(chat_id, "remind")
    cancel_timer(chat_id, "check")
    cancel_timer(chat_id, "support")

def reset_session(chat_id: int):
    sessions[chat_id] = {
        "step": "energy",           # energy -> actions -> typing -> scoring -> result -> started/delayed/idle
        "energy": None,             # low/mid/high
        "energy_msg_id": None,
        "energy_locked": False,

        "actions": [],              # [{"name":..., "type":..., "scores":{...}}]
        "cur_action": 0,
        "cur_crit": 0,

        "expected_type_msg_id": None,
        "expected_score_msg_id": None,

        "focus": None,
        "focus_type": None,

        "result_msg_id": None,
        "result_locked": False,
    }

# =========================
# UI
# =========================
MENU_TEXTS = {"🚀 Начать", "📊 Статистика", "❓ Как пользоваться", "⭐ Premium"}

def menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🚀 Начать", "⭐ Premium")
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

def result_kb_for_chat(chat_id: int):
    rules = plan_rules(chat_id)
    delays = rules.get("allowed_delays", [10])

    kb = types.InlineKeyboardMarkup()
    # row 1
    kb.row(types.InlineKeyboardButton("🚀 Я начал", callback_data="act:start"))

    # row 2: delays
    btns = []
    if 10 in delays:
        btns.append(types.InlineKeyboardButton("⏸ Отложить 10 минут", callback_data="act:delay10"))
    if 20 in delays:
        btns.append(types.InlineKeyboardButton("⏸ Отложить 20 минут", callback_data="act:delay20"))
    if 30 in delays:
        btns.append(types.InlineKeyboardButton("🕒 Попозже (30 минут)", callback_data="act:delay30"))
    if btns:
        # распределим по строкам
        if len(btns) == 1:
            kb.row(btns[0])
        elif len(btns) == 2:
            kb.row(btns[0], btns[1])
        else:
            kb.row(btns[0], btns[1])
            kb.row(btns[2])

    kb.row(types.InlineKeyboardButton("❌ Не хочу сейчас", callback_data="act:skip"))
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
    kb.row(
        types.InlineKeyboardButton("🔁 Попробовать снова (меньше)", callback_data="quit:retry"),
        types.InlineKeyboardButton("🕒 Вернуться позже", callback_data="quit:later"),
    )
    kb.row(types.InlineKeyboardButton("🚀 Начать другое действие", callback_data="quit:new"))
    return kb

def premium_kb():
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🟡 1 день — 299₸", callback_data=f"buy:{PLAN_DAY}"))
    kb.row(types.InlineKeyboardButton("🟠 7 дней — 399₸", callback_data=f"buy:{PLAN_WEEK}"))
    kb.row(types.InlineKeyboardButton("🔵 30 дней — 1490₸", callback_data=f"buy:{PLAN_MONTH}"))
    kb.row(types.InlineKeyboardButton("🟣 60 дней — 2290₸", callback_data=f"buy:{PLAN_2MONTH}"))
    return kb

# =========================
# SCORING LOGIC
# =========================
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

def pick_best(data: Dict[str, Any]) -> Dict[str, Any]:
    level = data.get("energy", "mid")
    weight = {"low": 2.0, "mid": 1.0, "high": 0.6}.get(level, 1.0)

    best = None
    best_score = -10**9
    for a in data["actions"]:
        s = a["scores"]  # dict
        energy_bonus = 6 - s["energy"]
        score = (
            s["influence"] * 2 +
            s["urgency"] * 2 +
            s["meaning"] * 1 +
            energy_bonus * weight
        )
        if score > best_score:
            best_score = score
            best = a
    return best

# =========================
# MOTIVATION (твои тексты)
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
    return random.choice(arr) if arr else "Сделай самый маленький шаг. Этого достаточно."

# =========================
# FLOW
# =========================
def start_flow(chat_id: int):
    # лимит по плану
    if not can_use_focus(chat_id):
        rules = plan_rules(chat_id)
        limit = rules.get("max_daily_focus", 3)
        bot.send_message(
            chat_id,
            f"⛔ Лимит на сегодня исчерпан.\n"
            f"Твой лимит: <b>{limit}</b> выбор(а/ов) в день.\n\n"
            f"Хочешь больше — открой ⭐ Premium.",
            reply_markup=menu_kb()
        )
        return

    cancel_all(chat_id)
    reset_session(chat_id)
    bot.send_message(chat_id, f"Текущий план: <b>{plan_name(chat_id)}</b>", reply_markup=menu_kb())
    msg = bot.send_message(chat_id, "Твоя энергия сейчас?", reply_markup=energy_kb())
    sessions[chat_id]["energy_msg_id"] = msg.message_id
    log(chat_id, "start_flow", "ok")

def ask_actions(chat_id: int):
    bot.send_message(chat_id, "✍️ Напиши <b>как минимум 3</b> действия (каждое с новой строки):", reply_markup=menu_kb())

def ask_type(chat_id: int):
    s = sessions[chat_id]
    a = s["actions"][s["cur_action"]]
    msg = bot.send_message(chat_id, f"Выбери тип для:\n<b>{a['name']}</b>", reply_markup=type_kb())
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
    s["step"] = "result"
    s["result_locked"] = False

    best = pick_best(s)
    s["focus"] = best["name"]
    s["focus_type"] = best.get("type")

    # логируем выбор (это и есть "использование бота" для лимита)
    log(chat_id, "focus", s["focus"])

    msg = bot.send_message(
        chat_id,
        f"🔥 <b>Главное действие сейчас:</b>\n\n"
        f"<b>{best['name']}</b>\n"
        f"Тип: <b>{type_label(best.get('type'))}</b>",
        reply_markup=result_kb_for_chat(chat_id)
    )
    s["result_msg_id"] = msg.message_id

# =========================
# MENU / COMMANDS
# =========================
@bot.message_handler(commands=["start"])
def cmd_start(m):
    start_flow(m.chat.id)

@bot.message_handler(commands=["premium"])
def cmd_premium(m):
    show_premium(m.chat.id)

@bot.message_handler(func=lambda m: (m.text or "").strip() in MENU_TEXTS)
def menu_handler(m):
    chat_id = m.chat.id
    txt = (m.text or "").strip()

    if txt == "🚀 Начать":
        start_flow(chat_id)
        return

    if txt == "⭐ Premium":
        show_premium(chat_id)
        return

    if txt == "❓ Как пользоваться":
        bot.send_message(
            chat_id,
            "Как пользоваться:\n"
            "1) 🚀 Начать\n"
            "2) Выбери энергию\n"
            "3) Напиши минимум 3 действия\n"
            "4) Для каждого выбери тип\n"
            "5) Оцени 4 критерия (кнопками)\n"
            "6) Получишь главное действие\n"
            "7) Нажми: Я начал / Отложить / Попозже / Не хочу\n\n"
            "После «Я начал» бот НЕ отвлекает.\n"
            "Через 10 минут спросит «Как идёт?» 🙂",
            reply_markup=menu_kb()
        )
        return

    if txt == "📊 Статистика":
        today_focus = count_today(chat_id, "focus")
        today_started = count_today(chat_id, "started")
        today_progress = count_today(chat_id, "progress")
        bot.send_message(
            chat_id,
            f"📊 Статистика (сегодня)\n"
            f"• Выборов: <b>{today_focus}</b>\n"
            f"• Начал: <b>{today_started}</b>\n"
            f"• Ответов «как идёт»: <b>{today_progress}</b>\n\n"
            f"План: <b>{plan_name(chat_id)}</b>",
            reply_markup=menu_kb()
        )
        return

def show_premium(chat_id: int):
    plan, until = get_subscription(chat_id)
    until_txt = until.strftime("%Y-%m-%d %H:%M") if until else "—"
    bot.send_message(
        chat_id,
        "⭐ <b>Premium планы</b>\n\n"
        "🟡 1 день — 299₸ (пробный)\n"
        "🟠 7 дней — 399₸\n"
        "🔵 30 дней — 1490₸\n"
        "🟣 60 дней — 2290₸ (максимум)\n\n"
        f"Текущий: <b>{plan.upper()}</b>\n"
        f"Активен до: <b>{until_txt}</b>\n\n"
        "Нажми план ниже, чтобы оплатить:",
        reply_markup=premium_kb()
    )

# =========================
# ENERGY PICK
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("energy:"))
def energy_pick(c):
    chat_id = c.message.chat.id
    if chat_id not in sessions:
        bot.answer_callback_query(c.id, "Нажми 🚀 Начать")
        return

    s = sessions[chat_id]
    if s.get("energy_locked"):
        bot.answer_callback_query(c.id, "Уже выбрано ✅")
        return

    # только актуальное сообщение энергии
    if s.get("energy_msg_id") and c.message.message_id != s["energy_msg_id"]:
        bot.answer_callback_query(c.id, "Это старое сообщение")
        return

    code = c.data.split(":", 1)[1]  # low/mid/high
    s["energy"] = code
    s["energy_locked"] = True
    s["step"] = "actions"

    log(chat_id, "energy", code)

    try:
        bot.edit_message_text(
            f"✅ Энергия: <b>{energy_label(code)}</b>",
            chat_id, c.message.message_id
        )
    except Exception:
        pass

    bot.answer_callback_query(c.id, "Ок ✅")
    ask_actions(chat_id)

# =========================
# ACTIONS INPUT
# =========================
@bot.message_handler(func=lambda m: (m.chat.id in sessions and sessions[m.chat.id].get("step") == "actions"))
def actions_input(m):
    chat_id = m.chat.id
    txt = (m.text or "").strip()

    # не воспринимаем меню как "действия"
    if txt in MENU_TEXTS:
        return

    lines = [x.strip() for x in txt.split("\n") if x.strip()]
    if len(lines) < 3:
        bot.send_message(chat_id, "❌ Нужно минимум 3 действия (каждое с новой строки).", reply_markup=menu_kb())
        return

    s = sessions[chat_id]
    s["actions"] = [{"name": name, "type": None, "scores": {}} for name in lines]
    s["cur_action"] = 0
    s["cur_crit"] = 0
    s["step"] = "typing"

    log(chat_id, "actions_count", str(len(lines)))
    ask_type(chat_id)

# =========================
# TYPE PICK
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("type:"))
def type_pick(c):
    chat_id = c.message.chat.id
    if chat_id not in sessions:
        bot.answer_callback_query(c.id, "Нажми 🚀 Начать")
        return

    s = sessions[chat_id]
    if s.get("step") != "typing":
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
        bot.edit_message_text(
            f"✅ <b>{a['name']}</b> — {type_label(t)}",
            chat_id, c.message.message_id
        )
    except Exception:
        pass

    bot.answer_callback_query(c.id, "Ок ✅")

    # переходим к оценкам
    s["cur_crit"] = 0
    s["step"] = "scoring"
    ask_score(chat_id)

# =========================
# SCORE PICK
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("score:"))
def score_pick(c):
    chat_id = c.message.chat.id
    if chat_id not in sessions:
        bot.answer_callback_query(c.id, "Нажми 🚀 Начать")
        return

    s = sessions[chat_id]
    if s.get("step") != "scoring":
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
        bot.edit_message_text(
            f"✅ {title}: <b>{score}</b>",
            chat_id, c.message.message_id
        )
    except Exception:
        pass

    bot.answer_callback_query(c.id, "Ок ✅")

    s["cur_crit"] += 1
    if s["cur_crit"] >= len(CRITERIA):
        # закончили критерии для одного действия
        s["cur_action"] += 1
        if s["cur_action"] >= len(s["actions"]):
            show_result(chat_id)
            return

        # следующее действие: сначала тип
        s["cur_crit"] = 0
        s["step"] = "typing"
        ask_type(chat_id)
        return

    ask_score(chat_id)

# =========================
# RESULT ACTIONS
# =========================
def schedule_check(chat_id: int, minutes: int):
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

def schedule_support(chat_id: int, minutes: int, t: Optional[str]):
    cancel_timer(chat_id, "support")

    def support():
        try:
            msg = pick(MOTIVATION_OK_BY_TYPE, t)
            bot.send_message(chat_id, f"Мотивация: {msg}")
            log(chat_id, "support_sent", f"{minutes}m")
        except Exception:
            pass

    tt = threading.Timer(minutes * 60, support)
    timers.setdefault(chat_id, {})["support"] = tt
    tt.start()

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

@bot.callback_query_handler(func=lambda c: c.data.startswith("act:"))
def act_handler(c):
    chat_id = c.message.chat.id
    if chat_id not in sessions:
        bot.answer_callback_query(c.id, "Нажми 🚀 Начать")
        return

    s = sessions[chat_id]
    if s.get("step") != "result" or not s.get("focus"):
        bot.answer_callback_query(c.id, "Сначала получи «главное действие» через 🚀 Начать")
        return

    # защита от двойного клика
    if s.get("result_locked"):
        bot.answer_callback_query(c.id, "Уже принято ✅")
        return

    # только актуальное result-сообщение
    if s.get("result_msg_id") and c.message.message_id != s["result_msg_id"]:
        bot.answer_callback_query(c.id, "Это старое сообщение")
        return

    cmd = c.data.split(":", 1)[1]
    focus = s["focus"]
    t = s.get("focus_type")

    # блокируем повторные клики, убираем клавиатуру
    s["result_locked"] = True
    try:
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    rules = plan_rules(chat_id)
    check_gap = int(rules.get("checkin_gap_min", 10))
    extra_support = int(rules.get("extra_support_after_ok", 0))

    if cmd == "start":
        log(chat_id, "started", focus)
        cancel_all(chat_id)

        # 1) отдельно
        bot.send_message(chat_id, f"🚀 Ты начал: <b>{focus}</b>")
        # 2) отдельно мотивация
        bot.send_message(chat_id, f"Мотивация: {pick(MOTIVATION_START_BY_TYPE, t)}")
        # 3) отдельно
        bot.send_message(chat_id, "Я не буду отвлекать.\nЧерез 10 минут спрошу, как идёт.")

        schedule_check(chat_id, check_gap)

        # для 2 months: после OK можно дать ещё поддержку (без вопроса)
        if extra_support:
            # мы запланируем поддержку только после OK (в progress_handler)
            pass

        bot.answer_callback_query(c.id, "Погнали 🔥")
        s["step"] = "started"
        return

    # delays
    if cmd.startswith("delay"):
        minutes = 10
        if cmd == "delay20":
            minutes = 20
        elif cmd == "delay30":
            minutes = 30

        allowed = rules.get("allowed_delays", [10])
        if minutes not in allowed:
            bot.send_message(chat_id, "⛔ Эта отсрочка доступна только в Premium плане.", reply_markup=menu_kb())
            bot.answer_callback_query(c.id, "Недоступно")
            s["step"] = "idle"
            return

        log(chat_id, "delayed", f"{minutes}m|{focus}")
        bot.send_message(chat_id, f"Ок.\nЯ напомню через {minutes} минут.", reply_markup=menu_kb())
        schedule_remind(chat_id, minutes)
        bot.answer_callback_query(c.id, "Ок ⏸")
        s["step"] = "idle"
        return

    if cmd == "skip":
        log(chat_id, "skip", focus)
        bot.send_message(chat_id, "Ок.\nИногда лучше не давить на себя.", reply_markup=menu_kb())
        bot.answer_callback_query(c.id, "Ок")
        s["step"] = "idle"
        return

# =========================
# PROGRESS HANDLER
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("prog:"))
def progress_handler(c):
    chat_id = c.message.chat.id
    if chat_id not in sessions:
        bot.answer_callback_query(c.id, "Нажми 🚀 Начать")
        return

    s = sessions[chat_id]
    val = c.data.split(":", 1)[1]
    t = s.get("focus_type")

    log(chat_id, "progress", val)

    # убираем кнопки (чтобы не жали 2 раза)
    try:
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    rules = plan_rules(chat_id)
    extra_support = int(rules.get("extra_support_after_ok", 0))

    if val == "ok":
        bot.send_message(chat_id, "👍 Принято: Норм.")
        bot.send_message(chat_id, f"Мотивация: {pick(MOTIVATION_OK_BY_TYPE, t)}")

        # для 2 months: ещё 10 минут тишины → поддержка (без вопроса)
        if extra_support:
            schedule_support(chat_id, 10, t)

        bot.answer_callback_query(c.id, "✅")
        return

    if val == "hard":
        bot.send_message(chat_id, "😵 Принято: Тяжело.")
        bot.send_message(chat_id, f"Мотивация: {MOTIVATION_HARD_BASE}")
        bot.send_message(chat_id, f"Мотивация: {pick(MOTIVATION_HARD_BY_TYPE, t)}")
        bot.answer_callback_query(c.id, "Ок")
        return

    if val == "quit":
        bot.send_message(chat_id, "❌ Принято: Бросил.")
        bot.send_message(chat_id, f"Мотивация: {random.choice(QUIT_TEXTS)}", reply_markup=quit_kb())
        bot.answer_callback_query(c.id, "Ок")
        return

# =========================
# QUIT ACTIONS
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("quit:"))
def quit_handler(c):
    chat_id = c.message.chat.id
    cmd = c.data.split(":", 1)[1]
    log(chat_id, "quit_action", cmd)

    try:
        bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=None)
    except Exception:
        pass

    if cmd == "retry":
        bot.send_message(chat_id, "Ок. Сделаем шаг меньше и начнём заново 🙂", reply_markup=menu_kb())
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

# =========================
# PREMIUM BUY FLOW (Telegram Payments)
# =========================
@bot.callback_query_handler(func=lambda c: c.data.startswith("buy:"))
def buy_handler(c):
    chat_id = c.message.chat.id
    plan = c.data.split(":", 1)[1]

    if not PROVIDER_TOKEN:
        bot.answer_callback_query(c.id, "Оплата не настроена")
        bot.send_message(
            chat_id,
            "⚠️ Оплата пока не подключена.\n"
            "Добавь переменную окружения <b>PAYMENT_PROVIDER_TOKEN</b> (Telegram Payments).",
            reply_markup=menu_kb()
        )
        return

    if plan not in PLAN_META:
        bot.answer_callback_query(c.id, "Неизвестный план")
        return

    meta = PLAN_META[plan]
    title = meta["title"]
    days = meta["days"]
    price_kzt = meta["price_kzt"]

    prices = [types.LabeledPrice(label=title, amount=price_kzt * 100)]

    payload = f"sub:{plan}"
    bot.answer_callback_query(c.id, "Открываю оплату...")

    bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=f"Доступ Premium на {days} дней. План: {plan.upper()}",
        invoice_payload=payload,
        provider_token=PROVIDER_TOKEN,
        currency=CURRENCY,
        prices=prices,
        need_name=False,
        need_phone_number=False,
        need_email=False,
        need_shipping_address=False,
        is_flexible=False,
    )

@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout_query(preq):
    # Telegram требует ответить OK
    bot.answer_pre_checkout_query(preq.id, ok=True)

@bot.message_handler(content_types=['successful_payment'])
def successful_payment(m):
    chat_id = m.chat.id
    payload = (m.successful_payment.invoice_payload or "").strip()

    if not payload.startswith("sub:"):
        bot.send_message(chat_id, "✅ Оплата прошла. (payload не распознан)", reply_markup=menu_kb())
        return

    plan = payload.split(":", 1)[1]
    if plan not in PLAN_META:
        bot.send_message(chat_id, "✅ Оплата прошла. План не распознан.", reply_markup=menu_kb())
        return

    days = PLAN_META[plan]["days"]
    set_subscription(chat_id, plan, days)

    plan_now, until = get_subscription(chat_id)
    until_txt = until.strftime("%Y-%m-%d %H:%M") if until else "—"
    bot.send_message(
        chat_id,
        "✅ <b>Premium активирован!</b>\n\n"
        f"План: <b>{plan_now.upper()}</b>\n"
        f"До: <b>{until_txt}</b>\n\n"
        "Теперь лимиты и напоминания расширены ⭐",
        reply_markup=menu_kb()
    )

# =========================
# RUN
# =========================
if __name__ == "__main__":
    init_db()
    print("Bot started")

    # устойчивый polling
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
