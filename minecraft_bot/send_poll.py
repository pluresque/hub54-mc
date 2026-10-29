import asyncio
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from .config import ADMIN_TELEGRAM_ID, TELEGRAM_BOT_TOKEN
from .database import get_poll_results, initialize_database
from .polls import (
    create_and_broadcast_poll,
    notify_admin_of_results,
    validate_poll,
)


async def main() -> None:
    if len(sys.argv) < 4:
        raise SystemExit(
            'Использование: python -m minecraft_bot.send_poll "вопрос" "ответ 1" "ответ 2"'
        )
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN")
    if ADMIN_TELEGRAM_ID <= 0:
        raise SystemExit("Не задан ADMIN_TELEGRAM_ID")

    question, options = sys.argv[1], sys.argv[2:]
    validation_error = validate_poll(question, options)
    if validation_error:
        raise SystemExit(validation_error)

    await initialize_database()
    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        result = await create_and_broadcast_poll(
            bot,
            question,
            options,
            ADMIN_TELEGRAM_ID,
        )
        poll = await get_poll_results(result["id"])
        if poll is not None:
            try:
                await notify_admin_of_results(bot, poll)
            except Exception as error:
                print(f"Не удалось отправить сводку администратору: {error}")

        print(
            f"Голосование №{result['id']}: отправлено {result['delivered']} "
            f"из {result['total']}; ошибок доставки {result['failed']}."
        )
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
