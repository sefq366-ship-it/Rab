"""
bot.py — вся логика бота: база, эмодзи, клавиатуры, статистика,
обработчики, планировщик.
Точка входа — main.py.
"""

import os
import re
from datetime import date, datetime, timedelta

import aiosqlite
from aiogram import Router, F, Bot
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from emoji_index import emoji_index

# ================================================================ #
#  КОНСТАНТЫ
# ================================================================ #
ADMIN_ID = 1012040843
DB_PATH = "bot.db"
MAX_QUANTITY = 20
TZ = pytz.timezone("Asia/Yekaterinburg")

MENU_BUTTONS = {"↩️ Отменить", "📊 Статистика", "🛠 Админка"}

router = Router()


# ================================================================ #
#  БАЗА ДАННЫХ
# ================================================================ #
async def init_db() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_name TEXT UNIQUE NOT NULL,
            emoji          TEXT DEFAULT '📦',
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS product_aliases (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL REFERENCES products(id),
            alias      TEXT UNIQUE NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mixes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER NOT NULL,
            product_id INTEGER NOT NULL REFERENCES products(id),
            quantity   INTEGER NOT NULL DEFAULT 1,
            date       TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER NOT NULL,
            send_at  TIMESTAMP NOT NULL,
            status   TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE TABLE IF NOT EXISTS day_flags (
            user_id     INTEGER NOT NULL,
            date        TEXT NOT NULL,
            asked_12    INTEGER DEFAULT 0,
            asked_16    INTEGER DEFAULT 0,
            report_sent INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, date)
        );
        CREATE INDEX IF NOT EXISTS idx_mixes_user_date ON mixes(user_id, date);
        """)
        await db.commit()


async def get_product_by_alias(alias: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT p.id, p.canonical_name, p.emoji
            FROM products p
            JOIN product_aliases a ON a.product_id = p.id
            WHERE LOWER(a.alias) = LOWER(?)
        """, (alias.strip(),))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_all_aliases() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT a.alias, p.id, p.canonical_name, p.emoji
            FROM product_aliases a
            JOIN products p ON p.id = a.product_id
        """)
        return [dict(r) for r in await cur.fetchall()]


async def create_product(canonical_name: str, emoji: str = "📦") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id FROM products WHERE canonical_name = ?",
            (canonical_name.strip(),))
        row = await cur.fetchone()
        if row:
            return row[0]
        cur = await db.execute(
            "INSERT INTO products (canonical_name, emoji) VALUES (?, ?)",
            (canonical_name.strip(), emoji))
        await db.commit()
        return cur.lastrowid


async def add_alias(product_id: int, alias: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO product_aliases (product_id, alias) VALUES (?, ?)",
                (product_id, alias.strip().lower()))
            await db.commit()
        except aiosqlite.IntegrityError:
            pass


async def get_all_products() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT p.id, p.canonical_name, p.emoji,
                   COUNT(a.id) AS aliases_count
            FROM products p
            LEFT JOIN product_aliases a ON a.product_id = p.id
            GROUP BY p.id ORDER BY p.created_at
        """)
        return [dict(r) for r in await cur.fetchall()]


async def get_product(pid: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, canonical_name, emoji FROM products WHERE id = ?", (pid,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def set_emoji(pid: int, emoji: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE products SET emoji = ? WHERE id = ?", (emoji, pid))
        await db.commit()


async def set_product_name(pid: int, name: str) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE products SET canonical_name = ? WHERE id = ?", (name, pid))
        await db.commit()


async def add_mix(user_id: int, product_id: int, quantity: int) -> int:
    today = str(date.today())
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO mixes (user_id, product_id, quantity, date) VALUES (?, ?, ?, ?)",
            (user_id, product_id, quantity, today))
        await db.commit()
        return cur.lastrowid


async def get_last_mix(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT m.id, m.quantity, m.created_at, p.canonical_name, p.emoji
            FROM mixes m JOIN products p ON p.id = m.product_id
            WHERE m.user_id = ? ORDER BY m.id DESC LIMIT 1
        """, (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def reduce_last_mix(user_id: int, remove_qty: int) -> bool:
    last = await get_last_mix(user_id)
    if not last:
        return False
    remaining = last["quantity"] - remove_qty
    async with aiosqlite.connect(DB_PATH) as db:
        if remaining > 0:
            await db.execute("UPDATE mixes SET quantity = ? WHERE id = ?",
                             (remaining, last["id"]))
        else:
            await db.execute("DELETE FROM mixes WHERE id = ?", (last["id"],))
        await db.commit()
    return True


async def count_today(user_id: int) -> int:
    today = str(date.today())
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM mixes WHERE user_id = ? AND date = ?",
            (user_id, today))
        row = await cur.fetchone()
        return row[0] if row else 0


async def create_reminder(user_id: int, minutes: int) -> None:
    send_at = datetime.now() + timedelta(minutes=minutes)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reminders SET status='cancelled' WHERE user_id=? AND status='pending'",
            (user_id,))
        await db.execute(
            "INSERT INTO reminders (user_id, send_at, status) VALUES (?, ?, 'pending')",
            (user_id, send_at))
        await db.commit()


async def get_due_reminders() -> list[dict]:
    now = datetime.now()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, user_id FROM reminders WHERE status='pending' AND send_at <= ?",
            (now,))
        return [dict(r) for r in await cur.fetchall()]


async def mark_reminder_sent(rid: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE reminders SET status='sent' WHERE id = ?", (rid,))
        await db.commit()


async def get_day_flags(user_id: int) -> dict:
    today = str(date.today())
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT asked_12, asked_16, report_sent FROM day_flags WHERE user_id=? AND date=?",
            (user_id, today))
        row = await cur.fetchone()
        if row:
            return dict(row)
        await db.execute("INSERT INTO day_flags (user_id, date) VALUES (?, ?)",
                         (user_id, today))
        await db.commit()
        return {"asked_12": 0, "asked_16": 0, "report_sent": 0}


async def set_day_flag(user_id: int, flag: str) -> None:
    if flag not in ("asked_12", "asked_16", "report_sent"):
        raise ValueError("Неизвестный флаг")
    today = str(date.today())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO day_flags (user_id, date) VALUES (?, ?)",
                         (user_id, today))
        await db.execute(
            f"UPDATE day_flags SET {flag}=1 WHERE user_id=? AND date=?",
            (user_id, today))
        await db.commit()


# ================================================================ #
#  ЭМОДЗИ
# ================================================================ #
EMOJI_CATEGORIES: dict[str, list[str]] = {}


def _iter_group(group_name: str):
    if group_name not in emoji_index.groups:
        return
    group = emoji_index[group_name]
    for subgroup_name in group.subgroups:
        for emoji_obj in group[subgroup_name]:
            yield emoji_obj


def _filter_by_keywords(group_name: str, keywords: list[str]) -> list[str]:
    result = []
    for emoji_obj in _iter_group(group_name):
        name_lower = emoji_obj.name.lower()
        if any(kw in name_lower for kw in keywords):
            result.append(emoji_obj.emoji)
    seen, unique = set(), []
    for e in result:
        if e not in seen:
            seen.add(e)
            unique.append(e)
    return unique


EMOJI_CATEGORIES["🍎 Фрукты"] = _filter_by_keywords("Food & Drink", [
    "fruit", "apple", "banana", "grape", "melon", "peach", "pear",
    "pineapple", "mango", "lemon", "cherry", "berry", "strawberry",
    "kiwi", "coconut", "avocado", "tomato", "orange", "tangerine",
])
EMOJI_CATEGORIES["🥤 Напитки"] = _filter_by_keywords("Food & Drink", [
    "beverage", "drink", "cup", "glass", "bottle", "coffee", "tea",
    "juice", "milk", "beer", "wine", "cocktail", "mate", "bubble tea",
])
EMOJI_CATEGORIES["🍦 Десерты"] = _filter_by_keywords("Food & Drink", [
    "ice cream", "dessert", "cake", "candy", "chocolate", "cookie",
    "doughnut", "pie", "sweet", "honey", "lollipop", "custard",
])
EMOJI_CATEGORIES["🍞 Выпечка"] = _filter_by_keywords("Food & Drink", [
    "bread", "bakery", "croissant", "baguette", "pretzel", "bagel",
    "pancake", "waffle", "sandwich", "pizza", "taco", "burger",
])
EMOJI_CATEGORIES["🥛 Молочное"] = _filter_by_keywords("Food & Drink", [
    "milk", "cheese", "butter", "cream", "yogurt", "dairy",
])
EMOJI_CATEGORIES["🌿 Природа"] = _filter_by_keywords("Animals & Nature", [
    "flower", "leaf", "tree", "plant", "herb", "seed", "mushroom",
])
EMOJI_CATEGORIES["📦 Разное"] = [
    "📦", "🎁", "⭐", "🔥", "💎", "🏆", "⚡", "🎯", "💼", "🔧",
    "✅", "❌", "❤️", "💙", "💚", "💛", "🧡", "💜", "🖤", "🤍",
    "👍", "👎", "👏", "🙌", "💪", "🤝", "✌️", "👌", "🫡", "🫶",
]
EMOJI_CATEGORIES = {k: v for k, v in EMOJI_CATEGORIES.items() if v}

_KEYWORD_TO_EMOJI = {
    "манго": "🥭", "mango": "🥭", "яблок": "🍎", "apple": "🍎",
    "банан": "🍌", "banana": "🍌", "виноград": "🍇", "grape": "🍇",
    "клубник": "🍓", "strawberry": "🍓", "апельсин": "🍊", "orange": "🍊",
    "лимон": "🍋", "lemon": "🍋", "арбуз": "🍉", "watermelon": "🍉",
    "персик": "🍑", "peach": "🍑", "вишн": "🍒", "cherry": "🍒",
    "киви": "🥝", "kiwi": "🥝", "ананас": "🍍", "pineapple": "🍍",
    "кокос": "🥥", "coconut": "🥥", "авокадо": "🥑", "avocado": "🥑",
    "мороженое": "🍦", "ice cream": "🍦", "icecream": "🍦",
    "йогурт": "🍨", "yogurt": "🍨", "торт": "🎂", "cake": "🎂",
    "печень": "🍪", "cookie": "🍪", "шоколад": "🍫", "chocolate": "🍫",
    "конфет": "🍬", "candy": "🍬", "пирог": "🥧", "pie": "🥧",
    "пончик": "🍩", "doughnut": "🍩", "кофе": "☕", "coffee": "☕",
    "чай": "🍵", "tea": "🍵", "сок": "🧃", "juice": "🧃",
    "кола": "🥤", "cola": "🥤", "вода": "💧", "water": "💧",
    "молоко": "🥛", "milk": "🥛", "пиво": "🍺", "beer": "🍺",
    "вино": "🍷", "wine": "🍷", "коктейль": "🍹", "cocktail": "🍹",
    "хлеб": "🍞", "bread": "🍞", "багет": "🥖", "baguette": "🥖",
    "круассан": "🥐", "croissant": "🥐", "пицца": "🍕", "pizza": "🍕",
    "бургер": "🍔", "burger": "🍔", "протеин": "💪", "protein": "💪",
    "витамин": "💊", "vitamin": "💊",
}


def suggest_emojis(product_name: str, limit: int = 8) -> list[str]:
    name_lower = product_name.lower().strip()
    result: list[str] = []
    for keyword, emoji in _KEYWORD_TO_EMOJI.items():
        if keyword in name_lower and emoji not in result:
            result.append(emoji)
    if len(result) < limit:
        words = [w for w in name_lower.split() if len(w) > 2]
        for group_name in emoji_index.groups:
            for emoji_obj in _iter_group(group_name):
                if emoji_obj.emoji in result:
                    continue
                if any(w in emoji_obj.name.lower() for w in words):
                    result.append(emoji_obj.emoji)
                    if len(result) >= limit:
                        break
            if len(result) >= limit:
                break
    if not result:
        result = ["📦", "🎁", "⭐", "🔥", "💎", "🏆", "⚡", "🎯"]
    return result[:limit]


def is_single_emoji(text: str) -> bool:
    text = text.strip()
    if not text or len(text) > 8:
        return False
    for group_name in emoji_index.groups:
        for emoji_obj in _iter_group(group_name):
            if emoji_obj.emoji == text:
                return True
    return False


def get_category_emojis(category_name: str) -> list[str]:
    return EMOJI_CATEGORIES.get(category_name, [])


def get_all_category_names() -> list[str]:
    return list(EMOJI_CATEGORIES.keys())


# ================================================================ #
#  КЛАВИАТУРЫ
# ================================================================ #
def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура внизу экрана."""
    return ReplyKeyboardMarkup(
        keyboard=[[
            KeyboardButton(text="↩️ Отменить"),
            KeyboardButton(text="📊 Статистика"),
            KeyboardButton(text="🛠 Админка"),
        ]],
        resize_keyboard=True,
        persistent=True,
    )


def quantity_keyboard(product_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for n in range(1, 6):
        kb.button(text=str(n), callback_data=f"qty:{product_id}:{n}")
    kb.button(text="✏️ Другое", callback_data=f"qty_other:{product_id}")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(3, 2, 1, 1)
    return kb.as_markup()


def confirm_big_quantity_keyboard(product_id: int, qty: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Да, {qty} замесов",
              callback_data=f"qty_confirm:{product_id}:{qty}")
    kb.button(text="✏️ Нет, исправить", callback_data=f"qty_other:{product_id}")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()


def undo_keyboard(quantity: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    partial = list(range(1, quantity))
    labels = [str(n) for n in partial] if len(partial) <= 2 else ["1", "2"]
    for label in labels:
        kb.button(text=label, callback_data=f"undo:{int(label)}")
    kb.button(text=f"Всё ({quantity})", callback_data=f"undo:{quantity}")
    kb.button(text="🔙 Назад", callback_data="cancel")
    total = len(labels) + 2
    kb.adjust(total if total <= 4 else 3)
    return kb.as_markup()


def emoji_suggest_keyboard(emojis: list[str], product_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for e in emojis:
        kb.button(text=e, callback_data=f"emoji_set:{product_id}:{e}")
    kb.button(text="📂 Больше категорий", callback_data=f"emoji_cats:{product_id}")
    kb.button(text="⌨️ Ввести вручную", callback_data=f"emoji_manual:{product_id}")
    kb.button(text="⏭ Пропустить", callback_data=f"emoji_skip:{product_id}")
    rows = [4] * (len(emojis) // 4)
    if len(emojis) % 4:
        rows.append(len(emojis) % 4)
    rows += [1, 1, 1]
    kb.adjust(*rows)
    return kb.as_markup()


def emoji_categories_keyboard(product_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for cat in get_all_category_names():
        kb.button(text=cat, callback_data=f"emoji_cat:{product_id}:{cat}")
    kb.button(text="🔙 Назад", callback_data=f"emoji_back:{product_id}")
    n = len(get_all_category_names())
    rows = [2] * (n // 2)
    if n % 2:
        rows.append(1)
    rows.append(1)
    kb.adjust(*rows)
    return kb.as_markup()


EMOJI_PER_PAGE = 24


def emoji_category_page_keyboard(product_id: int, category: str,
                                page: int = 0) -> InlineKeyboardMarkup:
    emojis = get_category_emojis(category)
    start = page * EMOJI_PER_PAGE
    chunk = emojis[start:start + EMOJI_PER_PAGE]
    kb = InlineKeyboardBuilder()
    for e in chunk:
        kb.button(text=e, callback_data=f"emoji_set:{product_id}:{e}")
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️", callback_data=f"emoji_page:{product_id}:{category}:{page-1}"))
    if start + EMOJI_PER_PAGE < len(emojis):
        nav.append(InlineKeyboardButton(
            text="➡️", callback_data=f"emoji_page:{product_id}:{category}:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.row(InlineKeyboardButton(text="🔙 К категориям",
                                callback_data=f"emoji_cats:{product_id}"))
    kb.row(InlineKeyboardButton(text="⌨️ Ввести вручную",
                                callback_data=f"emoji_manual:{product_id}"))
    kb.row(InlineKeyboardButton(text="⏭ Пропустить",
                                callback_data=f"emoji_skip:{product_id}"))
    rows = [4] * (len(chunk) // 4)
    if len(chunk) % 4:
        rows.append(len(chunk) % 4)
    if nav:
        rows.append(len(nav))
    rows += [1, 1, 1]
    kb.adjust(*rows)
    return kb.as_markup()


def emoji_manual_keyboard(product_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📂 Выбрать из категорий", callback_data=f"emoji_cats:{product_id}")
    kb.button(text="⏭ Пропустить (📦)", callback_data=f"emoji_skip:{product_id}")
    kb.adjust(1)
    return kb.as_markup()


def after_mix_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Отменить последний", callback_data="undo_start")
    kb.adjust(1)
    return kb.as_markup()


def daily_report_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить сейчас", callback_data="report_now")
    kb.button(text="⏰ +10 мин", callback_data="report_snooze:10")
    kb.button(text="⏰ +30 мин", callback_data="report_snooze:30")
    kb.adjust(1, 2)
    return kb.as_markup()


def empty_day_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Всё ок, пока не начал", callback_data="empty_ok")
    kb.button(text="➕ Добавлю замес", callback_data="empty_add")
    kb.adjust(1)
    return kb.as_markup()


def admin_main_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Продукты", callback_data="admin_products")
    kb.button(text="📊 Статистика", callback_data="admin_stats")
    kb.button(text="🔙 Закрыть", callback_data="admin_close")
    kb.adjust(2, 1)
    return kb.as_markup()


def admin_products_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить продукт", callback_data="admin_product_add")
    kb.button(text="✏️ Редактировать", callback_data="admin_product_edit")
    kb.button(text="🔙 Назад", callback_data="admin_main")
    kb.adjust(1)
    return kb.as_markup()


def admin_product_list_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in products:
        kb.button(text=f"{p['emoji']} {p['canonical_name']}",
                  callback_data=f"admin_pedit:{p['id']}")
    kb.button(text="🔙 Назад", callback_data="admin_products")
    kb.adjust(1)
    return kb.as_markup()


def admin_product_edit_keyboard(product_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить название", callback_data=f"admin_pname:{product_id}")
    kb.button(text="😊 Изменить эмодзи", callback_data=f"admin_pemoji:{product_id}")
    kb.button(text="➕ Добавить синоним", callback_data=f"admin_palias:{product_id}")
    kb.button(text="🔙 Назад", callback_data="admin_product_edit")
    kb.adjust(1)
    return kb.as_markup()


def admin_add_steps_keyboard(step: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if step > 1:
        kb.button(text="🔙 Назад", callback_data=f"admin_add_back:{step-1}")
    kb.button(text="❌ Отмена", callback_data="admin_products")
    kb.adjust(2 if step > 1 else 1)
    return kb.as_markup()


def stats_period_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📅 День", callback_data="stats:day")
    kb.button(text="📆 Неделя", callback_data="stats:week")
    kb.button(text="🗓 Месяц", callback_data="stats:month")
    kb.button(text="📈 Всё время", callback_data="stats:all")
    kb.button(text="🔙 Назад", callback_data="admin_main")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def stats_back_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 К периодам", callback_data="admin_stats")
    kb.button(text="🏠 В админку", callback_data="admin_main")
    kb.adjust(2)
    return kb.as_markup()


def cancel_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="cancel")
    return kb.as_markup()


# ================================================================ #
#  СТАТИСТИКА
# ================================================================ #
MONTHS_RU_NOM = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
                 "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]
WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг",
               "пятница", "суббота", "воскресенье"]


def bar(value: int, max_value: int, width: int = 10) -> str:
    if max_value <= 0:
        return "░" * width
    filled = round(value / max_value * width)
    return "█" * filled + "░" * (width - filled)


def plural(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 19:
        return many
    last = n_abs % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def format_mixes(n: int) -> str:
    return f"{n} {plural(n, 'замес', 'замеса', 'замесов')}"


def human_date(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


async def _by_product(user_id: int, where: str, params: tuple) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(f"""
            SELECT p.canonical_name, p.emoji, SUM(m.quantity) AS total
            FROM mixes m JOIN products p ON p.id = m.product_id
            WHERE m.user_id = ? AND {where}
            GROUP BY p.id ORDER BY total DESC, p.canonical_name
        """, (user_id, *params))
        return [dict(r) for r in await cur.fetchall()]


async def _total(user_id: int, where: str, params: tuple) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"SELECT COALESCE(SUM(quantity),0) FROM mixes WHERE user_id=? AND {where}",
            (user_id, *params))
        row = await cur.fetchone()
        return row[0] if row else 0


async def _day_times(user_id: int, d: str) -> tuple:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT MIN(created_at), MAX(created_at) FROM mixes WHERE user_id=? AND date=?",
            (user_id, d))
        row = await cur.fetchone()
        if not row or not row[0]:
            return None, None
        first = row[0].split(" ")[1][:5] if " " in str(row[0]) else None
        last = row[1].split(" ")[1][:5] if row[1] and " " in str(row[1]) else None
        return first, last


async def _active_days(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(DISTINCT date) FROM mixes WHERE user_id=?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else 0


async def stats_day(user_id: int) -> str:
    today = date.today()
    d_str = str(today)
    by_product = await _by_product(user_id, "m.date = ?", (d_str,))
    if not by_product:
        return (f"📅 <b>{human_date(today)}</b> ({WEEKDAYS_RU[today.weekday()]})\n\n"
                f"Пока нет ни одного замеса за сегодня.")
    total = sum(r["total"] for r in by_product)
    first, last = await _day_times(user_id, d_str)
    lines = [f"📅 <b>{human_date(today)}</b> ({WEEKDAYS_RU[today.weekday()]})", ""]
    for r in by_product:
        lines.append(f"{r['emoji']} {r['canonical_name']} — <b>{r['total']}</b>")
    lines += ["", "───────────────", f"📦 Всего: <b>{format_mixes(total)}</b>"]
    if first:
        lines.append(f"⏱ Первый: {first}")
    if last:
        lines.append(f"⏱ Последний: {last}")
    yesterday = str(today - timedelta(days=1))
    y_total = await _total(user_id, "date = ?", (yesterday,))
    if y_total > 0:
        diff = total - y_total
        if diff > 0:
            lines.append(f"📈 К вчера: +{diff}")
        elif diff < 0:
            lines.append(f"📉 К вчера: {diff}")
        else:
            lines.append("➖ К вчера: без изменений")
    return "\n".join(lines)


async def stats_week(user_id: int) -> str:
    today = date.today()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    by_product = await _by_product(
        user_id, "m.date BETWEEN ? AND ?", (str(start), str(end)))
    if not by_product:
        return (f"📆 <b>Неделя {start.strftime('%d.%m')}–{end.strftime('%d.%m')}</b>\n\n"
                f"За эту неделю замесов ещё нет.")
    total = sum(r["total"] for r in by_product)
    max_val = by_product[0]["total"]
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"📆 <b>НЕДЕЛЯ {start.strftime('%d.%m')}–{end.strftime('%d.%m')}</b>", ""]
    for i, r in enumerate(by_product):
        medal = medals[i] if i < 3 else "▫️"
        lines.append(f"{medal} {r['emoji']} {r['canonical_name']} — <b>{r['total']}</b>\n"
                     f"   <code>{bar(r['total'], max_val, 10)}</code>")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT date, SUM(quantity) FROM mixes WHERE user_id=? AND date BETWEEN ? AND ? GROUP BY date",
            (user_id, str(start), str(end)))
        daily = {r[0]: r[1] for r in await cur.fetchall()}
    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    max_day = max(daily.values()) if daily else 0
    lines += ["", "📊 По дням:"]
    for i in range(7):
        d = start + timedelta(days=i)
        cnt = daily.get(str(d), 0)
        b = bar(cnt, max_day, 8) if max_day else "░" * 8
        lines.append(f"{day_names[i]} <code>{b}</code> {cnt}")
    lines += ["", "───────────────", f"📦 Всего: <b>{format_mixes(total)}</b>",
              f"📊 В среднем: {total/7:.1f}/день"]
    return "\n".join(lines)


async def stats_month(user_id: int) -> str:
    today = date.today()
    prefix = today.strftime("%Y-%m")
    by_product = await _by_product(user_id, "strftime('%Y-%m', m.date) = ?", (prefix,))
    if not by_product:
        return (f"🗓 <b>{MONTHS_RU_NOM[today.month-1]} {today.year}</b>\n\n"
                f"В этом месяце замесов пока нет.")
    total = sum(r["total"] for r in by_product)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT date, SUM(quantity) FROM mixes WHERE user_id=? AND strftime('%Y-%m', date)=? GROUP BY date",
            (user_id, prefix))
        daily = await cur.fetchall()
    best_day = max(daily, key=lambda r: r[1]) if daily else None
    worst_day = min(daily, key=lambda r: r[1]) if daily else None
    days_in_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    days_passed = min(today, days_in_month).day
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"🗓 <b>{MONTHS_RU_NOM[today.month-1].upper()} {today.year}</b>", "",
             f"📦 Всего: <b>{format_mixes(total)}</b>",
             f"📊 В среднем: {total/days_passed:.1f}/день"]
    if best_day:
        lines.append(f"🔥 Лучший день: {human_date(date.fromisoformat(best_day[0]))} — {best_day[1]}")
    if worst_day and len(daily) > 1:
        lines.append(f"📉 Слабый день: {human_date(date.fromisoformat(worst_day[0]))} — {worst_day[1]}")
    lines += ["", "ТОП-3:"]
    for i, r in enumerate(by_product[:3]):
        lines.append(f"{medals[i]} {r['emoji']} {r['canonical_name']} — {r['total']}")
    return "\n".join(lines)


async def stats_all(user_id: int) -> str:
    by_product = await _by_product(user_id, "1=1", ())
    if not by_product:
        return "📈 Пока нет ни одного замеса за всё время."
    total = sum(r["total"] for r in by_product)
    active_days = await _active_days(user_id)
    avg = total / active_days if active_days else 0
    medals = ["🥇", "🥈", "🥉"]
    lines = ["📈 <b>ВСЁ ВРЕМЯ</b>", "",
             f"📦 Всего: <b>{format_mixes(total)}</b>",
             f"📅 Дней с замесами: {active_days}",
             f"📊 В среднем: {avg:.1f}/день", "", "ТОП продуктов:"]
    for i, r in enumerate(by_product[:5]):
        medal = medals[i] if i < 3 else "▫️"
        lines.append(f"{medal} {r['emoji']} {r['canonical_name']} — {r['total']}")
    return "\n".join(lines)


async def build_stats(user_id: int, period: str) -> str:
    if period == "day":
        return await stats_day(user_id)
    if period == "week":
        return await stats_week(user_id)
    if period == "month":
        return await stats_month(user_id)
    if period == "all":
        return await stats_all(user_id)
    raise ValueError(period)


# ================================================================ #
#  FSM
# ================================================================ #
class NewProduct(StatesGroup):
    waiting_name = State()
    waiting_emoji = State()
    waiting_manual_emoji = State()


class QtyInput(StatesGroup):
    waiting_qty = State()


class AdminAddProduct(StatesGroup):
    waiting_name = State()
    waiting_emoji = State()
    waiting_aliases = State()


class AdminEditProduct(StatesGroup):
    waiting_name = State()
    waiting_emoji = State()
    waiting_alias = State()


# ================================================================ #
#  ВСПОМОГАТЕЛЬНОЕ
# ================================================================ #
def parse_quantity(text: str) -> tuple[str, int]:
    text = text.strip()
    m = re.search(r"\s+(\d{1,3})$", text)
    if m:
        qty = int(m.group(1))
        name = text[:m.start()].strip()
        if name and qty >= 1:
            return name, qty
    return text, 1


async def resolve_product(text: str) -> dict | None:
    exact = await get_product_by_alias(text)
    if exact:
        return exact
    try:
        from rapidfuzz import process, fuzz
    except ImportError:
        return None
    aliases = await get_all_aliases()
    if not aliases:
        return None
    alias_map = {a["alias"]: a for a in aliases}
    match, score, _ = process.extractOne(
        text.lower(), list(alias_map.keys()), scorer=fuzz.ratio)
    if match and score >= 85:
        a = alias_map[match]
        return {"id": a["id"], "canonical_name": a["canonical_name"],
                "emoji": a["emoji"]}
    return None


async def record_mix(message: Message, product: dict, qty: int):
    await add_mix(message.from_user.id, product["id"], qty)
    total_today = await count_today(message.from_user.id)
    qty_text = f"×{qty}" if qty > 1 else ""
    await message.answer(
        f"✅ {product['emoji']} {product['canonical_name']} {qty_text}\n"
        f"Сегодня: {total_today}",
        reply_markup=after_mix_keyboard())


async def record_mix_cb(call: CallbackQuery, product: dict, qty: int):
    await add_mix(call.from_user.id, product["id"], qty)
    total_today = await count_today(call.from_user.id)
    qty_text = f"×{qty}" if qty > 1 else ""
    await call.message.answer(
        f"✅ {product['emoji']} {product['canonical_name']} {qty_text}\n"
        f"Сегодня: {total_today}",
        reply_markup=after_mix_keyboard())


# ================================================================ #
#  КОМАНДЫ И КНОПКИ МЕНЮ
# ================================================================ #
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет!\n\n"
        "Пиши замесы просто: <code>фм</code>, <code>фм 3</code>, "
        "<code>прот 75 2</code>.\n"
        "Если продукт новый — бот спросит название и эмодзи.\n\n"
        "Кнопки внизу — для быстрого доступа.",
        reply_markup=main_menu_keyboard())


@router.message(Command("undo"))
async def cmd_undo(message: Message):
    await start_undo(message.from_user.id, message)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    await message.answer("Выбери период:", reply_markup=stats_period_keyboard())


@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Нет доступа.")
        return
    await message.answer("🛠 Админ-панель:", reply_markup=admin_main_keyboard())


@router.message(F.text == "↩️ Отменить")
async def btn_undo(message: Message):
    await start_undo(message.from_user.id, message)


@router.message(F.text == "📊 Статистика")
async def btn_stats(message: Message):
    await message.answer("Выбери период:", reply_markup=stats_period_keyboard())


@router.message(F.text == "🛠 Админка")
async def btn_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("Нет доступа.")
        return
    await message.answer("🛠 Админ-панель:", reply_markup=admin_main_keyboard())


# ================================================================ #
#  ВВОД ТЕКСТА
# ================================================================ #
@router.message(
    F.text
    & ~F.text.startswith("/")
    & ~F.text.in_(MENU_BUTTONS)
)
async def handle_text(message: Message, state: FSMContext):
    current = await state.get_state()
    if current == NewProduct.waiting_name.state:
        await new_product_save_name(message, state)
        return
    if current == NewProduct.waiting_manual_emoji.state:
        await new_product_save_manual_emoji(message, state)
        return
    if current == QtyInput.waiting_qty.state:
        await qty_manual_input(message, state)
        return
    if current == AdminAddProduct.waiting_name.state:
        await admin_add_name(message, state)
        return
    if current == AdminAddProduct.waiting_emoji.state:
        await admin_add_emoji(message, state)
        return
    if current == AdminAddProduct.waiting_aliases.state:
        await admin_add_aliases(message, state)
        return
    if current == AdminEditProduct.waiting_name.state:
        await admin_edit_name(message, state)
        return
    if current == AdminEditProduct.waiting_emoji.state:
        await admin_edit_emoji(message, state)
        return
    if current == AdminEditProduct.waiting_alias.state:
        await admin_edit_alias(message, state)
        return
    await handle_mix_text(message, state)


async def handle_mix_text(message: Message, state: FSMContext):
    raw = message.text.strip()
    name_part, qty = parse_quantity(raw)
    product = await resolve_product(name_part)
    if not product:
        await state.update_data(alias=name_part, pending_qty=qty)
        await state.set_state(NewProduct.waiting_name)
        await message.answer(
            f"🤔 Не знаю продукт «{name_part}».\n"
            f"Напиши полное название (сохранится для будущего):")
        return
    if qty > MAX_QUANTITY:
        await state.update_data(product_id=product["id"], big_qty=qty)
        await message.answer(
            f"⚠️ {qty} замесов за раз — это много. Точно?",
            reply_markup=confirm_big_quantity_keyboard(product["id"], qty))
        return
    if qty == 1 and not re.search(r"\s+\d{1,3}$", raw):
        await message.answer(
            f"{product['emoji']} {product['canonical_name']} — сколько замесов?",
            reply_markup=quantity_keyboard(product["id"]))
        return
    await record_mix(message, product, qty)


async def new_product_save_name(message: Message, state: FSMContext):
    data = await state.get_data()
    alias = data.get("alias", "")
    pending_qty = data.get("pending_qty", 1)
    canonical = message.text.strip()
    pid = await create_product(canonical)
    await add_alias(pid, alias)
    await add_alias(pid, canonical.lower())
    await state.update_data(product_id=pid, canonical=canonical, pending_qty=pending_qty)
    await state.set_state(NewProduct.waiting_emoji)
    emojis = suggest_emojis(canonical)
    await message.answer(
        f"✅ Название: «{canonical}»\n\nВыбери эмодзи:",
        reply_markup=emoji_suggest_keyboard(emojis, pid))


async def new_product_save_manual_emoji(message: Message, state: FSMContext):
    text = message.text.strip()
    if not is_single_emoji(text):
        await message.answer(
            "Это не похоже на эмодзи. Скопируй символ из клавиатуры эмодзи "
            "и пришли ещё раз.")
        return
    data = await state.get_data()
    pid = data["product_id"]
    pending_qty = data.get("pending_qty", 1)
    await set_emoji(pid, text)
    await state.clear()
    product = {"id": pid, "canonical_name": data["canonical"], "emoji": text}
    await record_mix(message, product, pending_qty)


async def qty_manual_input(message: Message, state: FSMContext):
    text = message.text.strip()
    if not text.isdigit():
        await message.answer("Нужно число. Попробуй ещё раз:")
        return
    n = int(text)
    if n < 1:
        await message.answer("Число должно быть ≥ 1:")
        return
    data = await state.get_data()
    if n > MAX_QUANTITY:
        await message.answer(
            f"⚠️ {n} замесов — это много. Точно?",
            reply_markup=confirm_big_quantity_keyboard(data["qty_product_id"], n))
        return
    product = await get_product(data["qty_product_id"])
    await state.clear()
    await record_mix(message, product, n)


# ================================================================ #
#  CALLBACK: КОЛИЧЕСТВО
# ================================================================ #
@router.callback_query(F.data.startswith("qty:"))
async def cb_qty(call: CallbackQuery):
    _, pid, n = call.data.split(":")
    product = await get_product(int(pid))
    await call.message.delete()
    await record_mix_cb(call, product, int(n))
    await call.answer()


@router.callback_query(F.data.startswith("qty_other:"))
async def cb_qty_other(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    await state.update_data(qty_product_id=pid)
    await state.set_state(QtyInput.waiting_qty)
    await call.message.edit_text("Напиши число (от 1 до 20):",
                                 reply_markup=cancel_keyboard())
    await call.answer()


@router.callback_query(F.data.startswith("qty_confirm:"))
async def cb_qty_confirm(call: CallbackQuery):
    _, pid, n = call.data.split(":")
    product = await get_product(int(pid))
    await call.message.delete()
    await record_mix_cb(call, product, int(n))
    await call.answer()


# ================================================================ #
#  CALLBACK: ЭМОДЗИ
# ================================================================ #
@router.callback_query(F.data.startswith("emoji_set:"))
async def cb_emoji_set(call: CallbackQuery, state: FSMContext):
    _, pid, emoji = call.data.split(":", 2)
    await _set_emoji_and_finish(call, int(pid), emoji, state)


@router.callback_query(F.data.startswith("emoji_skip:"))
async def cb_emoji_skip(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    await _set_emoji_and_finish(call, pid, "📦", state)


@router.callback_query(F.data.startswith("emoji_cats:"))
async def cb_emoji_cats(call: CallbackQuery):
    pid = int(call.data.split(":")[1])
    await call.message.edit_text("Выбери категорию:",
                                 reply_markup=emoji_categories_keyboard(pid))
    await call.answer()


@router.callback_query(F.data.startswith("emoji_cat:"))
async def cb_emoji_cat(call: CallbackQuery):
    _, pid, cat = call.data.split(":", 2)
    await call.message.edit_text(cat,
                                 reply_markup=emoji_category_page_keyboard(int(pid), cat, 0))
    await call.answer()


@router.callback_query(F.data.startswith("emoji_page:"))
async def cb_emoji_page(call: CallbackQuery):
    _, pid, cat, page = call.data.split(":", 3)
    await call.message.edit_reply_markup(
        reply_markup=emoji_category_page_keyboard(int(pid), cat, int(page)))
    await call.answer()


@router.callback_query(F.data.startswith("emoji_manual:"))
async def cb_emoji_manual(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    await state.update_data(manual_emoji_pid=pid)
    await state.set_state(NewProduct.waiting_manual_emoji)
    await call.message.edit_text("Пришли эмодзи сообщением (скопируй из клавиатуры):",
                                 reply_markup=emoji_manual_keyboard(pid))
    await call.answer()


@router.callback_query(F.data.startswith("emoji_back:"))
async def cb_emoji_back(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    data = await state.get_data()
    canonical = data.get("canonical", "")
    emojis = suggest_emojis(canonical) if canonical else ["📦"]
    await call.message.edit_text(f"Выбери эмодзи для «{canonical}»:",
                                 reply_markup=emoji_suggest_keyboard(emojis, pid))
    await call.answer()


async def _set_emoji_and_finish(call: CallbackQuery, pid: int, emoji: str,
                                state: FSMContext):
    data = await state.get_data()
    pending_qty = data.get("pending_qty", 1)
    canonical = data.get("canonical", "")
    await set_emoji(pid, emoji)
    await state.clear()
    await call.message.delete()
    product = {"id": pid, "canonical_name": canonical, "emoji": emoji}
    await record_mix_cb(call, product, pending_qty)
    await call.answer()


# ================================================================ #
#  ОТМЕНА
# ================================================================ #
async def start_undo(user_id: int, message: Message):
    last = await get_last_mix(user_id)
    if not last:
        await message.answer("Нечего отменять — записей нет.")
        return
    created = str(last["created_at"])
    time_part = created.split(" ")[1][:5] if " " in created else "—"
    await message.answer(
        f"Последняя запись:\n"
        f"{last['emoji']} {last['canonical_name']} — {last['quantity']} (в {time_part})\n\n"
        f"Сколько убрать?",
        reply_markup=undo_keyboard(last["quantity"]))


@router.callback_query(F.data == "undo_start")
async def cb_undo_start(call: CallbackQuery):
    await call.message.delete()
    await start_undo(call.from_user.id, call.message)
    await call.answer()


@router.callback_query(F.data.startswith("undo:"))
async def cb_undo(call: CallbackQuery):
    n = int(call.data.split(":")[1])
    last = await get_last_mix(call.from_user.id)
    if not last:
        await call.answer("Нечего отменять", show_alert=True)
        return
    if n >= last["quantity"]:
        await reduce_last_mix(call.from_user.id, last["quantity"])
        await call.message.edit_text(
            f"🗑 Удалено: {last['emoji']} {last['canonical_name']} — {last['quantity']}")
    else:
        await reduce_last_mix(call.from_user.id, n)
        await call.message.edit_text(
            f"↩️ Убрано {n} из {last['quantity']}.\n"
            f"Осталось: {last['quantity']-n} {last['emoji']} {last['canonical_name']}")
    await call.answer()


# ================================================================ #
#  ВЕЧЕРНЯЯ СВОДКА / ПУСТОЙ ДЕНЬ
# ================================================================ #
@router.callback_query(F.data == "report_now")
async def cb_report_now(call: CallbackQuery):
    await call.message.delete()
    text = await stats_day(call.from_user.id)
    await call.message.answer(text, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("report_snooze:"))
async def cb_report_snooze(call: CallbackQuery):
    minutes = int(call.data.split(":")[1])
    await create_reminder(call.from_user.id, minutes)
    await call.message.edit_text(f"⏰ Напомню через {minutes} мин.")
    await call.answer()


@router.callback_query(F.data == "empty_ok")
async def cb_empty_ok(call: CallbackQuery):
    await call.message.edit_text("Ок, понял 👌")
    await call.answer()


@router.callback_query(F.data == "empty_add")
async def cb_empty_add(call: CallbackQuery):
    await call.message.edit_text("Жду, присылай замесы 👇")
    await call.answer()


# ================================================================ #
#  СТАТИСТИКА (callback)
# ================================================================ #
@router.callback_query(F.data.startswith("stats:"))
async def cb_stats(call: CallbackQuery):
    period = call.data.split(":")[1]
    text = await build_stats(call.from_user.id, period)
    await call.message.edit_text(text, parse_mode="HTML",
                                 reply_markup=stats_back_keyboard())
    await call.answer()


# ================================================================ #
#  АДМИНКА
# ================================================================ #
@router.callback_query(F.data == "admin_main")
async def cb_admin_main(call: CallbackQuery):
    await call.message.edit_text("🛠 Админ-панель:", reply_markup=admin_main_keyboard())
    await call.answer()


@router.callback_query(F.data == "admin_close")
async def cb_admin_close(call: CallbackQuery):
    await call.message.delete()
    await call.answer()


@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    await call.message.edit_text("Выбери период:", reply_markup=stats_period_keyboard())
    await call.answer()


@router.callback_query(F.data == "admin_products")
async def cb_admin_products(call: CallbackQuery):
    await call.message.edit_text("📦 Управление продуктами:",
                                 reply_markup=admin_products_keyboard())
    await call.answer()


@router.callback_query(F.data == "admin_product_edit")
async def cb_admin_product_edit(call: CallbackQuery):
    products = await get_all_products()
    if not products:
        await call.message.edit_text("Список продуктов пуст.",
                                     reply_markup=admin_products_keyboard())
        await call.answer()
        return
    await call.message.edit_text("Выбери продукт:",
                                 reply_markup=admin_product_list_keyboard(products))
    await call.answer()


@router.callback_query(F.data.startswith("admin_pedit:"))
async def cb_admin_pedit(call: CallbackQuery):
    pid = int(call.data.split(":")[1])
    p = await get_product(pid)
    await call.message.edit_text(
        f"{p['emoji']} <b>{p['canonical_name']}</b>\n\nЧто изменить?",
        parse_mode="HTML", reply_markup=admin_product_edit_keyboard(pid))
    await call.answer()


@router.callback_query(F.data.startswith("admin_pname:"))
async def cb_admin_pname(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    await state.update_data(edit_pid=pid)
    await state.set_state(AdminEditProduct.waiting_name)
    await call.message.edit_text("Пришли новое название:")
    await call.answer()


@router.callback_query(F.data.startswith("admin_pemoji:"))
async def cb_admin_pemoji(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    await state.update_data(edit_pid=pid)
    await state.set_state(AdminEditProduct.waiting_emoji)
    await call.message.edit_text("Пришли новый эмодзи (сообщением):")
    await call.answer()


@router.callback_query(F.data.startswith("admin_palias:"))
async def cb_admin_palias(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    await state.update_data(edit_pid=pid)
    await state.set_state(AdminEditProduct.waiting_alias)
    await call.message.edit_text("Пришли новый синоним:")
    await call.answer()


async def admin_edit_name(message: Message, state: FSMContext):
    data = await state.get_data()
    await set_product_name(data["edit_pid"], message.text.strip())
    await state.clear()
    await message.answer("✅ Название обновлено.",
                         reply_markup=admin_products_keyboard())


async def admin_edit_emoji(message: Message, state: FSMContext):
    text = message.text.strip()
    if not is_single_emoji(text):
        await message.answer("Это не эмодзи. Попробуй ещё раз.")
        return
    data = await state.get_data()
    await set_emoji(data["edit_pid"], text)
    await state.clear()
    await message.answer("✅ Эмодзи обновлён.",
                         reply_markup=admin_products_keyboard())


async def admin_edit_alias(message: Message, state: FSMContext):
    data = await state.get_data()
    await add_alias(data["edit_pid"], message.text.strip())
    await state.clear()
    await message.answer("✅ Синоним добавлен.",
                         reply_markup=admin_products_keyboard())


@router.callback_query(F.data == "admin_product_add")
async def cb_admin_product_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminAddProduct.waiting_name)
    await call.message.edit_text("➕ Шаг 1/3. Введи название продукта:",
                                 reply_markup=admin_add_steps_keyboard(1))
    await call.answer()


async def admin_add_name(message: Message, state: FSMContext):
    await state.update_data(canonical=message.text.strip())
    await state.set_state(AdminAddProduct.waiting_emoji)
    await message.answer("Шаг 2/3. Введи эмодзи (или /skip):",
                         reply_markup=admin_add_steps_keyboard(2))


@router.message(Command("skip"))
async def admin_skip(message: Message, state: FSMContext):
    current = await state.get_state()
    if current == AdminAddProduct.waiting_emoji.state:
        await state.update_data(emoji="📦")
        await state.set_state(AdminAddProduct.waiting_aliases)
        await message.answer("Шаг 3/3. Синонимы через запятую (или /skip):")


async def admin_add_emoji(message: Message, state: FSMContext):
    if not is_single_emoji(message.text.strip()):
        await message.answer("Это не эмодзи. Попробуй ещё раз или /skip.")
        return
    await state.update_data(emoji=message.text.strip())
    await state.set_state(AdminAddProduct.waiting_aliases)
    await message.answer("Шаг 3/3. Синонимы через запятую (или /skip):")


async def admin_add_aliases(message: Message, state: FSMContext):
    data = await state.get_data()
    pid = await create_product(data["canonical"], data.get("emoji", "📦"))
    if message.text != "/skip":
        for a in message.text.split(","):
            a = a.strip()
            if a:
                await add_alias(pid, a)
    await state.clear()
    await message.answer(f"✅ Продукт «{data['canonical']}» добавлен.",
                         reply_markup=admin_products_keyboard())


@router.callback_query(F.data.startswith("admin_add_back:"))
async def cb_admin_add_back(call: CallbackQuery, state: FSMContext):
    step = int(call.data.split(":")[1])
    if step == 1:
        await state.set_state(AdminAddProduct.waiting_name)
        await call.message.edit_text("➕ Шаг 1/3. Введи название продукта:",
                                     reply_markup=admin_add_steps_keyboard(1))
    await call.answer()


@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await call.answer()


# ================================================================ #
#  ПЛАНИРОВЩИК
# ================================================================ #
scheduler = AsyncIOScheduler(timezone=TZ)


async def job_ask_empty(bot: Bot, hour_flag: str):
    flags = await get_day_flags(ADMIN_ID)
    if flags.get(hour_flag):
        return
    total = await count_today(ADMIN_ID)
    if total == 0:
        await bot.send_message(
            ADMIN_ID,
            "Привет! Сегодня ещё нет замесов. Всё ок?",
            reply_markup=empty_day_keyboard())
        await set_day_flag(ADMIN_ID, hour_flag)


async def job_daily_report(bot: Bot):
    flags = await get_day_flags(ADMIN_ID)
    if flags.get("report_sent"):
        return
    total = await count_today(ADMIN_ID)
    if total == 0:
        await bot.send_message(
            ADMIN_ID, "Замесов нет. Пустой день?",
            reply_markup=empty_day_keyboard())
    else:
        await bot.send_message(
            ADMIN_ID, "📊 Сводка за сегодня готова. Отправить?",
            reply_markup=daily_report_keyboard())


async def job_check_reminders(bot: Bot):
    due = await get_due_reminders()
    for r in due:
        text = await stats_day(r["user_id"])
        await bot.send_message(r["user_id"], text, parse_mode="HTML")
        await mark_reminder_sent(r["id"])


def setup_scheduler(bot: Bot):
    scheduler.add_job(job_ask_empty, CronTrigger(hour=12, minute=0, timezone=TZ),
                      args=[bot, "asked_12"])
    scheduler.add_job(job_ask_empty, CronTrigger(hour=16, minute=0, timezone=TZ),
                      args=[bot, "asked_16"])
    scheduler.add_job(job_daily_report, CronTrigger(hour=16, minute=45, timezone=TZ),
                      args=[bot])
    scheduler.add_job(job_check_reminders, "interval", minutes=1, args=[bot])
    scheduler.start()
