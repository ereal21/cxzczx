from aiogram.utils import executor
from aiogram import Bot, Dispatcher
from aiogram.contrib.fsm_storage.memory import MemoryStorage

from bot.filters import register_all_filters
from bot.misc import EnvKeys
from bot.handlers import register_all_handlers
from bot.database.models import register_models
from bot.logger_mesh import logger, file_handler
from bot.middlewares import setup_middlewares

logger.addHandler(file_handler)


async def __on_start_up(dp: Dispatcher) -> None:
    register_all_filters(dp)
    register_all_handlers(dp)
    register_models()

    try:
        owner_id = int(EnvKeys.OWNER_ID) if EnvKeys.OWNER_ID else None
    except (TypeError, ValueError):
        owner_id = None

    if owner_id:
        try:
            await dp.bot.send_message(owner_id, "✅ Viskas turetu veikt be problemu pagal viska, nes sita tik parodo kai viska sutikrina, sekmes <3")
        except Exception as e:
            logger.error("Startup ping to OWNER_ID=%s failed: %s", owner_id, e)
    else:
        logger.warning("OWNER_ID is not set or invalid; cannot send startup ping.")


def start_bot():
    bot = Bot(token=EnvKeys.TOKEN, parse_mode='HTML')
    dp = Dispatcher(bot, storage=MemoryStorage())
    setup_middlewares(dp)
    executor.start_polling(dp, skip_updates=True, on_startup=__on_start_up)
