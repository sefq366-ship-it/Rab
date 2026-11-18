"""
keyboards.py — все inline-клавиатуры бота.
Callback-данные краткие, чтобы не превышать лимит Telegram (64 байта).
"""

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from emoji_data import get_all_category_names, get_category_emojis


# ---------------------------------------------------------------- #
#  Ввод количества: 1–5 + «Другое»
# ---------------------------------------------------------------- #
def quantity_keyboard(product_id: int) -> InlineKeyboardMarkup:
    """Кнопки 1–5 и ручной ввод для продукта."""
    kb = InlineKeyboardBuilder()
    for n in range(1, 6):
        kb.button(text=str(n), callback_data=f"qty:{product_id}:{n}")
    kb.button(text="✏️ Другое", callback_data=f"qty_other:{product_id}")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(3, 2, 1, 1)  # 3 в ряд, потом 2, потом по одной
    return kb.as_markup()


# ---------------------------------------------------------------- #
#  Подтверждение большого числа (>20)
# ---------------------------------------------------------------- #
def confirm_big_quantity_keyboard(product_id: int, qty: int) -> InlineKeyboardMarkup:
    """Переспрос при quantity > MAX_QUANTITY."""
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Да, {qty} замесов",
              callback_data=f"qty_confirm:{product_id}:{qty}")
    kb.button(text="✏️ Нет, исправить",
              callback_data=f"qty_other:{product_id}")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()


# ---------------------------------------------------------------- #
#  Отмена последнего: 1 / 2 / 3 / Всё / Назад
# ---------------------------------------------------------------- #
def undo_keyboard(quantity: int) -> InlineKeyboardMarkup:
    """
    Кнопки для выбора, сколько убрать из последней записи.
    Показываем только реально возможные варианты.
    """
    kb = InlineKeyboardBuilder()

    # варианты 1..quantity-1 (частичное уменьшение)
    partial = list(range(1, quantity))
    if len(partial) <= 3:
        buttons = [str(n) for n in partial]
    else:
        # если больше 3 — показываем 1, 2 и «...»
        buttons = ["1", "2"]

    for label in buttons:
        kb.button(text=label, callback_data=f"undo:{int(label)}")

    kb.button(text=f"Всё ({quantity})", callback_data=f"undo:{quantity}")
    kb.button(text="🔙 Назад", callback_data="cancel")

    # выравниваем: все кнопки в один ряд, если их <=4, иначе по 3
    total = len(buttons) + 2
    if total <= 4:
        kb.adjust(total)
    else:
        kb.adjust(3, 2)
    return kb.as_markup()


# ---------------------------------------------------------------- #
#  Отмена: подтверждение (после выбора количества)
# ---------------------------------------------------------------- #
def undo_confirm_keyboard(remove_qty: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ Убрать {remove_qty}",
              callback_data=f"undo_confirm:{remove_qty}")
    kb.button(text="🔙 Назад", callback_data="undo_back")
    kb.adjust(2)
    return kb.as_markup()


# ---------------------------------------------------------------- #
#  Эмодзи: первый экран (предложенные + «Другое»)
# ---------------------------------------------------------------- #
def emoji_suggest_keyboard(emojis: list[str], product_id: int) -> InlineKeyboardMarkup:
    """
    Сетка предложенных эмодзи + кнопки «Больше категорий» и «Ввести вручную».
    """
    kb = InlineKeyboardBuilder()
    for e in emojis:
        kb.button(text=e, callback_data=f"emoji_set:{product_id}:{e}")
    kb.button(text="📂 Больше категорий", callback_data=f"emoji_cats:{product_id}")
    kb.button(text="⌨️ Ввести вручную", callback_data=f"emoji_manual:{product_id}")
    kb.button(text="⏭ Пропустить", callback_data=f"emoji_skip:{product_id}")

    # эмодзи по 4 в ряд, потом служебные кнопки по одной
    rows = [4] * (len(emojis) // 4)
    if len(emojis) % 4:
        rows.append(len(emojis) % 4)
    rows += [1, 1, 1]
    kb.adjust(*rows)
    return kb.as_markup()


# ---------------------------------------------------------------- #
#  Эмодзи: меню категорий
# ---------------------------------------------------------------- #
def emoji_categories_keyboard(product_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for cat in get_all_category_names():
        kb.button(text=cat, callback_data=f"emoji_cat:{product_id}:{cat}")
    kb.button(text="🔙 Назад", callback_data=f"emoji_back:{product_id}")
    kb.adjust(2, 2, 2, 1, 1)  # подстраивается под число категорий
    return kb.as_markup()


# ---------------------------------------------------------------- #
#  Эмодзи: сетка внутри категории (постранично)
# ---------------------------------------------------------------- #
EMOJI_PER_PAGE = 24


def emoji_category_page_keyboard(
    product_id: int, category: str, page: int = 0
) -> InlineKeyboardMarkup:
    emojis = get_category_emojis(category)
    start = page * EMOJI_PER_PAGE
    chunk = emojis[start:start + EMOJI_PER_PAGE]

    kb = InlineKeyboardBuilder()
    for e in chunk:
        kb.button(text=e, callback_data=f"emoji_set:{product_id}:{e}")

    # пагинация
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️", callback_data=f"emoji_page:{product_id}:{category}:{page-1}"
        ))
    if start + EMOJI_PER_PAGE < len(emojis):
        nav.append(InlineKeyboardButton(
            text="➡️", callback_data=f"emoji_page:{product_id}:{category}:{page+1}"
        ))
    if nav:
        kb.row(*nav)

    kb.row(InlineKeyboardButton(
        text="🔙 К категориям", callback_data=f"emoji_cats:{product_id}"
    ))
    kb.row(InlineKeyboardButton(
        text="⌨️ Ввести вручную", callback_data=f"emoji_manual:{product_id}"
    ))
    kb.row(InlineKeyboardButton(
        text="⏭ Пропустить", callback_data=f"emoji_skip:{product_id}"
    ))

    # 4 эмодзи в ряд
    rows = [4] * (len(chunk) // 4)
    if len(chunk) % 4:
        rows.append(len(chunk) % 4)
    if nav:
        rows.append(len(nav))
    rows += [1, 1, 1]
    kb.adjust(*rows)
    return kb.as_markup()


# ---------------------------------------------------------------- #
#  Ручной ввод эмодзи
# ---------------------------------------------------------------- #
def emoji_manual_keyboard(product_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📂 Выбрать из категорий",
              callback_data=f"emoji_cats:{product_id}")
    kb.button(text="⏭ Пропустить (📦)",
              callback_data=f"emoji_skip:{product_id}")
    kb.adjust(1)
    return kb.as_markup()


# ---------------------------------------------------------------- #
#  Кнопка «Отменить последний» (под сообщением о записи)
# ---------------------------------------------------------------- #
def after_mix_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="↩️ Отменить последний", callback_data="undo_start")
    kb.adjust(1)
    return kb.as_markup()


# ---------------------------------------------------------------- #
#  Вечерняя сводка: отправить сейчас / +10 / +30
# ---------------------------------------------------------------- #
def daily_report_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Отправить сейчас", callback_data="report_now")
    kb.button(text="⏰ +10 мин", callback_data="report_snooze:10")
    kb.button(text="⏰ +30 мин", callback_data="report_snooze:30")
    kb.adjust(1, 2)
    return kb.as_markup()


# ---------------------------------------------------------------- #
#  Напоминание «были ли замесы» (12:00 / 16:00)
# ---------------------------------------------------------------- #
def empty_day_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Всё ок, пока не начал", callback_data="empty_ok")
    kb.button(text="➕ Добавлю замес", callback_data="empty_add")
    kb.adjust(1)
    return kb.as_markup()


# ---------------------------------------------------------------- #
#  Админ-панель
# ---------------------------------------------------------------- #
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
    kb.adjust(1, 1, 1)
    return kb.as_markup()


def admin_product_list_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    """Список продуктов для редактирования (по кнопке на продукт)."""
    kb = InlineKeyboardBuilder()
    for p in products:
        kb.button(
            text=f"{p['emoji']} {p['canonical_name']}",
            callback_data=f"admin_pedit:{p['id']}"
        )
    kb.button(text="🔙 Назад", callback_data="admin_products")
    kb.adjust(1)
    return kb.as_markup()


def admin_product_edit_keyboard(product_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✏️ Изменить название",
              callback_data=f"admin_pname:{product_id}")
    kb.button(text="😊 Изменить эмодзи",
              callback_data=f"admin_pemoji:{product_id}")
    kb.button(text="➕ Добавить синоним",
              callback_data=f"admin_palias:{product_id}")
    kb.button(text="🔙 Назад", callback_data="admin_product_edit")
    kb.adjust(1)
    return kb.as_markup()


def admin_product_add_steps_keyboard(step: int) -> InlineKeyboardMarkup:
    """Кнопки на шагах добавления продукта (1=название, 2=эмодзи, 3=синонимы)."""
    kb = InlineKeyboardBuilder()
    if step > 1:
        kb.button(text="🔙 Назад", callback_data=f"admin_add_back:{step-1}")
    kb.button(text="❌ Отмена", callback_data="admin_products")
    kb.adjust(2 if step > 1 else 1)
    return kb.as_markup()


# ---------------------------------------------------------------- #
#  Статистика: выбор периода
# ---------------------------------------------------------------- #
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


# ---------------------------------------------------------------- #
#  Универсальные
# ---------------------------------------------------------------- #
def cancel_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="❌ Отмена", callback_data="cancel")
    return kb.as_markup()


def back_keyboard(callback_data: str = "cancel") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Назад", callback_data=callback_data)
    return kb.as_markup()
