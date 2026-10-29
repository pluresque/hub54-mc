import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

from .config import ADMIN_TELEGRAM_ID, TELEGRAM_BOT_TOKEN
from .database import initialize_database
from .handlers import router
from .notifications import (
    dispatch_minecraft_notifications,
    dispatch_minecraft_private_messages,
)


logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger(__name__)


async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN")
    if ADMIN_TELEGRAM_ID <= 0:
        raise RuntimeError("Не задан ADMIN_TELEGRAM_ID")

    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    background_tasks = []

    try:
        await initialize_database()
        bot_info = await bot.get_me()
        common_commands = [
            BotCommand(command="start", description="Открыть меню аккаунта"),
            BotCommand(command="cancel", description="Отменить действие"),
            BotCommand(command="msg", description="Личное сообщение игроку"),
        ]
        admin_commands = [
            BotCommand(
                command="access_requests",
                description="Показать заявки на доступ",
            ),
            BotCommand(
                command="whitelist_add",
                description="Добавить Telegram ID в список",
            ),
            BotCommand(
                command="whitelist_remove",
                description="Убрать Telegram ID из списка",
            ),
            BotCommand(
                command="whitelist_list",
                description="Показать список доступа",
            ),
            BotCommand(
                command="poll",
                description="Создать голосование для пользователей",
            ),
            BotCommand(
                command="poll_close",
                description="Завершить голосование",
            ),
        ]
        await bot.set_my_commands(common_commands, scope=BotCommandScopeDefault())
        await bot.set_my_commands(
            common_commands + admin_commands,
            scope=BotCommandScopeChat(chat_id=ADMIN_TELEGRAM_ID),
        )
        background_tasks = [
            asyncio.create_task(dispatch_minecraft_notifications(bot)),
            asyncio.create_task(dispatch_minecraft_private_messages(bot)),
        ]
        logger.info("Бот Minecraft запущен: @%s", bot_info.username)
        await dispatcher.start_polling(bot, bot_info=bot_info)
    finally:
        for task in background_tasks:
            task.cancel()
        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
