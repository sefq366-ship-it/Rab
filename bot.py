"""
bot.py — вся логика бота: база, эмодзи, клавиатуры, статистика,
обработчики, планировщик.
Точка входа — main.py.
"""

import os
import re
from pathlib import Path
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
# Укажи BOT_DB_PATH в панели Bothost на постоянный путь/том, если он доступен.
# По умолчанию база лежит рядом с bot.py.
DB_PATH = os.getenv("BOT_DB_PATH", str(Path(__file__).resolve().with_name("bot.db")))
MAX_QUANTITY = 20
TZ = pytz.timezone("Asia/Yekaterinburg")

MENU_BUTTONS = {"↩️ Отменить", "📊 Статистика", "🛠 Админка"}

router = Router()


# ================================================================ #
#  БАЗА ДАННЫХ
# ================================================================ #
async def _ensure_column(db, table: str, column: str, ddl: str) -> None:
    """Добавляет колонку, если её ещё нет (мягкая миграция без потери данных)."""
    cur = await db.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in await cur.fetchall()}
    if column not in cols:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


async def init_db() -> None:
    # Создаём каталог базы, если BOT_DB_PATH указывает в отдельную папку.
    Path(DB_PATH).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
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
            alias      TEXT NOT NULL,
            UNIQUE(alias, product_id)
        );
        CREATE TABLE IF NOT EXISTS equipment (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS brands (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS mixes (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            product_id   INTEGER NOT NULL REFERENCES products(id),
            quantity     INTEGER NOT NULL DEFAULT 1,
            equipment_id INTEGER REFERENCES equipment(id),
            brand_id     INTEGER REFERENCES brands(id),
            date         TEXT NOT NULL,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        # --- Мягкая миграция: вес замеса (граммы) ---
        await _ensure_column(db, "mixes", "weight_g", "INTEGER")
        await db.commit()


# --- Продукты ---
async def get_products_by_alias(alias: str) -> list[dict]:
    """Все продукты, у которых есть такой синоним (может быть >1)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT DISTINCT p.id, p.canonical_name, p.emoji
            FROM products p
            JOIN product_aliases a ON a.product_id = p.id
            WHERE LOWER(a.alias) = LOWER(?)
            ORDER BY p.canonical_name
        """, (alias.strip(),))
        return [dict(r) for r in await cur.fetchall()]


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
    """Добавляет синоним продукту. Дубликат пары (alias, product) игнорируется."""
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


# --- Оборудование ---
async def get_all_equipment() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, name FROM equipment ORDER BY name")
        return [dict(r) for r in await cur.fetchall()]


async def add_equipment(name: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO equipment (name) VALUES (?)", (name.strip(),))
        await db.commit()
        if cur.lastrowid:
            return cur.lastrowid
        cur = await db.execute(
            "SELECT id FROM equipment WHERE name = ?", (name.strip(),))
        row = await cur.fetchone()
        return row[0]


async def get_equipment(eid: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, name FROM equipment WHERE id = ?", (eid,))
        row = await cur.fetchone()
        return dict(row) if row else None


# --- Бренды ---
async def get_all_brands() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, name FROM brands ORDER BY name")
        return [dict(r) for r in await cur.fetchall()]


async def add_brand(name: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO brands (name) VALUES (?)", (name.strip(),))
        await db.commit()
        if cur.lastrowid:
            return cur.lastrowid
        cur = await db.execute(
            "SELECT id FROM brands WHERE name = ?", (name.strip(),))
        row = await cur.fetchone()
        return row[0]


async def get_brand(bid: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT id, name FROM brands WHERE id = ?", (bid,))
        row = await cur.fetchone()
        return dict(row) if row else None


# --- Замесы ---
async def add_mix(user_id: int, product_id: int, quantity: int,
                  equipment_id: int | None = None,
                  brand_id: int | None = None,
                  weight_g: int | None = None) -> int:
    today = datetime.now(TZ).date().isoformat()
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cur = await db.execute(
            "INSERT INTO mixes (user_id, product_id, quantity, equipment_id, brand_id, weight_g, date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, product_id, quantity, equipment_id, brand_id, weight_g, today))
        await db.commit()
        return cur.lastrowid


async def get_last_mix(user_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT m.id, m.quantity, m.weight_g, m.created_at,
                   p.canonical_name, p.emoji,
                   e.name AS equipment_name, b.name AS brand_name
            FROM mixes m
            JOIN products p ON p.id = m.product_id
            LEFT JOIN equipment e ON e.id = m.equipment_id
            LEFT JOIN brands b ON b.id = m.brand_id
            WHERE m.user_id = ? ORDER BY m.id DESC LIMIT 1
        """, (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_mix_by_id(user_id: int, mix_id: int) -> dict | None:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT m.id, m.product_id, m.quantity, m.equipment_id, m.brand_id,
                   m.weight_g, m.date, m.created_at, p.canonical_name, p.emoji,
                   e.name AS equipment_name, b.name AS brand_name
            FROM mixes m JOIN products p ON p.id=m.product_id
            LEFT JOIN equipment e ON e.id=m.equipment_id
            LEFT JOIN brands b ON b.id=m.brand_id
            WHERE m.id=? AND m.user_id=?
        """, (mix_id, user_id))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_recent_mixes(user_id: int, limit: int = 10) -> list[dict]:
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT m.id, m.quantity, m.weight_g, m.date, m.created_at,
                   p.canonical_name, p.emoji,
                   e.name AS equipment_name, b.name AS brand_name
            FROM mixes m JOIN products p ON p.id=m.product_id
            LEFT JOIN equipment e ON e.id=m.equipment_id
            LEFT JOIN brands b ON b.id=m.brand_id
            WHERE m.user_id=? ORDER BY m.id DESC LIMIT ?
        """, (user_id, limit))
        return [dict(r) for r in await cur.fetchall()]


async def update_mix_field(user_id: int, mix_id: int, field: str, value) -> bool:
    allowed = {"product_id", "equipment_id", "brand_id", "quantity", "weight_g"}
    if field not in allowed:
        raise ValueError("Недопустимое поле замеса")
    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        cur = await db.execute(
            f"UPDATE mixes SET {field}=? WHERE id=? AND user_id=?",
            (value, mix_id, user_id))
        await db.commit()
        return cur.rowcount > 0


async def reduce_last_mix(user_id: int, remove_qty: int) -> bool:
    last = await get_last_mix(user_id)
    if not last:
        return False
    remaining = last["quantity"] - remove_qty
    async with aiosqlite.connect(DB_PATH) as db:
        if remaining > 0:
            # Пропорционально уменьшаем вес, если он был указан.
            new_weight = last["weight_g"]
            if new_weight:
                try:
                    new_weight = int(round(last["weight_g"] * remaining / last["quantity"]))
                except ZeroDivisionError:
                    new_weight = last["weight_g"]
            await db.execute("UPDATE mixes SET quantity = ?, weight_g = ? WHERE id = ?",
                             (remaining, new_weight, last["id"]))
        else:
            await db.execute("DELETE FROM mixes WHERE id = ?", (last["id"],))
        await db.commit()
    return True


async def count_today(user_id: int) -> int:
    today = datetime.now(TZ).date().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM mixes WHERE user_id = ? AND date = ?",
            (user_id, today))
        row = await cur.fetchone()
        return row[0] if row else 0


# --- Напоминания ---
async def create_reminder(user_id: int, minutes: int) -> None:
    send_at = datetime.now(TZ).replace(tzinfo=None) + timedelta(minutes=minutes)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reminders SET status='cancelled' WHERE user_id=? AND status='pending'",
            (user_id,))
        await db.execute(
            "INSERT INTO reminders (user_id, send_at, status) VALUES (?, ?, 'pending')",
            (user_id, send_at))
        await db.commit()


async def get_due_reminders() -> list[dict]:
    now = datetime.now(TZ).replace(tzinfo=None)
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


# --- Флаги дня ---
async def get_day_flags(user_id: int) -> dict:
    today = datetime.now(TZ).date().isoformat()
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
    today = datetime.now(TZ).date().isoformat()
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


def new_product_mode_keyboard(alias: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Новый продукт — записать замес",
              callback_data="newprod_mode:mix")
    kb.button(text="📖 Только расшифровка (без замеса)",
              callback_data="newprod_mode:alias_only")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()


def weight_keyboard() -> InlineKeyboardMarkup:
    """Только пропуск и отмена — быстрые подсказки веса убраны.
    Вес вводится текстом (поддерживаются значения до 1000 кг).
    """
    kb = InlineKeyboardBuilder()
    kb.button(text="⏭ Пропустить", callback_data="weight_skip")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(1, 1)
    return kb.as_markup()


def after_mix_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Редактировать замес", callback_data="edit_mix_start")
    kb.button(text="↩️ Отменить последний", callback_data="undo_start")
    kb.adjust(1)
    return kb.as_markup()


def recent_mixes_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for item in items:
        dt = str(item.get("created_at") or "")
        time_part = dt.split(" ")[1][:5] if " " in dt else str(item.get("date", ""))
        label = f"{item['emoji']} {item['canonical_name']} ×{item['quantity']} · {time_part}"
        kb.button(text=label[:60], callback_data=f"edit_pick:{item['id']}")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()


def edit_fields_keyboard(mix_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📦 Продукт / расшифровка", callback_data=f"edit_field:{mix_id}:product")
    kb.button(text="🔧 Оборудование", callback_data=f"edit_field:{mix_id}:equipment")
    kb.button(text="🏷 Бренд", callback_data=f"edit_field:{mix_id}:brand")
    kb.button(text="🔢 Количество", callback_data=f"edit_field:{mix_id}:quantity")
    kb.button(text="⚖️ Вес", callback_data=f"edit_field:{mix_id}:weight")
    kb.button(text="❌ Закрыть", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()


def edit_options_keyboard(mix_id: int, field: str, items: list[dict], label_key: str,
                          id_key: str, allow_none: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for item in items:
        kb.button(text=str(item[label_key])[:60],
                  callback_data=f"edit_set:{mix_id}:{field}:{item[id_key]}")
    if allow_none:
        kb.button(text="🚫 Убрать значение", callback_data=f"edit_set:{mix_id}:{field}:0")
    if field in ("equipment", "brand"):
        kb.button(text="➕ Ввести новое значение", callback_data=f"edit_new:{mix_id}:{field}")
    kb.button(text="🔙 К полям замеса", callback_data=f"edit_pick:{mix_id}")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()


def equipment_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for it in items:
        kb.button(text=it["name"], callback_data=f"eq_sel:{it['id']}")
    kb.button(text="➕ Добавить оборудование", callback_data="eq_add")
    kb.button(text="❌ Отмена", callback_data="cancel")
    rows = [2] * (len(items) // 2)
    if len(items) % 2:
        rows.append(1)
    rows += [1, 1]
    kb.adjust(*rows)
    return kb.as_markup()


def brand_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for it in items:
        kb.button(text=it["name"], callback_data=f"br_sel:{it['id']}")
    kb.button(text="➕ Добавить бренд", callback_data="br_add")
    kb.button(text="❌ Отмена", callback_data="cancel")
    rows = [2] * (len(items) // 2)
    if len(items) % 2:
        rows.append(1)
    rows += [1, 1]
    kb.adjust(*rows)
    return kb.as_markup()


def alias_choice_keyboard(products: list[dict], alias: str) -> InlineKeyboardMarkup:
    """Выбор расшифровки или добавление ещё одного продукта к сокращению."""
    kb = InlineKeyboardBuilder()
    for p in products:
        kb.button(text=f"{p['emoji']} {p['canonical_name']}",
                  callback_data=f"alias_use:{p['id']}")
    kb.button(text="➕ Добавить ещё расшифровку",
              callback_data="alias_new")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()


def alias_admin_choice_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in products:
        kb.button(text=f"Это тот же продукт: {p['emoji']} {p['canonical_name']}",
                  callback_data=f"alias_admin_use:{p['id']}")
    kb.button(text="➕ Это другой продукт", callback_data="alias_admin_new")
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
    kb.button(text="🔧 Оборудование", callback_data="admin_equipment")
    kb.button(text="🏷 Бренды", callback_data="admin_brands")
    kb.button(text="📊 Статистика", callback_data="admin_stats")
    kb.button(text="🔙 Закрыть", callback_data="admin_close")
    kb.adjust(2, 2, 1)
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


def admin_equipment_list_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for it in items:
        kb.button(text=it["name"], callback_data=f"admin_eq_edit:{it['id']}")
    kb.button(text="➕ Добавить", callback_data="admin_eq_add")
    kb.button(text="🔙 Назад", callback_data="admin_main")
    kb.adjust(1)
    return kb.as_markup()


def admin_brand_list_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for it in items:
        kb.button(text=it["name"], callback_data=f"admin_br_edit:{it['id']}")
    kb.button(text="➕ Добавить", callback_data="admin_br_add")
    kb.button(text="🔙 Назад", callback_data="admin_main")
    kb.adjust(1)
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


def format_weight(weight_g: int | None) -> str:
    if not weight_g:
        return "—"
    kg = weight_g / 1000
    if kg == int(kg):
        return f"{int(kg)} кг"
    return f"{kg:.3f}".rstrip("0").rstrip(".") + " кг"


def human_date(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


async def _by_product(user_id: int, where: str, params: tuple) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(f"""
            SELECT p.canonical_name, p.emoji, SUM(m.quantity) AS total,
                   COALESCE(SUM(m.weight_g), 0) AS total_weight_g
            FROM mixes m JOIN products p ON p.id = m.product_id
            WHERE m.user_id = ? AND {where}
            GROUP BY p.id ORDER BY total DESC, p.canonical_name
        """, (user_id, *params))
        return [dict(r) for r in await cur.fetchall()]


async def _by_equipment(user_id: int, where: str, params: tuple) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(f"""
            SELECT COALESCE(e.name, '— без оборудования') AS equipment_name,
                   SUM(m.quantity) AS total_qty,
                   COALESCE(SUM(m.weight_g), 0) AS total_weight_g
            FROM mixes m
            LEFT JOIN equipment e ON e.id = m.equipment_id
            WHERE m.user_id = ? AND {where}
            GROUP BY e.id
            ORDER BY total_qty DESC, equipment_name
        """, (user_id, *params))
        return [dict(r) for r in await cur.fetchall()]


async def _total(user_id: int, where: str, params: tuple) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"SELECT COALESCE(SUM(quantity),0), COALESCE(SUM(weight_g),0) "
            f"FROM mixes WHERE user_id=? AND {where}",
            (user_id, *params))
        row = await cur.fetchone()
        if not row:
            return {"qty": 0, "weight_g": 0}
        return {"qty": row[0] or 0, "weight_g": row[1] or 0}


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


async def _day_details(user_id: int, d: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT p.canonical_name, p.emoji, m.quantity, m.weight_g,
                   e.name AS equipment_name, b.name AS brand_name,
                   m.created_at
            FROM mixes m
            JOIN products p ON p.id = m.product_id
            LEFT JOIN equipment e ON e.id = m.equipment_id
            LEFT JOIN brands b ON b.id = m.brand_id
            WHERE m.user_id = ? AND m.date = ?
            ORDER BY m.id
        """, (user_id, d))
        return [dict(r) for r in await cur.fetchall()]


def _equipment_block(by_eq: list[dict]) -> list[str]:
    """Строки блока «по оборудованию»:
       название — вес — количество замесов."""
    if not by_eq:
        return []
    lines = ["", "🔧 По оборудованию:"]
    for i, r in enumerate(by_eq, start=1):
        name = r["equipment_name"]
        weight = format_weight(r["total_weight_g"]) if r["total_weight_g"] else "—"
        mixes = format_mixes(r["total_qty"])
        lines.append(f"   {i}) {name} — {weight} — {mixes}")
    return lines


async def stats_day(user_id: int) -> str:
    today = datetime.now(TZ).date()
    d_str = str(today)
    details = await _day_details(user_id, d_str)
    if not details:
        return (f"📅 <b>{human_date(today)}</b> ({WEEKDAYS_RU[today.weekday()]})\n\n"
                f"Пока нет ни одного замеса за сегодня.")
    total = sum(r["quantity"] for r in details)
    total_weight = sum((r["weight_g"] or 0) for r in details)
    first, last = await _day_times(user_id, d_str)
    lines = [f"📅 <b>{human_date(today)}</b> ({WEEKDAYS_RU[today.weekday()]})", ""]
    for r in details:
        q = f" ×{r['quantity']}" if r["quantity"] > 1 else ""
        w = f" · {format_weight(r['weight_g'])}" if r["weight_g"] else ""
        lines.append(f"{r['emoji']} {r['canonical_name']}{q}{w}")
        parts = []
        if r.get("equipment_name"):
            parts.append(f"🔧 {r['equipment_name']}")
        if r.get("brand_name"):
            parts.append(f"🏷 {r['brand_name']}")
        if parts:
            lines.append("   " + " | ".join(parts))
    by_eq = await _by_equipment(user_id, "date = ?", (d_str,))
    lines += _equipment_block(by_eq)
    lines += ["", "───────────────", f"📦 Всего: <b>{format_mixes(total)}</b>"]
    if total_weight:
        lines.append(f"⚖️ Общий вес: <b>{format_weight(total_weight)}</b>")
    if first:
        lines.append(f"⏱ Первый: {first}")
    if last:
        lines.append(f"⏱ Последний: {last}")
    yesterday = str(today - timedelta(days=1))
    y = await _total(user_id, "date = ?", (yesterday,))
    if y["qty"] > 0:
        diff = total - y["qty"]
        if diff > 0:
            lines.append(f"📈 К вчера: +{diff}")
        elif diff < 0:
            lines.append(f"📉 К вчера: {diff}")
        else:
            lines.append("➖ К вчера: без изменений")
    return "\n".join(lines)


async def stats_week(user_id: int) -> str:
    today = datetime.now(TZ).date()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    by_product = await _by_product(
        user_id, "m.date BETWEEN ? AND ?", (str(start), str(end)))
    if not by_product:
        return (f"📆 <b>Неделя {start.strftime('%d.%m')}–{end.strftime('%d.%m')}</b>\n\n"
                f"За эту неделю замесов ещё нет.")
    total = sum(r["total"] for r in by_product)
    total_weight = sum(r["total_weight_g"] for r in by_product)
    max_val = by_product[0]["total"]
    medals = ["🥇", "🥈", "🥉"]
    lines = [f"📆 <b>НЕДЕЛЯ {start.strftime('%d.%m')}–{end.strftime('%d.%m')}</b>", ""]
    for i, r in enumerate(by_product):
        medal = medals[i] if i < 3 else "▫️"
        w = f" · {format_weight(r['total_weight_g'])}" if r["total_weight_g"] else ""
        lines.append(f"{medal} {r['emoji']} {r['canonical_name']} — <b>{r['total']}</b>{w}\n"
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
    by_eq = await _by_equipment(
        user_id, "m.date BETWEEN ? AND ?", (str(start), str(end)))
    lines += _equipment_block(by_eq)
    lines += ["", "───────────────", f"📦 Всего: <b>{format_mixes(total)}</b>"]
    if total_weight:
        lines.append(f"⚖️ Общий вес: <b>{format_weight(total_weight)}</b>")
    lines.append(f"📊 В среднем: {total/7:.1f}/день")
    return "\n".join(lines)


async def stats_month(user_id: int) -> str:
    today = datetime.now(TZ).date()
    prefix = today.strftime("%Y-%m")
    by_product = await _by_product(user_id, "strftime('%Y-%m', m.date) = ?", (prefix,))
    if not by_product:
        return (f"🗓 <b>{MONTHS_RU_NOM[today.month-1]} {today.year}</b>\n\n"
                f"В этом месяце замесов пока нет.")
    total = sum(r["total"] for r in by_product)
    total_weight = sum(r["total_weight_g"] for r in by_product)
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
             f"📦 Всего: <b>{format_mixes(total)}</b>"]
    if total_weight:
        lines.append(f"⚖️ Общий вес: <b>{format_weight(total_weight)}</b>")
    lines.append(f"📊 В среднем: {total/days_passed:.1f}/день")
    if best_day:
        lines.append(f"🔥 Лучший день: {human_date(date.fromisoformat(best_day[0]))} — {best_day[1]}")
    if worst_day and len(daily) > 1:
        lines.append(f"📉 Слабый день: {human_date(date.fromisoformat(worst_day[0]))} — {worst_day[1]}")
    lines += ["", "ТОП-3:"]
    for i, r in enumerate(by_product[:3]):
        w = f" · {format_weight(r['total_weight_g'])}" if r["total_weight_g"] else ""
        lines.append(f"{medals[i]} {r['emoji']} {r['canonical_name']} — {r['total']}{w}")
    by_eq = await _by_equipment(
        user_id, "strftime('%Y-%m', m.date) = ?", (prefix,))
    lines += _equipment_block(by_eq)
    return "\n".join(lines)


async def stats_all(user_id: int) -> str:
    by_product = await _by_product(user_id, "1=1", ())
    if not by_product:
        return "📈 Пока нет ни одного замеса за всё время."
    total = sum(r["total"] for r in by_product)
    total_weight = sum(r["total_weight_g"] for r in by_product)
    active_days = await _active_days(user_id)
    avg = total / active_days if active_days else 0
    medals = ["🥇", "🥈", "🥉"]
    lines = ["📈 <b>ВСЁ ВРЕМЯ</b>", "",
             f"📦 Всего: <b>{format_mixes(total)}</b>"]
    if total_weight:
        lines.append(f"⚖️ Общий вес: <b>{format_weight(total_weight)}</b>")
    lines += [f"📅 Дней с замесами: {active_days}",
              f"📊 В среднем: {avg:.1f}/день", "", "ТОП продуктов:"]
    for i, r in enumerate(by_product[:5]):
        medal = medals[i] if i < 3 else "▫️"
        w = f" · {format_weight(r['total_weight_g'])}" if r["total_weight_g"] else ""
        lines.append(f"{medal} {r['emoji']} {r['canonical_name']} — {r['total']}{w}")
    by_eq = await _by_equipment(user_id, "1=1", ())
    lines += _equipment_block(by_eq)
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
    waiting_mode = State()
    waiting_name = State()
    waiting_emoji = State()
    waiting_manual_emoji = State()


class QtyInput(StatesGroup):
    waiting_qty = State()


class MixFlow(StatesGroup):
    waiting_equipment = State()
    waiting_brand = State()
    waiting_new_equipment = State()
    waiting_new_brand = State()
    waiting_weight = State()


class AliasConflict(StatesGroup):
    waiting_choice = State()


class AdminAddProduct(StatesGroup):
    waiting_name = State()
    waiting_emoji = State()
    waiting_aliases = State()


class AdminEditProduct(StatesGroup):
    waiting_name = State()
    waiting_emoji = State()
    waiting_alias = State()


class AdminAddEquipment(StatesGroup):
    waiting_name = State()


class AdminAddBrand(StatesGroup):
    waiting_name = State()


class EditMix(StatesGroup):
    waiting_new_equipment = State()
    waiting_new_brand = State()
    waiting_quantity = State()
    waiting_weight = State()


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


def parse_weight(text: str) -> int | None:
    """Вес в граммах из строки.
    Поддерживает: '1200', '1.2', '1,2', '1.2 кг', '49.72 кг', '500г'.
    Без единицы: дробное число или значение ≤ 2000 трактуем как кг,
    иначе — как граммы. Максимум 1000 кг.
    """
    text = text.strip().lower().replace(",", ".")
    if not text:
        return None
    has_kg = "кг" in text or text.endswith("kg") or " kg" in text
    has_g = ("г" in text or text.endswith("g") or " g" in text) and not has_kg
    clean = (
        text.replace("кг", "")
        .replace("kg", "")
        .replace("г", "")
        .replace("g", "")
        .strip()
    )
    try:
        val = float(clean)
    except ValueError:
        return None
    if has_kg:
        multiplier = 1000
    elif has_g:
        multiplier = 1
    else:
        # Без единицы: дробное или ≤ 1000 → килограммы, иначе граммы.
        if ("." in clean) or (0.01 <= val <= 1000):
            multiplier = 1000
        else:
            multiplier = 1
    g = int(round(val * multiplier))
    # До 1000 кг включительно
    if g <= 0 or g > 1_000_000:
        return None
    return g


async def resolve_products(text: str) -> list[dict]:
    """Возвращает список продуктов, подходящих под сокращение (0, 1 или >1)."""
    exact = await get_products_by_alias(text)
    if exact:
        return exact
    try:
        from rapidfuzz import process, fuzz
    except ImportError:
        return []
    aliases = await get_all_aliases()
    if not aliases:
        return []
    alias_map: dict[str, dict] = {}
    for a in aliases:
        alias_map.setdefault(a["alias"].lower(), a)
    result = process.extractOne(
        text.lower(), list(alias_map.keys()), scorer=fuzz.ratio)
    if not result:
        return []
    match, score, _ = result
    if match and score >= 85:
        return await get_products_by_alias(match)
    return []


# ================================================================ #
#  ЦЕПОЧКА ВВОДА: ПРОДУКТ → КОЛИЧЕСТВО → ОБОРУДОВАНИЕ → БРЕНД → ВЕС
# ================================================================ #
async def ask_equipment(message: Message, state: FSMContext,
                        product: dict, qty: int):
    equipment = await get_all_equipment()
    await state.update_data(flow_product=product, flow_qty=qty, flow_equipment=None)
    if len(equipment) == 0:
        await state.set_state(MixFlow.waiting_new_equipment)
        await message.answer(
            "Сначала добавь оборудование (его ещё нет).\n"
            "Напиши название оборудования:")
        return
    if len(equipment) == 1:
        await state.update_data(flow_equipment=equipment[0])
        await ask_brand(message, state)
        return
    await state.set_state(MixFlow.waiting_equipment)
    await message.answer("Выбери оборудование:",
                         reply_markup=equipment_keyboard(equipment))


async def ask_brand(message: Message, state: FSMContext):
    brands = await get_all_brands()
    await state.update_data(flow_brand=None)
    if len(brands) == 0:
        await state.set_state(MixFlow.waiting_new_brand)
        await message.answer(
            "Сначала добавь бренд (их ещё нет).\n"
            "Напиши название бренда:")
        return
    if len(brands) == 1:
        await state.update_data(flow_brand=brands[0])
        await ask_weight(message, state)
        return
    await state.set_state(MixFlow.waiting_brand)
    await message.answer("Выбери бренд:", reply_markup=brand_keyboard(brands))


async def ask_weight(message: Message, state: FSMContext):
    await state.set_state(MixFlow.waiting_weight)
    await message.answer(
        "⚖️ Укажи общий вес замеса (всей партии).\n"
        "Можно в кг (например 49.72 или 1.5 кг) или в граммах (например 1200 г).\n"
        "Допускаются значения до 1000 кг.",
        reply_markup=weight_keyboard())


async def finish_mix(message: Message, state: FSMContext):
    data = await state.get_data()
    # В callback-сценариях message.from_user — это сам бот, а не человек.
    # Поэтому ID владельца замеса сохраняем в FSM при первоначальном вводе.
    user_id = int(data.get("flow_user_id") or message.from_user.id)
    product = data["flow_product"]
    qty = data["flow_qty"]
    equipment = data.get("flow_equipment")
    brand = data.get("flow_brand")
    weight_g = data.get("flow_weight_g")

    await add_mix(
        user_id, product["id"], qty,
        equipment["id"] if equipment else None,
        brand["id"] if brand else None,
        weight_g,
    )
    total_today = await count_today(user_id)
    qty_text = f"×{qty}" if qty > 1 else ""
    lines = [f"✅ {product['emoji']} {product['canonical_name']} {qty_text}"]
    parts = []
    if equipment:
        parts.append(f"🔧 {equipment['name']}")
    if brand:
        parts.append(f"🏷 {brand['name']}")
    if parts:
        lines.append(" | ".join(parts))
    if weight_g:
        lines.append(f"⚖️ {format_weight(weight_g)}")
    lines.append(f"Сегодня: {total_today}")
    await state.clear()
    await message.answer("\n".join(lines), reply_markup=after_mix_keyboard())


async def mix_new_equipment(message: Message, state: FSMContext):
    name = message.text.strip()
    eid = await add_equipment(name)
    eq = await get_equipment(eid)
    await state.update_data(flow_equipment=eq)
    await ask_brand(message, state)


async def mix_new_brand(message: Message, state: FSMContext):
    name = message.text.strip()
    bid = await add_brand(name)
    br = await get_brand(bid)
    await state.update_data(flow_brand=br)
    await ask_weight(message, state)


# ================================================================ #
#  КОМАНДЫ И КНОПКИ МЕНЮ
# ================================================================ #
@router.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет!\n\n"
        "Пиши замесы просто: <code>фм</code>, <code>фм 3</code>, "
        "<code>прот 75 2</code>.\n"
        "Если продукт новый — бот спросит название и эмодзи, "
        "либо можно сохранить только расшифровку без замеса.\n"
        "Потом выберешь оборудование, бренд и вес кнопками.\n\n"
        "Кнопки внизу — для быстрого доступа.",
        reply_markup=main_menu_keyboard())


@router.message(Command("undo"))
async def cmd_undo(message: Message):
    await start_undo(message.from_user.id, message)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    await message.answer("Выбери период:", reply_markup=stats_period_keyboard())


@router.message(Command("history"))
async def cmd_history(message: Message):
    items = await get_recent_mixes(message.from_user.id, 20)
    if not items:
        await message.answer("История пока пуста.")
        return
    lines = ["📚 <b>Последние замесы</b>", ""]
    for item in items:
        dt = str(item.get("created_at") or "")
        stamp = dt if dt else item.get("date", "")
        w = f" · {format_weight(item['weight_g'])}" if item.get("weight_g") else ""
        lines.append(f"#{item['id']} · {stamp} — {item['emoji']} {item['canonical_name']} ×{item['quantity']}{w}")
        extra = []
        if item.get("equipment_name"):
            extra.append(f"🔧 {item['equipment_name']}")
        if item.get("brand_name"):
            extra.append(f"🏷 {item['brand_name']}")
        if extra:
            lines.append("   " + " | ".join(extra))
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("edit"))
async def cmd_edit_mix(message: Message):
    await show_recent_for_edit(message.from_user.id, message)


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

    if current == MixFlow.waiting_new_equipment.state:
        await mix_new_equipment(message, state)
        return
    if current == MixFlow.waiting_new_brand.state:
        await mix_new_brand(message, state)
        return
    if current == MixFlow.waiting_weight.state:
        g = parse_weight(message.text)
        if g is None:
            await message.answer(
                "Не понял вес. Примеры: 49.72  |  1.5 кг  |  1200 г  |  250\n"
                "Дробные числа без единицы считаются килограммами. Максимум 1000 кг.",
                reply_markup=weight_keyboard())
            return
        await state.update_data(flow_weight_g=g)
        await finish_mix(message, state)
        return

    if current == EditMix.waiting_new_equipment.state:
        data = await state.get_data()
        eid = await add_equipment(message.text.strip())
        await update_mix_field(message.from_user.id, int(data["edit_mix_id"]), "equipment_id", eid)
        await state.clear()
        await message.answer("✅ Оборудование в замесе исправлено.", reply_markup=main_menu_keyboard())
        return
    if current == EditMix.waiting_new_brand.state:
        data = await state.get_data()
        bid = await add_brand(message.text.strip())
        await update_mix_field(message.from_user.id, int(data["edit_mix_id"]), "brand_id", bid)
        await state.clear()
        await message.answer("✅ Бренд в замесе исправлен.", reply_markup=main_menu_keyboard())
        return
    if current == EditMix.waiting_quantity.state:
        text = message.text.strip()
        if not text.isdigit() or not 1 <= int(text) <= MAX_QUANTITY:
            await message.answer(f"Введи целое число от 1 до {MAX_QUANTITY}:", reply_markup=cancel_keyboard())
            return
        data = await state.get_data()
        await update_mix_field(message.from_user.id, int(data["edit_mix_id"]), "quantity", int(text))
        await state.clear()
        await message.answer("✅ Количество исправлено.", reply_markup=main_menu_keyboard())
        return
    if current == EditMix.waiting_weight.state:
        g = parse_weight(message.text)
        if g is None:
            await message.answer(
                "Не понял вес. Примеры: 49.72  |  1.5 кг  |  1200 г\n"
                "Дробные числа без единицы = кг. Максимум 1000 кг.",
                reply_markup=cancel_keyboard())
            return
        data = await state.get_data()
        await update_mix_field(message.from_user.id, int(data["edit_mix_id"]), "weight_g", g)
        await state.clear()
        await message.answer("✅ Вес исправлен.", reply_markup=main_menu_keyboard())
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

    if current == AdminAddEquipment.waiting_name.state:
        await admin_eq_save(message, state)
        return
    if current == AdminAddBrand.waiting_name.state:
        await admin_br_save(message, state)
        return

    await handle_mix_text(message, state)


async def handle_mix_text(message: Message, state: FSMContext):
    # Сохраняем ID реального пользователя до перехода на inline-кнопки.
    await state.update_data(flow_user_id=message.from_user.id)
    raw = message.text.strip()
    name_part, qty = parse_quantity(raw)
    products = await resolve_products(name_part)

    if not products:
        await state.update_data(
            alias=name_part,
            pending_qty=qty,
            pending_had_qty=bool(re.search(r"\s+\d{1,3}$", raw)),
        )
        await state.set_state(NewProduct.waiting_mode)
        await message.answer(
            f"🤔 Не знаю продукт «{name_part}».\nЧто сделать?",
            reply_markup=new_product_mode_keyboard(name_part))
        return

    await state.update_data(
        pending_qty=qty,
        pending_alias=name_part,
        pending_had_qty=bool(re.search(r"\s+\d{1,3}$", raw)),
    )
    await state.set_state(AliasConflict.waiting_choice)
    await message.answer(
        f"Нашёл расшифровки для «{name_part}». Выбери нужную или добавь ещё одну:",
        reply_markup=alias_choice_keyboard(products, name_part))


# ================================================================ #
#  CALLBACK: РЕЖИМ НОВОГО ПРОДУКТА
# ================================================================ #
@router.callback_query(F.data.startswith("newprod_mode:"))
async def cb_newprod_mode(call: CallbackQuery, state: FSMContext):
    mode = call.data.split(":")[1]
    data = await state.get_data()
    await state.update_data(new_product_mode=mode)
    await state.set_state(NewProduct.waiting_name)
    alias = data.get("alias", "")
    if mode == "alias_only":
        prompt = f"📖 Сохраняем расшифровку для «{alias}».\nНапиши полное название продукта:"
    else:
        prompt = f"➕ Создаём новый продукт для «{alias}».\nНапиши полное название:"
    await call.message.edit_text(prompt, reply_markup=cancel_keyboard())
    await call.answer()


@router.callback_query(F.data.startswith("alias_use:"))
async def cb_alias_use(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    product = await get_product(pid)
    if not product:
        await call.answer("Продукт не найден. Повтори ввод.", show_alert=True)
        return
    data = await state.get_data()
    qty = int(data.get("pending_qty", 1))
    had_qty = bool(data.get("pending_had_qty", False))
    await state.clear()
    await call.message.delete()
    if qty > MAX_QUANTITY:
        await state.update_data(product_id=pid, big_qty=qty,
                                flow_user_id=call.from_user.id)
        await call.message.answer(
            f"⚠️ {qty} замесов за раз — это много. Точно?",
            reply_markup=confirm_big_quantity_keyboard(pid, qty))
    elif not had_qty:
        await state.update_data(pending_product=product,
                                flow_user_id=call.from_user.id)
        await call.message.answer(
            f"{product['emoji']} {product['canonical_name']} — сколько замесов?",
            reply_markup=quantity_keyboard(pid))
    else:
        await state.update_data(flow_user_id=call.from_user.id)
        await ask_equipment(call.message, state, product, qty)
    await call.answer()


@router.callback_query(F.data == "alias_new")
async def cb_alias_new(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    alias = data.get("pending_alias", "")
    await state.update_data(alias=alias, new_product_mode="alias_only",
                            flow_user_id=call.from_user.id)
    await state.set_state(NewProduct.waiting_name)
    await call.message.edit_text(
        f"Добавляем ещё одну расшифровку сокращения «{alias}».\n"
        f"Напиши полное название нового продукта:")
    await call.answer()


async def new_product_save_name(message: Message, state: FSMContext):
    data = await state.get_data()
    alias = data.get("alias", "")
    pending_qty = data.get("pending_qty", 1)
    canonical = (message.text or "").strip()
    if not canonical:
        await message.answer("Название не должно быть пустым. Напиши полное название:")
        return
    pid = await create_product(canonical)
    if alias:
        await add_alias(pid, alias)
    await add_alias(pid, canonical.lower())
    await state.update_data(product_id=pid, canonical=canonical, pending_qty=pending_qty)

    # Режим «только расшифровка» — без эмодзи и без замеса.
    if data.get("new_product_mode") == "alias_only":
        await state.clear()
        if alias:
            msg = f"✅ Расшифровка сохранена: «{alias}» → {canonical}.\nЗамес не создан."
        else:
            msg = f"✅ Продукт «{canonical}» добавлен в БД."
        await message.answer(msg, reply_markup=main_menu_keyboard())
        return

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
    product = {"id": pid, "canonical_name": data["canonical"], "emoji": text}
    await ask_equipment(message, state, product, pending_qty)


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
    await ask_equipment(message, state, product, n)


# ================================================================ #
#  CALLBACK: КОЛИЧЕСТВО
# ================================================================ #
@router.callback_query(F.data.startswith("qty:"))
async def cb_qty(call: CallbackQuery, state: FSMContext):
    _, pid, n = call.data.split(":")
    product = await get_product(int(pid))
    await state.update_data(flow_user_id=call.from_user.id)
    await call.message.delete()
    await ask_equipment(call.message, state, product, int(n))
    await call.answer()


@router.callback_query(F.data.startswith("qty_other:"))
async def cb_qty_other(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    await state.update_data(qty_product_id=pid, flow_user_id=call.from_user.id)
    await state.set_state(QtyInput.waiting_qty)
    await call.message.edit_text("Напиши число (от 1 до 20):",
                                 reply_markup=cancel_keyboard())
    await call.answer()


@router.callback_query(F.data.startswith("qty_confirm:"))
async def cb_qty_confirm(call: CallbackQuery, state: FSMContext):
    _, pid, n = call.data.split(":")
    product = await get_product(int(pid))
    await state.update_data(flow_user_id=call.from_user.id)
    await call.message.delete()
    await ask_equipment(call.message, state, product, int(n))
    await call.answer()


# ================================================================ #
#  CALLBACK: ОБОРУДОВАНИЕ, БРЕНД, ВЕС
# ================================================================ #
@router.callback_query(F.data.startswith("eq_sel:"))
async def cb_eq_sel(call: CallbackQuery, state: FSMContext):
    eid = int(call.data.split(":")[1])
    eq = await get_equipment(eid)
    await state.update_data(flow_equipment=eq, flow_user_id=call.from_user.id)
    await call.message.delete()
    await ask_brand(call.message, state)
    await call.answer()


@router.callback_query(F.data == "eq_add")
async def cb_eq_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(MixFlow.waiting_new_equipment)
    await call.message.edit_text("Напиши название оборудования:")
    await call.answer()


@router.callback_query(F.data.startswith("br_sel:"))
async def cb_br_sel(call: CallbackQuery, state: FSMContext):
    bid = int(call.data.split(":")[1])
    br = await get_brand(bid)
    await state.update_data(flow_brand=br, flow_user_id=call.from_user.id)
    await call.message.delete()
    await ask_weight(call.message, state)
    await call.answer()


@router.callback_query(F.data == "br_add")
async def cb_br_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(MixFlow.waiting_new_brand)
    await call.message.edit_text("Напиши название бренда:")
    await call.answer()


@router.callback_query(F.data.startswith("weight:"))
async def cb_weight(call: CallbackQuery, state: FSMContext):
    g = int(call.data.split(":")[1])
    await state.update_data(flow_weight_g=g, flow_user_id=call.from_user.id)
    await call.message.delete()
    await finish_mix(call.message, state)
    await call.answer()


@router.callback_query(F.data == "weight_skip")
async def cb_weight_skip(call: CallbackQuery, state: FSMContext):
    await state.update_data(flow_weight_g=None, flow_user_id=call.from_user.id)
    await call.message.delete()
    await finish_mix(call.message, state)
    await call.answer()


@router.callback_query(F.data == "weight_other")
async def cb_weight_other(call: CallbackQuery, state: FSMContext):
    await state.set_state(MixFlow.waiting_weight)
    await call.message.edit_text(
        "Напиши вес (примеры: 49.72  |  1.5 кг  |  1200 г).\n"
        "Дробные числа без единицы = кг. Максимум 1000 кг.",
        reply_markup=cancel_keyboard())
    await call.answer()


# ================================================================ #
#  CALLBACK: ЭМОДЗИ
# ================================================================ #
@router.callback_query(F.data.startswith("emoji_set:"))
async def cb_emoji_set(call: CallbackQuery, state: FSMContext):
    _, pid, emoji = call.data.split(":", 2)
    await _set_emoji_and_continue(call, int(pid), emoji, state)


@router.callback_query(F.data.startswith("emoji_skip:"))
async def cb_emoji_skip(call: CallbackQuery, state: FSMContext):
    pid = int(call.data.split(":")[1])
    await _set_emoji_and_continue(call, pid, "📦", state)


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


async def _set_emoji_and_continue(call: CallbackQuery, pid: int, emoji: str,
                                  state: FSMContext):
    data = await state.get_data()
    pending_qty = data.get("pending_qty", 1)
    canonical = data.get("canonical", "")
    await set_emoji(pid, emoji)
    await call.message.delete()
    product = {"id": pid, "canonical_name": canonical, "emoji": emoji}
    await state.update_data(flow_user_id=call.from_user.id)
    await ask_equipment(call.message, state, product, pending_qty)
    await call.answer()


# ================================================================ #
#  РЕДАКТИРОВАНИЕ ЗАМЕСОВ
# ================================================================ #
async def show_recent_for_edit(user_id: int, message: Message):
    items = await get_recent_mixes(user_id, 10)
    if not items:
        await message.answer("Пока нечего редактировать — история замесов пуста.")
        return
    await message.answer("Выбери замес, который нужно исправить:",
                         reply_markup=recent_mixes_keyboard(items))


@router.callback_query(F.data == "edit_mix_start")
async def cb_edit_mix_start(call: CallbackQuery):
    await call.message.delete()
    await show_recent_for_edit(call.from_user.id, call.message)
    await call.answer()


@router.callback_query(F.data.startswith("edit_pick:"))
async def cb_edit_pick(call: CallbackQuery):
    mix_id = int(call.data.split(":")[1])
    mix = await get_mix_by_id(call.from_user.id, mix_id)
    if not mix:
        await call.answer("Замес не найден.", show_alert=True)
        return
    w = f" · {format_weight(mix['weight_g'])}" if mix.get("weight_g") else ""
    text = (f"✏️ <b>Редактирование замеса #{mix_id}</b>\n\n"
            f"📦 {mix['emoji']} {mix['canonical_name']} ×{mix['quantity']}{w}\n"
            f"🔧 {mix.get('equipment_name') or 'не указано'}\n"
            f"🏷 {mix.get('brand_name') or 'не указан'}\n\nЧто исправить?")
    await call.message.edit_text(text, parse_mode="HTML",
                                 reply_markup=edit_fields_keyboard(mix_id))
    await call.answer()


@router.callback_query(F.data.startswith("edit_field:"))
async def cb_edit_field(call: CallbackQuery, state: FSMContext):
    _, mix_id_text, field = call.data.split(":", 2)
    mix_id = int(mix_id_text)
    if not await get_mix_by_id(call.from_user.id, mix_id):
        await call.answer("Замес не найден.", show_alert=True)
        return
    await state.update_data(edit_mix_id=mix_id)
    if field == "product":
        items = await get_all_products()
        kb = edit_options_keyboard(mix_id, "product", items, "canonical_name", "id")
        prompt = "Выбери правильный продукт / расшифровку:"
    elif field == "equipment":
        items = await get_all_equipment()
        kb = edit_options_keyboard(mix_id, "equipment", items, "name", "id", True)
        prompt = "Выбери правильное оборудование или введи новое:"
    elif field == "brand":
        items = await get_all_brands()
        kb = edit_options_keyboard(mix_id, "brand", items, "name", "id", True)
        prompt = "Выбери правильный бренд или введи новый:"
    elif field == "quantity":
        await state.set_state(EditMix.waiting_quantity)
        await call.message.edit_text(f"Введи правильное количество (1–{MAX_QUANTITY}):",
                                     reply_markup=cancel_keyboard())
        await call.answer()
        return
    elif field == "weight":
        await state.set_state(EditMix.waiting_weight)
        await call.message.edit_text(
            "Введи вес (примеры: 49.72  |  1.5 кг  |  1200 г).\n"
            "Дробные числа без единицы = кг. Максимум 1000 кг.",
            reply_markup=cancel_keyboard())
        await call.answer()
        return
    else:
        await call.answer("Неизвестное поле", show_alert=True)
        return
    await call.message.edit_text(prompt, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("edit_set:"))
async def cb_edit_set(call: CallbackQuery):
    _, mix_id_text, field, value_text = call.data.split(":", 3)
    mix_id, value = int(mix_id_text), int(value_text)
    if field == "product":
        db_field, db_value = "product_id", value
    elif field == "equipment":
        db_field, db_value = "equipment_id", value or None
    elif field == "brand":
        db_field, db_value = "brand_id", value or None
    else:
        await call.answer("Недопустимое поле", show_alert=True)
        return
    ok = await update_mix_field(call.from_user.id, mix_id, db_field, db_value)
    if not ok:
        await call.answer("Не удалось обновить запись", show_alert=True)
        return
    mix = await get_mix_by_id(call.from_user.id, mix_id)
    w = f" · {format_weight(mix['weight_g'])}" if mix.get("weight_g") else ""
    await call.message.edit_text(
        f"✅ Замес #{mix_id} исправлен.\n\n"
        f"📦 {mix['emoji']} {mix['canonical_name']} ×{mix['quantity']}{w}\n"
        f"🔧 {mix.get('equipment_name') or 'не указано'}\n"
        f"🏷 {mix.get('brand_name') or 'не указан'}\n\nМожно исправить ещё что-нибудь:",
        parse_mode="HTML", reply_markup=edit_fields_keyboard(mix_id))
    await call.answer("Сохранено")


@router.callback_query(F.data.startswith("edit_new:"))
async def cb_edit_new(call: CallbackQuery, state: FSMContext):
    _, mix_id_text, field = call.data.split(":", 2)
    await state.update_data(edit_mix_id=int(mix_id_text))
    if field == "equipment":
        await state.set_state(EditMix.waiting_new_equipment)
        prompt = "Напиши правильное название оборудования:"
    elif field == "brand":
        await state.set_state(EditMix.waiting_new_brand)
        prompt = "Напиши правильное название бренда:"
    else:
        await call.answer("Неизвестное поле", show_alert=True)
        return
    await call.message.edit_text(prompt, reply_markup=cancel_keyboard())
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
    parts = []
    if last.get("equipment_name"):
        parts.append(f"🔧 {last['equipment_name']}")
    if last.get("brand_name"):
        parts.append(f"🏷 {last['brand_name']}")
    if last.get("weight_g"):
        parts.append(f"⚖️ {format_weight(last['weight_g'])}")
    extra = ("\n" + " | ".join(parts)) if parts else ""
    await message.answer(
        f"Последняя запись:\n"
        f"{last['emoji']} {last['canonical_name']} — {last['quantity']} (в {time_part}){extra}\n\n"
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
    await call.message.edit_text(
        "Пришли сокращение (синоним).\n"
        "Оно добавится к этому продукту — оборудование и бренд не нужны.")
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
    """
    Добавление сокращения через админку.
    Проверяем: не занято ли оно другим продуктом.
    Если занято — спрашиваем, добавить как новое или это тот же продукт.
    """
    data = await state.get_data()
    pid = data["edit_pid"]
    alias = message.text.strip().lower()

    existing = await get_products_by_alias(alias)
    same = [p for p in existing if p["id"] == pid]
    others = [p for p in existing if p["id"] != pid]

    if others and not same:
        await state.update_data(pending_alias=alias, edit_pid=pid)
        await state.set_state(AliasConflict.waiting_choice)
        await message.answer(
            f"Сокращение «{alias}» уже используется:\n\n"
            + "\n".join(f"{p['emoji']} {p['canonical_name']}" for p in others)
            + "\n\nЭто тот же продукт или другой?",
            reply_markup=alias_admin_choice_keyboard(others))
        return

    await add_alias(pid, alias)
    await state.clear()
    await message.answer(f"✅ Сокращение «{alias}» добавлено.",
                         reply_markup=admin_products_keyboard())


@router.callback_query(F.data.startswith("alias_admin_use:"))
async def cb_alias_admin_use(call: CallbackQuery, state: FSMContext):
    """Пользователь сказал: это тот же продукт, что уже есть."""
    other_pid = int(call.data.split(":")[1])
    data = await state.get_data()
    current_pid = data["edit_pid"]
    alias = data["pending_alias"]
    await add_alias(current_pid, alias)
    await state.clear()
    await call.message.edit_text(
        f"✅ Сокращение «{alias}» добавлено (теперь доступно у нескольких продуктов).")
    await call.answer()


@router.callback_query(F.data == "alias_admin_new")
async def cb_alias_admin_new(call: CallbackQuery, state: FSMContext):
    """Пользователь сказал: это другой продукт — создаём новый."""
    data = await state.get_data()
    alias = data.get("pending_alias", "")
    await state.update_data(alias=alias, pending_qty=1, new_product_mode="alias_only")
    await state.set_state(NewProduct.waiting_name)
    await call.message.edit_text(
        f"Хорошо, создаём новый продукт для сокращения «{alias}».\n"
        f"Напиши полное название:")
    await call.answer()


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


# --- Оборудование ---
@router.callback_query(F.data == "admin_equipment")
async def cb_admin_equipment(call: CallbackQuery):
    items = await get_all_equipment()
    text = "🔧 Оборудование:\n\n" + ("\n".join(i["name"] for i in items) or "Пусто")
    await call.message.edit_text(text,
                                 reply_markup=admin_equipment_list_keyboard(items))
    await call.answer()


@router.callback_query(F.data == "admin_eq_add")
async def cb_admin_eq_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminAddEquipment.waiting_name)
    await call.message.edit_text("Название оборудования:")
    await call.answer()


async def admin_eq_save(message: Message, state: FSMContext):
    await add_equipment(message.text.strip())
    await state.clear()
    items = await get_all_equipment()
    text = "🔧 Оборудование:\n\n" + ("\n".join(i["name"] for i in items) or "Пусто")
    await message.answer(text, reply_markup=admin_equipment_list_keyboard(items))


# --- Бренды ---
@router.callback_query(F.data == "admin_brands")
async def cb_admin_brands(call: CallbackQuery):
    items = await get_all_brands()
    text = "🏷 Бренды:\n\n" + ("\n".join(i["name"] for i in items) or "Пусто")
    await call.message.edit_text(text, reply_markup=admin_brand_list_keyboard(items))
    await call.answer()


@router.callback_query(F.data == "admin_br_add")
async def cb_admin_br_add(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminAddBrand.waiting_name)
    await call.message.edit_text("Название бренда:")
    await call.answer()


async def admin_br_save(message: Message, state: FSMContext):
    await add_brand(message.text.strip())
    await state.clear()
    items = await get_all_brands()
    text = "🏷 Бренды:\n\n" + ("\n".join(i["name"] for i in items) or "Пусто")
    await message.answer(text, reply_markup=admin_brand_list_keyboard(items))


@router.callback_query(F.data == "cancel")
async def cb_cancel(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.delete()
    except Exception:
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    await call.answer("Отменено")


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
    await set_day_flag(ADMIN_ID, "report_sent")


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