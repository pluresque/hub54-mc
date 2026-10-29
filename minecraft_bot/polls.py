import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import ADMIN_TELEGRAM_ID
from .database import (
    create_poll,
    list_poll_deliveries,
    list_poll_recipient_ids,
    save_poll_delivery,
)


logger = logging.getLogger(__name__)
MAX_POLL_OPTIONS = 10


def validate_poll(question: str, options: list[str]) -> str | None:
    if not question or len(question) > 300:
        return "Вопрос должен содержать от 1 до 300 символов."
    if not 2 <= len(options) <= MAX_POLL_OPTIONS:
        return "Добавь от 2 до 10 вариантов ответа."
    if any(not option or len(option) > 64 for option in options):
        return "Варианты должны содержать от 1 до 64 символов."
    if len({option.casefold() for option in options}) != len(options):
        return "Варианты ответа должны различаться."
    return None


def poll_keyboard(
    poll_id: int,
    options: list[str],
    selected_option: int | None = None,
    closed: bool = False,
) -> InlineKeyboardMarkup | None:
    if closed:
        return None

    buttons = [
        InlineKeyboardButton(
            text=(f"✅ {option}" if index == selected_option else option),
            callback_data=f"v:{poll_id}:{index}",
        )
        for index, option in enumerate(options)
    ]
    rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def poll_message_text(
    poll: dict,
    selected_option: int | None = None,
    closed: bool = False,
) -> str:
    lines = ["🗳 <b>Голосование</b>", "", html.escape(poll["question"]), ""]
    if selected_option is not None:
        selected = html.escape(poll["options"][selected_option])
        lines.append(f"Твой ответ: <b>{selected}</b>")
        lines.append("Голос можно изменить.")
    elif closed:
        lines.append("Голосование завершено.")
    else:
        lines.append("Выбери один вариант.")

    if poll["total_votes"]:
        lines.extend(["", "<b>Текущие результаты</b>"])
        for option, count in zip(poll["options"], poll["counts"]):
            lines.append(f"{html.escape(option)}: {count}")
        lines.append(f"Всего голосов: {poll['total_votes']}")
    return "\n".join(lines)


def admin_results_text(poll: dict, delivered_count: int) -> str:
    lines = [
        f"🗳 <b>Голосование №{poll['id']}</b>",
        "",
        html.escape(poll["question"]),
        "",
    ]
    for option, count in zip(poll["options"], poll["counts"]):
        lines.append(f"{html.escape(option)}: <b>{count}</b>")
    lines.extend(
        [
            "",
            f"Всего голосов: {poll['total_votes']}",
            f"Доставлено участникам: {delivered_count}",
        ]
    )
    if poll["status"] == "closed":
        lines.append("Голосование завершено.")
    return "\n".join(lines)


async def create_and_broadcast_poll(
    bot: Bot,
    question: str,
    options: list[str],
    created_by: int,
) -> dict:
    poll_id = await create_poll(question, options, created_by)
    recipients = await list_poll_recipient_ids()
    poll = {
        "id": poll_id,
        "question": question,
        "options": options,
        "status": "active",
        "counts": [0] * len(options),
        "total_votes": 0,
    }
    delivered = 0
    failed = 0

    for telegram_id in recipients:
        try:
            message = await bot.send_message(
                telegram_id,
                poll_message_text(poll),
                reply_markup=poll_keyboard(poll_id, poll["options"]),
            )
        except TelegramAPIError:
            failed += 1
            logger.info("Не удалось отправить голосование пользователю %s", telegram_id)
            continue

        await save_poll_delivery(poll_id, telegram_id, message.message_id)
        delivered += 1

    return {
        "id": poll_id,
        "delivered": delivered,
        "failed": failed,
        "total": len(recipients),
    }


async def notify_admin_of_results(bot: Bot, poll: dict) -> None:
    deliveries = await list_poll_deliveries(poll["id"])
    await bot.send_message(
        ADMIN_TELEGRAM_ID,
        admin_results_text(poll, len(deliveries)),
    )
