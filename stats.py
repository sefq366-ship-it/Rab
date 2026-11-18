"""
stats.py — расчёт и форматирование статистики.
Все запросы к БД — через database.py.
"""

import aiosqlite
from datetime import date, datetime, timedelta

from database import DB_PATH

# Понедельник = 0 (weekday()), воскресенье = 6
MONTHS_RU = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
MONTHS_RU_NOM = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]
WEEKDAYS_RU = ["понедельник", "вторник", "среда",
                "четверг", "пятница", "суббота", "воскресенье"]


# ---------------------------------------------------------------- #
#  Вспомогательное форматирование
# ---------------------------------------------------------------- #
def bar(value: int, max_value: int, width: int = 10) -> str:
    """Текстовый прогресс-бар."""
    if max_value <= 0:
        return "░" * width
    filled = round(value / max_value * width)
    return "█" * filled + "░" * (width - filled)


def plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 замес, 2 замеса, 5 замесов."""
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


# ---------------------------------------------------------------- #
#  Базовые запросы
# ---------------------------------------------------------------- #
async def _by_product(user_id: int, where: str, params: tuple) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(f"""
            SELECT p.canonical_name, p.emoji, SUM(m.quantity) AS total
            FROM mixes m
            JOIN products p ON p.id = m.product_id
            WHERE m.user_id = ? AND {where}
            GROUP BY p.id
            ORDER BY total DESC, p.canonical_name
        """, (user_id, *params))
        return [dict(r) for r in await cur.fetchall()]


async def _total(user_id: int, where: str, params: tuple) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(f"""
            SELECT COALESCE(SUM(quantity), 0)
            FROM mixes
            WHERE user_id = ? AND {where}
        """, (user_id, *params))
        row = await cur.fetchone()
        return row[0] if row else 0


async def _day_times(user_id: int, d: str) -> tuple[str | None, str | None]:
    """Время первого и последнего замеса за день."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT MIN(created_at), MAX(created_at)
            FROM mixes
            WHERE user_id = ? AND date = ?
        """, (user_id, d))
        row = await cur.fetchone()
        if not row or not row[0]:
            return None, None
        first = row[0].split(" ")[1][:5] if " " in row[0] else None
        last = row[1].split(" ")[1][:5] if row[1] and " " in row[1] else None
        return first, last


async def _active_days(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT COUNT(DISTINCT date) FROM mixes WHERE user_id = ?",
            (user_id,)
        )
        row = await cur.fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------- #
#  День
# ---------------------------------------------------------------- #
async def stats_day(user_id: int) -> str:
    today = date.today()
    d_str = str(today)
    by_product = await _by_product(user_id, "m.date = ?", (d_str,))

    if not by_product:
        return (
            f"📅 <b>{human_date(today)}</b> ({WEEKDAYS_RU[today.weekday()]})\n\n"
            f"Пока нет ни одного замеса за сегодня."
        )

    total = sum(r["total"] for r in by_product)
    first, last = await _day_times(user_id, d_str)

    lines = [
        f"📅 <b>{human_date(today)}</b> ({WEEKDAYS_RU[today.weekday()]})",
        "",
    ]
    for r in by_product:
        lines.append(f"{r['emoji']} {r['canonical_name']} — <b>{r['total']}</b>")
    lines += [
        "",
        "───────────────",
        f"📦 Всего: <b>{format_mixes(total)}</b>",
    ]
    if first:
        lines.append(f"⏱ Первый: {first}")
    if last:
        lines.append(f"⏱ Последний: {last}")

    # сравнение с вчера
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


# ---------------------------------------------------------------- #
#  Неделя (текущая, Пн–Вс)
# ---------------------------------------------------------------- #
async def stats_week(user_id: int) -> str:
    today = date.today()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    start_s, end_s = str(start), str(end)

    by_product = await _by_product(
        user_id, "m.date BETWEEN ? AND ?", (start_s, end_s)
    )

    if not by_product:
        return (
            f"📆 <b>Неделя {start.strftime('%d.%m')}–{end.strftime('%d.%m')}</b>\n\n"
            f"За эту неделю замесов ещё нет."
        )

    total = sum(r["total"] for r in by_product)
    max_val = by_product[0]["total"]
    medals = ["🥇", "🥈", "🥉"]

    lines = [
        f"📆 <b>НЕДЕЛЯ {start.strftime('%d.%m')}–{end.strftime('%d.%m')}</b>",
        "",
    ]
    for i, r in enumerate(by_product):
        medal = medals[i] if i < 3 else "▫️"
        b = bar(r["total"], max_val, 10)
        lines.append(
            f"{medal} {r['emoji']} {r['canonical_name']} — <b>{r['total']}</b>\n"
            f"   <code>{b}</code>"
        )

    # по дням
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT date, SUM(quantity) AS cnt
            FROM mixes
            WHERE user_id = ? AND date BETWEEN ? AND ?
            GROUP BY date
        """, (user_id, start_s, end_s))
        daily = {r[0]: r[1] for r in await cur.fetchall()}

    day_names = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    max_day = max(daily.values()) if daily else 0
    lines += ["", "📊 По дням:"]
    for i in range(7):
        d = start + timedelta(days=i)
        cnt = daily.get(str(d), 0)
        b = bar(cnt, max_day, 8) if max_day else "░" * 8
        lines.append(f"{day_names[i]} <code>{b}</code> {cnt}")

    lines += [
        "",
        "───────────────",
        f"📦 Всего: <b>{format_mixes(total)}</b>",
        f"📊 В среднем: {total / 7:.1f}/день",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- #
#  Месяц
# ---------------------------------------------------------------- #
async def stats_month(user_id: int) -> str:
    today = date.today()
    prefix = today.strftime("%Y-%m")
    month_start = today.replace(day=1)

    by_product = await _by_product(
        user_id, "strftime('%Y-%m', m.date) = ?", (prefix,)
    )
    if not by_product:
        return (
            f"🗓 <b>{MONTHS_RU_NOM[today.month - 1]} {today.year}</b>\n\n"
            f"В этом месяце замесов пока нет."
        )

    total = sum(r["total"] for r in by_product)

    # лучший и слабый день
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT date, SUM(quantity) AS cnt
            FROM mixes
            WHERE user_id = ? AND strftime('%Y-%m', date) = ?
            GROUP BY date
        """, (user_id, prefix))
        daily = await cur.fetchall()

    best_day = max(daily, key=lambda r: r[1]) if daily else None
    worst_day = min(daily, key=lambda r: r[1]) if daily else None
    days_in_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    days_passed = min(today, days_in_month).day

    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"🗓 <b>{MONTHS_RU_NOM[today.month - 1].upper()} {today.year}</b>",
        "",
        f"📦 Всего: <b>{format_mixes(total)}</b>",
        f"📊 В среднем: {total / days_passed:.1f}/день",
    ]
    if best_day:
        lines.append(f"🔥 Лучший день: {human_date(date.fromisoformat(best_day[0]))} — {best_day[1]}")
    if worst_day and len(daily) > 1:
        lines.append(f"📉 Слабый день: {human_date(date.fromisoformat(worst_day[0]))} — {worst_day[1]}")

    lines += ["", "ТОП-3:"]
    for i, r in enumerate(by_product[:3]):
        lines.append(f"{medals[i]} {r['emoji']} {r['canonical_name']} — {r['total']}")

    return "\n".join(lines)


# ---------------------------------------------------------------- #
#  Всё время
# ---------------------------------------------------------------- #
async def stats_all(user_id: int) -> str:
    by_product = await _by_product(user_id, "1=1", ())
    if not by_product:
        return "📈 Пока нет ни одного замеса за всё время."

    total = sum(r["total"] for r in by_product)
    active_days = await _active_days(user_id)
    avg = total / active_days if active_days else 0

    medals = ["🥇", "🥈", "🥉"]
    lines = [
        "📈 <b>ВСЁ ВРЕМЯ</b>",
        "",
        f"📦 Всего: <b>{format_mixes(total)}</b>",
        f"📅 Дней с замесами: {active_days}",
        f"📊 В среднем: {avg:.1f}/день",
        "",
        "ТОП продуктов:",
    ]
    for i, r in enumerate(by_product[:5]):
        medal = medals[i] if i < 3 else "▫️"
        lines.append(f"{medal} {r['emoji']} {r['canonical_name']} — {r['total']}")

    return "\n".join(lines)


# ---------------------------------------------------------------- #
#  Универсальная точка входа
# ---------------------------------------------------------------- #
async def build_stats(user_id: int, period: str) -> str:
    """
    period: 'day' | 'week' | 'month' | 'all'
    """
    if period == "day":
        return await stats_day(user_id)
    if period == "week":
        return await stats_week(user_id)
    if period == "month":
        return await stats_month(user_id)
    if period == "all":
        return await stats_all(user_id)
    raise ValueError(f"Неизвестный период: {period}")
