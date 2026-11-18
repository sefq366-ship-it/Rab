"""
main.py — точка входа.
Токен берётся из переменной окружения BOT_TOKEN.
Для локального запуска можно раскомментировать строку ниже.
"""

import asyncio
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot import router, init_db, setup_scheduler, scheduler

# --- Локальный запуск: раскомментируй и вставь токен ---
# BOT_TOKEN = "1234567890:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# --- Bothost / сервер: токен из переменной окружения ---
BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise SystemExit(
        "BOT_TOKEN не найден.\n"
        "Bothost: добавь переменную BOT_TOKEN в панели.\n"
        "Локально: раскомментируй строку BOT_TOKEN = \"...\" в main.py"
    )


async def main():
    await init_db()

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    setup_scheduler(bot)

    print("Бот запущен")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        print("Бот остановлен")
