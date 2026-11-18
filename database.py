"""
database.py — слой работы с SQLite.
Все запросы к БД собраны здесь, чтобы handlers.py и stats.py
не писали SQL напрямую.
"""

import aiosqlite
from datetime import date, datetime, timedelta
from typing import Optional

DB_PATH = "bot.db"

# Порог, после которого бот переспрашивает "точно столько?"
MAX_QUANTITY = 20


# ---------------------------------------------------------------- #
#  Инициализация схемы
# ---------------------------------------------------------------- #
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
            date       TEXT NOT NULL,              -- YYYY-MM-DD
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS reminders (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id  INTEGER NOT NULL,
            send_at  TIMESTAMP NOT NULL,
            status   TEXT NOT NULL DEFAULT 'pending'  -- pending / sent / cancelled
        );

        CREATE TABLE IF NOT EXISTS day_flags (
            user_id     INTEGER NOT NULL,
            date        TEXT NOT NULL,
            asked_12    INTEGER DEFAULT 0,
            asked_16    INTEGER DEFAULT 0,
            report_sent INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, date)
        );

        CREATE INDEX IF NOT EXISTS idx_mixes_user_date
            ON mixes(user_id, date);
        """)
        await db.commit()


# ---------------------------------------------------------------- #
#  Продукты и синонимы
# ---------------------------------------------------------------- #
async def get_product_by_alias(alias: str) -> Optional[dict]:
    """Ищет продукт по точному совпадению синонима (без учёта регистра)."""
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
    """Все синонимы — для нечёткого поиска (опечатки)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT a.alias, p.id, p.canonical_name, p.emoji
            FROM product_aliases a
            JOIN products p ON p.id = a.product_id
        """)
        return [dict(r) for r in await cur.fetchall()]


async def create_product(canonical_name: str, emoji: str = "📦") -> int:
    """Создаёт продукт, возвращает id. Если уже есть — возвращает существующий id."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id FROM products WHERE canonical_name = ?",
            (canonical_name.strip(),)
        )
        row = await cur.fetchone()
        if row:
            return row[0]
        cur = await db.execute(
            "INSERT INTO products (canonical_name, emoji) VALUES (?, ?)",
            (canonical_name.strip(), emoji)
        )
        await db.commit()
        return cur.lastrowid


async def add_alias(product_id: int, alias: str) -> None:
    """Добавляет синоним. Дубликаты игнорируются."""
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO product_aliases (product_id, alias) VALUES (?, ?)",
                (product_id, alias.strip().lower())
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            pass  # синоним уже есть


async def get_all_products() -> list[dict]:
    """Все продукты с числом синонимов — для админ-панели."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT p.id, p.canonical_name, p.emoji,
                   COUNT(a.id) AS aliases_count
            FROM products p
            LEFT JOIN product_aliases a ON a.product_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at
        """)
        return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------- #
#  Замесы
# ---------------------------------------------------------------- #
async def add_mix(user_id: int, product_id: int, quantity: int) -> int:
    """
    Добавляет замес. Возвращает id записи.
    quantity уже проверен на допустимость в handlers.py.
    """
    today = str(date.today())
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO mixes (user_id, product_id, quantity, date)
            VALUES (?, ?, ?, ?)
        """, (user_id, product_id, quantity, today))
        await db.commit()
        return cur.lastrowid


async def get_last_mix(user_id: int) -> Optional[dict]:
    """Последняя запись замеса — для отмены."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT m.id, m.quantity, m.created_at,
                   p.canonical_name, p.emoji
            FROM mixes m
            JOIN products p ON p.id = m.product_id
            WHERE m.user_id = ?
            ORDER BY m.id DESC
            LIMIT 1
        """, (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def reduce_last_mix(user_id: int, remove_qty: int) -> bool:
    """
    Убирает remove_qty из последней записи.
    Если quantity станет 0 — запись удаляется.
    Возвращает True, если что-то изменилось.
    """
    last = await get_last_mix(user_id)
    if not last:
        return False
    remaining = last["quantity"] - remove_qty
    async with aiosqlite.connect(DB_PATH) as db:
        if remaining > 0:
            await db.execute(
                "UPDATE mixes SET quantity = ? WHERE id = ?",
                (remaining, last["id"])
            )
        else:
            await db.execute("DELETE FROM mixes WHERE id = ?", (last["id"],))
        await db.commit()
    return True


async def count_today(user_id: int) -> int:
    """Сколько замесов (суммарно) за сегодня — для напоминаний."""
    today = str(date.today())
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT COALESCE(SUM(quantity), 0)
            FROM mixes
            WHERE user_id = ? AND date = ?
        """, (user_id, today))
        row = await cur.fetchone()
        return row[0] if row else 0


async def get_today_by_product(user_id: int) -> list[dict]:
    """Сводка за сегодня по продуктам — для отчёта."""
    today = str(date.today())
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT p.canonical_name, p.emoji, SUM(m.quantity) AS total
            FROM mixes m
            JOIN products p ON p.id = m.product_id
            WHERE m.user_id = ? AND m.date = ?
            GROUP BY p.id
            ORDER BY total DESC
        """, (user_id, today))
        return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------- #
#  Напоминания (отложенная отправка +10 / +30)
# ---------------------------------------------------------------- #
async def create_reminder(user_id: int, minutes: int) -> None:
    """Отложить сводку на N минут. Отменяет прошлые pending."""
    send_at = datetime.now() + timedelta(minutes=minutes)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE reminders SET status = 'cancelled'
            WHERE user_id = ? AND status = 'pending'
        """, (user_id,))
        await db.execute("""
            INSERT INTO reminders (user_id, send_at, status)
            VALUES (?, ?, 'pending')
        """, (user_id, send_at))
        await db.commit()


async def get_due_reminders() -> list[dict]:
    """Все pending-напоминания, время которых пришло."""
    now = datetime.now()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT id, user_id FROM reminders
            WHERE status = 'pending' AND send_at <= ?
        """, (now,))
        return [dict(r) for r in await cur.fetchall()]


async def mark_reminder_sent(reminder_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE reminders SET status = 'sent' WHERE id = ?",
            (reminder_id,)
        )
        await db.commit()


# ---------------------------------------------------------------- #
#  Флаги дня (чтобы не спамить напоминаниями)
# ---------------------------------------------------------------- #
async def get_day_flags(user_id: int) -> dict:
    today = str(date.today())
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("""
            SELECT asked_12, asked_16, report_sent
            FROM day_flags
            WHERE user_id = ? AND date = ?
        """, (user_id, today))
        row = await cur.fetchone()
        if row:
            return dict(row)
        await db.execute("""
            INSERT INTO day_flags (user_id, date) VALUES (?, ?)
        """, (user_id, today))
        await db.commit()
        return {"asked_12": 0, "asked_16": 0, "report_sent": 0}


async def set_day_flag(user_id: int, flag: str) -> None:
    """flag: 'asked_12' | 'asked_16' | 'report_sent'"""
    if flag not in ("asked_12", "asked_16", "report_sent"):
        raise ValueError("Неизвестный флаг")
    today = str(date.today())
    async with aiosqlite.connect(DB_PATH) as db:
        # на случай, если записи ещё нет
        await db.execute("""
            INSERT OR IGNORE INTO day_flags (user_id, date) VALUES (?, ?)
        """, (user_id, today))
        await db.execute(
            f"UPDATE day_flags SET {flag} = 1 WHERE user_id = ? AND date = ?",
            (user_id, today)
        )
        await db.commit()
