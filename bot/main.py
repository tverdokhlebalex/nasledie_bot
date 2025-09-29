import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from .config import BOT_TOKEN, get_http, HTTP
from .handlers import registration
from .handlers import common 
from .handlers import submissions_heritage as submissions
from .handlers import admin_heritage as admin_handlers


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.info("Starting aiogram polling...")

    bot = Bot(BOT_TOKEN, parse_mode="Markdown")

    # Снимаем вебхук и чистим очередь апдейтов
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_routers(
        registration.router,
        submissions.router,
        admin_handlers.router,
        common.router,  
    )

    # Открываем общую HTTP-сессию для api_client
    await get_http()

    # не запускаем AdminWatcher
    # watcher = AdminWatcher(); watcher.start(bot)

    try:
        await dp.start_polling(bot, polling_timeout=30)
    finally:
        # 1) Останавливаем watcher (чтобы не было запросов во время закрытия HTTP)
        # try:
        #     await watcher.stop()
        # except Exception:
        #     logging.exception("Failed to stop AdminWatcher")

        # 2) Закрываем общую HTTP-сессию api_client
        try:
            if HTTP and not HTTP.closed:
                await HTTP.close()
        except Exception:
            logging.exception("Failed to close shared HTTP session")

        # 3) Закрываем сессию самого бота (aiohttp у aiogram)
        try:
            await bot.session.close()
        except Exception:
            pass

        logging.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())