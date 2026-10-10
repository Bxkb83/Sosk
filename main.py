# -*- coding: utf-8 -*-
import asyncio, logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties

from config import BOT_TOKEN
import database as db
from handlers import router as user_router
from admin import router as admin_router
from scheduler import start_scheduler

async def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s | %(levelname)s | %(message)s")
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    dp.include_router(user_router)

    await db.init()
    await start_scheduler(bot)
    print("Bot calisiyor...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
