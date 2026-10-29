import asyncio
import html
import logging

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramForbiddenError,
    TelegramRetryAfter,
)

from .database import (
    get_pending_notification_deliveries,
    get_pending_minecraft_private_messages,
    mark_notification_delivered,
    mark_minecraft_private_message_delivered,
)


logger = logging.getLogger(__name__)

NOTIFICATION_LABELS = {
    "chat": "Сообщения в чате",
    "join": "Входы игроков",
    "leave": "Выходы игроков",
    "kill": "Убийства игроков",
    "death": "Смерти игроков",
}

NOTIFICATION_ICONS = {
    "chat": "💬",
    "join": "🟢",
    "leave": "⚪",
    "kill": "⚔️",
    "death": "☠️",
}


def notification_settings_text(preferences: dict[str, bool]) -> str:
    lines = [
        "🔔 <b>Оповещения сервера</b>",
        "",
        "Нажмите кнопку, чтобы включить или выключить тип оповещений.",
        "",
    ]
    for event_type, label in NOTIFICATION_LABELS.items():
        status = "включены" if preferences.get(event_type, False) else "выключены"
        lines.append(f"{label}: {status}")
    return "\n".join(lines)


async def dispatch_minecraft_notifications(bot: Bot) -> None:
    while True:
        try:
            deliveries = await get_pending_notification_deliveries()
            for notification_id, event_type, message, telegram_id in deliveries:
                icon = NOTIFICATION_ICONS.get(event_type, "🔔")
                text = f"{icon} {message[:3800]}"

                try:
                    await bot.send_message(
                        chat_id=telegram_id,
                        text=text,
                        parse_mode=None,
                    )
                except TelegramForbiddenError:
                    logger.info(
                        "Пользователь %s недоступен для оповещений", telegram_id
                    )
                except TelegramRetryAfter as error:
                    await asyncio.sleep(float(error.retry_after))
                    break
                except TelegramAPIError:
                    logger.exception(
                        "Не удалось отправить оповещение пользователю %s",
                        telegram_id,
                    )
                    break

                await mark_notification_delivered(notification_id, telegram_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка обработки оповещений Minecraft")
            await asyncio.sleep(5)
            continue

        await asyncio.sleep(1 if deliveries else 2)


async def dispatch_minecraft_private_messages(bot: Bot) -> None:
    while True:
        try:
            deliveries = await get_pending_minecraft_private_messages()
            for message_id, recipient_id, sender_nickname, message in deliveries:
                text = (
                    f"🔒 <b>Личное сообщение от {html.escape(sender_nickname)}</b>\n\n"
                    f"{html.escape(message)}\n\n"
                    "Ответь на это сообщение или напиши:\n"
                    "<code>/msg ник текст</code>"
                )
                try:
                    sent = await bot.send_message(
                        chat_id=recipient_id,
                        text=text,
                        parse_mode="HTML",
                    )
                except TelegramForbiddenError:
                    logger.info(
                        "Пользователь %s недоступен для личных сообщений",
                        recipient_id,
                    )
                    await mark_minecraft_private_message_delivered(
                        message_id, recipient_id
                    )
                    continue
                except TelegramRetryAfter as error:
                    await asyncio.sleep(float(error.retry_after))
                    break
                except TelegramAPIError:
                    logger.exception(
                        "Не удалось отправить личное сообщение Telegram ID %s",
                        recipient_id,
                    )
                    break

                await mark_minecraft_private_message_delivered(
                    message_id,
                    recipient_id,
                    sent.message_id,
                    sender_nickname,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка обработки личных сообщений Minecraft")
            await asyncio.sleep(5)
            continue

        await asyncio.sleep(1 if deliveries else 2)
