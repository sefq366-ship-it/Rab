"""
emoji_data.py — работа с эмодзи.
Источник: библиотека emoji-index (Unicode CLDR).
Установка: pip install emoji-index
"""

from emoji_index import emoji_index


# ---------------------------------------------------------------- #
#  Категории для кнопок (ключ = текст кнопки)
# ---------------------------------------------------------------- #
EMOJI_CATEGORIES: dict[str, list[str]] = {}


def _iter_group(group_name: str):
    """Итерирует по всем эмодзи указанной группы emoji-index."""
    if group_name not in emoji_index.groups:
        return
    group = emoji_index[group_name]
    for subgroup_name in group.subgroups:
        for emoji_obj in group[subgroup_name]:
            yield emoji_obj


def _filter_by_keywords(group_name: str, keywords: list[str]) -> list[str]:
    """Возвращает эмодзи группы, в названии которых есть ключевые слова."""
    result = []
    for emoji_obj in _iter_group(group_name):
        name_lower = emoji_obj.name.lower()
        if any(kw in name_lower for kw in keywords):
            result.append(emoji_obj.emoji)
    # убираем дубликаты, сохраняя порядок
    seen = set()
    unique = []
    for e in result:
        if e not in seen:
            seen.add(e)
            unique.append(e)
    return unique


# --- Еда и напитки ---
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

# --- Природа (если понадобится) ---
EMOJI_CATEGORIES["🌿 Природа"] = _filter_by_keywords("Animals & Nature", [
    "flower", "leaf", "tree", "plant", "herb", "seed", "mushroom",
])

# --- Универсальные ---
EMOJI_CATEGORIES["📦 Разное"] = [
    "📦", "🎁", "⭐", "🔥", "💎", "🏆", "⚡", "🎯", "💼", "🔧",
    "✅", "❌", "❤️", "💙", "💚", "💛", "🧡", "💜", "🖤", "🤍",
    "👍", "👎", "👏", "🙌", "💪", "🤝", "✌️", "👌", "🫡", "🫶",
]

# Убираем пустые категории (на случай, если emoji-index не нашёл совпадений)
EMOJI_CATEGORIES = {k: v for k, v in EMOJI_CATEGORIES.items() if v}


# ---------------------------------------------------------------- #
#  Подбор эмодзи по названию продукта
# ---------------------------------------------------------------- #
# Ключевые слова → предпочтительные эмодзи (приоритетная карта)
_KEYWORD_TO_EMOJI = {
    "манго": "🥭", "mango": "🥭",
    "яблок": "🍎", "apple": "🍎",
    "банан": "🍌", "banana": "🍌",
    "виноград": "🍇", "grape": "🍇",
    "клубник": "🍓", "strawberry": "🍓",
    "апельсин": "🍊", "orange": "🍊",
    "лимон": "🍋", "lemon": "🍋",
    "арбуз": "🍉", "watermelon": "🍉",
    "персик": "🍑", "peach": "🍑",
    "вишн": "🍒", "cherry": "🍒",
    "киви": "🥝", "kiwi": "🥝",
    "ананас": "🍍", "pineapple": "🍍",
    "кокос": "🥥", "coconut": "🥥",
    "авокадо": "🥑", "avocado": "🥑",

    "мороженое": "🍦", "ice cream": "🍦", "icecream": "🍦",
    "йогурт": "🍨", "yogurt": "🍨",
    "торт": "🎂", "cake": "🎂",
    "печень": "🍪", "cookie": "🍪",
    "шоколад": "🍫", "chocolate": "🍫",
    "конфет": "🍬", "candy": "🍬",
    "пирог": "🥧", "pie": "🥧",
    "пончик": "🍩", "doughnut": "🍩",

    "кофе": "☕", "coffee": "☕",
    "чай": "🍵", "tea": "🍵",
    "сок": "🧃", "juice": "🧃",
    "кола": "🥤", "cola": "🥤",
    "вода": "💧", "water": "💧",
    "молоко": "🥛", "milk": "🥛",
    "пиво": "🍺", "beer": "🍺",
    "вино": "🍷", "wine": "🍷",
    "коктейль": "🍹", "cocktail": "🍹",

    "хлеб": "🍞", "bread": "🍞",
    "багет": "🥖", "baguette": "🥖",
    "круассан": "🥐", "croissant": "🥐",
    "пицца": "🍕", "pizza": "🍕",
    "бургер": "🍔", "burger": "🍔",

    "протеин": "💪", "protein": "💪",
    "витамин": "💊", "vitamin": "💊",
}


def suggest_emojis(product_name: str, limit: int = 8) -> list[str]:
    """
    Предлагает эмодзи на основе названия продукта.
    Сначала — по ключевым словам (приоритет), потом — по emoji-index.
    """
    name_lower = product_name.lower().strip()
    result: list[str] = []

    # 1. Приоритетные совпадения по словарю
    for keyword, emoji in _KEYWORD_TO_EMOJI.items():
        if keyword in name_lower and emoji not in result:
            result.append(emoji)

    # 2. Дополняем из emoji-index (поиск слова в названии эмодзи)
    if len(result) < limit:
        words = [w for w in name_lower.split() if len(w) > 2]
        for group_name in emoji_index.groups:
            for emoji_obj in _iter_group(group_name):
                if emoji_obj.emoji in result:
                    continue
                emoji_name = emoji_obj.name.lower()
                if any(w in emoji_name for w in words):
                    result.append(emoji_obj.emoji)
                    if len(result) >= limit:
                        break
            if len(result) >= limit:
                break

    # 3. Если ничего — универсальный набор
    if not result:
        result = ["📦", "🎁", "⭐", "🔥", "💎", "🏆", "⚡", "🎯"]

    return result[:limit]


# ---------------------------------------------------------------- #
#  Проверка ручного ввода
# ---------------------------------------------------------------- #
def is_single_emoji(text: str) -> bool:
    """Проверяет, что строка — ровно один эмодзи из индекса."""
    text = text.strip()
    if not text or len(text) > 8:
        return False
    for group_name in emoji_index.groups:
        for emoji_obj in _iter_group(group_name):
            if emoji_obj.emoji == text:
                return True
    return False


# ---------------------------------------------------------------- #
#  Доступ к категориям
# ---------------------------------------------------------------- #
def get_category_emojis(category_name: str) -> list[str]:
    return EMOJI_CATEGORIES.get(category_name, [])


def get_all_category_names() -> list[str]:
    return list(EMOJI_CATEGORIES.keys())


def total_emojis() -> int:
    """Сколько всего уникальных эмодзи доступно в категориях."""
    seen = set()
    for lst in EMOJI_CATEGORIES.values():
        seen.update(lst)
    return len(seen)
